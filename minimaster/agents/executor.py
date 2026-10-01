"""Executor：单任务的 function calling 工具调用循环。

可用动作 = 6 个业务工具 + 2 个控制工具（submit_result / request_block），
全部经 Action Policy 白名单校验；重复/振荡动作由 Guard 拦截；步数上限强制
收口；live 消息流超阈值（字符估算或真实 token 反馈）自动压缩。

每个任务由 Harness 注入独立的 ToolContext（cwd 会话隔离）与 Guard 实例，
支持并行执行。消息簿记按协议自适应（NATIVE / TEXT_JSON）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..core.action_policy import Action, Role
from ..core.guard import RepeatedActionGuard
from ..llm.client import LLMClient
from ..memory.live_context import LiveContextTrimmer
from ..memory.working_memory import WorkingMemory
from ..prompts.builder import PromptBuilder
from ..tools.base import ToolContext, ToolSpec
from ..tools.service import ToolService
from ..utils.reporter import Reporter
from .base import BaseAgent


@dataclass
class ExecutionResult:
    success: bool
    summary: str
    blocked: bool = False
    steps_used: int = 0
    timed_out: bool = False


# 控制工具：由 Harness/Executor 拦截处理，不进 ToolService
SUBMIT_RESULT_SPEC = ToolSpec(
    name=Action.SUBMIT_RESULT.value,
    description="提交当前子任务的最终结论并结束任务。summary 需含可验证证据。",
    parameters={
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "description": "任务是否成功完成"},
            "summary": {"type": "string", "description": "执行结论与证据"},
        },
        "required": ["success", "summary"],
    },
)
REQUEST_BLOCK_SPEC = ToolSpec(
    name=Action.REQUEST_BLOCK.value,
    description="任务因外部因素无法推进（缺依赖/权限等）时申报阻塞并说明原因。",
    parameters={
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "阻塞原因"}},
        "required": ["reason"],
    },
)


class Executor(BaseAgent):
    role = Role.EXECUTOR

    def __init__(
        self,
        llm: LLMClient,
        builder: PromptBuilder,
        policy,
        reporter: Reporter,
        tool_service: ToolService,
        tool_context: ToolContext,
        memory: WorkingMemory,
        guard: RepeatedActionGuard,
        trimmer: LiveContextTrimmer | None = None,
        token_budget: int = 24000,
    ) -> None:
        super().__init__(llm, builder, policy, reporter)
        self.tools = tool_service
        self.tool_context = tool_context
        self.memory = memory
        self.guard = guard
        self.trimmer = trimmer
        self.token_budget = token_budget

    def tool_schemas(self) -> list[dict]:
        return self.tools.schemas() + [
            SUBMIT_RESULT_SPEC.to_openai_schema(),
            REQUEST_BLOCK_SPEC.to_openai_schema(),
        ]

    def run(
        self,
        task,
        retry_lessons: str,
        max_steps: int,
        ctx: ToolContext | None = None,
        guard: RepeatedActionGuard | None = None,
    ) -> ExecutionResult:
        ctx = ctx or self.tool_context
        guard = guard or self.guard
        guard.reset()  # 跨任务重置指纹序列
        context = {
            "task_id": task.id,
            "max_steps": max_steps,
            "task": task,
            "retry_lessons": retry_lessons,
            "memory_view": self.memory.render_executor_view(task.id)
            if retry_lessons or self.memory.task_steps(task.id)
            else [],
        }
        messages: list[dict] = [
            {
                "role": "system",
                "content": self.builder.system_message(Role.EXECUTOR, context),
            },
            {
                "role": "user",
                "content": f"请执行子任务 {task.id}：{task.title}。完成标准：{task.done_criteria}",
            },
        ]
        schemas = self.tool_schemas()

        for step in range(1, max_steps + 1):
            # live 上下文裁剪：字符估算超阈值，或上次真实 prompt token 超预算时触发
            if self.trimmer is not None:
                overflow = self.llm.last_prompt_tokens > self.token_budget
                before = self.trimmer.total_chars(messages)
                messages, trimmed = self.trimmer.trim(messages, token_overflow=overflow)
                if trimmed:
                    self.reporter.emit(
                        "live_trim",
                        {"role": "MEMORY", "task": task.id, "before_chars": before,
                         "after_chars": self.trimmer.total_chars(messages),
                         "last_prompt_tokens": self.llm.last_prompt_tokens},
                    )
            try:
                resp = self.llm.chat(
                    messages,
                    tools=schemas,
                    service=self.tools,
                    whitelist=None,
                    on_content=self.reporter.stream_text,
                )
            except Exception as exc:
                return ExecutionResult(
                    success=False,
                    summary=f"模型调用失败（第 {step} 步）: {exc}",
                    steps_used=step,
                )

            if not resp.has_tool_calls:
                # 纯文本无动作：提示必须出动作（协议引导纠偏）
                messages.append({"role": "assistant", "content": resp.content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "你没有调用任何工具。请通过 function call 调用工具推进任务，"
                            "或调用 submit_result 提交结论。"
                        ),
                    }
                )
                self.memory.add_action(self.role.value, task.id, "no_op", resp.content[:300])
                continue

            # assistant 消息簿记；TEXT_JSON 协议为单动作假设，多动作只执行第一个
            native = all(tc.call_id for tc in resp.tool_calls)
            calls = resp.tool_calls if native else resp.tool_calls[:1]
            if not native and len(resp.tool_calls) > 1:
                self.reporter.emit(
                    "protocol_note",
                    {"role": "HARNESS", "task": task.id,
                     "detail": f"文本协议一次返回 {len(resp.tool_calls)} 个动作，仅执行第一个"},
                )
            messages.append(self._assistant_message(resp, native))

            for call in calls:
                done = self._handle_call(call, task, native, messages, ctx, guard)
                if done is not None:
                    done.steps_used = step
                    return done
            if native:
                messages.append(
                    {
                        "role": "user",
                        "content": f"继续。剩余步数预算 {max_steps - step} 步。",
                    }
                )

        return ExecutionResult(
            success=False,
            summary=f"步数上限 {max_steps} 步已用完，被强制收口；最后状态不足以证明任务完成。",
            steps_used=max_steps,
            timed_out=True,
        )

    # ---- 单个动作处理：白名单 → Guard → 分发；返回终止结果或 None ----
    def _handle_call(self, call, task, native: bool, messages: list[dict],
                     ctx: ToolContext, guard: RepeatedActionGuard):
        name, args = call.name, call.arguments
        args_full = json.dumps(args, ensure_ascii=False)

        # 1) Action Policy 白名单
        try:
            self.enforce(name)
        except Exception as exc:
            self.reporter.emit("policy_violation", {"role": "POLICY", "task": task.id,
                                                    "action": name, "detail": str(exc)})
            self.memory.add_action(self.role.value, task.id, name, f"[拦截] {exc}")
            self._append_observation(messages, native, call, f"[策略拦截] {exc}")
            return None

        # 2) 控制动作（submit_result / request_block）
        if name == Action.SUBMIT_RESULT.value:
            summary = str(args.get("summary", ""))
            self.reporter.emit(
                "task_submit",
                {"role": self.role.value, "task": task.id,
                 "success": bool(args.get("success")), "summary": summary},
            )
            self.memory.add_action(self.role.value, task.id, name, args_full)
            return ExecutionResult(success=bool(args.get("success")), summary=summary)
        if name == Action.REQUEST_BLOCK.value:
            reason = str(args.get("reason", "未说明原因"))
            self.reporter.emit("task_block_request", {"role": self.role.value, "task": task.id,
                                                      "reason": reason})
            self.memory.add_action(self.role.value, task.id, name, args_full)
            return ExecutionResult(success=False, summary=f"任务申报阻塞: {reason}", blocked=True)

        # 3) 重复/振荡动作 Guard
        verdict = guard.check(name, args)
        if not verdict.allowed:
            self.reporter.emit("guard_block", {"role": "GUARD", "task": task.id,
                                               "action": name, "rule": verdict.rule,
                                               "repeat": verdict.repeat_count,
                                               "message": verdict.message})
            self.memory.add_action(self.role.value, task.id, name,
                                   f"[Guard拦截] {verdict.message}")
            self._append_observation(messages, native, call, f"[Guard 拦截] {verdict.message}")
            return None

        # 4) 真正分发工具
        result = self.tools.dispatch(name, args, ctx)
        self.reporter.emit(
            "tool_result",
            {"role": self.role.value, "task": task.id, "tool": name,
             "ok": result.ok, "args": args_full, "output": result.render()},
        )
        self.memory.add_action(self.role.value, task.id, name, args_full)
        self.memory.add_observation(task.id, result.render())
        self._append_observation(messages, native, call, result.render())
        return None

    # ---- 消息簿记 ----
    @staticmethod
    def _assistant_message(resp, native: bool) -> dict:
        if native:
            return {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.call_id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in resp.tool_calls
                ],
            }
        action = {
            "action": "tool",
            "name": resp.tool_calls[0].name,
            "arguments": resp.tool_calls[0].arguments,
        }
        return {"role": "assistant", "content": json.dumps(action, ensure_ascii=False)}

    def _append_observation(self, messages: list[dict], native: bool, call, text: str) -> None:
        if native:
            messages.append({"role": "tool", "tool_call_id": call.call_id, "content": text})
        else:
            messages.append({"role": "user", "content": f"[工具 {call.name} 执行结果]\n{text}"})
