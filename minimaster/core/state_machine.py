"""任务状态机：PENDING → RUNNING → DONE/FAILED/BLOCKED，硬约束迁移路径。

任何非法跳转（如 PENDING → DONE、DONE → RUNNING）直接抛
IllegalTransitionError，从机制上杜绝越权操作与非法状态迁移。
"""
from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


# 合法迁移表。FAILED → PENDING 用于有重试额度时的归档重注入；
# PENDING → BLOCKED 用于依赖未满足时任务无法启动即阻塞。
ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.BLOCKED},
    TaskStatus.RUNNING: {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BLOCKED},
    TaskStatus.BLOCKED: {TaskStatus.RUNNING, TaskStatus.FAILED},
    TaskStatus.FAILED: {TaskStatus.PENDING},
    TaskStatus.DONE: set(),
}


class IllegalTransitionError(Exception):
    """非法状态迁移。"""


class Task:
    """子任务：Planner 产出的最小执行单元，自带状态机约束与依赖声明。"""

    def __init__(
        self,
        task_id: str,
        title: str,
        description: str,
        done_criteria: str,
        status: TaskStatus = TaskStatus.PENDING,
        depends_on: list[str] | None = None,
    ) -> None:
        self.id = task_id
        self.title = title
        self.description = description
        self.done_criteria = done_criteria
        self.status = status
        self.depends_on = list(depends_on or [])
        self.attempts = 0
        self.result_summary: str | None = None

    def transition(self, new_status: TaskStatus) -> None:
        allowed = ALLOWED_TRANSITIONS[self.status]
        if new_status not in allowed:
            raise IllegalTransitionError(
                f"任务 {self.id} 非法迁移: {self.status.value} → {new_status.value}，"
                f"允许的迁移: {[s.value for s in sorted(allowed, key=lambda x: x.value)]}"
            )
        self.status = new_status
        if new_status == TaskStatus.RUNNING:
            self.attempts += 1

    @property
    def is_terminal(self) -> bool:
        return self.status is TaskStatus.DONE

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "done_criteria": self.done_criteria,
            "status": self.status.value,
            "attempts": self.attempts,
            "result_summary": self.result_summary,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        """快照恢复用：按已保存的状态直接重建（不经状态机校验）。"""
        task = cls(
            task_id=str(data["id"]),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            done_criteria=str(data.get("done_criteria", "")),
            status=TaskStatus(str(data.get("status", "PENDING"))),
            depends_on=[str(d) for d in data.get("depends_on") or []],
        )
        task.attempts = int(data.get("attempts", 0))
        task.result_summary = data.get("result_summary")
        return task

    def render(self) -> str:
        head = f"[{self.id}] ({self.status.value}) {self.title}"
        if self.depends_on:
            head += f"  依赖: {', '.join(self.depends_on)}"
        return (
            f"{head}\n"
            f"  描述: {self.description}\n"
            f"  完成标准: {self.done_criteria}"
            + (f"\n  执行结论: {self.result_summary}" if self.result_summary else "")
        )
