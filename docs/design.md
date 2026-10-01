# 设计决策与权衡（Design Rationale）

面试深挖时的权衡依据，每条都是「为什么选 A 不选 B」而不是功能清单。

## 1. 手写编排 vs LangGraph/LangChain

**选择**：纯手写 Planner-Executor-Validator 编排，openai SDK 直连 Function Call 协议。

**理由**：这个项目的核心价值是 Harness Engineering 本身——状态机、白名单、Guard、
记忆裁剪都是需要**精确控制**的机制。框架的抽象（如图节点的隐式状态）会把这些
机制藏起来；出错时调试的是框架而不是自己的逻辑。代价是协议簿记（tool_calls 配对、
双模式消息序列）要自己写对——这部分用集成测试锁死。

## 2. 成功判据：Checklist 驱动，而非「全部任务 DONE」（真实事故驱动）

**事故**：真实验证中，任务 T1 因步数预算过小失败，重规划产出替代任务 T3/T4 并达成
目标，但最终判定「未完成」——因为旧判据要求所有任务 DONE，被替代的 FAILED 任务
永久阻塞验收。

**修正**：成功判据 = `verdict.completed and checklist.is_complete`，以验收清单为
唯一事实源；任务状态是手段不是目的。回归测试
`test_checklist_acceptance_overrides_superseded_failed_task` 锁死该行为。
（trace 证据见 `docs/evidence.md` §3）

## 3. 上下文裁剪：字符估算 + 真实 token 双触发，不引 tiktoken

**选择**：三级裁剪阈值用字符数估算，同时把上次响应的真实 `usage.prompt_tokens`
作为第二触发源（超 `MEMORY_TOKEN_BUDGET` 立即压缩）。

**理由**：tiktoken 对非 OpenAI 官方模型（本项目走中转站）词表不一定准；字符估算
+真实反馈的混合方案在工程上足够且零依赖。误差由「提前压缩」的保守方向吸收。

## 4. 并行：ThreadPoolExecutor vs asyncio

**选择**：线程池，`--parallel N` 控制独立任务并行度。

**理由**：工具层全部是阻塞式同步实现（subprocess、文件 IO）；openai SDK 也有同步
客户端。改 asyncio 意味着全部工具与客户端异步化，牵一发动全身；线程池在 GIL 下
对 IO 密集的 agent 负载已经够用。共享可变状态（WorkingMemory、Reporter、token
计数器）用锁保护——compress 的「锁外 LLM 调用 + 写回」曾被审计出 TOCTOU 竞态，
以「压缩全程串行锁 + 写回合并新增步骤」修复（`test_compress_writeback_preserves_concurrent_appends`）。

## 5. 状态机硬约束 vs 约定式状态管理

**选择**：`ALLOWED_TRANSITIONS` 迁移表，非法跳转抛 `IllegalTransitionError`。

**理由**：「PENDING → DONE」这类跳转在 prompt 层防不住（模型不会主动越权，但编排
代码改着改着就会）。把迁移路径变成数据结构后，非法路径在测试里一次性枚举覆盖。
特殊迁移的语义都有明确对应：`FAILED→PENDING`（有重试额度时的归档重注入）、
`PENDING→BLOCKED`（依赖未满足的启动期阻塞）。

## 6. 协议双模式（NATIVE → TEXT_JSON 自动降级）

**理由**：项目走 OpenAI 兼容中转站，不同端点对 `tools`/`response_format` 支持参差。
NATIVE 失败（400 或从不返回 tool_calls）时降级为「工具清单进 system + JSON 动作
文本协议」，消息簿记两套序列自适应。已知取舍：TEXT 协议是单动作假设（一次响应只
执行第一个动作），在 Executor 中显式截断并留痕（`protocol_note` 事件）。

## 7. Bash 工具的威胁模型（安全边界声明）

Bash 工具**按设计**执行 LLM 生成的 shell 命令——这是 agent 框架的功能本体
（管道/重定向/`&&` 需要 shell 解释，参数列表化会废掉工具）。纵深防御分层：

1. **工作目录沙箱**：所有文件工具路径经 `ToolContext.resolve` 锚定，越界抛异常；
   bash 的 `cd` 持久化同样校验沙箱；
2. **危险命令 denylist**：`rm -rf /`、`format C:`、关机、`curl | bash` 等 12 类
   系统破坏模式在执行前拦截（尽力而为的黑名单，不声称完备）；
3. **超时进程树清理**：Popen + `taskkill /T`（Windows）/ `killpg`（POSIX），
   防止孙进程残留；
4. **Guard**：重复/振荡动作拦截，间接限制资源滥用；
5. **全局 token 预算熔断**（`AGENT_MAX_TOTAL_TOKENS`）：超限后剩余任务置
   BLOCKED，Executor 不再发起任何模型调用。

信任模型：运行者是本机用户，模型是用户自己配置的端点——框架防的是「模型失控」
（死循环、误删工作区外文件、预算失控），不防「恶意用户」。

## 8. 重试与轮次解耦（轮内工作队列）

**理由**：最初实现里 FAILED→PENDING 的重试要等下一轮，`--max-rounds 1` 时
`--max-retries 2` 形同虚设。改为轮内工作队列后，round 只表示一个完整的
「执行→验收→重规划」周期，重试即时重入队，语义干净且有回归测试
（`test_fail_then_same_round_retry_with_archive`）。

## 9. eval 成败判定：客观检查器，不信 Agent 自述

**选择**：每个基准任务的成败由独立检查器判定（运行产物、断言输出、比对内容），
HarnessReport.success 只是参考之一。

**理由**：这直接对应该框架自己的设计哲学——Validator「只认证据，不做乐观推断」。
如果 eval 用 agent_success 当成败，就是在用被测系统的自我评估证明被测系统，与
「自我评估偏差是 agent 工程核心难题」的项目立论自相矛盾。

## 10. MCP 接入：stdio JSON-RPC 手写客户端 vs 官方 SDK

**选择**：手写约 150 行的 stdio 客户端（initialize 握手 / tools/list / tools/call），
零新依赖。

**理由**：只用到 MCP 三个方法，引入 SDK 依赖不划算；手写恰好证明工具层四层抽象的
适配能力——远程 `inputSchema`（JSON Schema）直接作为 `ToolSpec.parameters`，经
`to_openai_schema()` 产出 Function Call 协议，外部工具与内置六工具在 ToolService
里无差别分发。读侧用后台线程+队列实现带超时等待（Windows 管道不支持 select）。
