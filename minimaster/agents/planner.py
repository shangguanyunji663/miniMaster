"""Planner：目标拆解 + Completion Checklist 生成 + BLOCKED/缺失项增量重规划。

动作白名单: create_tasks / replan（不持有任何业务工具）。
输出为结构化 JSON（chat_structured 带解析失败重试环），可声明任务依赖。
"""
from __future__ import annotations

from ..core.action_policy import Action, Role
from ..core.checklist import ChecklistItem, CompletionChecklist
from ..core.state_machine import Task
from ..llm.client import LLMClient
from ..prompts.builder import PromptBuilder
from ..utils.reporter import Reporter
from .base import BaseAgent


class Planner(BaseAgent):
    role = Role.PLANNER

    def __init__(self, llm: LLMClient, builder: PromptBuilder, policy, reporter: Reporter,
                 model: str | None = None, temperature: float | None = None) -> None:
        super().__init__(llm, builder, policy, reporter, model=model, temperature=temperature)

    # ---- 初次规划 ----
    def plan(self, goal: str) -> tuple[list[Task], CompletionChecklist]:
        self.enforce(Action.CREATE_TASKS.value)  # 白名单校验
        messages = [
            {"role": "system", "content": self.builder.system_message(Role.PLANNER, {"goal": goal})},
            {"role": "user", "content": f"目标：{goal}\n\n请拆解子任务并生成验收清单。"},
        ]
        data = self._chat_structured(messages)
        tasks = self._parse_tasks(data.get("tasks") or [])
        if not tasks:
            # 语义级重问：合法 JSON 但无子任务（如 tasks 为空），反馈后重试一次
            messages.append({
                "role": "user",
                "content": "上次输出没有可执行的子任务（tasks 为空）。"
                           "请重新输出：至少 1 个子任务，且每项带可观测的 done_criteria。",
            })
            data = self._chat_structured(messages)
            tasks = self._parse_tasks(data.get("tasks") or [])
        if not tasks:
            raise ValueError("Planner 两次尝试后仍未产出任何子任务")
        items = [
            ChecklistItem(item_id=f"C{i}", text=str(text).strip())
            for i, text in enumerate(data.get("checklist") or [], 1)
            if str(text).strip()
        ]
        if not items:
            # 兜底：checklist 缺失/为空时从各任务完成标准派生，避免验收永假死局
            items = [
                ChecklistItem(item_id=f"C{i}", text=f"任务 {t.id} 达成: {t.done_criteria or t.title}")
                for i, t in enumerate(tasks, 1)
            ]
        checklist = CompletionChecklist(goal=goal, items=items)
        self.reporter.emit(
            "plan_created",
            {"role": "PLANNER", "tasks": [f"{t.id}:{t.title}" for t in tasks],
             "checklist_items": len(items)},
        )
        return tasks, checklist

    # ---- 增量重规划（BLOCKED / 验收缺失时）----
    def replan(self, goal: str, tasks: list[Task], missing_feedback: list[str]) -> list[Task]:
        self.enforce(Action.REPLAN.value)  # 白名单校验
        if not missing_feedback:
            return []
        context = {
            "replan": True,
            "goal": goal,
            "planner_view": "\n\n".join(t.render() for t in tasks),
            "missing_feedback": missing_feedback[:8],
        }
        messages = [
            {"role": "system", "content": self.builder.system_message(Role.PLANNER, context)},
            {
                "role": "user",
                "content": (
                    "请针对上述未满足项做增量重规划：只补充真正必要的新任务，"
                    "或对已 BLOCKED 任务给出替代路径。若无必要新增任务，tasks 返回空数组。"
                ),
            },
        ]
        data = self._chat_structured(messages)
        new_tasks = self._parse_tasks(data.get("tasks") or [], start_index=len(tasks))
        self.reporter.emit(
            "replan",
            {"role": "PLANNER", "analysis": str(data.get("analysis", ""))[:200],
             "new_tasks": [t.id for t in new_tasks]},
        )
        return new_tasks

    # ---- 内部 ----
    @staticmethod
    def _parse_tasks(raw_tasks: list, start_index: int = 0) -> list[Task]:
        tasks: list[Task] = []
        for i, raw in enumerate(raw_tasks, start_index + 1):
            if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
                continue
            task_id = str(raw.get("id") or f"T{i}").strip()
            depends_on = [
                str(d).strip()
                for d in (raw.get("depends_on") or [])
                if str(d).strip() and str(d).strip() != task_id
            ]
            tasks.append(
                Task(
                    task_id=task_id,
                    title=str(raw["title"]).strip(),
                    description=str(raw.get("description", "")).strip(),
                    done_criteria=str(raw.get("done_criteria", "")).strip(),
                    depends_on=depends_on,
                )
            )
        return tasks
