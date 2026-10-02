# 当前代码调整计划

更新时间：2026-10-02

## 当前节点

实施 roadmap 第 2 阶段：把一次 review 的准备、计划、reviewer/Judge 执行、报告生成、资源清理和终态写入收敛到 `ReviewLoop`。只有结果可读取且资源清理完成，才能在同一 session 开放对话。本文件记录待实施方案，不代表代码已落地；现状以工作区代码为准，旧 handoff 仅作参考。

## 已确认边界

- 本轮只迁移 review 生命周期。保留 `AgentLoop` 的 MessageBus 入站、session 串行、控制命令、取消与 outbound 交付；不移动 `SessionCoordinator`，不实现 `ConversationLoop` 的执行能力，不修改 `review-webui/`。
- `ReviewLoop` 是 `ReviewRunState`、report artifact 和 review 终态 metadata 的唯一写入者。`SessionCoordinator` 负责准入、路由、门禁和 handoff 索引；`AgentRunner` 只执行单个 agent run。
- 将 `ReviewOrchestrator` 的流程控制并入 `ReviewLoop`，复用 planning、evidence、validator、Judge、finalizer 等领域组件。核对生产与测试调用方后移除旧控制器，避免两个 supervisor 同时写 run 状态。
- ReviewLoop 使用阶段方法顺序执行并更新 `ReviewRunState.phase`，不引入另一张状态转移表或通用 `BaseLoop`。

## 阶段与完成门禁

```text
PREPARE -> PLAN -> REVIEW -> FINALIZE -> CLEANUP -> DONE
```

- `PREPARE`：从已准入的 run、target、snapshot 和 session metadata 准备 evidence、policy、dimensions、校验上下文及 review prompt。保留准入边界，不凭持久化 target 重入 review。
- `PLAN`：运行 coordinator，验证提交的 assignments，记录 plan、input fingerprint 和 usage。
- `REVIEW`：按 assignments 并发调度、收集 reviewer；验证 finding，执行 Judge batching；在业务边界记录 reviewer/Judge 状态、usage、错误及覆盖缺口。
- `FINALIZE`：汇总 findings、coverage、gaps、warnings，生成 report，并写入独立 artifact。artifact 写入失败不得宣称完整成功。
- `CLEANUP`：等待或取消属于该 run 的剩余子任务，释放运行资源。正常、错误和停止出口都要经过此阶段。
- `DONE`：清理完成后才持久化 `completed`、`error` 或 `stopped` 终态、最终 phase 和 session metadata，返回结构化 `ReviewResult`，随后写 handoff 索引并解除 conversation 门禁。终态字段本身不能单独证明交接已就绪。

`RESPOND` 不再是 ReviewLoop phase；review report stream、turn_end 和其他 transport 事件由上层交付。`FINALIZE` 合并旧方案的报告生成与持久化；`DONE` 只在 cleanup 完成后写终态。

## 实施顺序

1. 在 `ReviewLoop` 接管 `AgentLoop._run_agent_loop()` 中 review 专属的 preparation、metadata 归一化、execution context、异常处理和报告结果；`AgentLoop` 只识别已准入 turn、委托执行和交付，不修改 review 状态。
2. 迁移 `ReviewOrchestrator.execute_run()` 的 plan、review、Judge、finalizer 顺序及其必要的私有辅助方法。保留已有执行限制、并发、超时、usage 和失败映射行为；核对 `tests/review/test_orchestration.py` 等调用方后删除或收敛旧入口。
3. 统一正常、异常和取消出口。reviewer/Judge 失败不能转成干净报告；可用部分结果和有界错误需进入 result/handoff。停止不恢复、不重跑，保留已完成结果与已观察 usage。
4. 让 `SessionCoordinator` 只在 ReviewLoop 完成清理、终态与结果持久化后写一次 `review_context` 索引并切换路由。无 live executor 的历史 `running` 仍规范化为 `error`，不恢复执行。
5. 核对后端 API、CLI、WebSocket 生产者和 WebUI 消费者的 phase/status、报告流、终态和 handoff 契约；前端交互适配另行安排。

## 文件级实施细节

### `nanoreview/agent/review_state.py`

- 将 `ReviewPhase.RESPOND` 替换为 `ReviewPhase.CLEANUP`，保留 `PREPARE/PLAN/REVIEW/FINALIZE/DONE` 的 wire value 不变。
- `ReviewRunState.metadata_payload()` 在 `DONE` 前仍可写 `review_status=running` 和当前 phase；只有 `_complete_run()` 在 cleanup 成功后写终态和 `phase=done`。
- 不新增恢复字段，不保存 task、future、lock、callback、provider state 或中间压缩状态。

### `nanoreview/agent/review_loop.py`

将现有 `execute()` 改成一次 review 的唯一 supervisor 入口，内部按以下私有方法顺序执行：

```text
_prepare_review()
_run_plan()
_run_review()
_finalize_and_persist()
_cleanup_run()
_complete_run()
```

- `_prepare_review()` 接收 admitted turn 的 `session`、`coordinator_messages` 和 review metadata，调用现有 `prepare_code_review_context()`，返回 `ReviewPreparation`、validation workspace、changed files、local target、remote diff 和 `ReviewExecutionContext`。进入方法时将 phase 设为 `PREPARE`；准备失败直接进入失败收尾。
- `_run_plan()` 从 `ReviewOrchestrator._collect_plan()` 迁移 coordinator runner、`submit_review_plan` 校验和 plan usage，设置 `PLAN`，写入 `run_state.plan` 与 input fingerprint，返回 assignments。
- `_run_review()` 从 `ReviewOrchestrator.execute_run()` 迁移 execution limits、`ReviewFinalizer` 创建、reviewer dispatch/collect、Judge batch 和 finalizer 输入收集，设置 `REVIEW`；Judge 完成后进入 `FINALIZE`，返回 `ReviewFinalizerResult`。
- `_finalize_and_persist()` 序列化 findings/verdicts，生成 report markdown，写 `ReviewArtifactStore`，填充 `run_state.findings/report_ref/summary/warnings`。该方法不把 run 标成 terminal；artifact 写入失败将待完成状态记为 fatal error。
- `_cleanup_run()` 设置 `CLEANUP`，调用 `SubagentManager.cancel_by_session()` 并等待返回；清理失败记录有界 warning，不能提前开放 conversation。
- `_complete_run()` 只在 cleanup 返回后执行，设置 `DONE`、最终 `ReviewRunStatus`，写 session metadata 并构造 `ReviewResult`。正常 artifact 可读时为 `COMPLETED`；取消为 `STOPPED`；准备、计划、finalizer 或 artifact 的致命失败为 `ERROR`。
- `finalize()` 继续作为 `/stop` 和外部取消入口，但改为复用统一的 `_cleanup_run()` 与 `_complete_run()`，不能直接把 phase 写成 `DONE`。
- `mark_responding()`、`mark_responded()` 删除或改为兼容 no-op；transport 不再驱动 ReviewLoop phase。

### `nanoreview/agent/orchestration.py`

- 把 `ReviewExecutionContext`、必要的 outcome 数据结构和下列私有实现迁入 `ReviewLoop`：`_collect_plan()`、`_derive_execution_limits()`、`_estimate_input_tokens()`、`_dispatch_and_collect()`、`_build_subagent_task()`。
- 保留纯领域/执行辅助函数时，确保它们不接受或修改 `ReviewRunState`；状态更新统一由 ReviewLoop 的阶段方法完成。
- 删除 `ReviewOrchestrator` 的生产实例化和 `execute_run()` 调用；迁移期间若测试需要兼容类，只保留显式 deprecated wrapper，最终验证后删除。

### `nanoreview/agent/loop.py`

- `_run_agent_loop()` 不再执行 `prepare_code_review_context()`、review metadata 同步、review execution context 构造或 `ReviewPlanningError` 的 review 专用分支。
- 对 admitted review turn，构造最小 `ReviewTurnRequest`（session、messages、metadata、channel/chat/message id）并调用 `ReviewLoop.execute()`；ReviewLoop 返回 report/result，AgentLoop 只负责组装 outbound 和报告 stream。
- 对普通 conversation turn 保留现有通用 Runner 路径；review gate、`/status`、`/stop` 和 pending queue 语义保持不变。
- review 异常、取消和 `finally` 清理只调用 ReviewLoop 的统一终态入口，删除直接写 review phase/status 的重复逻辑。

### `nanoreview/session/coordinator.py`

- 保留 admission、route、gate、handoff 读取和索引写入 API。
- `finalize()` 只调用 ReviewLoop 的统一终态入口，并在结果确认后写一次 `review_context`；不得自己设置 `ReviewRunState.phase/status`。
- `pending_handoff()` 的前提改为 `ReviewLoop` 已完成 `DONE` 且 artifact/失败结果可读；不依据单独的 `review_status` 提前交接。

## 终态和错误矩阵

| 发生位置 | report/artifact | 最终 status | handoff |
|---|---|---|---|
| 全流程成功，可能有覆盖缺口 | 可读 | `completed` | `complete` 或 `partial` |
| 单个 reviewer/Judge 失败但 finalizer 仍产出报告 | 可读 | `completed` | `partial`，错误和缺口进入报告 |
| PREPARE/PLAN/finalizer 致命失败 | 无可用 artifact 或失败结果 | `error` | `failed` 或有界 `partial` |
| artifact 写入失败 | 不可读 | `error` | `failed` |
| `/stop` 或任务取消 | 可用部分结果或无 artifact | `stopped` | `partial` 或 `failed` |

所有行都必须经过 `CLEANUP`，并在 `DONE` 后才解除运行期门禁。`ReviewResult`、session metadata 和 handoff index 的写入顺序必须可重复执行且不重复追加历史。

## 测试落点

- `tests/agent/test_review_loop.py`（新增或从现有 loop 用例拆出）：阶段顺序、每阶段 phase 持久化、成功/错误/停止最终状态、cleanup 调用顺序和 artifact 写入失败。
- `tests/review/test_orchestration.py`：迁移为 ReviewLoop 执行测试；保留 coordinator plan、并发 reviewer、Judge usage、失败 reviewer 不得变成 `no_findings` 的断言。
- `tests/agent/test_loop_modes.py`：只验证 AgentLoop 委托 admitted review、普通 conversation 不进入 review、stream/outbound 不重复；删除对 `ReviewOrchestrator` 构造的直接 patch。
- `tests/session/test_coordinator.py`：验证只有 DONE 后 route 为 conversation、handoff/index 幂等、orphan running 规范化为 error。
- `tests/api/`、`tests/channels/`：验证 completed/error/stopped 的事件顺序、结构化状态和 report stream。

## 验收与验证

- 正常 review 的阶段顺序、artifact、`ReviewRunState`、session metadata 和 `ReviewResult` 一致；未清理完时普通消息仍被门禁。
- preparation、planning、reviewer、Judge、finalizer、artifact 写入及 session 持久化失败均有可见、有界结果；不会把缺失 artifact 或失败维度呈现为完整成功。
- `/stop` 取消并等待子任务后持久化 `stopped`；完成、错误和停止均可在同 session 读取结果并进行 handoff，不自动恢复或重跑。
- 同一 session 只接受一次 review；report 索引与完整 handoff 不重复注入；Conversation Agent 不获得 reviewer/Judge 内部 transcript。
- 更新最近的 `tests/agent/`、`tests/review/`、`tests/session/`，并按契约影响补充 `tests/api/` 与 `tests/channels/`。先运行定向 pytest 和 `ruff check nanoreview/`，再运行全量 `pytest`；报告本次实测结果，不沿用历史数字。

## 本轮之外

Conversation Agent 的 prompt、独立工具注册表、修复权限和执行留待第 3 阶段；长对话 Consolidator 与 AutoCompact 产品路径留待第 4 阶段。SessionCoordinator 的目录迁移、AgentLoop 总入口替换、WebUI 交互适配、scope 用户入口、远端目标、同 session 多次 review 和自动 worktree 均不纳入本节点。
