"""Executor 单元测试：工具循环 / Guard / Policy / 控制动作 / 步数预算 / 协议簿记。"""
from __future__ import annotations

import pytest
from fakes import FakeLLMClient, make_settings, text_resp, tool_call_resp

from minimaster.agents.executor import Executor
from minimaster.core.action_policy import ActionPolicy
from minimaster.core.guard import RepeatedActionGuard
from minimaster.core.state_machine import Task
from minimaster.llm.client import LLMResponse, ProtocolMode, ToolCall
from minimaster.memory.live_context import LiveContextTrimmer
from minimaster.memory.working_memory import WorkingMemory
from minimaster.prompts.builder import PromptBuilder
from minimaster.tools.base import ToolContext
from minimaster.tools.filesystem import default_filesystem_tools
from minimaster.tools.service import ToolService
from minimaster.utils.reporter import Reporter


@pytest.fixture()
def tool_env(tmp_path):
    svc = ToolService()
    for tool in default_filesystem_tools():
        svc.register(tool)
    return svc, ToolContext(workdir=tmp_path), tmp_path


def make_executor(llm, tool_env, tmp_path) -> Executor:
    svc, ctx, _ = tool_env
    settings = make_settings()
    reporter = Reporter(run_dir=None, quiet=True)
    builder = PromptBuilder(tmp_path, settings.max_steps)
    return Executor(
        llm, builder, ActionPolicy.for_executor_with_tools(svc.names()), reporter,
        svc, ctx, WorkingMemory(), RepeatedActionGuard(),
        trimmer=LiveContextTrimmer(threshold_chars=10**9), token_budget=settings.token_budget,
    )


def test_runs_tool_then_submits(tool_env, tmp_path):
    llm = FakeLLMClient([
        tool_call_resp("write", {"path": "a.txt", "content": "hi"}, "c1"),
        tool_call_resp("submit_result", {"success": True, "summary": "a.txt 已创建"}, "c2"),
    ])
    executor = make_executor(llm, tool_env, tmp_path)
    result = executor.run(Task("T1", "写文件", "创建 a.txt", "a.txt 存在"), "", max_steps=5)

    assert result.success and result.steps_used == 2
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hi"
    # NATIVE 消息簿记：assistant(tool_calls) 与 role=tool 按 call_id 配对
    final = llm.calls[-1]["messages"]
    tool_msgs = {m["tool_call_id"]: m for m in final if m.get("role") == "tool"}
    assert set(tool_msgs) == {"c1"}  # submit_result 由 Executor 拦截，不产生 tool 消息
    assistants = [m for m in final if m.get("role") == "assistant" and m.get("tool_calls")]
    assert assistants[0]["tool_calls"][0]["id"] == "c1"


def test_guard_blocks_repeated_tool_call(tool_env, tmp_path):
    llm = FakeLLMClient([
        tool_call_resp("write", {"path": "b.txt", "content": "x"}, "c1"),
        tool_call_resp("write", {"path": "b.txt", "content": "x"}, "c2"),
        tool_call_resp("write", {"path": "b.txt", "content": "x"}, "c3"),  # 第 3 次 → Guard
        tool_call_resp("submit_result", {"success": True, "summary": "ok"}, "c4"),
    ])
    executor = make_executor(llm, tool_env, tmp_path)
    result = executor.run(Task("T1", "写文件", "创建", "存在"), "", max_steps=6)

    assert result.success
    final = llm.calls[-1]["messages"]
    blocked = [m for m in final if m.get("role") == "tool" and "Guard 拦截" in m.get("content", "")]
    assert blocked and blocked[0]["tool_call_id"] == "c3"


def test_policy_violation_intercepted(tool_env, tmp_path):
    llm = FakeLLMClient([
        tool_call_resp("create_tasks", {"tasks": []}, "c1"),  # Executor 白名单外
        tool_call_resp("submit_result", {"success": True, "summary": "ok"}, "c2"),
    ])
    executor = make_executor(llm, tool_env, tmp_path)
    result = executor.run(Task("T1", "越权", "试图规划", "拒绝"), "", max_steps=5)

    assert result.success  # 拦截后模型改用 submit_result 收口
    final = llm.calls[-1]["messages"]
    assert any(m.get("role") == "tool" and "[策略拦截]" in m.get("content", "") for m in final)


def test_request_block_branch(tool_env, tmp_path):
    llm = FakeLLMClient([
        tool_call_resp("request_block", {"reason": "缺少网络权限"}, "c1"),
    ])
    executor = make_executor(llm, tool_env, tmp_path)
    result = executor.run(Task("T1", "联网", "下载", "成功"), "", max_steps=3)

    assert result.blocked and not result.success
    assert "缺少网络权限" in result.summary


def test_step_budget_forced_stop(tool_env, tmp_path):
    llm = FakeLLMClient([text_resp("我觉得应该差不多快好了") for _ in range(3)])
    executor = make_executor(llm, tool_env, tmp_path)
    result = executor.run(Task("T1", "空转", "无动作", "完成"), "", max_steps=3)

    assert result.timed_out and not result.success
    assert result.steps_used == 3
    assert "强制收口" in result.summary


def test_text_protocol_bookkeeping(tool_env, tmp_path):
    # TEXT_JSON 协议：无 call_id → assistant 出 JSON 动作文本 + user 观察回灌
    llm = FakeLLMClient([
        LLMResponse(tool_calls=[ToolCall(name="write", arguments={"path": "c.txt", "content": "z"},
                                         call_id="")], finish_reason="tool_calls"),
        LLMResponse(tool_calls=[ToolCall(name="submit_result",
                                         arguments={"success": True, "summary": "done"},
                                         call_id="")], finish_reason="tool_calls"),
    ])
    llm.protocol_mode = ProtocolMode.TEXT_JSON
    executor = make_executor(llm, tool_env, tmp_path)
    result = executor.run(Task("T1", "写文件", "创建", "存在"), "", max_steps=4)

    assert result.success
    final = llm.calls[-1]["messages"]
    assert any(m.get("role") == "user" and "[工具 write 执行结果]" in m.get("content", "")
               for m in final)
    assistants = [m["content"] for m in final if m.get("role") == "assistant"]
    assert any('"action": "tool"' in c for c in assistants)
