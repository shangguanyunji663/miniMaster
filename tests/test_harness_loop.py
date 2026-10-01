"""Harness 闭环集成测试（FakeLLM，无 API）：规划→执行→验收/重规划/重试/依赖/并行/续跑。"""
from __future__ import annotations

import pytest
from fakes import FakeLLMClient, json_resp, make_settings, text_resp, tool_call_resp

from minimaster.core.harness import Harness
from minimaster.core.state_machine import TaskStatus
from minimaster.utils.reporter import Reporter

ACCEPT = {
    "items": [{"item_id": "C1", "satisfied": True, "evidence": "a.txt 存在"}],
    "completed": True,
    "missing_requirements": [],
    "summary": "全部满足",
}
REJECT = {
    "items": [{"item_id": "C1", "satisfied": False, "evidence": ""}],
    "completed": False,
    "missing_requirements": ["缺少 a.txt 内容验证"],
    "summary": "内容未验证",
}


def build_harness(llm, tmp_path, snapshot=True, **settings_overrides):
    settings_overrides.setdefault("max_steps", 6)
    from fakes import make_settings

    settings = make_settings(**settings_overrides)
    workdir = tmp_path / "work"
    snap_dir = tmp_path / "snap" if snapshot else None
    harness = Harness(settings, workdir, Reporter(run_dir=None, quiet=True),
                      llm=llm, snapshot_dir=snap_dir)
    return harness, snap_dir, workdir


def test_happy_path_one_round(tmp_path):
    llm = FakeLLMClient([
        json_resp({"tasks": [{"id": "T1", "title": "写文件", "description": "创建 a.txt",
                              "done_criteria": "a.txt 存在"}],
                   "checklist": ["a.txt 文件存在"]}),
        tool_call_resp("write", {"path": "a.txt", "content": "data"}, "c1"),
        tool_call_resp("submit_result", {"success": True, "summary": "已创建 a.txt"}, "c2"),
        json_resp(ACCEPT),
    ])
    harness, snap_dir, workdir = build_harness(llm, tmp_path)
    report = harness.run("创建 a.txt")

    assert report.success and report.rounds_used == 1
    assert harness.tasks[0].status is TaskStatus.DONE
    assert harness.checklist.is_complete
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "data"
    assert (snap_dir / "state.json").exists()  # 快照已落盘


def test_fail_then_same_round_retry_with_archive(tmp_path):
    llm = FakeLLMClient([
        json_resp({"tasks": [{"id": "T1", "title": "写文件", "description": "d",
                              "done_criteria": "c"}],
                   "checklist": ["a.txt 文件存在"]}),
        tool_call_resp("submit_result", {"success": False, "summary": "写入被拒绝"}, "c1"),
        text_resp("根因：写入被拒；教训：先检查目录再写入"),  # 失败归档压缩
        tool_call_resp("write", {"path": "a.txt", "content": "data"}, "c2"),  # 第 2 次尝试
        tool_call_resp("submit_result", {"success": True, "summary": "已完成"}, "c3"),
        json_resp(ACCEPT),
    ])
    harness, _, _ = build_harness(llm, tmp_path)
    report = harness.run("创建 a.txt")

    assert report.success and report.rounds_used == 1  # 重试与轮次解耦：同轮完成
    task = harness.tasks[0]
    assert task.attempts == 2 and task.status is TaskStatus.DONE
    assert harness.archive.for_task("T1")  # 失败轨迹已归档
    # 重试时教训已注入 Executor 上下文
    retry_call = llm.calls[3]["messages"]  # 第 2 次尝试的首个请求
    assert any("教训" in str(m.get("content")) for m in retry_call if m.get("role") == "system")


def test_reject_then_replan_second_round_accept(tmp_path):
    llm = FakeLLMClient([
        json_resp({"tasks": [{"id": "T1", "title": "写文件", "description": "d",
                              "done_criteria": "c"}],
                   "checklist": ["a.txt 文件存在"]}),
        tool_call_resp("write", {"path": "a.txt", "content": "data"}, "c1"),
        tool_call_resp("submit_result", {"success": True, "summary": "已创建"}, "c2"),
        json_resp(REJECT),  # round 1 验收不过
        json_resp({"analysis": "补充验证任务",
                   "tasks": [{"id": "T2", "title": "验证 a.txt", "description": "读取确认",
                              "done_criteria": "内容为 data"}]}),
        tool_call_resp("read", {"path": "a.txt"}, "c3"),
        tool_call_resp("submit_result", {"success": True, "summary": "内容 data 正确"}, "c4"),
        json_resp(ACCEPT),  # round 2 验收通过
    ])
    harness, _, _ = build_harness(llm, tmp_path)
    report = harness.run("创建并验证 a.txt")

    assert report.success and report.rounds_used == 2
    assert [t.id for t in harness.tasks] == ["T1", "T2"]


def test_unmet_deps_block_task(tmp_path):
    llm = FakeLLMClient([
        json_resp({"tasks": [
            {"id": "T1", "title": "写文件", "description": "d", "done_criteria": "c"},
            {"id": "T2", "title": "运行", "description": "d", "done_criteria": "c",
             "depends_on": ["T1"]},
        ], "checklist": ["a.txt 存在"]}),
        tool_call_resp("submit_result", {"success": False, "summary": "磁盘已满"}, "c1"),
        json_resp(REJECT),  # validator（max_retries=0 → 不归档、无压缩调用）
    ])
    harness, _, _ = build_harness(llm, tmp_path, max_retries=0, max_rounds=1)
    report = harness.run("写并运行")

    assert not report.success
    assert harness.tasks[0].status is TaskStatus.FAILED  # 重试额度 0 → 直接终态
    assert harness.tasks[1].status is TaskStatus.BLOCKED  # 依赖失败 → 阻塞
    assert "依赖" in harness.tasks[1].result_summary


def test_parallel_independent_tasks(tmp_path):
    llm = FakeLLMClient([
        json_resp({"tasks": [
            {"id": "T1", "title": "写 x1", "description": "d", "done_criteria": "c"},
            {"id": "T2", "title": "写 x2", "description": "d", "done_criteria": "c"},
        ], "checklist": ["两个文件存在"]}),
        # 并行下脚本弹出顺序不定，但写不同文件 + 提交成功与任务无关
        tool_call_resp("write", {"path": "x1.txt", "content": "1"}, "c1"),
        tool_call_resp("submit_result", {"success": True, "summary": "x1 完成"}, "c2"),
        tool_call_resp("write", {"path": "x2.txt", "content": "2"}, "c3"),
        tool_call_resp("submit_result", {"success": True, "summary": "x2 完成"}, "c4"),
        json_resp({"items": [{"item_id": "C1", "satisfied": True, "evidence": "ok"}],
                   "completed": True, "missing_requirements": [], "summary": "通过"}),
    ])
    harness, _, workdir = build_harness(llm, tmp_path, parallel=2)
    report = harness.run("并行写两个文件")

    assert report.success
    assert {t.status for t in harness.tasks} == {TaskStatus.DONE}
    assert (workdir / "x1.txt").exists() and (workdir / "x2.txt").exists()


def test_resume_from_snapshot(tmp_path):
    # 第一段：任务完成后验收被拒、重规划无新任务而收尾 → 留下快照（round 1, validated=False）
    llm = FakeLLMClient([
        json_resp({"tasks": [{"id": "T1", "title": "写文件", "description": "d",
                              "done_criteria": "c"}],
                   "checklist": ["a.txt 文件存在"]}),
        tool_call_resp("write", {"path": "a.txt", "content": "data"}, "c1"),
        tool_call_resp("submit_result", {"success": True, "summary": "已创建"}, "c2"),
        json_resp(REJECT),
        json_resp({"analysis": "任务已完成但验收口径不同，无需新增任务", "tasks": []}),
    ])
    harness, snap_dir, workdir = build_harness(llm, tmp_path)
    first = harness.run("创建 a.txt")
    assert not first.success

    # 第二段：从快照续跑 → 重新验收通过
    llm2 = FakeLLMClient([json_resp(ACCEPT)])
    resumed = Harness(make_settings(), workdir, Reporter(run_dir=None, quiet=True),
                      llm=llm2, snapshot_dir=snap_dir)
    resumed.load_state(snap_dir / "state.json")
    report = resumed.run(None)

    assert report.success
    assert resumed.goal == "创建 a.txt"
    assert resumed.tasks[0].status is TaskStatus.DONE
    assert resumed.checklist.is_complete


def test_checklist_acceptance_overrides_superseded_failed_task(tmp_path):
    # 旧任务失败后被重规划替代：只要 Checklist 逐项满足，旧 FAILED 不阻塞验收
    llm = FakeLLMClient([
        json_resp({"tasks": [{"id": "T1", "title": "写文件", "description": "d",
                              "done_criteria": "c"}],
                   "checklist": ["a.txt 文件存在"]}),
        tool_call_resp("submit_result", {"success": False, "summary": "磁盘已满"}, "c1"),
        json_resp(ACCEPT),  # validator：目标实际已由其他途径满足
    ])
    harness, _, _ = build_harness(llm, tmp_path, max_retries=0, max_rounds=1)
    report = harness.run("创建 a.txt")

    assert report.success
    assert harness.tasks[0].status is TaskStatus.FAILED  # 旧任务保持失败终态
    assert harness.checklist.is_complete


def test_goal_required_without_resume(tmp_path):
    harness, _, _ = build_harness(FakeLLMClient([]), tmp_path)
    with pytest.raises(ValueError):
        harness.run(None)
