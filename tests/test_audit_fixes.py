"""审计修复的回归测试：compress 竞态 / 空 checklist 兜底 / 非法依赖 / 预算熔断。"""
import threading
import time

from fakes import FakeLLMClient, json_resp, make_settings, tool_call_resp

from minimaster.memory.working_memory import WorkingMemory


def test_compress_writeback_preserves_concurrent_appends():
    """compress 的 LLM 调用期间新写入的步骤不被旧快照覆盖（TOCTOU 回归）。"""
    gate = threading.Event()

    def slow_compress(steps):
        gate.wait(timeout=5)  # 模拟 LLM 耗时，等并发写入发生
        return "【摘要】"

    memory = WorkingMemory(
        view_compress_threshold=1, recent_steps=2, compress_fn=slow_compress
    )
    for i in range(5):
        memory.add_action("EXECUTOR", "T1", "bash", f"old-{i}")

    result: list = []

    def do_compress():
        result.append(memory.compress())

    thread = threading.Thread(target=do_compress)
    thread.start()
    time.sleep(0.1)  # 等 compress 拿到快照进入 LLM 调用
    memory.add_action("EXECUTOR", "T1", "bash", "late-write")  # 竞态写入
    gate.set()
    thread.join(timeout=5)

    assert result and result[0] == "【摘要】"
    details = [s.detail for s in memory.all_steps()]
    assert any("late-write" in d for d in details), "压缩期间并发写入的步骤被覆盖丢失"


def test_planner_empty_checklist_derived_from_done_criteria(tmp_path):
    """Planner 返回空 checklist → 从 done_criteria 派生，验收不再永假。"""
    from minimaster.core.harness import Harness
    from minimaster.utils.reporter import Reporter

    llm = FakeLLMClient([
        json_resp({"tasks": [{"id": "T1", "title": "写文件", "description": "d",
                              "done_criteria": "a.txt 存在"}],
                   "checklist": []}),  # 空 checklist（合法 JSON、语义无效）
        tool_call_resp("write", {"path": "a.txt", "content": "data"}, "c1"),
        tool_call_resp("submit_result", {"success": True, "summary": "已创建"}, "c2"),
        json_resp({"items": [{"item_id": "C1", "satisfied": True, "evidence": "ok"}],
                   "completed": True, "missing_requirements": [], "summary": "通过"}),
    ])
    settings = make_settings(max_steps=6)
    harness = Harness(settings, tmp_path / "w", Reporter(run_dir=None, quiet=True), llm=llm)
    report = harness.run("创建 a.txt")

    assert report.success  # 派生 checklist 后正常验收通过
    assert harness.checklist.items[0].item_id == "C1"
    assert "a.txt 存在" in harness.checklist.items[0].text


def test_planner_empty_tasks_semantic_retry(tmp_path):
    """tasks 为空的合法 JSON → 语义级重问一次后产出任务。"""
    from minimaster.agents.planner import Planner
    from minimaster.core.action_policy import ActionPolicy, Role
    from minimaster.prompts.builder import PromptBuilder
    from minimaster.utils.reporter import Reporter

    llm = FakeLLMClient([
        json_resp({"tasks": [], "checklist": []}),  # 第一次语义无效
        json_resp({"tasks": [{"id": "T1", "title": "做事", "description": "d",
                              "done_criteria": "c"}],
                   "checklist": ["完成"]}),
    ])
    planner = Planner(llm, PromptBuilder(tmp_path, 5), ActionPolicy(Role.PLANNER),
                      Reporter(run_dir=None, quiet=True))
    tasks, checklist = planner.plan("随便什么目标")
    assert [t.id for t in tasks] == ["T1"]
    assert checklist.items
    # 第二次请求应带有反馈消息
    second = llm.calls[1]["messages"]
    assert any("tasks 为空" in str(m.get("content")) for m in second)


def test_sanitize_deps_drops_unknown_ids(tmp_path):
    """depends_on 指向不存在任务 → 剔除并留痕，不产生永远 BLOCKED 的死任务。"""
    from minimaster.core.harness import Harness
    from minimaster.utils.reporter import Reporter

    llm = FakeLLMClient([
        json_resp({"tasks": [
            {"id": "T1", "title": "独立任务", "description": "d", "done_criteria": "c"},
            {"id": "T2", "title": "幽灵依赖", "description": "d", "done_criteria": "c",
             "depends_on": ["T1", "T9"]},
        ], "checklist": ["完成"]}),
        tool_call_resp("submit_result", {"success": True, "summary": "done"}, "c1"),
        tool_call_resp("submit_result", {"success": True, "summary": "done"}, "c2"),
        json_resp({"items": [{"item_id": "C1", "satisfied": True, "evidence": "ok"}],
                   "completed": True, "missing_requirements": [], "summary": "通过"}),
    ])
    settings = make_settings(max_steps=6)
    harness = Harness(settings, tmp_path / "w", Reporter(run_dir=None, quiet=True), llm=llm)
    report = harness.run("任意目标")

    t2 = next(t for t in harness.tasks if t.id == "T2")
    assert t2.depends_on == ["T1"]  # T9 被剔除
    assert t2.status.value in ("DONE", "PENDING")  # 不因幽灵依赖被阻塞
    assert harness.reporter.event_counts.get("deps_invalid") == 1
    assert report.success


def test_budget_breaker_blocks_pending_tasks(tmp_path):
    """token 预算熔断：规划后即超限 → 所有 PENDING 置 BLOCKED，不再发起执行调用。"""
    from minimaster.core.harness import Harness
    from minimaster.utils.reporter import Reporter

    llm = FakeLLMClient([
        json_resp({"tasks": [
            {"id": "T1", "title": "任务一", "description": "d", "done_criteria": "c"},
            {"id": "T2", "title": "任务二", "description": "d", "done_criteria": "c"},
        ], "checklist": ["完成"]}),
        # 预算熔断后不再有执行调用，直接进入验收与重规划收尾
        json_resp({"items": [], "completed": False,
                   "missing_requirements": ["预算耗尽未完成"], "summary": "预算不足"}),
        json_resp({"analysis": "预算耗尽，无法继续", "tasks": []}),
    ])
    settings = make_settings(max_steps=6, max_total_tokens=100)  # Fake 每次调用 +100，规划后即超限
    harness = Harness(settings, tmp_path / "w", Reporter(run_dir=None, quiet=True), llm=llm)
    report = harness.run("两个任务")

    assert not report.success
    statuses = {t.id: t.status.value for t in harness.tasks}
    assert statuses == {"T1": "BLOCKED", "T2": "BLOCKED"}
    assert harness.reporter.event_counts.get("budget_exceeded", 0) >= 1
    assert llm.total_requests == 3  # 仅规划+验收+重规划，Executor 的模型调用从未发生
