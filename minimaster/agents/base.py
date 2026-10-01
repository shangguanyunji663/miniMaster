"""Agent 基类：持有角色白名单、PromptBuilder 与 LLM 客户端。

所有角色动作（控制动作与工具调用）在执行前一律经 ActionPolicy.enforce
白名单校验，越权即抛 PolicyViolation，由调用方拦截回灌。
支持按角色路由模型与温度（None = 用主模型/主温度）。
"""
from __future__ import annotations

from ..core.action_policy import ActionPolicy, Role
from ..llm.client import LLMClient
from ..prompts.builder import PromptBuilder
from ..utils.reporter import Reporter


class BaseAgent:
    role: Role = Role.PLANNER

    def __init__(
        self,
        llm: LLMClient,
        builder: PromptBuilder,
        policy: ActionPolicy,
        reporter: Reporter,
        model: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self.llm = llm
        self.builder = builder
        self.policy = policy
        self.reporter = reporter
        self.model = model
        self.temperature = temperature

    def enforce(self, action: str) -> None:
        """Action Policy 白名单硬校验：越权动作在进入执行前被拦截。"""
        self.policy.enforce(action)

    def _chat_structured(self, messages: list[dict]) -> dict:
        """按角色路由的结构化输出（含 JSON 解析失败的反馈重试环）。"""
        return self.llm.chat_structured(
            messages, model=self.model, temperature=self.temperature
        )
