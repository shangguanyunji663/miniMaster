"""Validator：对照 Completion Checklist 验收，产出精确的 missing_requirements。

动作白名单: verify（不持有任何业务工具）。
判定结果写回 Checklist（satisfied + evidence），缺失项回流 Harness 反馈闭环。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.action_policy import Action, Role
from ..core.checklist import CompletionChecklist
from ..core.state_machine import Task
from ..llm.client import LLMClient
from ..memory.working_memory import WorkingMemory
from ..prompts.builder import PromptBuilder
from ..utils.reporter import Reporter
from .base import BaseAgent


@dataclass
class ValidationVerdict:
    completed: bool
    missing_requirements: list[str] = field(default_factory=list)
    summary: str = ""


class Validator(BaseAgent):
    role = Role.VALIDATOR

    def __init__(
        self,
        llm: LLMClient,
        builder: PromptBuilder,
        policy,
        reporter: Reporter,
        memory: WorkingMemory,
        model: str | None = None,
        temperature: float | None = None,
    ) -> None:
        super().__init__(llm, builder, policy, reporter, model=model, temperature=temperature)
        self.memory = memory

    def verify(self, goal: str, checklist: CompletionChecklist, tasks: list[Task]) -> ValidationVerdict:
        self.enforce(Action.VERIFY.value)  # 白名单校验
        view = self.memory.render_validator_view(checklist, tasks)
        context = {"goal": goal, "validator_view": view}
        messages = [
            {"role": "system", "content": self.builder.system_message(Role.VALIDATOR, context)},
            {
                "role": "user",
                "content": "请对照 Completion Checklist 逐项判定，输出 JSON 判定结果。",
            },
        ]
        data = self._chat_structured(messages)

        updates = data.get("items") or []
        checklist.apply_verdict(updates if isinstance(updates, list) else [])
        missing = [
            str(m) for m in (data.get("missing_requirements") or []) if str(m).strip()
        ]
        if not checklist.is_complete:
            missing += [
                f"Checklist 未满足: {item.text}"
                for item in checklist.unmet()
                if all(item.text != str(m).strip() for m in missing)
            ]
        verdict = ValidationVerdict(
            completed=bool(data.get("completed")) and checklist.is_complete,
            missing_requirements=missing,
            summary=str(data.get("summary", "")),
        )
        self.reporter.emit(
            "validated",
            {
                "role": self.role.value,
                "completed": verdict.completed,
                "unmet_items": len(checklist.unmet()),
                "missing": verdict.missing_requirements[:3],
                "summary": verdict.summary[:200],
            },
        )
        return verdict
