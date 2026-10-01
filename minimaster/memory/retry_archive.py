"""Retry Archive：失败任务轨迹压缩归档，供重试注入。

任务 FAILED 时，其完整轨迹被 LLM 压缩为「失败原因 + 已试方案 + 教训」一条记录；
任务回到 PENDING 重试时，归档内容注入 Executor 上下文，避免重蹈覆辙。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

CompressFn = Callable[[str, int, str], str]  # (task_title, attempt, failure_reason) -> lessons_text


@dataclass
class RetryRecord:
    task_id: str
    title: str
    attempt: int
    failure_reason: str
    lessons: str

    def render(self) -> str:
        return (
            f"第 {self.attempt} 次尝试失败 —— 原因: {self.failure_reason}\n"
            f"教训: {self.lessons}"
        )


@dataclass
class RetryArchive:
    compress_fn: CompressFn | None = None
    _records: dict[str, list[RetryRecord]] = field(default_factory=dict)

    def archive(self, task, failure_reason: str) -> RetryRecord | None:
        """压缩并归档一次失败轨迹；compress_fn 未注入时退化为截断拼接。"""
        from ..memory.working_memory import truncate_result

        attempt = task.attempts
        if self.compress_fn is not None:
            lessons = self.compress_fn(task.title, attempt, failure_reason)
        else:
            lessons = truncate_result(failure_reason, 2000)
        record = RetryRecord(
            task_id=task.id,
            title=task.title,
            attempt=attempt,
            failure_reason=truncate_result(failure_reason, 1500),
            lessons=truncate_result(lessons, 2500),
        )
        self._records.setdefault(task.id, []).append(record)
        return record

    def for_task(self, task_id: str) -> list[RetryRecord]:
        return self._records.get(task_id, [])

    def restore(self, records: list[RetryRecord]) -> None:
        """快照恢复：直接注入已压缩的归档记录。"""
        for record in records:
            self._records.setdefault(record.task_id, []).append(record)

    def render_for_task(self, task_id: str) -> str:
        records = self.for_task(task_id)
        if not records:
            return ""
        lines = ["== Retry Archive：本任务的历史失败记录（务必避免重蹈覆辙）=="]
        lines += [f"- {r.render()}" for r in records]
        return "\n".join(lines)

    def all_records(self) -> list[RetryRecord]:
        return [r for records in self._records.values() for r in records]
