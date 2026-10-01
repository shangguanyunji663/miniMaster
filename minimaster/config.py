"""全局配置：凭据与参数只从环境变量读取（.env 经 dotenv 注入），源码零密钥。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"缺少环境变量 {name}。请参照 .env.example 配置 .env（真实值不入源码）。"
        )
    return value or ""


@dataclass(frozen=True)
class Settings:
    """字段全部带默认值；真实凭据经 Settings.load() 从环境变量强制注入，
    测试可直接构造 Settings(api_key="") 的替身配置。"""

    # LLM（模型调用层）
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 8192
    timeout: float = 180.0
    # 按角色模型路由（空字符串 = 回落主模型）
    planner_model: str = ""
    validator_model: str = ""
    planner_temperature: float = 0.2
    validator_temperature: float = 0.0
    # 流式输出（端点不支持时自动回落非流式）
    stream: bool = True
    # Harness 预算
    max_rounds: int = 3
    max_steps: int = 15
    max_retries: int = 2
    parallel: int = 1
    # 全局 token 预算熔断（prompt+completion 总量；0 = 不限制）
    max_total_tokens: int = 800000
    # MCP 外部工具服务（stdio 命令行，空 = 不启用）
    mcp_server_cmd: str = ""
    mcp_timeout: float = 30.0
    # Guard：连续重复 + 振荡（窗口 / 周期）检测
    guard_max_repeat: int = 2
    guard_window_size: int = 12
    guard_window_max: int = 4
    # 记忆裁剪阈值（字符数估算 + 真实 token 反馈双触发）
    result_max_chars: int = 6000
    view_compress_threshold: int = 24000
    memory_recent_steps: int = 6
    live_threshold_chars: int = 30000
    token_budget: int = 24000

    @classmethod
    def load(cls) -> Settings:
        return cls(
            base_url=_env("LLM_BASE_URL", required=True),
            api_key=_env("LLM_API_KEY", required=True),
            model=_env("LLM_MODEL", required=True),
            temperature=float(_env("LLM_TEMPERATURE", "0.3")),
            max_tokens=int(_env("LLM_MAX_TOKENS", "8192")),
            timeout=float(_env("LLM_TIMEOUT", "180")),
            planner_model=_env("LLM_PLANNER_MODEL", ""),
            validator_model=_env("LLM_VALIDATOR_MODEL", ""),
            planner_temperature=float(_env("LLM_PLANNER_TEMPERATURE", "0.2")),
            validator_temperature=float(_env("LLM_VALIDATOR_TEMPERATURE", "0.0")),
            stream=_env("LLM_STREAM", "1") not in ("0", "false", "no"),
            max_rounds=int(_env("AGENT_MAX_ROUNDS", "3")),
            max_steps=int(_env("AGENT_MAX_STEPS", "15")),
            max_retries=int(_env("AGENT_MAX_RETRIES", "2")),
            parallel=int(_env("AGENT_PARALLEL", "1")),
            max_total_tokens=int(_env("AGENT_MAX_TOTAL_TOKENS", "800000")),
            mcp_server_cmd=_env("MCP_SERVER_CMD", ""),
            mcp_timeout=float(_env("MCP_SERVER_TIMEOUT", "30")),
            guard_max_repeat=int(_env("GUARD_MAX_REPEAT", "2")),
            guard_window_size=int(_env("GUARD_WINDOW_SIZE", "12")),
            guard_window_max=int(_env("GUARD_WINDOW_MAX", "4")),
            result_max_chars=int(_env("MEMORY_RESULT_MAX_CHARS", "6000")),
            view_compress_threshold=int(_env("MEMORY_VIEW_THRESHOLD", "24000")),
            memory_recent_steps=int(_env("MEMORY_RECENT_STEPS", "6")),
            live_threshold_chars=int(_env("MEMORY_LIVE_THRESHOLD", "30000")),
            token_budget=int(_env("MEMORY_TOKEN_BUDGET", "24000")),
        )
