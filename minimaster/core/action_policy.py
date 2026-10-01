"""Action Policy 白名单：硬约束每类角色可用动作集合，越权即拦截。

角色动作分两类：
- 显式控制动作（create_tasks / submit_result / verify ...），所有角色默认持有；
- 工具动作（Executor 的可用工具名集合，由 ToolService 注册表动态给出）。

白名单之外的任何动作在执行前被 enforce() 拒绝并回灌提示。
"""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    PLANNER = "PLANNER"
    EXECUTOR = "EXECUTOR"
    VALIDATOR = "VALIDATOR"


class Action(str, Enum):
    # Planner
    CREATE_TASKS = "create_tasks"
    REPLAN = "replan"
    # Executor 控制动作（非工具，由 Harness/Executor 拦截处理）
    SUBMIT_RESULT = "submit_result"
    REQUEST_BLOCK = "request_block"
    # Validator
    VERIFY = "verify"


# 角色基础白名单（Executor 的工具白名单在构造时并入）
BASE_WHITELIST: dict[Role, set[str]] = {
    Role.PLANNER: {Action.CREATE_TASKS.value, Action.REPLAN.value},
    Role.EXECUTOR: {Action.SUBMIT_RESULT.value, Action.REQUEST_BLOCK.value},
    Role.VALIDATOR: {Action.VERIFY.value},
}


class PolicyViolation(Exception):
    """越权动作：不在该角色白名单内。"""


class ActionPolicy:
    """某个角色在当前上下文下的动作白名单 = 基础控制动作 + 附加动作。"""

    def __init__(self, role: Role, extra_actions: set[str] | None = None) -> None:
        self.role = role
        self.allowed = frozenset(BASE_WHITELIST[role] | set(extra_actions or set()))

    @classmethod
    def for_executor_with_tools(cls, tool_names: list[str]) -> ActionPolicy:
        return cls(Role.EXECUTOR, set(tool_names))

    def allows(self, action: str) -> bool:
        return action in self.allowed

    def enforce(self, action: str) -> None:
        if not self.allows(action):
            raise PolicyViolation(
                f"角色 {self.role.value} 越权: 动作 '{action}' 不在白名单 "
                f"{sorted(self.allowed)} 内，操作已被拦截"
            )
