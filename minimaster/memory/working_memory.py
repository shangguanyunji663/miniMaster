"""分角色动态工作记忆：按职责裁剪上下文视图，三级策略控制上下文膨胀。

- 第一级 · 单结果裁剪：每条工具观察超限即截断（保头部 + 尾部 + 截断标记），
  在写入时立刻生效，避免单条超大结果污染整个视图。
- 第二级 · 超阈值语义压缩：视图总字符数超阈值时，把较早的步骤批量交给
  LLM 压缩成一条摘要（compress_fn 由上层注入，本模块不感知模型）。
- 第三级 · 保留最近 N 步：滚动窗口内的步骤永远保留全文，窗口外全部摘要化。

视图按角色职责裁剪：Planner 看任务状态与结论（不看原始工具输出），
Executor 看自己任务的完整轨迹，Validator 看 Checklist 与各任务结论。
写入与压缩均持锁，支持并行任务下的多线程安全。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

CompressFn = Callable[[list[str]], str]


def truncate_result(text: str, max_chars: int) -> str:
    """第一级：单结果截断，保留头部 60% / 尾部 40%。"""
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.6)]
    tail = text[-int(max_chars * 0.4) :]
    return f"{head}\n[... 截断 {len(text) - max_chars} 字符 ...]\n{tail}"


@dataclass
class Step:
    role: str
    task_id: str
    action: str  # 工具名 / 控制动作 / "observation"
    detail: str  # 参数摘要或观察文本（已做一级裁剪）
    kind: str = "action"  # action | observation | summary

    def render(self) -> str:
        if self.kind == "observation":
            return f"[{self.task_id}] 观察:\n{self.detail}"
        return f"[{self.task_id}] {self.role} -> {self.action}: {self.detail}"


@dataclass
class WorkingMemory:
    result_max_chars: int = 6000          # 一级阈值
    view_compress_threshold: int = 24000  # 二级阈值（视图总字符）
    recent_steps: int = 6                 # 三级滚动窗口
    compress_fn: CompressFn | None = None
    _steps: list[Step] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _compress_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # ---- 写入（一级裁剪在写入时生效）----
    def add_action(self, role: str, task_id: str, action: str, detail: str) -> None:
        step = Step(role, task_id, action, truncate_result(detail, self.result_max_chars))
        with self._lock:
            self._steps.append(step)

    def add_observation(self, task_id: str, observation: str) -> None:
        step = Step(
            "TOOL", task_id, "observation",
            truncate_result(observation, self.result_max_chars), kind="observation",
        )
        with self._lock:
            self._steps.append(step)

    # ---- 读取 ----
    def all_steps(self) -> list[Step]:
        with self._lock:
            return list(self._steps)

    def task_steps(self, task_id: str) -> list[Step]:
        with self._lock:
            return [s for s in self._steps if s.task_id == task_id]

    def _needs_compression(self) -> bool:
        return sum(len(s.detail) for s in self._steps) > self.view_compress_threshold

    def compress(self, force: bool = False) -> str | None:
        """第二级：把滚动窗口之外的旧步骤压缩成一条摘要，返回摘要文本。

        _compress_lock 串行化整个压缩过程（含锁外的 LLM 调用），防止并行任务
        下两个压缩互相覆盖；写回时合并在 LLM 调用期间新追加的步骤，避免丢失。
        """
        if self.compress_fn is None:
            return None
        with self._compress_lock:
            with self._lock:
                if not force and not self._needs_compression():
                    return None
                if len(self._steps) <= self.recent_steps:
                    return None
                old, recent = self._steps[:-self.recent_steps], self._steps[-self.recent_steps:]
                consumed = len(old) + len(recent)
            summary = self.compress_fn([s.render() for s in old])  # LLM 调用在 _steps 锁外
            step = Step("MEMORY", "archive", "compressed_history",
                        truncate_result(summary, self.result_max_chars), kind="summary")
            with self._lock:
                appended = self._steps[consumed:]  # 压缩期间并发写入的步骤，原位保留
                self._steps = [step, *recent, *appended]
            return summary

    # ---- 视图渲染（三级：窗口外摘要化 + 按角色取舍）----
    def render_executor_view(self, task_id: str) -> list[str]:
        """Executor 视图：本任务压缩史 + 最近 N 步全文。"""
        steps = self.task_steps(task_id)
        if len(steps) <= self.recent_steps:
            return [s.render() for s in steps]
        old, recent = steps[:-self.recent_steps], steps[-self.recent_steps:]
        digest = self.compress_fn([s.render() for s in old]) if self.compress_fn else (
            "\n".join(s.render() for s in old)
        )
        return [
            "[早期步骤摘要]",
            truncate_result(digest, self.result_max_chars),
            "[最近步骤]",
            *[s.render() for s in recent],
        ]

    def render_planner_view(self, tasks: list) -> str:
        """Planner 视图：只看任务状态与结论，不看原始工具输出。"""
        return "\n\n".join(t.render() for t in tasks)

    def render_validator_view(self, checklist, tasks: list) -> str:
        """Validator 视图：Checklist + 各任务执行结论。"""
        lines = [checklist.render(), "", "各任务执行结论:"]
        for t in tasks:
            summary = t.result_summary or "（未执行）"
            lines.append(f"- [{t.id}] ({t.status.value}, 尝试{t.attempts}次) {t.title} → {summary}")
        return "\n".join(lines)
