# 当前代码调整计划

更新时间：2026-10-02

## 实施进展（2026-10-02，上层调用链收尾缺口修正）

本节点已在工作区实现，等待验收。针对此前「收尾缺口修正」未覆盖的上层调用链五处缺口（用户经故障注入复核确认），全部按 review 生命周期后端修正：

- **`/stop` 兜底收尾**：`AgentLoop._settle_review_run_after_stop()` 在取消活动 task 后，若 session 仍有 `running` review run，补一次 `finalize(STOPPED)`；`cmd_stop` 据此如实报告（`Stopped N task(s).` / 收尾结果 / 收尾失败提示），不再回 `No active task to stop.`。兜底状态用 `STOPPED`（用户确实在要求停止）。
- **重启后 orphan 规范化缺口**：`pending_handoff()` 把 `result()` 提到 `_review_settled()` 之前调用，使无 live executor 的持久化 `running` 在真实 handoff 入口就被 `_normalize_interrupted_run()` 规范化为 `error`，而非永远卡在 `running`。`route()` 至今仍零生产调用点，属 ConversationLoop（第 3 阶段）未实现所致，非本节点修复范围。
- **规范化保存失败回滚**：`_normalize_interrupted_run()` 参照 `_complete_run()` 的 `_ABSENT` 哨兵模式，snapshot 后置 `STATUS/PHASE/SUMMARY/ERROR` 再 `save()`，失败时恢复原值并返回原 `running` 结果，不再让缓存提前发布 `error/done` 而磁盘仍 `running`。
- **排队取消不丢已准入 run**：`_dispatch()` 把 `finalize(STOPPED)` 从内层 `except CancelledError` 提升到外层，覆盖取消发生在 `async with lock, gate` 获取阶段的路径；已准入 `PREPARE` run 被收尾为 `stopped`，而非被 `finally` 的 `discard_unstarted()` 静默删除（磁盘残留 `running/prepare`）。
- **收尾失败原因透传**：`_execute_review_turn()` 在 `produces_report and error` 时把有界原因追加到 `final_content`（`> Review settlement failed: ...`），用户不再只看到原报告甚至「无问题」。

测试：`tests/test_builtin_commands.py` 修正 `/stop` 桩并新增无活动 task 兜底收尾用例；`tests/agent/test_review_gate.py` 新增 `/stop` 兜底、无 live run no-op、排队取消收尾（非丢弃）用例；`tests/session/test_coordinator.py` 新增 pending_handoff 真实入口规范化、失败回滚不提前发布 DONE 用例；`tests/agent/test_loop_modes.py` 新增收尾失败原因透传用例。

实测（2026-10-02，本次运行）：`pytest tests/ -q` → 593 passed；`ruff check nanoreview/` → 47 errors，与 `850aeb6b` 基线逐条比对完全一致（无新增、无减少）。`tests/` 下另有 2 项既有告警（`test_review_loop.py`、`test_subagent_trace.py`），非本节点引入。

待适配项（前端）：`/stop` 的回复文案由 `No active task to stop.` 改为可能携带收尾结果的一句话，WebUI 若把 `/stop` 回复当固定文案展示需同步；属文案级，不阻塞。

## 实施进展（2026-10-02，Runner review 边界清理）

本节点已在工作区实现，等待验收。作为第三阶段前置清理，未实现 Conversation Agent：

- `AgentRunSpec` 增加 `preserve_tool_result_tools: frozenset[str] = frozenset()`；`AgentRunner._run_tool()` 不再硬编码 `review_submit`，只把调用方显式声明的工具结果写入 `tool_events[*].raw_result`，默认不保留未截断结果。
- `SubagentExecutionProfile` 增加同名字段并由 `SubagentManager` 透传给 `AgentRunSpec`；review profile 显式声明 `review_submit`，generic profile 保持为空。
- 保留 `terminal_tools`、终止重试、压缩、权限、usage、取消和 checkpoint 等通用能力；只把 Runner 内的 review 语义注释改为通用的结果替换与终止工具提交描述。
- 删除 `SubagentManager._extract_review_submit_result()` 及其 3 个重复用例；review 结果只由 `review/profiles._handle_reviewer_result()` / `_canonical_review_submit()` 解析。
- 未改变 `AgentRunResult`、`tool_events` 结构，未改变 reviewer、planner、Judge 的执行顺序与错误语义。

测试：`tests/agent/tools/test_runner_tool_errors.py` 新增「未声明不保留 raw_result」「显式声明保留完整结果」及 terminal_tools 成功、失败重试、达上限、失败后成功、纯 prose 达上限用例；`tests/review/test_reviewer_profiles.py` 新增 reviewer 声明 `review_submit`、generic 不保留、真实 handler 解析完整提交、截断 tool message 不覆盖 `raw_result`、未成功提交不误判用例；`tests/agent/test_loop_modes.py` 增加 spec 透传断言并删除 3 个重复用例。

实测（2026-10-02，本次运行）：`pytest tests/ -q` → 585 passed, 1 failed；`ruff check nanoreview/` → 47 errors，与 `850aeb6b` 基线逐条比对完全一致（无新增、无减少）。

既有工作区失败（非本节点引入）：`tests/test_builtin_commands.py::test_stop_uses_effective_context_key` 因 `nanoreview/command/builtin.py` 的 `/stop` 收尾调用 `loop._settle_review_run_after_stop()`，而该用例的 `SimpleNamespace` 桩未提供此方法，抛 `AttributeError`。本节点未触碰该链路，建议由引入该调用的节点同步其测试桩。

## 实施进展（2026-10-02，收尾缺口修正）

本节点剩余四项收尾缺口已在工作区实现，等待验收：

- 清理失败不再写 `DONE`：`_cleanup_run()` 在子任务释放失败时记录有界警告并抛 `ReviewCleanupError`（再次取消仍抛 `CancelledError`）；`execute()` 的收尾、`_abort()` 和 `finalize()` 捕获后不发布终态，run 保持 `running`、门禁保持关闭，turn 得到有界错误。
- 终态保存失败不提前开放对话：`_complete_run()` 先构造终态 payload（`metadata_payload(status=..., phase=...)`）并 `save()`，失败时回滚 session metadata、抛 `ReviewPersistenceError`；内存 `phase=DONE`/终态 status 只在保存成功后写入，因此 live 终态等价于已持久化。
- 已准入 `PREPARE` 取消收尾：`finalize()` 不再 `discard()`，统一走 `CLEANUP -> DONE` 记录 `stopped`/`error` 及默认原因；已无调用方的 `ReviewLoop.discard()` 删除。
- 失败原因持久化：`metadata_payload()` 增加有界 `review_summary`/`review_error`；`result_from_session_metadata()` 优先读取持久化原因重建 `error`/`summary`；`_normalize_interrupted_run()` 修复 orphan `running` 时一并持久化中断原因。

测试：`tests/agent/test_review_loop.py` 新增清理失败、终态保存失败、`finalize` 门禁保持、二次取消保留待重试、PREPARE 收尾用例；`tests/agent/test_review_state.py` 新增 summary/reason 持久化与边界用例；`tests/review/test_result.py` 新增重启后原因重建用例；`tests/session/test_coordinator.py` 原「PREPARE 释放门禁」用例改为「收尾并持久化原因」。

实测（2026-10-02，本次运行）：`pytest tests/ -q` → 578 passed；`ruff check nanoreview/` → 47 errors，与 `850aeb6b` 基线逐条比对完全一致（无新增、无减少）。

待适配项（前端）：session metadata 新增 `review_summary`/`review_error` 两个只读字段，WebUI 若展示终态原因需在后端节点后单独适配。

## 实施进展（2026-10-02）

本节点已在工作区实现，等待验收。改动：`review_loop.py` 收敛为唯一 supervisor（新增 `ReviewTurnRequest`/`ReviewLoopOutcome`，内部阶段方法 `_prepare_review`/`_run_plan`/`_run_review`/`_finalize_and_persist`/`_cleanup_run`/`_complete_run`），`orchestration.py` 已删除；`review_state.py` 以 `CLEANUP` 替换 `SAVE`/`RESPOND`；`loop.py` 的 `_execute_review_turn` 只委托执行；`coordinator.py` 的 `pending_handoff`/`write_context_index` 以 `_review_settled`（DONE）为前提。测试：新增 `tests/agent/test_review_loop.py`，删除 `tests/review/test_orchestration.py`，迁移 `tests/agent/test_loop_modes.py`、`tests/agent/test_review_state.py`。

实测（2026-10-02，本次运行）：`pytest tests/ -q` → 569 passed；`ruff check nanoreview/` → 47 errors，与 `850aeb6b` 基线逐条比对无新增、无减少（均为既有告警）。

同批次顺带清理（用户点名，不属于本节点实施范围）：删除已无生产调用方的 `nanoreview/agent/hooks/review_finalizer.py`（`ReviewFinalizerHook`，2026-08-22 引入时 `AgentLoop.review_hook` 即为恒 `None` 的死槽位）及其在 `hooks/__init__.py` 的导出；删除同批遗留的 `ReviewFinalizer.set_validation_context()`（唯一调用方是已删 hook）。`tests/review/test_finalizer.py` 删除 3 个 hook 专属用例，另将「后置收紧 allowed dimensions」和「增量 ingest 累积」两个契约迁移到 `ReviewFinalizer` 直连 API。清理后：`pytest tests/ -q` → 566 passed；ruff 仍为 47 项、与基线一致。

最新确认目标：只有 review 完成 `DONE` 后才开放对话，清理、持久化和交接失败必须返回必要错误。以下方案已同步并落地；原记录的「清理失败仍写 DONE、终态保存失败后提前开放对话、已准入 PREPARE 取消收尾和失败原因重启读取」四项缺口已按本轮进展修正。既有测试原先未覆盖上述失败路径，本轮已补齐（见上节进展）。

## 当前节点

实施 roadmap 第 2 阶段：把一次 review 的准备、计划、reviewer/Judge 执行、报告生成、资源清理和终态写入收敛到 `ReviewLoop`。只有清理完成、终态及报告或有界失败结果已持久化，完成 `DONE` 后才开放同 session 对话；失败必须向用户返回必要错误。本文件记录待实施方案，不代表代码已落地；现状以工作区代码为准，旧 handoff 仅作参考。

## 已确认边界

- 本轮只迁移 review 生命周期。保留 `AgentLoop` 的 MessageBus 入站、session 串行、控制命令、取消与 outbound 交付；不移动 `SessionCoordinator`，不实现 `ConversationLoop` 的执行能力，不修改 `review-webui/`。
- `ReviewLoop` 是 `ReviewRunState`、report artifact 和 review 终态 metadata 的唯一写入者。`SessionCoordinator` 负责准入、路由、门禁、控制命令和 handoff 索引；`AgentRunner` 只执行单个 agent run。
- 将 `ReviewOrchestrator` 的流程控制并入 `ReviewLoop`，复用 planning、evidence、validator、Judge、finalizer 等领域组件。核对生产与测试调用方后移除旧控制器，避免两个 supervisor 同时写 run 状态。
- ReviewLoop 使用阶段方法顺序执行并更新 `ReviewRunState.phase`，不引入另一张状态转移表或通用 `BaseLoop`。

## 阶段与收尾

```text
PREPARE -> PLAN -> REVIEW -> FINALIZE -> CLEANUP -> DONE
```

- `PREPARE`：从已准入的 run、target、snapshot 和 session metadata 准备 evidence、policy、dimensions、校验上下文及 review prompt。保留准入边界，不凭持久化 target 重入 review。
- `PLAN`：运行 coordinator，验证提交的 assignments，记录 plan、input fingerprint 和 usage。
- `REVIEW`：按 assignments 并发调度、收集 reviewer；验证 finding，执行 Judge batching；在业务边界记录 reviewer/Judge 状态、usage、错误及覆盖缺口。
- `FINALIZE`：汇总 findings、coverage、gaps、warnings，生成 report，并写入独立 artifact。artifact 写入失败不得宣称完整成功。
- `CLEANUP`：等待或取消属于该 run 的剩余子任务，释放运行资源。正常、错误和停止出口都要经过此阶段；失败或再次取消须返回必要错误，未确认清理完成时不得进入 `DONE`。
- `DONE`：清理完成，`completed`、`error` 或 `stopped` 终态、最终 phase 及报告或有界失败结果已持久化后，才发布 live `DONE`、返回结构化 `ReviewResult`、写 handoff 索引并开放对话。保存失败返回有界错误并保持门禁，不能把内存终态当作已落盘证明。

`RESPOND` 不再是 ReviewLoop phase；review report stream、turn_end 和其他 transport 事件由上层交付。`FINALIZE` 合并旧方案的报告生成与持久化；`DONE` 是清理及结果持久化完成后的对话门禁条件。

## 实施顺序

1. 在 `ReviewLoop` 接管 `AgentLoop._run_agent_loop()` 中 review 专属的 preparation、metadata 归一化、execution context、异常处理和报告结果；`AgentLoop` 只识别已准入 turn、委托执行和交付，不修改 review 状态。
2. 迁移 `ReviewOrchestrator.execute_run()` 的 plan、review、Judge、finalizer 顺序及其必要的私有辅助方法。保留已有执行限制、并发、超时、usage 和失败映射行为；核对 `tests/review/test_orchestration.py` 等调用方后删除或收敛旧入口。
3. 统一正常、异常和取消出口。reviewer/Judge 失败不能转成干净报告；可用部分结果和有界错误需进入 result/handoff。停止不恢复、不重跑，保留已完成结果与已观察 usage。
4. `SessionCoordinator` 仅在 review 完成 `DONE` 后开放普通消息和非控制命令，并写一次 `review_context` 索引；清理或保存失败保持门禁并返回必要错误。无 live executor 的历史 `running` 仍规范化为 `error`，持久化中断原因及 `DONE` 后开放对话，不恢复执行。
5. 核对后端 API、CLI、WebSocket 生产者和 WebUI 消费者的 phase/status、报告流、终态和 handoff 契约；前端交互适配另行安排。

## 文件级实施细节

### `nanoreview/agent/review_state.py`

- 将 `ReviewPhase.RESPOND` 替换为 `ReviewPhase.CLEANUP`，保留 `PREPARE/PLAN/REVIEW/FINALIZE/DONE` 的 wire value 不变。
- `ReviewRunState.metadata_payload()` 在 `DONE` 前仍可写 `review_status=running` 和当前 phase；`_complete_run()` 在 cleanup 成功后持久化终态和 `phase=done`，保存成功才发布 live `DONE`。持久化有界失败原因与结果摘要，供重启后读取。
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
- `_cleanup_run()` 设置 `CLEANUP`，调用 `SubagentManager.cancel_by_session()` 并等待返回；异常或再次取消记录有界错误并返回用户，未确认清理完成时保持门禁，不继续发布 `DONE`。
- `_complete_run()` 只在 cleanup 成功后执行，持久化最终 `ReviewRunStatus`、`phase=done` 和可读结果，保存成功才发布 live `DONE`。正常 artifact 可读时为 `COMPLETED`；取消为 `STOPPED`；准备、计划、finalizer 或 artifact 的致命失败为 `ERROR`。保存失败必须返回必要错误并保持门禁。
- `finalize()` 继续作为 `/stop` 和外部取消入口，复用统一收尾；已准入且仍在 `PREPARE` 的 run 也必须记录 `stopped` 或 `error` 及原因，不能直接丢弃。
- `mark_responding()`、`mark_responded()` 删除或改为兼容 no-op；transport 不再驱动 ReviewLoop phase。

### `nanoreview/agent/orchestration.py`

- 把 `ReviewExecutionContext`、必要的 outcome 数据结构和下列私有实现迁入 `ReviewLoop`：`_collect_plan()`、`_derive_execution_limits()`、`_estimate_input_tokens()`、`_dispatch_and_collect()`、`_build_subagent_task()`。
- 保留纯领域/执行辅助函数时，确保它们不接受或修改 `ReviewRunState`；状态更新统一由 ReviewLoop 的阶段方法完成。
- 删除 `ReviewOrchestrator` 的生产实例化和 `execute_run()` 调用；迁移期间若测试需要兼容类，只保留显式 deprecated wrapper，最终验证后删除。

### `nanoreview/agent/loop.py`

- `_run_agent_loop()` 不再执行 `prepare_code_review_context()`、review metadata 同步、review execution context 构造或 `ReviewPlanningError` 的 review 专用分支。
- 对 admitted review turn，构造最小 `ReviewTurnRequest`（session、messages、metadata、channel/chat/message id）并调用 `ReviewLoop.execute()`；ReviewLoop 返回 report/result，AgentLoop 只负责组装 outbound 和报告 stream。
- 对普通 conversation turn 保留现有通用 Runner 路径；review 完成 `DONE` 前保留普通消息与非控制命令门禁，不写入 pending queue，保留 `/status`、`/stop` 和权限控制。
- review 异常、取消和 `finally` 清理只调用 ReviewLoop 的统一终态入口，删除直接写 review phase/status 的重复逻辑。

### `nanoreview/session/coordinator.py`

- 保留 admission、route、gate、重复 review/`/new` 限制、handoff 读取和索引写入 API；统一以 review 完成 `DONE` 作为对话准入条件。
- `finalize()` 只调用 ReviewLoop 的统一终态入口，并在结果确认后写一次 `review_context`；不得自己设置 `ReviewRunState.phase/status`。
- `pending_handoff()` 仅在 review 完成 `DONE` 后读取可用 artifact/有界失败结果；没有完整报告可以交接失败结果，清理或保存未完成不能提前交接。

## 终态和错误矩阵

| 发生位置 | report/artifact | 最终 status | handoff |
|---|---|---|---|
| 全流程成功，可能有覆盖缺口 | 可读 | `completed` | `complete` 或 `partial` |
| 单个 reviewer/Judge 失败但 finalizer 仍产出报告 | 可读 | `completed` | `partial`，错误和缺口进入报告 |
| PREPARE/PLAN/finalizer 致命失败 | 无可用 artifact 或失败结果 | `error` | `failed` 或有界 `partial` |
| artifact 写入失败 | 不可读 | `error` | `failed` |
| `/stop` 或任务取消 | 可用部分结果或无 artifact | `stopped` | `partial` 或 `failed` |
| cleanup 失败或再次取消 | 保留已有结果 | 保留未完成状态，不发布 `DONE` | 返回清理错误，保持门禁 |
| session 终态保存失败 | 已有 artifact 仍可读；终态未落盘 | 不发布 live `DONE` | 返回保存错误，保持门禁 |

所有终态出口都必须完成 `CLEANUP` 和终态/结果持久化；完成 `DONE` 后才解除对话门禁。`ReviewResult`、session metadata 和 handoff index 的写入顺序必须可重复执行且不重复追加历史；失败原因须支持重启后读取，写入失败须即时告知用户。

## 测试落点

- `tests/agent/test_review_loop.py`（新增或从现有 loop 用例拆出）：阶段顺序、每阶段 phase 持久化、成功/错误/停止最终状态、cleanup 调用顺序和 artifact 写入失败。
- `tests/review/test_orchestration.py`：迁移为 ReviewLoop 执行测试；保留 coordinator plan、并发 reviewer、Judge usage、失败 reviewer 不得变成 `no_findings` 的断言。
- `tests/agent/test_loop_modes.py`：只验证 AgentLoop 委托 admitted review、普通 conversation 不进入 review、stream/outbound 不重复；删除对 `ReviewOrchestrator` 构造的直接 patch。
- `tests/session/test_coordinator.py`：验证完成 `DONE` 后才开放对话、清理/保存失败保持门禁、handoff/index 幂等、orphan running 规范化为 error 且保留原因。
- `tests/api/`、`tests/channels/`：验证 completed/error/stopped 的事件顺序、结构化状态和 report stream。

## 验收与验证

- 正常 review 的阶段顺序、artifact、`ReviewRunState`、session metadata 和 `ReviewResult` 一致；清理或持久化未完成时普通消息仍被门禁，完成 `DONE` 后才进入对话。
- preparation、planning、reviewer、Judge、finalizer、artifact 写入及 session 持久化失败均有可见、有界结果；不会把缺失 artifact 或失败维度呈现为完整成功。
- `/stop` 取消并等待子任务后持久化 `stopped`，包含已准入的 `PREPARE`；清理失败明确披露并保持门禁。完成、错误和停止均在完成 `DONE` 后读取结果并进行 handoff，不自动恢复或重跑。
- 同一 session 只接受一次 review；report 索引与完整 handoff 不重复注入；Conversation Agent 不获得 reviewer/Judge 内部 transcript。
- 更新最近的 `tests/agent/`、`tests/review/`、`tests/session/`，并按契约影响补充 `tests/api/` 与 `tests/channels/`。先运行定向 pytest 和 `ruff check nanoreview/`，再运行全量 `pytest`；报告本次实测结果，不沿用历史数字。

## 本轮之外

Conversation Agent 的 prompt、独立工具注册表、修复权限和执行留待第 3 阶段；长对话 Consolidator 与 AutoCompact 产品路径留待第 4 阶段。SessionCoordinator 的目录迁移、AgentLoop 总入口替换、WebUI 交互适配、scope 用户入口、远端目标、同 session 多次 review 和自动 worktree 均不纳入本节点。
