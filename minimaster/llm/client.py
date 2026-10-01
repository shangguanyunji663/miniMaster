"""模型调用层（Prompt 四层解耦的最底层）。

直接对接 OpenAI Function Call 协议。特性：
- 协议双模式自适应：NATIVE（原生 tools/tool_calls）→ TEXT_JSON（降级文本协议）；
- 截断自愈：finish_reason=length 时自动以 2× max_tokens 重试一次（有上限），
  仍截断则标记 truncated=True 交上层处理；
- 结构化输出：chat_structured 带错误反馈重试环，JSON 解析失败自动重问；
- 流式输出：增量累积 content/tool_calls delta，on_content 回调实时输出，
  流式通道故障自动永久回落非流式；
- 按调用路由模型/温度（按角色模型路由的上层入口）；
- 记录 last_prompt_tokens，供 Executor 用真实 usage 校准 live 裁剪时机。

对外统一返回 LLMResponse，上层不感知协议差异。
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from openai import OpenAI

from ..config import Settings
from ..tools.service import ToolService
from ..utils.json_tools import extract_json


class ProtocolMode(str, Enum):
    NATIVE = "native"
    TEXT_JSON = "text_json"


class LLMError(Exception):
    """模型调用失败（重试后仍失败）。"""


@dataclass
class ToolCall:
    name: str
    arguments: dict
    call_id: str = ""


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    truncated: bool = False

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


_TEXT_PROTOCOL_INSTRUCTIONS = """\
## 工具调用协议（文本模式）
你可以使用下列工具（JSON Schema）：
{tool_catalog}

若要调用工具，回复且仅回复一个 JSON 对象（不要多余文本、不要 markdown 代码块、
禁止使用 <tool_call> 等 XML 标签）：
{{"action": "tool", "name": "<工具名>", "arguments": {{...}}}}

若认为任务已完成、无需再调用工具，回复：
{{"action": "final", "content": "<你的结论文本>"}}
"""


class LLMClient:
    MAX_TOKENS_CEILING = 32768

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            max_retries=2,
        )
        self.protocol_mode = ProtocolMode.NATIVE
        self._stream_enabled = settings.stream
        self._stream_broken = False
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_requests = 0
        self.last_prompt_tokens = 0
        self._acct_lock = threading.Lock()  # 并行任务下计数器累加的原子性

    # ---- 对外主入口 ----
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        service: ToolService | None = None,
        whitelist: list[str] | None = None,
        temperature: float | None = None,
        force_json: bool = False,
        model: str | None = None,
        on_content: Callable[[str], None] | None = None,
        _allow_growth: bool = True,
        _max_tokens: int | None = None,
    ) -> LLMResponse:
        """发起一次对话。tools 非空时按当前协议模式适配。"""
        max_tokens = _max_tokens or self._settings.max_tokens
        kwargs = dict(
            model=model or self._settings.model,
            messages=messages,
            temperature=self._settings.temperature if temperature is None else temperature,
            max_tokens=max_tokens,
        )
        if tools and self.protocol_mode is ProtocolMode.NATIVE:
            kwargs["tools"] = tools
        if force_json:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            if self._stream_enabled and not self._stream_broken:
                response = self._chat_stream(kwargs, on_content)
            else:
                response = self._chat_plain(kwargs)
        except Exception as exc:
            # 通道级降级：流式故障 → 非流式；端点不支持 tools/response_format → 逐级关闭重试
            if self._stream_enabled and not self._stream_broken:
                self._stream_broken = True
                return self.chat(messages, tools, service, whitelist, temperature,
                                 force_json, model, None, _allow_growth, _max_tokens)
            if tools and self.protocol_mode is ProtocolMode.NATIVE:
                self.protocol_mode = ProtocolMode.TEXT_JSON
                return self.chat(messages, tools, service, whitelist, temperature,
                                 force_json=False, model=model,
                                 _allow_growth=_allow_growth, _max_tokens=_max_tokens)
            if force_json:
                return self.chat(messages, tools, service, whitelist, temperature,
                                 force_json=False, model=model,
                                 _allow_growth=_allow_growth, _max_tokens=_max_tokens)
            raise LLMError(f"模型调用失败: {exc}") from exc

        self.total_requests += 1
        if response.prompt_tokens or response.completion_tokens:
            with self._acct_lock:
                self.total_prompt_tokens += response.prompt_tokens
                self.total_completion_tokens += response.completion_tokens
                self.last_prompt_tokens = response.prompt_tokens

        # 截断自愈：文本响应被 max_tokens 切断 → 2× max_tokens 重试一次
        if (
            response.finish_reason == "length"
            and _allow_growth
            and not response.has_tool_calls
        ):
            grown = min(self.MAX_TOKENS_CEILING, max_tokens * 2)
            if grown > max_tokens:
                return self.chat(messages, tools, service, whitelist, temperature,
                                 force_json, model, on_content,
                                 _allow_growth=False, _max_tokens=grown)
        response.truncated = response.finish_reason == "length"

        if response.has_tool_calls:
            return response

        if tools:
            parsed = self._parse_text_action(response.content)
            if parsed is None:
                if self.protocol_mode is ProtocolMode.NATIVE:
                    self.protocol_mode = ProtocolMode.TEXT_JSON
                    adapted = self._with_text_protocol(messages, service, whitelist)
                    return self.chat(adapted, tools, service, whitelist, temperature,
                                     force_json=False, model=model,
                                     _allow_growth=False, _max_tokens=max_tokens)
                if response.truncated:
                    return response  # 交由 chat_structured 的反馈重试环处理
                raise LLMError(
                    f"模型未按文本协议输出 JSON 动作，原文片段: {response.content[:200]}"
                )
            if parsed["action"] == "tool":
                response.tool_calls = [
                    ToolCall(name=parsed["name"], arguments=parsed.get("arguments", {}))
                ]
                response.content = ""
            else:
                response.content = parsed.get("content", "")
        return response

    def chat_structured(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_attempts: int = 3,
    ) -> dict:
        """结构化输出：JSON 解析失败/被截断时把错误反馈回模型重问。"""
        convo = list(messages)
        last_error = ""
        for _ in range(max_attempts):
            resp = self.chat(convo, force_json=True, model=model, temperature=temperature)
            if resp.truncated:
                feedback = "你的输出被 max_tokens 截断。请精简内容，重新只输出一个完整 JSON 对象。"
            else:
                try:
                    return extract_json(resp.content)
                except ValueError as exc:
                    feedback = (
                        f"输出不是合法 JSON（{exc}）。"
                        "请重新只输出一个完整 JSON 对象，不要附加任何其他文字。"
                    )
            last_error = feedback
            convo.append({"role": "assistant", "content": resp.content or ""})
            convo.append({"role": "user", "content": feedback})
        raise LLMError(f"结构化输出在 {max_attempts} 次尝试后仍失败: {last_error}")

    # ---- 通道实现 ----
    def _chat_plain(self, kwargs: dict) -> LLMResponse:
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        message = choice.message
        response = LLMResponse(
            content=message.content or "",
            finish_reason=choice.finish_reason or "",
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )
        if message.tool_calls:
            response.tool_calls = [
                ToolCall(
                    name=tc.function.name,
                    arguments=self._parse_args(tc.function.arguments),
                    call_id=tc.id,
                )
                for tc in message.tool_calls
            ]
        return response

    def _chat_stream(self, kwargs: dict, on_content: Callable[[str], None] | None) -> LLMResponse:
        content_parts: list[str] = []
        calls: dict[int, dict] = {}
        finish = ""
        usage_prompt = usage_completion = 0

        def create(with_usage: bool):
            extra = {"stream_options": {"include_usage": True}} if with_usage else {}
            return self._client.chat.completions.create(**kwargs, stream=True, **extra)

        try:
            stream = create(True)
        except Exception:
            stream = create(False)  # 端点不支持 stream_options 时退回

        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage_prompt = chunk.usage.prompt_tokens or 0
                usage_completion = chunk.usage.completion_tokens or 0
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
                if on_content:
                    on_content(delta.content)
            for tc in delta.tool_calls or []:
                slot = calls.setdefault(tc.index or 0, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments

        response = LLMResponse(
            content="".join(content_parts),
            finish_reason=finish,
            prompt_tokens=usage_prompt,
            completion_tokens=usage_completion,
        )
        for index in sorted(calls):
            slot = calls[index]
            response.tool_calls.append(
                ToolCall(name=slot["name"], arguments=self._parse_args(slot["args"]),
                         call_id=slot["id"])
            )
        return response

    # ---- 内部 ----
    def _with_text_protocol(
        self, messages: list[dict], service: ToolService | None, whitelist: list[str] | None
    ) -> list[dict]:
        if service is None:
            return messages
        instructions = _TEXT_PROTOCOL_INSTRUCTIONS.format(
            tool_catalog=service.render_catalog(whitelist)
        )
        copied = [dict(m) for m in messages]
        if copied and copied[0].get("role") == "system":
            copied[0]["content"] = copied[0]["content"] + "\n\n" + instructions
        else:
            copied.insert(0, {"role": "system", "content": instructions})
        return copied

    @staticmethod
    def _parse_args(raw: str | None) -> dict:
        if not raw or not raw.strip():
            return {}
        try:
            args = json.loads(raw)
            return args if isinstance(args, dict) else {"_raw": args}
        except json.JSONDecodeError:
            return {"_raw": raw}

    @staticmethod
    def _parse_text_action(content: str) -> dict | None:
        """从文本回复中提取动作：原生 XML 工具标签 → 代码块 → 裸 JSON → 首尾大括号。"""
        if not content or not content.strip():
            return None
        # 部分模型在文本模式下仍用 XML 风格序列化工具调用（<tool_call>{...}</tool_call>）
        for tag in ("tool_call", "function_call"):
            for raw in re.findall(rf"<{tag}>\s*(\{{.*?\}})\s*</{tag}>", content, flags=re.S):
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("name"):
                    return {
                        "action": "tool",
                        "name": str(obj["name"]),
                        "arguments": obj.get("arguments") or {},
                    }
        candidates = [content.strip()]
        fences = re.findall(r"```(?:json)?\s*(.*?)```", content, flags=re.S)
        candidates = [f.strip() for f in fences] + candidates
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end > start:
            candidates.append(content[start : end + 1])
        for cand in candidates:
            try:
                obj = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("action") in ("tool", "final"):
                if obj["action"] == "tool" and not obj.get("name"):
                    continue
                return obj
        return None
