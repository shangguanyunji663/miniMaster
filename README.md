# miniMaster — 多智能体协作执行框架

面向长周期 Agent 任务的 **Planner-Executor-Validator 三层嵌套循环 Harness 执行框架**：
从任务拆分、工具调度、工作记忆管理到验证纠错的完整闭环。

针对工程问题逐一对症：上下文窗口膨胀、自我评估偏差、策略退化与死循环。

**真实基准（全量 8 任务套件，客观检查器判定）：成功率 8/8**（agnes-2.0-flash @ OpenAI 兼容中转，2026-09-28）

| 任务 | 难度 | 轮数 | 尝试 | Guard拦截 | 重试 | tokens | 耗时 |
|---|---|---|---|---|---|---|---|
| hello | easy | 1 | 2 | 0 | 0 | 11.7k | 199s |
| notes | easy | 1 | 2 | 0 | 0 | 11.7k | 82s |
| calc | medium | 1 | 2 | 0 | 0 | 13.1k | 138s |
| fizzbuzz | medium | 1 | 3 | 0 | 0 | 22.2k | 191s |
| parallel-files（并行=2） | medium | 1 | 5 | 0 | 0 | 35.3k | 591s |
| dep-chain（三步依赖链） | hard | 1 | 3 | 0 | 0 | 23.0k | 252s |
| bugfix（植入 bug 修复） | hard | 1 | 4 | 0 | 0 | — | 305s |
| codebase-grep（多文件检索汇总） | hard | 2 | 8 | 0 | 2 | — | 1478s |

> 首轮真实跑出 **5/8**，暴露 3 个真实缺陷（eval 环境植入时序、端点降级后模型输出
> XML 工具标签未解析、并行任务同文件写竞态），修复后重跑 **8/8**——eval 不仅测出
> 成功率，还真实驱动了框架加固（Guard 在首轮 codebase-grep 中实际拦截 3 次，
> 证据见 [docs/evidence.md](docs/evidence.md)）。原始轨迹：`eval/results/`。

## 架构总览

```
                          ┌─────────────────────────────────────────────┐
                          │                Harness 主循环                │
                          │  round = 执行(工作队列,可并行)→验收→重规划     │
                          │  全局 token 预算熔断（AGENT_MAX_TOTAL_TOKENS）│
                          └─────────────────────────────────────────────┘
                                             │
        ┌────────────────┐          ┌────────┴─────────┐          ┌────────────────┐
        │    PLANNER     │          │     EXECUTOR     │          │    VALIDATOR   │
        │  任务拆分/重规划 │ ───────▶ │ 工具调用循环      │ ───────▶ │ Checklist 验收 │
        │  依赖声明       │          │ live 上下文裁剪   │          │ 低温确定性判定  │
        │ 白名单:         │          │ 白名单:           │          │ 白名单:        │
        │ create_tasks   │          │ bash/read/write/ │          │ verify         │
        │ replan         │          │ edit/glob/grep + │          │                │
        │                │          │ mcp_* + submit   │          │                │
        └────────────────┘          └──────────────────┘          └────────────────┘
                 │                        │        ▲                      │
                 │                        ▼        │                      │
                 │             ┌──────────────────────────────┐          │
                 │             │ Action Policy 白名单拦截       │          │
                 │             │ Guard：连续重复/窗口/周期       │          │
                 │             │ 步数上限强制收口               │          │
                 │             │ bash 危险命令拦截（guarded）    │          │
                 │             └──────────────────────────────┘          │
                 │                                                      │
        ┌────────┴──────────────────────────────────────────────────────▼──────┐
        │                      工作记忆层（分角色裁剪视图）                        │
        │  ①单结果截断  ②超阈值 LLM 语义压缩  ③保留最近 N 步                      │
        │  live 消息流裁剪（字符阈值 + 真实 token usage 双触发）                   │
        │  Retry Archive：失败轨迹压缩归档 → 重试注入                            │
        └──────────────────────────────────────────────────────────────────────┘
                                      │
                       ┌──────────────┴──────────────┐
                       │  ToolService 四层抽象         │
                       │  内置: bash/read/write/edit/ │
                       │       glob/grep              │
                       │  外部: MCP server (stdio)    │
                       └─────────────────────────────┘
```

**任务状态机**（硬约束迁移路径，非法跳转直接抛异常）:

```
PENDING ──▶ RUNNING ──▶ DONE
   │           │  ▲
   │           ▼  │（重试：FAILED 压缩归档后回 PENDING，轮内工作队列即时重入队）
   │        FAILED
   ▼           ▲
BLOCKED ───────┘（依赖未满足/预算熔断/外部阻塞 → 重规划后恢复）
```

## 五个技术亮点 → 代码映射

| 简历亮点 | 实现位置 |
|---|---|
| 三层架构 + Action Policy 白名单 + 状态机硬约束 | `core/action_policy.py` · `core/state_machine.py` · `agents/*` |
| 分角色动态工作记忆：单结果裁剪 / 超阈值语义压缩 / 保留最近 N 步 + Retry Archive | `memory/working_memory.py` · `memory/retry_archive.py` · `memory/live_context.py` |
| 工具层四层抽象 BaseTool → ToolSpec → ToolContext → ToolService，内置六工具 + MCP 外部工具直连 OpenAI Function Call | `tools/base.py` · `tools/service.py` · `tools/mcp_client.py` · `tools/shell.py` · `tools/filesystem.py` |
| 四重纠错闭环：Checklist 驱动 / 重复动作 Guard / 步数上限收口 / missing_requirements 回流 | `core/checklist.py` · `core/guard.py` · `agents/executor.py` · `core/harness.py` |
| Prompt 四层解耦：角色策略 / 动态上下文 / 协议适配 / 模型调用，按阶段差异化注入 | `prompts/templates.py` · `prompts/builder.py` · `llm/client.py` |

深入的设计权衡（手写编排 vs 框架、成功判据事故复盘、并行模型选型、安全威胁模型）
见 [docs/design.md](docs/design.md)；各纠错机制的真实触发证据见
[docs/evidence.md](docs/evidence.md)。

## 各层设计要点

- **Action Policy 白名单**：每类角色的可用动作集合在构造时确定，任何动作执行前经 `enforce()` 校验，越权抛 `PolicyViolation` 并把拦截原因回灌给模型自纠。
- **状态机**：迁移表硬约束，非法跳转直接抛异常；`FAILED→PENDING`（重试重注入）、`PENDING→BLOCKED`（依赖未满足/预算熔断）语义明确。
- **三级记忆裁剪 + live 裁剪**：单结果截断（头 60% + 尾 40%）→ 视图超阈值 LLM 语义压缩 → 最近 N 步保留全文；`LiveContextTrimmer` 把裁剪落到 Executor 对话流本身，字符估算**或**真实 `usage.prompt_tokens` 超预算即触发，且不破坏 NATIVE 协议配对。视图按角色裁剪：Planner 只看状态与结论，Executor 看本任务轨迹，Validator 只看 Checklist 与结论。
- **Guard 三重检测**：连续重复（同指纹连续 N 次）、窗口计数（最近 12 步内同动作累计 4 次）、周期振荡（A→B→A→B），命中即拦截回灌。
- **Retry Archive + 轮内重试**：失败轨迹压缩为「根因+教训」注入重试上下文；重试在轮内工作队列即时重入队，与轮次解耦。
- **协议双模式**：NATIVE（原生 tool_calls）→ TEXT_JSON（文本协议）自动降级，消息簿记自适应；`finish_reason=length` 自动 2× max_tokens 重试；结构化输出带解析失败反馈重问环。
- **工具沙箱与安全**：路径锚定沙箱；bash 危险命令 denylist（12 类系统破坏模式）；超时进程树清理；外部 MCP 工具统一 `guarded` 标记。
- **MCP 接入**：stdio JSON-RPC 客户端（initialize / tools/list / tools/call），远程 `inputSchema` 直接映射 ToolSpec，`mcp_*` 前缀注册进 ToolService 与内置工具无差别分发；连接失败自动降级纯内置工具。
- **按角色模型路由 + 流式输出**：Planner/Validator 独立模型与温度（Validator 默认 0）；流式增量打印，故障自动回落。
- **断点续跑**：任务收口与轮末快照 `state.json`，`--resume` 恢复（goal/workdir 自动带出）。

## 快速开始

```bat
:: 1. 初始化项目内置环境（.conda 局部环境，不动本机 Anaconda）
setup_env.bat

:: 2. 配置密钥：复制 .env.example 为 .env 并填入 OpenAI 兼容端点信息
copy .env.example .env

:: 3. 跑测试（不需要 API Key）
.conda\python.exe -m pytest tests/ -q

:: 4. 运行（pip install -e 后可用 console_script）
.conda\Scripts\minimaster.exe "在 workspaces/demo 创建一个计算器并验证"
:: 等价：.conda\python.exe main.py "目标" --workdir workspaces/demo
```

**CLI 参数**:

| 参数 | 默认 | 说明 |
|---|---|---|
| `--workdir` | `workspaces/run-<时间戳>` | 工作目录沙箱 |
| `--max-rounds` | 3 | 外层 Planner-Executor-Validator 轮数上限 |
| `--max-steps` | 15 | Executor 单任务步数上限 |
| `--max-retries` | 2 | 单任务最大重试次数（轮内即时重试） |
| `--parallel` | 1 | 无依赖任务的并行度 |
| `--resume <state.json>` | - | 从快照断点续跑 |
| `--quiet` / `--no-trace` | - | 输出与轨迹开关 |

## eval 基准

```bash
python eval/run_eval.py --suite full      # 8 任务全套（需 API Key）
python eval/run_eval.py --suite quick     # 前 4 个任务
python eval/run_eval.py --ablate guard    # 机制消融对照（guard/retry/budget）
```

任务成败由**客观检查器**判定（运行产物、断言输出、比对内容），不采信 Agent 自述；
指标含成功率 / 轮数 / 重试 / Guard 拦截 / token 成本，结果存 `eval/results/<tag>.json`，
每次任务运行的完整轨迹存 `eval/results/<tag>/<task>/trace.jsonl`。

## MCP 外部工具

```bash
# .env 中配置任一 MCP stdio server（示例）
MCP_SERVER_CMD=uvx mcp-server-fetch
MCP_SERVER_TIMEOUT=30
```

启动时自动握手并拉取工具清单，以 `mcp_<工具名>` 注册；远程工具与内置六工具在
Executor 的白名单与 Function Call 出表中无差别出现。server 不可用时自动降级为
纯内置工具并留痕（`mcp_unavailable` 事件）。

## 配置

全部经环境变量注入（`.env`，已被 gitignore；源码零密钥），完整清单见
[.env.example](.env.example)。关键项：

| 变量 | 说明 |
|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | OpenAI 兼容端点（需支持 function calling） |
| `LLM_PLANNER_MODEL` / `LLM_VALIDATOR_MODEL` | 按角色模型路由（留空回落主模型） |
| `LLM_VALIDATOR_TEMPERATURE` | 验收温度（默认 0，保证判定确定性） |
| `LLM_STREAM` | 流式输出开关（默认 1，故障自动回落） |
| `AGENT_MAX_ROUNDS` / `AGENT_MAX_STEPS` / `AGENT_MAX_RETRIES` | Harness 预算 |
| `AGENT_PARALLEL` | 独立任务并行度 |
| `AGENT_MAX_TOTAL_TOKENS` | 全局 token 预算熔断（默认 800000，0 = 不限） |
| `GUARD_MAX_REPEAT` / `GUARD_WINDOW_SIZE` / `GUARD_WINDOW_MAX` | Guard 三重检测阈值 |
| `MEMORY_LIVE_THRESHOLD` / `MEMORY_TOKEN_BUDGET` | live 裁剪（字符估算 / 真实 token 反馈） |
| `MCP_SERVER_CMD` / `MCP_SERVER_TIMEOUT` | MCP stdio server 命令与超时 |

## 项目结构

```
minimaster/
├── cli.py               # CLI（console_script 入口）
├── config.py            # 环境变量 → Settings（零密钥入源码）
├── llm/client.py        # 模型调用层：Function Call + 协议双模式 + 截断自愈 + 流式
├── prompts/             # Prompt 四层解耦（角色策略/动态上下文/协议适配）
├── tools/               # 工具层四层抽象 + 六内置工具 + MCP 客户端 + 安全拦截
├── memory/              # 三级裁剪工作记忆 + live 裁剪 + Retry Archive
├── core/                # 状态机/Action Policy/Guard/Checklist/快照/Harness 主循环
└── agents/              # Planner / Executor / Validator
eval/run_eval.py         # 基准套件：8 任务 + 客观检查器 + 消融开关
tests/                   # 85 个测试（FakeLLM 全闭环 + MCP 协议往返，无需 API Key）
docs/design.md           # 设计权衡与决策依据
docs/evidence.md         # 纠错机制真实触发证据（trace 摘录）
main.py                  # 兼容入口（薄壳 → minimaster.cli）
pyproject.toml           # 打包（pip install -e . → minimaster 命令）
```

## 依赖与质量

Python 3.12 · `openai` · `python-dotenv` · `rich` · `pytest` · `pytest-cov` · `ruff`（版本 pin 死）
测试 84 个全绿（CI 矩阵：ubuntu + windows），核心模块行覆盖率约 77%
（未覆盖部分为真实网络调用与终端渲染路径）。
