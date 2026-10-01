"""工具层四层抽象的基座。

- ToolSpec：工具的静态规格（名称/描述/JSON Schema/危险级），直接产出
  OpenAI Function Call 协议 schema，消除手写格式漂移。
- ToolContext：一次 Harness 运行的执行环境（工作目录沙箱、事件回调、会话态），
  工具实现不感知上层编排。
- BaseTool：工具实现基类，run(args, ctx) -> ToolResult。
"""
from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema（OpenAI Function Call 的 parameters 字段）
    danger: str = "safe"  # safe | guarded

    def to_openai_schema(self) -> dict:
        """直接对接 OpenAI Function Call 协议，不经过任何中间格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolContext:
    workdir: Path
    session: dict = field(default_factory=dict)  # 跨步会话态（如 shell cwd）
    emit: Callable[[str, dict], None] = staticmethod(lambda event, payload: None)

    def resolve(self, path_str: str) -> Path:
        """把相对路径锚定到 workdir，并拒绝逃逸沙箱的路径。"""
        p = Path(path_str)
        if not p.is_absolute():
            p = self.workdir / p
        p = Path(p).resolve()
        workdir = self.workdir.resolve()
        if workdir != p and workdir not in p.parents:
            raise PermissionError(
                f"路径越界: {path_str} 解析为 {p}，超出工作目录沙箱 {workdir}"
            )
        return p


@dataclass
class ToolResult:
    ok: bool
    output: str
    error: str | None = None

    def render(self) -> str:
        if self.ok:
            return self.output
        return f"[ERROR] {self.error or 'unknown error'}"


class BaseTool(abc.ABC):
    """所有工具的基类：声明 ToolSpec，执行 run()。"""

    spec: ToolSpec

    def validate(self, args: dict) -> str | None:
        """按 spec.parameters 做轻量校验，返回错误信息或 None。"""
        props = self.spec.parameters.get("properties", {})
        required = self.spec.parameters.get("required", [])
        for key in required:
            if key not in args:
                return f"缺少必需参数 '{key}'，期望参数: {sorted(props)}"
        for key in args:
            if key not in props:
                return f"未知参数 '{key}'，合法参数: {sorted(props)}"
        return None

    def is_dangerous(self, args: dict) -> str | None:
        """guarded 工具的安全检查钩子：返回拦截原因，安全返回 None。"""
        return None

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:  # pragma: no cover
        raise NotImplementedError
