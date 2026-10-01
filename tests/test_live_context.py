"""LiveContextTrimmer 单元测试：live 消息流压缩与 NATIVE 配对完整性。"""
from __future__ import annotations

from minimaster.memory.live_context import LiveContextTrimmer

SYSTEM = {"role": "system", "content": "你是 Executor"}


def build_messages(groups: int, payload: str = "x" * 2000) -> list[dict]:
    messages = [SYSTEM, {"role": "user", "content": "开始任务"}]
    for i in range(groups):
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "bash", "arguments": f'{{"command":"cmd{i}"}}'}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"输出{i} {payload}"})
    return messages


def test_noop_below_threshold():
    trimmer = LiveContextTrimmer(threshold_chars=10**9)
    messages = build_messages(3)
    out, trimmed = trimmer.trim(messages)
    assert not trimmed and out is messages


def test_trims_old_groups_keeps_recent_intact():
    trimmer = LiveContextTrimmer(
        threshold_chars=5000, keep_recent_groups=2,
        compress_fn=lambda steps: "【摘要】早期步骤已完成",
    )
    messages = build_messages(6)
    out, trimmed = trimmer.trim(messages)

    assert trimmed
    joined = "\n".join(str(m.get("content")) for m in out)
    assert "[前情摘要]" in joined and "【摘要】" in joined
    assert "输出5" in joined and "输出4" in joined  # 最近组完整保留
    assert "输出0" not in joined and "输出2" not in joined  # 旧组已压缩
    # 保留部分的 tool 消息前面必有带同 id tool_calls 的 assistant（配对不破坏）
    for idx, m in enumerate(out):
        if m.get("role") == "tool":
            prev = out[idx - 1]
            ids = {tc["id"] for tc in prev.get("tool_calls", [])}
            assert m["tool_call_id"] in ids


def test_token_overflow_forces_trim():
    trimmer = LiveContextTrimmer(threshold_chars=10**9, keep_recent_groups=2)
    messages = build_messages(5, payload="short")
    out, trimmed = trimmer.trim(messages, token_overflow=True)
    assert trimmed and len(out) < len(messages)


def test_fallback_without_compress_fn():
    trimmer = LiveContextTrimmer(threshold_chars=5000, keep_recent_groups=1)
    messages = build_messages(5)
    out, trimmed = trimmer.trim(messages)
    assert trimmed
    summary = next(m for m in out if "[前情摘要]" in str(m.get("content")))
    assert len(summary["content"]) <= 3500  # 兜底截断生效


def test_total_chars_counts_tool_calls():
    messages = build_messages(1)
    plain = LiveContextTrimmer.total_chars([m for m in messages if "tool_calls" not in m])
    with_calls = LiveContextTrimmer.total_chars(messages)
    assert with_calls > plain
