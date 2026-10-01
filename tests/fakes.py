"""测试替身：无需 API Key 的 FakeLLMClient 与 Settings 工厂。"""
from __future__ import annotations

import copy
import json

from minimaster.config import Settings
from minimaster.llm.client import LLMResponse, ProtocolMode, ToolCall


def make_settings(**overrides) -> Settings:
    # 测试替身配置：api_key 为空串占位（FakeLLMClient 不发起任何网络请求）
    base = dict(
        base_url="http://localhost/v1",
        api_key="",
        model="fake-model",
        temperature=0.3,
        max_tokens=1024,
        timeout=5.0,
        planner_model="",
        validator_model="",
        planner_temperature=0.2,
        validator_temperature=0.0,
        stream=False,
        max_rounds=3,
        max_steps=6,
        max_retries=2,
        parallel=1,
        guard_max_repeat=2,
        guard_window_size=12,
        guard_window_max=4,
        result_max_chars=6000,
        view_compress_threshold=24000,
        memory_recent_steps=6,
        live_threshold_chars=30000,
        token_budget=24000,
    )
    base.update(overrides)
    return Settings(**base)


def tool_call_resp(name: str, arguments: dict, call_id: str) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(name=name, arguments=arguments, call_id=call_id)],
        finish_reason="tool_calls",
    )


def text_resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop")


def json_resp(obj: dict) -> LLMResponse:
    return LLMResponse(content=json.dumps(obj, ensure_ascii=False), finish_reason="stop")


class FakeLLMClient:
    """脚本化 LLM 替身：按序弹出 LLMResponse，记录全部请求。"""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[dict] = []
        self.protocol_mode = ProtocolMode.NATIVE
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_requests = 0
        self.last_prompt_tokens = 0

    def chat(self, messages, tools=None, service=None, whitelist=None,
             temperature=None, force_json=False, model=None, on_content=None, **_):
        self.calls.append(
            {"messages": copy.deepcopy(messages), "tools": tools, "model": model}
        )
        self.total_requests += 1
        self.total_prompt_tokens += 100  # 模拟真实 usage 累加（预算熔断测试依赖）
        self.total_completion_tokens += 10
        self.last_prompt_tokens = 100
        if not self._script:
            raise AssertionError("FakeLLMClient 脚本耗尽（多余的模型调用）")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(messages)
        return item

    def chat_structured(self, messages, model=None, temperature=None, max_attempts=3) -> dict:
        from minimaster.utils.json_tools import extract_json

        resp = self.chat(messages, force_json=True, model=model)
        return extract_json(resp.content)
