"""文本协议解析的 XML 工具调用格式测试（端点降级后的真实行为回归）。"""
from minimaster.llm.client import LLMClient


def test_parse_xml_tool_call():
    content = '好的\n<tool_call>\n{"name": "bash", "arguments": {"command": "ls"}}\n</tool_call>'
    parsed = LLMClient._parse_text_action(content)
    assert parsed == {"action": "tool", "name": "bash", "arguments": {"command": "ls"}}


def test_parse_xml_function_call_variant():
    content = '<function_call>{"name": "read", "arguments": {"path": "a.txt"}}</function_call>'
    parsed = LLMClient._parse_text_action(content)
    assert parsed["name"] == "read" and parsed["arguments"] == {"path": "a.txt"}


def test_plain_json_still_works():
    parsed = LLMClient._parse_text_action('{"action": "final", "content": "done"}')
    assert parsed["action"] == "final"


def test_unparseable_returns_none():
    assert LLMClient._parse_text_action("随便聊聊天") is None
