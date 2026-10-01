"""MCP stdio 客户端与代理工具测试（本地 fake server，真实子进程+管道）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from minimaster.tools.base import ToolContext
from minimaster.tools.mcp_client import MCPProxyTool, MCPStdioClient
from minimaster.tools.service import ToolService

SERVER = Path(__file__).parent / "mcp_fake_server.py"


@pytest.fixture()
def client():
    client = MCPStdioClient([sys.executable, str(SERVER)], request_timeout=15)
    yield client
    client.close()


def test_start_handshake_and_list_tools(client):
    tools = client.start()
    assert [t["name"] for t in tools] == ["add", "echo"]
    assert tools[0]["inputSchema"]["required"] == ["a", "b"]


def test_call_tool_roundtrip(client):
    client.start()
    ok, text = client.call_tool("add", {"a": 2, "b": 3})
    assert ok and text == "5"
    ok, text = client.call_tool("echo", {"text": "你好 mcp"})
    assert ok and text == "你好 mcp"


def test_call_unknown_tool_is_error(client):
    client.start()
    ok, _ = client.call_tool("nope", {})
    assert not ok


def test_proxy_tool_through_tool_service(client, tmp_path):
    client.start()
    descs = client.list_tools()
    svc = ToolService()
    for desc in descs:
        svc.register(MCPProxyTool(client, desc))

    # mcp_ 前缀避免与内置工具冲突；schema 直接来自远端 inputSchema
    assert set(svc.names()) >= {"mcp_add", "mcp_echo"}
    schema = svc.schemas(["mcp_add"])[0]
    assert schema["function"]["parameters"]["required"] == ["a", "b"]

    ctx = ToolContext(workdir=tmp_path)
    out = svc.dispatch("mcp_add", {"a": 20, "b": 22}, ctx)
    assert out.ok and out.output == "42"

    out = svc.dispatch("mcp_add", {"a": 1}, ctx)  # 缺参数 → 本地 schema 校验拦截
    assert not out.ok and "缺少必需参数" in (out.error or "")


def test_server_crash_raises_mcp_error():
    from minimaster.tools.mcp_client import MCPError

    client = MCPStdioClient([sys.executable, "-c", "import sys; sys.exit(1)"])
    with pytest.raises(MCPError):
        client.start()
