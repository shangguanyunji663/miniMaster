# 机制触发证据（Evidence）

纠错闭环的每个机制不是纸面设计——以下摘录自真实 API 运行的 `trace.jsonl`
（原始文件在 `runs/<时间戳>/trace.jsonl` 与 `eval/results/<tag>/<task>/trace.jsonl`，
此处摘录关键事件）。Guard 拦截在 eval 基准中有真实触发记录（§5）。

## 1. Retry Archive：失败压缩归档 → 教训注入重试

场景：`--max-steps 1` 故意制造步数不足。T1 失败后轨迹被 LLM 压缩为「根因+教训」，
重试时注入 Executor 上下文：

```json
{"event": "task_retry_scheduled", "role": "MEMORY", "task": "T1", "attempt": 1,
 "archived": true,
 "lessons": "**失败根因**：步数预算仅1步，不足以完成\"创建文件+实现函数\"的完整流程，
  被强制收口导致任务……"}
```

（run `20260928-025136`，共归档 2 次，第 3 次尝试因重试额度耗尽转 FAILED 终态）

## 2. 依赖阻塞 → 精确回流 → 增量重规划

T1 失败后，依赖它的 T2 被状态机置 BLOCKED（`PENDING→BLOCKED`），缺失项精确回流给
Planner，产出替代路径：

```json
{"event": "task_blocked", "role": "HARNESS", "task": "T2",
 "summary": "依赖未完成或不存在，任务无法启动: ['T1']"}

{"event": "replan", "role": "PLANNER",
 "analysis": "T1 失败原因是步数上限 1 步不足以完成文件创建和验证。T2 被 T1 阻塞。
  需重新规划 T1 增加步骤……（产出替代任务 T3/T4）"}
```

（run `20260928-025246`）

## 3. Checklist 驱动验收：被替代的失败任务不阻塞整体收口

T1 保持 FAILED 终态，Validator 依据证据判定目标已由替代任务达成：

```json
{"event": "validated", "role": "VALIDATOR", "completed": true, "unmet_items": 0,
 "missing": [],
 "summary": "T1 虽因步数上限失败，但 T3 和 T4 已成功完成：calc.py 已创建并实现
  add(a,b) 函数，add(2,3)=5 验证通过。所有验收项均满足。"}
```

（run `20260928-025506`；该事故推动成功判据从「全任务 DONE」改为 Checklist 驱动，
见 `docs/design.md` §2）

## 4. 并行执行：独立任务时间重叠

同一轮内 T1 与 T2 交错执行（T2 start 先于 T1 done），产物正确
（run `20260928-024741`）：

```
task_start T1 → task_start T2 → task_done T1 → task_start T3 → task_done T2 …
```

## 5. Guard 拦截（真实 API 运行触发）

eval 基准的 codebase-grep 任务中，Executor 连续重复相同的 bash 调用被 Guard
连续拦截并回灌（真实 trace，run `eval/results/full-0728/codebase-grep-192601`）：

```json
{"event": "guard_block", "task": "T3", "action": "bash", "rule": "consecutive",
 "message": "动作 'bash' 与上一次完全相同，已连续重复 3 次，超过上限 2，
  本次调用被 Guard 拦截。请更换策略：调整参数、换用其他工具，或用 submit_result 汇报结论。"}

{"event": "guard_block", "task": "T3", "action": "bash", "rule": "consecutive",
 "message": "动作 'bash' 与上一次完全相同，已连续重复 4 次，超过上限 2，……"}
```

振荡检测（A→B→A→B 周期）与窗口高频检测的触发逻辑见单元测试
`tests/test_guard.py::test_alternating_cycle_blocked`、
`tests/test_guard.py::test_high_frequency_in_window_blocked`。

## 6. 回归测试作为机制契约

每个机制都有不依赖 API 的测试锁死行为，改动即红：

| 机制 | 测试 |
|---|---|
| 状态机非法迁移 | `tests/test_state_machine.py` |
| 白名单越权拦截 | `tests/test_action_policy.py` |
| Guard 三重检测 | `tests/test_guard.py` |
| 三级记忆裁剪 + live 裁剪 | `tests/test_memory.py` · `tests/test_live_context.py` |
| 全闭环（规划→执行→验收→重规划→重试→续跑） | `tests/test_harness_loop.py` |
| MCP 协议往返 | `tests/test_mcp.py` |
| 审计修复回归（竞态/空清单/幽灵依赖/预算熔断） | `tests/test_audit_fixes.py` |
