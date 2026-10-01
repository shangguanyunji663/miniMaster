import pytest

from minimaster.core.action_policy import (
    Action,
    ActionPolicy,
    PolicyViolation,
    Role,
)


def test_base_actions_always_included():
    # 基础控制动作自动并入白名单，防止配置出"瘸腿"角色
    planner = ActionPolicy(Role.PLANNER)
    planner.enforce(Action.CREATE_TASKS.value)
    planner.enforce(Action.REPLAN.value)
    with pytest.raises(PolicyViolation):
        planner.enforce("bash")  # Planner 不允许任何工具调用
    with pytest.raises(PolicyViolation):
        planner.enforce(Action.SUBMIT_RESULT.value)  # 他角色的控制动作也不行


def test_executor_whitelist_with_tools():
    policy = ActionPolicy.for_executor_with_tools(["bash", "read", "write"])
    for name in ("bash", "read", "write", Action.SUBMIT_RESULT.value, Action.REQUEST_BLOCK.value):
        policy.enforce(name)
    with pytest.raises(PolicyViolation):
        policy.enforce(Action.CREATE_TASKS.value)  # Executor 不能规划
    with pytest.raises(PolicyViolation):
        policy.enforce("rm_rf_everything")  # 未注册工具


def test_validator_only_verifies():
    policy = ActionPolicy(Role.VALIDATOR)
    policy.enforce(Action.VERIFY.value)
    with pytest.raises(PolicyViolation):
        policy.enforce("write")


def test_violation_message_contains_whitelist():
    policy = ActionPolicy(Role.VALIDATOR)
    with pytest.raises(PolicyViolation) as exc_info:
        policy.enforce("bash")
    assert "VALIDATOR" in str(exc_info.value)
    assert "白名单" in str(exc_info.value)
