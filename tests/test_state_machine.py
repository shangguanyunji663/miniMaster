import pytest

from minimaster.core.state_machine import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    Task,
    TaskStatus,
)


def test_happy_path_transitions():
    task = Task("T1", "写文件", "创建 a.txt", "a.txt 存在")
    task.transition(TaskStatus.RUNNING)
    assert task.status is TaskStatus.RUNNING
    assert task.attempts == 1
    task.transition(TaskStatus.DONE)
    assert task.is_terminal


def test_illegal_transitions_rejected():
    task = Task("T1", "写文件", "创建 a.txt", "a.txt 存在")
    with pytest.raises(IllegalTransitionError):
        task.transition(TaskStatus.DONE)  # PENDING → DONE 非法
    with pytest.raises(IllegalTransitionError):
        task.transition(TaskStatus.FAILED)  # PENDING → FAILED 非法


def test_pending_to_blocked_allowed_for_unmet_deps():
    # 依赖未满足时任务无法启动即阻塞，属合法迁移
    task = Task("T1", "x", "y", "z", depends_on=["T0"])
    task.transition(TaskStatus.BLOCKED)
    assert task.status is TaskStatus.BLOCKED
    task.transition(TaskStatus.FAILED)  # BLOCKED → FAILED 合法


def test_done_is_terminal():
    task = Task("T1", "x", "y", "z")
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.DONE)
    with pytest.raises(IllegalTransitionError):
        task.transition(TaskStatus.RUNNING)


def test_retry_path_failed_to_pending():
    task = Task("T1", "x", "y", "z")
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.FAILED)
    task.transition(TaskStatus.PENDING)  # 归档重注入的合法迁移
    assert task.status is TaskStatus.PENDING


def test_blocked_can_resume_or_fail():
    task = Task("T1", "x", "y", "z")
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.BLOCKED)
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.FAILED)
    assert ALLOWED_TRANSITIONS[TaskStatus.FAILED] == {TaskStatus.PENDING}


def test_depends_on_and_dict_roundtrip():
    task = Task("T2", "运行", "跑脚本", "输出正确", depends_on=["T1"])
    task.transition(TaskStatus.RUNNING)
    task.result_summary = "ok"
    restored = Task.from_dict(task.to_dict())
    assert restored.id == "T2"
    assert restored.depends_on == ["T1"]
    assert restored.status is TaskStatus.RUNNING
    assert restored.attempts == 1
    assert restored.result_summary == "ok"


def test_render_shows_deps():
    task = Task("T2", "运行", "跑脚本", "输出正确", depends_on=["T1"])
    assert "T1" in task.render()
