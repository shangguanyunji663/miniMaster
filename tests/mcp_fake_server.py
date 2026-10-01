"""测试用 MCP fake server：以子进程运行，按 MCP stdio 协议（换行分隔 JSON-RPC）应答。

提供两个工具：
- add(a, b) → 文本结果 a+b
- echo(text) → 原样回显
"""
import json
import sys

TOOLS = [
    {
        "name": "add",
        "description": "两数相加",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "第一个数"},
                "b": {"type": "number", "description": "第二个数"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "echo",
        "description": "原样回显文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]


def respond(req_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in req:  # 通知（如 notifications/initialized）不回包
            continue
        method = req.get("method")
        if method == "initialize":
            respond(req["id"], {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1.0"},
            })
        elif method == "tools/list":
            respond(req["id"], {"tools": TOOLS})
        elif method == "tools/call":
            name = req["params"]["name"]
            args = req["params"].get("arguments") or {}
            if name == "add":
                text = str(args.get("a", 0) + args.get("b", 0))
            elif name == "echo":
                text = str(args.get("text", ""))
            else:
                respond(req["id"], {"content": [{"type": "text", "text": "unknown"}],
                                    "isError": True})
                continue
            respond(req["id"], {"content": [{"type": "text", "text": text}]})
        else:
            respond(req["id"], {"error": {"code": -32601, "message": f"未知方法 {method}"}})


if __name__ == "__main__":
    main()
