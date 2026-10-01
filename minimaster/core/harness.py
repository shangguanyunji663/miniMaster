"""Harness：Planner-Executor-Validator 三层嵌套循环的编排中枢。

round 表示一个完整的「执行 → 验收 →（未过则）重规划」周期；任务重试在
轮内工作队列中即时重入队（重试与轮次解耦）。无未满足依赖的任务可并行执行
（--parallel N，默认 1 顺序）。四重纠错闭环贯穿全程：
  1. Completion Checklist 驱动的验收判定；
  2. 重复/振荡动作 Guard 拦截（Executor 内）；
  3. 步数上限强制收口（Executor 内）；
  4. Validator missing_requirements 回流系统反馈。

每个任务收口与每轮结束写快照 state.json，支持 --resume 断点续跑。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..agents.executor import Executor
from ..agents.planner import Planner
from ..agents.validator import Validator
from ..config import Settings
from ..core.action_policy import ActionPolicy, Role
from ..core.checklist import CompletionChecklist
from ..core.guard import RepeatedActionGuard
from ..core.snapshot import load_snapshot, restore_state, save_snapshot, snapshot_to_dict
from ..core.state_machine import Task, TaskStatus
from ..llm.client import LLMClient
from ..memory.live_context import LiveContextTrimmer
from ..memory.retry_archive import RetryArchive
from ..memory.working_memory import WorkingMemory
from ..prompts import templates
from ..prompts.builder import PromptBuilder
from ..tools.base import ToolContext
from ..tools.filesystem import default_filesystem_tools
from ..tools.mcp_client import MCPProxyTool, MCPStdioClient
from ..tools.service import ToolService
from ..tools.shell import BashTool
from ..utils.reporter import Reporter


@dataclass
class HarnessReport:
    success: bool
    goal: str
    rounds_used: int
    checklist: CompletionChecklist | None = None
    tasks: list[Task] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    requests: int = 0
    stop_reason: str = ""

    def render(self) -> str:
        lines = [
            ("=" * 60),
            f"目标: {self.goal}",
            f"结果: {'✅ 完成' if self.success else '❌ 未完成'}"
            f"  (轮数 {self.rounds_used}, 模型请求 {self.requests} 次, "
            f"tokens {self.prompt_tokens}+{self.completion_tokens})",
            f"停止原因: {self.stop_reason}",
            "",
        ]
        if self.checklist is not None:
            lines.append(self.checklist.render())
            lines.append("")
        for t in self.tasks:
            lines.append(t.render())
        return "\n".join(lines)


class Harness:
    def __init__(
        self,
        settings: Settings,
        workdir: Path,
        reporter: Reporter,
        llm: LLMClient | None = None,
        snapshot_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.reporter = reporter
        self.snapshot_dir = snapshot_dir

        # 模型调用层（可注入替身用于测试）+ 工具层 + 记忆层
        self.llm = llm or LLMClient(settings)
        self.tool_context = ToolContext(workdir=self.workdir, emit=reporter.emit)
        self.tool_service = ToolService()
        self.tool_service.register(BashTool())
        for tool in default_filesystem_tools():
            self.tool_service.register(tool)
        self._mcp_client: MCPStdioClient | None = self._connect_mcp()
        self.memory = WorkingMemory(
            result_max_chars=settings.result_max_chars,
            view_compress_threshold=settings.view_compress_threshold,
            recent_steps=settings.memory_recent_steps,
            compress_fn=self._compress_history,
        )
        self.archive = RetryArchive(compress_fn=self._compress_failure)
        self.guard = RepeatedActionGuard(
            settings.guard_max_repeat, settings.guard_window_size, settings.guard_window_max
        )
        self.trimmer = LiveContextTrimmer(
            threshold_chars=settings.live_threshold_chars,
            keep_recent_groups=4,
            compress_fn=self._compress_history,
        )

        builder = PromptBuilder(self.workdir, settings.max_steps)
        # 三层角色：独立 Action Policy 白名单 + 按角色模型路由
        self.planner = Planner(
            self.llm, builder, ActionPolicy(Role.PLANNER), reporter,
            model=settings.planner_model or None, temperature=settings.planner_temperature,
        )
        self.executor = Executor(
            self.llm,
            builder,
            ActionPolicy.for_executor_with_tools(self.tool_service.names()),
            reporter,
            self.tool_service,
            self.tool_context,
            self.memory,
            self.guard,
            trimmer=self.trimmer,
            token_budget=settings.token_budget,
        )
        self.validator = Validator(
            self.llm, builder, ActionPolicy(Role.VALIDATOR), reporter, self.memory,
            model=settings.validator_model or None, temperature=settings.validator_temperature,
        )

        self.goal: str = ""
        self.tasks: list[Task] = []
        self.checklist: CompletionChecklist | None = None
        self.round_no = 0
        # 续跑状态
        self._resumed = False
        self._resume_round = 0
        self._resume_validated = False

    # ---- MCP 外部工具接入 ----
    def _connect_mcp(self) -> MCPStdioClient | None:
        """按配置连接 MCP server，把远程工具注册进 ToolService。失败降级为纯内置工具。"""
        import atexit
        import shlex

        if not self.settings.mcp_server_cmd:
            return None
        try:
            client = MCPStdioClient(
                shlex.split(self.settings.mcp_server_cmd),
                request_timeout=self.settings.mcp_timeout,
            )
            descs = client.start()
            if not descs:
                client.close()
                self.reporter.emit("mcp_empty", {"role": "HARNESS", "detail": "server 无工具"})
                return None
            for desc in descs:
                self.tool_service.register(MCPProxyTool(client, desc))
            atexit.register(client.close)
            self.reporter.emit(
                "mcp_connected",
                {"role": "HARNESS", "server": self.settings.mcp_server_cmd,
                 "tools": [d.get("name") for d in descs]},
            )
            return client
        except Exception as exc:
            self.reporter.emit(
                "mcp_unavailable", {"role": "HARNESS", "detail": f"{type(exc).__name__}: {exc}"}
            )
            return None

    # ---- 对外：主循环 ----
    def run(self, goal: str | None = None) -> HarnessReport:
        if self._resumed:
            self.reporter.emit(
                "resume",
                {"goal": self.goal, "from_round": self._resume_round,
                 "validated": self._resume_validated, "tasks": len(self.tasks)},
            )
        else:
            if not goal:
                raise ValueError("未提供目标（goal），也未加载续跑快照")
            self.goal = goal
            self.reporter.emit(
                "harness_start",
                {"goal": goal, "workdir": str(self.workdir),
                 "parallel": self.settings.parallel, "stream": self.settings.stream},
            )
            self.tasks, self.checklist = self.planner.plan(goal)
            self._sanitize_deps()
            self._write_snapshot(validated=False)

        success = False
        stop_reason = "达到最大轮数上限"
        if self._resumed:
            start_round = (
                self._resume_round + 1 if self._resume_validated else max(self._resume_round, 1)
            )
        else:
            start_round = 1
        round_no = start_round - 1

        for round_no in range(start_round, self.settings.max_rounds + 1):
            self.round_no = round_no
            self.reporter.banner(f"Round {round_no}/{self.settings.max_rounds}")
            self._execute_pending_tasks()

            verdict = self.validator.verify(self.goal, self.checklist, self.tasks)
            # 成功判据以 Completion Checklist 为准：被重规划替代的旧 FAILED 任务
            # 不阻塞验收（其需求已由新任务覆盖并逐项判定）
            if verdict.completed and self.checklist.is_complete:
                success = True
                stop_reason = "Completion Checklist 验收通过"
                self.reporter.emit("round_end", {"round": round_no, "result": "accept"})
                self._write_snapshot(validated=True)
                break

            # 未通过 → 缺失项精确回流
            missing = list(verdict.missing_requirements)
            for t in self.tasks:
                if t.status is TaskStatus.BLOCKED:
                    missing.append(f"任务 {t.id}({t.title}) 被阻塞: {t.result_summary}，需规划绕行路径")
                elif t.status is TaskStatus.FAILED:
                    missing.append(f"任务 {t.id}({t.title}) 重试耗尽仍失败: {t.result_summary}，需拆成更小步骤")
            self.reporter.emit(
                "round_end",
                {"round": round_no, "result": "reject", "missing": missing[:4]},
            )
            if round_no == self.settings.max_rounds:
                break

            new_tasks = self.planner.replan(self.goal, self.tasks, missing)
            if not new_tasks and not any(t.status is TaskStatus.PENDING for t in self.tasks):
                stop_reason = "无 PENDING 任务且重规划未产出新任务，无法继续推进"
                break
            self.tasks.extend(new_tasks)
            self._sanitize_deps()
            self._write_snapshot(validated=True)

        self.memory.compress(force=True)  # 收口前做一次全量语义压缩
        report = HarnessReport(
            success=success,
            goal=self.goal,
            rounds_used=round_no,
            checklist=self.checklist,
            tasks=self.tasks,
            prompt_tokens=self.llm.total_prompt_tokens,
            completion_tokens=self.llm.total_completion_tokens,
            requests=self.llm.total_requests,
            stop_reason=stop_reason,
        )
        self.reporter.emit(
            "harness_end",
            {"success": success, "rounds": round_no,
             "protocol": getattr(self.llm.protocol_mode, "value", self.llm.protocol_mode)},
        )
        return report

    # ---- 对外：断点续跑 ----
    def load_state(self, snapshot_path: str | Path) -> None:
        raw = load_snapshot(Path(snapshot_path))
        goal, round_no, validated, tasks, checklist, records = restore_state(raw)
        self.goal = goal
        self.tasks = tasks
        self.checklist = checklist
        self.archive.restore(records)
        self.round_no = round_no
        self._resumed = True
        self._resume_round = round_no
        self._resume_validated = validated

    # ---- 单轮：工作队列（依赖就绪即可执行，重试即时重入队）----
    def _execute_pending_tasks(self) -> None:
        while True:
            if self._budget_exceeded():
                for task in [t for t in self.tasks if t.status is TaskStatus.PENDING]:
                    task.result_summary = (
                        f"全局 token 预算耗尽（上限 {self.settings.max_total_tokens}），任务未执行"
                    )
                    task.transition(TaskStatus.BLOCKED)
                    self.reporter.emit("budget_exceeded", {"role": "HARNESS", "task": task.id})
                return
            pending = [t for t in self.tasks if t.status is TaskStatus.PENDING]
            if not pending:
                return
            ready = [t for t in pending if self._deps_satisfied(t)]
            if not ready:
                for task in pending:
                    self._block_on_unmet_deps(task)
                return
            if self.settings.parallel > 1 and len(ready) > 1:
                with ThreadPoolExecutor(max_workers=self.settings.parallel) as pool:
                    list(pool.map(self._run_single, ready))
            else:
                self._run_single(ready[0])

    def _done_ids(self) -> set[str]:
        return {t.id for t in self.tasks if t.status is TaskStatus.DONE}

    def _budget_exceeded(self) -> bool:
        budget = self.settings.max_total_tokens
        if budget <= 0:
            return False
        used = self.llm.total_prompt_tokens + self.llm.total_completion_tokens
        if used >= budget and not getattr(self, "_budget_reported", False):
            self._budget_reported = True
            self.reporter.emit(
                "budget_exceeded",
                {"role": "HARNESS", "used_tokens": used, "budget": budget},
            )
        return used >= budget

    def _sanitize_deps(self) -> int:
        """清理非法依赖：指向不存在任务或自身的 depends_on 直接剔除并留痕。"""
        known = {t.id for t in self.tasks}
        fixed = 0
        for task in self.tasks:
            valid = [d for d in task.depends_on if d in known and d != task.id]
            if valid != task.depends_on:
                fixed += len(task.depends_on) - len(valid)
                self.reporter.emit(
                    "deps_invalid",
                    {"role": "HARNESS", "task": task.id,
                     "dropped": [d for d in task.depends_on if d not in valid]},
                )
                task.depends_on = valid
        return fixed

    def _deps_satisfied(self, task: Task) -> bool:
        done = self._done_ids()
        return all(dep in done for dep in task.depends_on)

    def _block_on_unmet_deps(self, task: Task) -> None:
        done = self._done_ids()
        unmet = [dep for dep in task.depends_on if dep not in done]
        task.result_summary = f"依赖未完成或不存在，任务无法启动: {unmet}"
        task.transition(TaskStatus.BLOCKED)
        self.reporter.emit(
            "task_blocked", {"role": "HARNESS", "task": task.id, "summary": task.result_summary}
        )

    # ---- 单任务执行（含归档重注入；线程池下每任务独立 ctx/guard）----
    def _run_single(self, task: Task) -> None:
        self.reporter.emit("task_start", {"role": "HARNESS", "task": task.id, "title": task.title})
        task.transition(TaskStatus.RUNNING)  # 状态机硬约束
        lessons = self.archive.render_for_task(task.id)  # Retry Archive 重试注入
        ctx = ToolContext(workdir=self.workdir, emit=self.reporter.emit)  # cwd 会话隔离
        guard = RepeatedActionGuard(
            self.settings.guard_max_repeat, self.settings.guard_window_size,
            self.settings.guard_window_max,
        )
        result = self.executor.run(task, lessons, self.settings.max_steps, ctx=ctx, guard=guard)
        task.result_summary = result.summary

        if result.blocked:
            task.transition(TaskStatus.BLOCKED)
            self.reporter.emit("task_blocked", {"role": "HARNESS", "task": task.id,
                                                "summary": result.summary})
        elif result.success:
            task.transition(TaskStatus.DONE)
            self.reporter.emit("task_done", {"role": "HARNESS", "task": task.id})
        else:
            task.transition(TaskStatus.FAILED)
            if task.attempts <= self.settings.max_retries:
                record = self.archive.archive(task, result.summary)  # 轨迹压缩归档
                self.reporter.emit(
                    "task_retry_scheduled",
                    {"role": "MEMORY", "task": task.id, "attempt": task.attempts,
                     "archived": record is not None,
                     "lessons": record.lessons if record else ""},
                )
                task.transition(TaskStatus.PENDING)  # FAILED → PENDING（有重试额度）
            else:
                self.reporter.emit("task_failed", {"role": "HARNESS", "task": task.id,
                                                   "summary": result.summary})
        self._write_snapshot(validated=False)
        self.memory.compress()  # 二级语义压缩：超阈值时触发

    # ---- 快照 ----
    def _write_snapshot(self, validated: bool) -> None:
        if self.snapshot_dir is None or self.checklist is None:
            return
        state = snapshot_to_dict(
            goal=self.goal,
            round_no=self.round_no,
            validated=validated,
            tasks=self.tasks,
            checklist=self.checklist,
            records=self.archive.all_records(),
            workdir=str(self.workdir),
        )
        save_snapshot(self.snapshot_dir / "state.json", state)

    # ---- LLM 压缩函数（注入记忆层，本层感知模型）----
    def _compress_history(self, steps: list[str]) -> str:
        prompt = templates.HISTORY_COMPRESS_PROMPT.format(steps="\n".join(steps[-40:]))
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
            return resp.content.strip() or "\n".join(steps)[-3000:]
        except Exception:
            return "\n".join(steps)[-3000:]  # 压缩失败退化为截断拼接

    def _compress_failure(self, title: str, attempt: int, failure: str) -> str:
        prompt = templates.FAILURE_COMPRESS_PROMPT.format(
            title=title, attempt=attempt, failure=failure[:3000]
        )
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
            return resp.content.strip() or failure[:2000]
        except Exception:
            return failure[:2000]
