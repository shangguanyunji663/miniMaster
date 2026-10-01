"""PromptBuilder：四层解耦的提示构造器，替代静态 Prompt 加载。

- 第一层 角色策略（templates.*_IDENTITY）：这个角色是谁、行为边界在哪；
- 第二层 动态上下文（本类 _context_sections）：按阶段注入工作目录、操作系统
  环境、Completion Checklist、Retry Archive 教训、步数预算、上轮缺失反馈等；
- 第三层 协议适配（templates.*_PROTOCOL）：该角色这一阶段必须遵守的输出协议；
- 第四层 模型调用在 llm.client.LLMClient（本类只产出 messages，不碰网络）。

每一层独立演化：换模型只动第四层，换输出协议只动第三层，互不牵连。
"""
from __future__ import annotations

import platform

from ..core.action_policy import Role
from . import templates


class PromptBuilder:
    def __init__(self, workdir, max_steps: int) -> None:
        self.workdir = workdir
        self.max_steps = max_steps

    # ---- 对外：组装某角色的 system 消息 ----
    def system_message(self, role: Role, context: dict | None = None) -> str:
        context = context or {}
        sections: list[str] = [self._identity(role, context)]
        sections += self._context_sections(role, context)
        sections.append(self._protocol(role, context))
        return "\n\n".join(s for s in sections if s)

    # ---- 第一层：角色策略 ----
    @staticmethod
    def _identity(role: Role, context: dict) -> str:
        if role is Role.PLANNER:
            return templates.PLANNER_IDENTITY
        if role is Role.VALIDATOR:
            return templates.VALIDATOR_IDENTITY
        return templates.EXECUTOR_IDENTITY.format(
            task_id=context.get("task_id", "?"),
            max_steps=context.get("max_steps", "?"),
        )

    # ---- 第二层：动态上下文（按阶段差异化注入）----
    def _context_sections(self, role: Role, context: dict) -> list[str]:
        sections = [self._env_section()]
        if role is Role.EXECUTOR:
            if context.get("task"):
                sections.append(f"## 当前子任务\n{context['task'].render()}")
            if context.get("retry_lessons"):
                sections.append(f"## 重试历史注入\n{context['retry_lessons']}")
            if context.get("memory_view"):
                sections.append(
                    "## 本任务已有工作记忆\n" + "\n".join(context["memory_view"])
                )
        elif role is Role.VALIDATOR:
            if context.get("goal"):
                sections.append(f"## 用户目标\n{context['goal']}")
            if context.get("validator_view"):
                sections.append(f"## 验收输入\n{context['validator_view']}")
        elif role is Role.PLANNER:
            if context.get("goal"):
                sections.append(f"## 用户目标\n{context['goal']}")
            if context.get("planner_view"):
                sections.append(f"## 当前任务状态\n{context['planner_view']}")
            if context.get("missing_feedback"):
                feedback = "\n".join(f"- {m}" for m in context["missing_feedback"])
                sections.append(f"## 上轮验收缺失项（精确回流，本轮必须针对性解决）\n{feedback}")
        return sections

    def _env_section(self) -> str:
        return (
            "## 运行环境\n"
            f"- 工作目录（沙箱根）: {self.workdir}\n"
            f"- 操作系统: {platform.system()} {platform.release()} / "
            f"Python {platform.python_version()}\n"
            "- 文件工具路径一律使用相对工作目录的相对路径"
        )

    # ---- 第三层：协议适配 ----
    @staticmethod
    def _protocol(role: Role, context: dict) -> str:
        if role is Role.PLANNER:
            return templates.REPLAN_PROTOCOL if context.get("replan") else templates.PLANNER_PROTOCOL
        if role is Role.VALIDATOR:
            return templates.VALIDATOR_PROTOCOL
        return templates.EXECUTOR_PROTOCOL
