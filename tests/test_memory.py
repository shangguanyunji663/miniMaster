from minimaster.core.checklist import ChecklistItem, CompletionChecklist
from minimaster.core.state_machine import Task, TaskStatus
from minimaster.memory.retry_archive import RetryArchive
from minimaster.memory.working_memory import WorkingMemory, truncate_result


def test_truncate_keeps_head_and_tail():
    text = "A" * 500 + "MARK" + "B" * 500
    cut = truncate_result(text, 200)
    assert len(cut) < len(text)
    assert cut.startswith("A")
    assert cut.rstrip().endswith("B")
    assert "截断" in cut


def test_short_text_untouched():
    assert truncate_result("hello", 100) == "hello"


def test_executor_view_recent_window_full():
    memory = WorkingMemory(recent_steps=2)
    for i in range(4):
        memory.add_action("EXECUTOR", "T1", "bash", f"step-{i}")
    view = memory.render_executor_view("T1")
    joined = "\n".join(view)
    assert "step-0" in joined and "step-1" in joined  # 早期步骤仍在（压缩函数兜底为原文）
    assert "step-3" in joined


def test_level2_compression_triggered_by_threshold():
    calls = []

    def fake_compress(steps):
        calls.append(steps)
        return "【压缩摘要】做过一些事"

    # 每条 1000 字符 × 8 条 = 8000 > 阈值 4000，最近窗口 2
    memory = WorkingMemory(
        result_max_chars=2000,
        view_compress_threshold=4000,
        recent_steps=2,
        compress_fn=fake_compress,
    )
    for _ in range(8):
        memory.add_action("EXECUTOR", "T1", "bash", "x" * 1000)
    memory.compress()
    assert calls, "超过阈值应触发语义压缩"
    summary = memory.all_steps()[0]
    assert summary.kind == "summary"
    assert len(memory.all_steps()) == 3  # 1 条摘要 + 2 条最近


def test_level2_not_triggered_below_threshold():
    memory = WorkingMemory(view_compress_threshold=10**9, recent_steps=2,
                           compress_fn=lambda steps: "不应被调用")
    for _ in range(5):
        memory.add_action("EXECUTOR", "T1", "bash", "short")
    assert memory.compress() is None


def test_planner_view_hides_raw_output():
    memory = WorkingMemory()
    memory.add_action("EXECUTOR", "T1", "bash", "run tests")
    memory.add_observation("T1", "SECRET-RAW-OUTPUT-123")
    task = Task("T1", "t", "d", "c")
    task.status = TaskStatus.DONE
    task.result_summary = "完成"
    view = memory.render_planner_view([task])
    assert "T1" in view and "完成" in view
    assert "SECRET-RAW-OUTPUT-123" not in view  # 按职责裁剪：规划者不看原始观察


def test_validator_view_contains_checklist_and_summaries():
    memory = WorkingMemory()
    checklist = CompletionChecklist(goal="g", items=[ChecklistItem("C1", "文件存在")])
    task = Task("T1", "写文件", "d", "c")
    task.status = TaskStatus.DONE
    task.result_summary = "a.txt 已创建"
    view = memory.render_validator_view(checklist, [task])
    assert "C1" in view and "a.txt 已创建" in view


def test_retry_archive_injects_lessons():
    archive = RetryArchive(compress_fn=lambda title, attempt, failure: f"教训:{failure[:10]}")
    task = Task("T1", "装依赖", "pip install", "import 成功")
    task.transition(TaskStatus.RUNNING)
    record = archive.archive(task, "网络超时")
    assert record.attempt == 1
    rendered = archive.render_for_task("T1")
    assert "Retry Archive" in rendered and "网络超时" in rendered
    assert archive.render_for_task("T9") == ""
