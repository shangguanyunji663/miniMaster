"""断点续跑快照：Harness 状态 ↔ state.json 的序列化与恢复。

保存内容：目标、轮次进度、任务全量（状态/尝试/结论/依赖）、Checklist、
Retry Archive 教训。工作记忆的原始步骤轨迹不入快照（重启后由任务结论与
归档教训提供上下文），这是有意的取舍——快照轻量、恢复语义清晰。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..memory.retry_archive import RetryRecord
from .checklist import CompletionChecklist
from .state_machine import Task

SNAPSHOT_VERSION = 1


def snapshot_to_dict(
    goal: str,
    round_no: int,
    validated: bool,
    tasks: list[Task],
    checklist: CompletionChecklist,
    records: list[RetryRecord],
    workdir: str = "",
) -> dict:
    return {
        "version": SNAPSHOT_VERSION,
        "goal": goal,
        "round_no": round_no,
        "validated": validated,
        "workdir": workdir,
        "tasks": [t.to_dict() for t in tasks],
        "checklist": checklist.to_dict(),
        "archive": [asdict(r) for r in records],
    }


def save_snapshot(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_snapshot(path: Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(raw.get("version", 0)) != SNAPSHOT_VERSION:
        raise ValueError(f"不支持的快照版本: {raw.get('version')}（期望 {SNAPSHOT_VERSION}）")
    return raw


def restore_state(raw: dict) -> tuple[str, int, bool, list[Task], CompletionChecklist, list[RetryRecord]]:
    """从快照 dict 重建（goal, round_no, validated, tasks, checklist, archive 记录）。"""
    goal = str(raw["goal"])
    round_no = int(raw.get("round_no", 0))
    validated = bool(raw.get("validated", False))
    tasks = [Task.from_dict(d) for d in raw.get("tasks", [])]
    checklist = CompletionChecklist.from_dict(raw.get("checklist", {}))
    records = [RetryRecord(**r) for r in raw.get("archive", [])]
    return goal, round_no, validated, tasks, checklist, records


def read_workdir(path: Path) -> str:
    """读取快照记录的工作目录（resume 时默认复用）。"""
    return str(load_snapshot(path).get("workdir") or "")
