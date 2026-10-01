"""五个文件系统工具：Read / Write / Edit / Glob / Grep。

纯 Python 实现（pathlib / fnmatch / re），不依赖系统二进制；
所有路径经 ToolContext.resolve 锚定并限制在 workdir 沙箱内。
"""
from __future__ import annotations

import fnmatch
import re

from .base import BaseTool, ToolContext, ToolResult, ToolSpec

_READ_MAX_CHARS = 60000
_GREP_MAX_MATCHES = 200
_GLOB_MAX_ITEMS = 500


class ReadTool(BaseTool):
    spec = ToolSpec(
        name="read",
        description="读取文本文件内容，带行号输出。可用 offset/limit 分段读大文件。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对工作目录）"},
                "offset": {"type": "integer", "description": "起始行号（1-based），默认 1"},
                "limit": {"type": "integer", "description": "读取行数，默认 2000"},
            },
            "required": ["path"],
        },
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args["path"])
        if not path.is_file():
            return ToolResult(ok=False, output="", error=f"文件不存在: {args['path']}")
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        offset = max(int(args.get("offset", 1)), 1)
        limit = max(int(args.get("limit", 2000)), 1)
        window = lines[offset - 1 : offset - 1 + limit]
        if not window:
            return ToolResult(ok=False, output="", error=f"行区间越界（共 {len(lines)} 行）")
        body = "\n".join(f"{offset + i:>6}\t{line}" for i, line in enumerate(window))
        suffix = f"\n[共 {len(lines)} 行，显示 {offset}-{offset + len(window) - 1}]" if len(lines) > offset - 1 + len(window) else ""
        output = f"<file path={path.name}>\n{body}{suffix}\n</file>"
        if len(output) > _READ_MAX_CHARS:
            output = output[:_READ_MAX_CHARS] + "\n[... 内容过长截断，请用 offset/limit 分段读取 ...]"
        return ToolResult(ok=True, output=output)


class WriteTool(BaseTool):
    spec = ToolSpec(
        name="write",
        description="写入（或创建）文本文件，完整覆盖内容。父目录不存在会自动创建。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对工作目录）"},
                "content": {"type": "string", "description": "完整文件内容"},
            },
            "required": ["path", "content"],
        },
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args["path"])
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        action = "覆盖" if existed else "创建"
        return ToolResult(ok=True, output=f"已{action} {args['path']}（{len(args['content'])} 字符）")


class EditTool(BaseTool):
    spec = ToolSpec(
        name="edit",
        description="精确字符串替换编辑文件：old_string 必须与文件内容唯一匹配（除非 replace_all）。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对工作目录）"},
                "old_string": {"type": "string", "description": "要被替换的原文（需唯一）"},
                "new_string": {"type": "string", "description": "替换后的文本"},
                "replace_all": {"type": "boolean", "description": "替换全部匹配，默认 false"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args["path"])
        if not path.is_file():
            return ToolResult(ok=False, output="", error=f"文件不存在: {args['path']}")
        text = path.read_text(encoding="utf-8", errors="replace")
        old, new = args["old_string"], args["new_string"]
        count = text.count(old)
        if count == 0:
            return ToolResult(ok=False, output="", error="old_string 在文件中未找到，编辑未生效")
        if count > 1 and not args.get("replace_all"):
            return ToolResult(
                ok=False,
                output="",
                error=f"old_string 匹配 {count} 处（要求唯一），请扩大上下文或设 replace_all=true",
            )
        replaced = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        path.write_text(replaced, encoding="utf-8")
        return ToolResult(ok=True, output=f"已编辑 {args['path']}（替换 {count if args.get('replace_all') else 1} 处）")


class GlobTool(BaseTool):
    spec = ToolSpec(
        name="glob",
        description="按 glob 模式匹配文件（如 **/*.py），返回路径与大小，按修改时间排序。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 模式，如 src/**/*.py"},
                "path": {"type": "string", "description": "起始目录（相对工作目录），默认工作目录"},
            },
            "required": ["pattern"],
        },
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        base = ctx.resolve(args.get("path") or ".")
        if not base.is_dir():
            return ToolResult(ok=False, output="", error=f"目录不存在: {args.get('path', '.')}")
        matches = sorted(
            (p for p in base.glob(args["pattern"]) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not matches:
            return ToolResult(ok=True, output=f"无匹配: {args['pattern']}")
        items = [
            f"{p.relative_to(ctx.workdir.resolve())}  ({p.stat().st_size} B)"
            for p in matches[:_GLOB_MAX_ITEMS]
        ]
        if len(matches) > _GLOB_MAX_ITEMS:
            items.append(f"... 另有 {len(matches) - _GLOB_MAX_ITEMS} 个未显示")
        return ToolResult(ok=True, output="\n".join(items))


class GrepTool(BaseTool):
    spec = ToolSpec(
        name="grep",
        description="按正则表达式搜索文件内容，输出 路径:行号: 行文本。可用 include 过滤文件名。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "文件或目录（相对工作目录），默认工作目录"},
                "include": {"type": "string", "description": "文件名 glob 过滤，如 *.py"},
            },
            "required": ["pattern"],
        },
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        target = ctx.resolve(args.get("path") or ".")
        try:
            regex = re.compile(args["pattern"])
        except re.error as exc:
            return ToolResult(ok=False, output="", error=f"非法正则: {exc}")
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files = sorted(p for p in target.rglob("*") if p.is_file())
        else:
            return ToolResult(ok=False, output="", error=f"路径不存在: {args.get('path', '.')}")

        include = args.get("include")
        if include:
            files = [p for p in files if fnmatch.fnmatch(p.name, include)]

        hits: list[str] = []
        for p in files:
            try:
                for lineno, line in enumerate(
                    p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if regex.search(line):
                        rel = p.relative_to(ctx.workdir.resolve())
                        hits.append(f"{rel}:{lineno}: {line.strip()[:300]}")
                        if len(hits) >= _GREP_MAX_MATCHES:
                            hits.append(f"... 达到 {_GREP_MAX_MATCHES} 条上限，提前停止")
                            return ToolResult(ok=True, output="\n".join(hits))
            except OSError:
                continue
        return ToolResult(
            ok=True,
            output="\n".join(hits) if hits else f"无匹配: {args['pattern']}",
        )


def default_filesystem_tools() -> list[BaseTool]:
    return [ReadTool(), WriteTool(), EditTool(), GlobTool(), GrepTool()]
