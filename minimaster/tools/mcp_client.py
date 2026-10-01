"""MCP（Model Context Protocol）stdio 客户端与工具代理。

通过 JSON-RPC 2.0 over stdio 连接外部 MCP server：
initialize 握手 → tools/list 拉取工具清单 → 每个远程工具包装为 MCPProxyTool
（remote inputSchema 直接作为 ToolSpec.parameters，产出 OpenAI Function Call
schema）注册进 ToolService——工具层四层抽象对外部协议工具同样适用。

读侧用后台线程 + 队列实现带超时的请求等待（Windows 管道不支持 select）。
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Any

from .base import BaseTool, ToolContext, ToolResult, ToolSpec

MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPError(Exception):
    """MCP 通信或协议错误。"""


class MCPStdioClient:
    """一个 MCP server 进程的 stdio 连接（一个 client 对应一个 server）。"""

    def __init__(self, command: list[str], request_timeout: float = 30.0) -> None:
        self.command = command
        self.request_timeout = request_timeout
        self._proc: subprocess.Popen | None = None
        self._next_id = 1
        self._responses: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._send_lock = threading.Lock()

    # ---- 生命周期 ----
    def start(self) -> list[dict[str, Any]]:
        """启动 server 并完成握手，返回远程工具描述列表。"""
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # server 日志走 stderr，避免撑爆管道
                text=True,
                encoding="utf-8",
            )
        except OSError as exc:
            raise MCPError(f"MCP server 启动失败 ({self.command}): {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "miniMaster", "version": "0.2.0"},
        })
        self._notify("notifications/initialized", {})
        result = self._request("tools/list", {})
        tools = result.get("tools") or []
        if not isinstance(tools, list):
            raise MCPError("tools/list 返回格式异常")
        return tools

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    # ---- 请求 ----
    def list_tools(self) -> list[dict[str, Any]]:
        return self._request("tools/list", {}).get("tools") or []

    def call_tool(self, name: str, arguments: dict) -> tuple[bool, str]:
        """调用远程工具，返回 (是否成功, 文本结果)。"""
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        ok = not result.get("isError")
        return ok, self._content_text(result)

    # ---- 内部 ----
    @staticmethod
    def _content_text(result: dict) -> str:
        parts = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts) or json.dumps(result, ensure_ascii=False)

    def _request(self, method: str, params: dict) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            raise MCPError("MCP server 未运行")
        with self._send_lock:
            req_id = self._next_id
            self._next_id += 1
            message = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            assert self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        try:
            response = self._responses.get(timeout=self.request_timeout)
        except queue.Empty as exc:
            raise MCPError(f"MCP 请求超时（>{self.request_timeout}s）: {method}") from exc
        if isinstance(response, MCPError):
            raise response
        if response.get("id") != req_id:
            raise MCPError(f"MCP 响应 id 不匹配: 期望 {req_id}")
        if "error" in response:
            raise MCPError(f"MCP 错误[{method}]: {response['error']}")
        return response.get("result") or {}

    def _notify(self, method: str, params: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._responses.put(message)
        self._responses.put(MCPError("MCP server 输出流已关闭"))


class MCPProxyTool(BaseTool):
    """把一个远程 MCP 工具包装为本地 ToolService 工具。

    远程 inputSchema 即 ToolSpec.parameters（同是 JSON Schema，零转换），
    经 ToolSpec.to_openai_schema() 直接产出 Function Call 协议。
    名称加 mcp_ 前缀避免与内置工具冲突；远程语义由 server 自治，
    本地按 guarded 标记便于审计。
    """

    def __init__(self, client: MCPStdioClient, tool_desc: dict[str, Any]) -> None:
        self._client = client
        self._remote_name = str(tool_desc.get("name", ""))
        parameters = tool_desc.get("inputSchema") or {"type": "object", "properties": {}}
        self.spec = ToolSpec(
            name=f"mcp_{self._remote_name}",
            description=str(tool_desc.get("description") or f"MCP 远程工具 {self._remote_name}"),
            parameters=parameters,
            danger="guarded",
        )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        del ctx  # 远程工具由 server 治理，本地沙箱不适用
        ok, text = self._client.call_tool(self._remote_name, args)
        return ToolResult(ok=ok, output=text if ok else "",
                          error=None if ok else text)
