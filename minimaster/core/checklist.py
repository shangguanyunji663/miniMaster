"""Completion Checklist：目标验收清单，驱动 Validator 的验证闭环。

Planner 拆解目标时同步生成验收项；每轮结束 Validator 逐项给出
satisfied + evidence；未满足项以 missing_requirements 精确回流系统反馈。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChecklistItem:
    item_id: str
    text: str
    satisfied: bool | None = None  # None = 尚未验收
    evidence: str = ""


@dataclass
class CompletionChecklist:
    goal: str
    items: list[ChecklistItem] = field(default_factory=list)

    def apply_verdict(self, updates: list[dict]) -> None:
        """应用 Validator 的逐项判定：[{item_id, satisfied, evidence}]。"""
        by_id = {i.item_id: i for i in self.items}
        for upd in updates:
            item = by_id.get(str(upd.get("item_id")))
            if item is None:
                continue
            item.satisfied = bool(upd.get("satisfied"))
            item.evidence = str(upd.get("evidence", ""))[:500]

    def unmet(self) -> list[ChecklistItem]:
        return [i for i in self.items if not i.satisfied]

    @property
    def is_complete(self) -> bool:
        return bool(self.items) and all(i.satisfied for i in self.items)

    def to_dict(self) -> dict:
        """快照序列化。"""
        return {
            "goal": self.goal,
            "items": [
                {
                    "item_id": i.item_id,
                    "text": i.text,
                    "satisfied": i.satisfied,
                    "evidence": i.evidence,
                }
                for i in self.items
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> CompletionChecklist:
        items = [
            ChecklistItem(
                item_id=str(i.get("item_id")),
                text=str(i.get("text", "")),
                satisfied=i.get("satisfied"),
                evidence=str(i.get("evidence", "")),
            )
            for i in data.get("items", [])
        ]
        return cls(goal=str(data.get("goal", "")), items=items)

    def render(self) -> str:
        """渲染清单（不含目标行——目标由报告/上下文单独给出，避免重复）。"""
        lines = ["Completion Checklist:"]
        for i in self.items:
            state = {True: "[x]", False: "[ ]", None: "[?]"}[i.satisfied]
            lines.append(f"  {state} {i.item_id}: {i.text}")
            if i.evidence:
                lines.append(f"      证据: {i.evidence}")
        return "\n".join(lines)
