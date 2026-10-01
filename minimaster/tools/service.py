"""ToolService：工具注册表 + 按角色白名单出表 + 统一分发。

出表直接给 OpenAI Function Call 协议的 tools 数组；分发时统一做参数校验、
异常兜底与结果格式化，模型侧永远拿到一致的字符串观察。
"""
from __future__ import annotations

from .base import BaseTool, ToolContext, ToolResult


class ToolService:
    def __init__(self, output_max_chars: int = 20000) -> None:
        self._tools: dict[str, BaseTool] = {}
        self.output_max_chars = output_max_chars

    def register(self, tool: BaseTool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, whitelist: list[str] | None = None) -> list[dict]:
        """产出 OpenAI Function Call schema；whitelist 为空则全量。"""
        names = whitelist if whitelist is not None else self.names()
        return [self._tools[n].spec.to_openai_schema() for n in names if n in self._tools]

    def render_catalog(self, whitelist: list[str] | None = None) -> str:
        """文本协议模式用的工具清单（JSON Schema 逐个列出）。"""
        import json

        return "\n".join(
            json.dumps(s["function"], ensure_ascii=False) for s in self.schemas(whitelist)
        )

    def dispatch(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                output="",
                error=f"未知工具 '{name}'，可用工具: {self.names()}",
            )
        try:
            err = tool.validate(args)
            if err:
                return ToolResult(ok=False, output="", error=err)
            if tool.spec.danger == "guarded":
                danger = tool.is_dangerous(args)
                if danger:
                    return ToolResult(ok=False, output="", error=f"[安全拦截] {danger}")
            result = tool.run(args, ctx)
        except Exception as exc:  # 工具内部异常统一兜底为观察文本
            return ToolResult(ok=False, output="", error=f"{type(exc).__name__}: {exc}")
        if len(result.output) > self.output_max_chars:
            cut = len(result.output) - self.output_max_chars
            head = result.output[: self.output_max_chars // 2]
            tail = result.output[-self.output_max_chars // 2 :]
            result = ToolResult(
                ok=result.ok,
                output=f"{head}\n[... 工具输出过长，截断 {cut} 字符 ...]\n{tail}",
                error=result.error,
            )
        return result
