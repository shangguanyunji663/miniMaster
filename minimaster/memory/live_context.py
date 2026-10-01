"""Executor live 上下文裁剪：把裁剪策略落到对话消息流本身。

WorkingMemory 的三级裁剪只作用于「任务开始时注入的视图」，而 Executor
循环内的 messages 会随工具观察裸增长。本模块在每步 LLM 调用前检查：
- 字符估算超阈值（threshold_chars），或
- 真实 prompt token 反馈超预算（token_overflow，来自上次响应的 usage），

则把早期的 assistant(tool_calls)+tool 观察组压缩为一条「[前情摘要]」user
消息，保留 system 与最近 N 组完整。NATIVE 协议的 tool_calls/tool 配对
只存在于组内，因此压缩不会破坏配对完整性。
"""
from __future__ import annotations

import json
from collections.abc import Callable

from .working_memory import truncate_result

CompressFn = Callable[[list[str]], str]

_SUMMARY_MAX_CHARS = 3000
_GROUP_RENDER_MAX_CHARS = 4000


class LiveContextTrimmer:
    def __init__(
        self,
        threshold_chars: int = 30000,
        keep_recent_groups: int = 4,
        compress_fn: CompressFn | None = None,
    ) -> None:
        self.threshold_chars = threshold_chars
        self.keep_recent_groups = keep_recent_groups
        self.compress_fn = compress_fn

    @staticmethod
    def total_chars(messages: list[dict]) -> int:
        total = 0
        for m in messages:
            total += len(str(m.get("content") or ""))
            for tc in m.get("tool_calls") or []:
                total += len(json.dumps(tc, ensure_ascii=False))
        return total

    def trim(
        self, messages: list[dict], token_overflow: bool = False
    ) -> tuple[list[dict], bool]:
        """必要时压缩，返回 (新消息列表, 是否发生压缩)。"""
        if not token_overflow and self.total_chars(messages) <= self.threshold_chars:
            return messages, False
        head, groups = self._split_groups(messages)
        if len(groups) <= self.keep_recent_groups:
            return messages, False
        old, recent = groups[:-self.keep_recent_groups], groups[-self.keep_recent_groups:]
        texts = [self._render_group(g) for g in old]
        summary = self._compress(texts)
        trimmed: list[dict] = []
        if head is not None:
            trimmed.append(head)
        trimmed.append(
            {
                "role": "user",
                "content": f"[前情摘要] 早期执行步骤已被压缩，要点如下：\n{summary}",
            }
        )
        trimmed.extend(msg for group in recent for msg in group)
        return trimmed, True

    def _compress(self, texts: list[str]) -> str:
        fallback = truncate_result("\n\n".join(texts), _SUMMARY_MAX_CHARS)
        if self.compress_fn is None:
            return fallback
        try:
            return truncate_result(self.compress_fn(texts), _SUMMARY_MAX_CHARS) or fallback
        except Exception:
            return fallback  # 压缩失败退化为头尾截断

    @staticmethod
    def _split_groups(messages: list[dict]) -> tuple[dict | None, list[list[dict]]]:
        """按 assistant 消息切组：assistant 起组，后续 tool/user 归入本组。"""
        head: dict | None = None
        groups: list[list[dict]] = []
        current: list[dict] | None = None
        for m in messages:
            role = m.get("role")
            if role == "system" and not groups:
                head = m
                continue
            if role == "assistant" or current is None:
                current = [m]
                groups.append(current)
            else:
                current.append(m)
        return head, groups

    @staticmethod
    def _render_group(group: list[dict]) -> str:
        lines: list[str] = []
        for m in group:
            role = m.get("role")
            content = str(m.get("content") or "").strip()
            calls = m.get("tool_calls") or []
            call_text = " ".join(
                f"{tc['function']['name']}({tc['function']['arguments']})"
                for tc in calls
                if isinstance(tc, dict) and "function" in tc
            )
            if role == "assistant" and call_text:
                lines.append(f"assistant 调用: {call_text}")
            elif content:
                lines.append(f"{role}: {content}")
        return truncate_result("\n".join(lines), _GROUP_RENDER_MAX_CHARS)
