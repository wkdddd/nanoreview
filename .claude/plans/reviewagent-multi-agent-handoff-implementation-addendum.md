# ReviewAgent Multi-Agent 实施后交接补充（最新进展）

更新时间：2026-09-25（文档核对与当前基线刷新）

本文件基于 `.claude/plans/reviewagent-multi-agent-handoff.md`，是该目标每次调整后的最新实施进展记录。原交接文档是目标架构和验收基线；本文件只记录经过代码、测试和命令核对的当前事实，不替代原文。每次涉及该目标的调整完成后，必须更新本文件的日期、基线、已落地能力、缺口和验证结果；历史状态要明确标注为历史，不得继续写成当前状态。

## 本轮目标调整（2026-09-19）

从目标和验收标准中**删除“同进程恢复”**：不再要求 reviewer/judge 结果复用、稳定 batch ID、重复派发恢复，也不增加 `/resume`、自动 workflow 重入、result reference 或跨进程恢复。

- 目标 handoff 已删除“同进程恢复设计”章节及相关验收项、产品决策和实施步骤，并新增“重试与失败边界（与恢复无关）”和“ReviewRunState 的边界”。
- supervisor 职责收敛为流程编排、生命周期、取消、状态和持久化，不再包含恢复。
- `ReviewRunState` 保留为运行期可观测性对象（运行状态、phase、usage、取消、session metadata、artifact 可观测性），不是恢复协议。
- 现有 Provider 重试、`AgentRunner` terminal retry 和超时控制保持不变；reviewer/judge 失败继续显式进入不完整结果或 `needs_confirmation`，不自动重跑。
- 与该目标无关的既有状态错误（reviewer 终态映射）在本轮一并修复，见“已落地能力 / Reviewer 终态状态映射”。

## 本轮变更（2026-09-20）：Judge batch 迁移到 AgentRunner

按目标文档“Judge Agent 改造”章节完成第 2 步。范围只替换 batch 内的执行入口：

- 新增 `nanoreview/agent/tools/review_judge.py`（`JudgeVerdictReceiver`、`SubmitJudgeVerdictsTool`，工具名固定 `submit_verdicts`）；
- `ReviewJudge._judge_batch()` 改为构造独立 `AgentRunSpec` 并调用共享 `AgentRunner.run()`，由 terminal tool 提交；不再直接调用 `provider.chat_with_retry()`；
- `ReviewJudge` 构造函数改为 `(runner, model, config)`，不接受 `provider`，不创建第二个 runner，也不再接受独立 model preset；
- `judge_dimensions()` 返回不可变 `JudgeExecutionResult`，删除 `last_usage` / `last_stats` / `last_error` 跨调用可变字段；
- `ReviewFinalizer.apply_judge()` 消费并返回 `JudgeExecutionResult`；`ReviewOrchestrator.execute_run()` 改为从该结果写回 batch status/stats/usage。

本轮**不**包含（仍为缺口，见“尚未满足原始验收标准的部分”）：run-level compression、supervisor phase 迁移、orchestrator 删除。

## 本轮文档核对（2026-09-25）

- 目标文档、架构约束和实现进展的职责边界保持不变：目标文档描述目标状态，本文件描述代码事实，`ROADMAP.md` 只承载跨阶段执行顺序。
- 当前工作区未新增 ReviewAgent 运行时代码；本轮只刷新已实测的验证基线并收敛下一步安排。
- 目标没有恢复语义变化：不增加 `/resume`、自动 workflow 重入、result reference 或跨进程恢复路径。

## 文档更新规则

- 目标架构或验收标准变化：修改原始 handoff，并在本文件记录影响和迁移边界。
- 代码实现变化：不改写原始 handoff 以掩盖缺口；在本文件更新实际状态、风险、测试和未完成项。
- “当前基线”必须以更新时的 `git HEAD`、工作区状态和实测命令为准；不得复制上一轮的工作区统计或测试结果而不复核。
- 只要改动跨越 supervisor、`reviewstate`、session metadata、artifact、Judge、WebUI/API 或失败与重试语义，就必须同步检查对应 `.agents/` 专题约束和完整调用链。

## 当前基线

- 当前实现基线：`bb99aafd feat: Implement usage tracking and aggregation for ReviewAgent`（工作区改动尚未提交，HEAD 与本轮开始时相同）。
- 当前工作区保留原有 `ROADMAP.md` 的已完成项、其他方向和暂不计划，本轮按 ReviewAgent 目标重排执行顺序；同时包含两份 handoff 文档、`AGENTS.md`、`.agents/architecture.md`、`.agents/budget.md`、`.agents/reference-summary.md` 的文档调整，以及代码与测试改动：`nanoreview/agent/orchestration.py`、`nanoreview/review/output/finalizer.py`、`nanoreview/review/output/judge.py`、`nanoreview/agent/review_state.py`、`nanoreview/agent/loop.py`，新增 `nanoreview/agent/tools/review_judge.py`；测试改动 `tests/review/test_orchestration.py`、`tests/review/test_finalizer.py`、`tests/review/test_judge.py`、`tests/agent/test_review_state.py`，新增/增补 `tests/agent/test_review_gate.py`。
- 当前工作区验证结果（2026-09-25 实测，`bb99aafd` + 上述工作区改动）：`python -m pytest -q` 为 `426 passed`（约 31 秒）。相对 `bb99aafd` 的 `412 passed` 净增 14 例，来自 Judge 迁移（11 例）与失败 batch usage 修复（3 例：`test_judge_timeout_keeps_usage_from_completed_iterations`、`test_judge_failed_batch_keeps_consumed_usage`、`test_execute_run_judge_failed_batch_keeps_consumed_usage`）。`tests/review/test_judge.py` 现 26 例，覆盖 runner 共享/AgentRunSpec 契约、prose 与非法参数 terminal retry、单 batch 失败不阻断其他 batch、取消传播、`model_preset` 被忽略、disabled 不建 judge、结果不可变，以及失败/超时 batch 的 usage 不丢失。
- `cd review-webui && bun run test` 于 2026-09-25 实测 `30 passed`。本轮未改动 WebUI 代码，但该结果作为当前交接基线保留。
- `python -m ruff check nanoreview/` 当前仍为 `47 errors`（与本方案无关的既有问题，与 `bb99aafd` 基线一致）。本轮改动文件 `ruff check nanoreview/agent/tools/review_judge.py nanoreview/review/output/judge.py nanoreview/review/output/finalizer.py nanoreview/agent/orchestration.py nanoreview/agent/loop.py tests/review/test_judge.py tests/review/test_finalizer.py tests/review/test_orchestration.py` 为 `All checks passed!`。不要在本方案的改动中顺带批量修复其余既有告警。

## 已落地能力

### 架构约束

`.agents/architecture.md` 已更新：

- `agent/loop.py` 被定义为 ReviewAgent supervisor 生命周期 owner；
- `agent/review_state.py` 负责进程内状态、fingerprint 和 artifact 持久化；
- `agent/orchestration.py` 被标记为迁移中的 legacy compatibility shell；
- WebUI/API 只消费 review metadata 和 report API。

注意：文档 ownership 已经迁移，但核心编排代码仍然位于 `ReviewOrchestrator.execute_run()`（`agent/orchestration.py:152`）；这属于后续迁移缺口，不能认为 orchestrator 已经只是空壳。

orchestrator 现有两个入口，迁移前需要区分：

- `execute_run()`（`:152`）是唯一的生产入口，由 `agent/loop.py:1313` 调用；
- `execute()`（`:127`）只是对 `execute_run()` 的薄封装，生产代码无调用方，仅被 `tests/review/test_orchestration.py`（`:140`、`:164`、`:248`）使用。第 5 步迁移时应把这些测试改到 `execute_run()` 并删除 `execute()`，否则它会成为迁移后残留的死入口。

### ReviewRunState 与 metadata

已新增：

- `ReviewPhase`：`prepare`、`plan`、`review`、`finalize`、`save`、`respond`、`done`；
- `ReviewRunStatus`：`running`、`completed`、`error`、`stopped`；
- `ReviewRunState`、`ReviewerRunState`、`JudgeBatchState`；
- `ReviewMetaKey.RUN_ID`、`STATUS`、`PHASE`、`REPORT_REF`、`INPUT_FINGERPRINT`。

当前 `AgentLoop` 已实现：

- session 维度的一次性 review run 注册；
- 同一 session 的重复 review 提交门禁；
- 状态 metadata 写回 session；
- review 阶段和最终状态日志；
- artifact 写入成功/失败后的状态更新。

### Report artifact 与读取 API

`ReviewArtifactStore` 已实现：

- workspace 下 `review-artifacts/` 目录；
- run id 白名单校验；
- JSON 大小限制；
- 临时文件加 `os.replace` 原子写入；
- run id、session key、input fingerprint 校验；
- 相对 `review_report_ref`，不返回绝对服务器路径。

同一条路径在两套 HTTP 栈中各注册一次，共享同一个 `_review_report_payload()` 实现：

```text
GET /api/sessions/{key}/review-report
```

- 内建 WS HTTP 栈：`channels/websocket.py:721` 的正则路由 -> `_handle_review_report_get()`；
- aiohttp 栈：`channels/websocket.py:2010` 的 `app.router.add_get()` -> `_aiohttp_review_report()`。

两处都复用 API token 鉴权，只服务 `websocket:*` session。缺失 session/report 返回 `404`，artifact 损坏或引用/fingerprint 不匹配返回 `409`（由 `ReviewArtifactError.status` 决定），未初始化返回 `503`。

WebUI 已增加：

- `fetchReviewReport()`；
- session metadata 中的 run id/status/phase/report ref 映射；
- hydration 时优先使用 artifact，旧 session 无 artifact 时继续 transcript fallback；
- report fetch 失败时显示错误状态。

边界：当前 UI hydration 实际按 `review_status` 映射 `idle/reviewing/completed/error/stopped`，虽然 API 类型保留了 `review_phase`，但没有把 `prepare/plan/review/finalize/save/respond` 细粒度 phase 还原到 UI 状态；不能把 metadata 字段映射误认为完整 phase 还原。

### Artifact 终态写入时序（已修复）

早前的实现先在 `run_state.status == running` 时构造 artifact，再把 run state 置为 `completed`，导致落盘 artifact 的 `status` 与 session metadata/API 外层状态不一致。

当前 `build_report_artifact()`（`agent/review_state.py:310`）新增显式 `status: ReviewRunStatus | None` 参数，`(status or state.status).value` 中 state 自身只作 fallback；`agent/loop.py:1351` 在序列化前显式传入 `status=ReviewRunStatus.COMPLETED`。artifact 写入失败时不留下 artifact，run 本身降级为 `error`。

已覆盖测试：`test_build_report_artifact_status_overrides_running_state`、`test_build_report_artifact_can_record_a_failed_run`。

### ReviewRunState usage 汇总（已实现）

新增共享 `merge_token_usage()`（`utils/helpers.py`），并在三层挂上累加入口：

- `ReviewerRunState.add_usage()`（`agent/review_state.py:83`）、`JudgeBatchState.add_usage()`（`:97`）、`ReviewRunState.add_usage()`（`:130`）；
- coordinator：`_collect_plan()` 在 plan accepted 后写入 `run_state.add_usage(result.usage)`；
- reviewer：`_dispatch_and_collect()` 从 `metadata["subagent_usage"]` 读取，同时写入 reviewer 级和 run 级；
- judge：`judge_dimensions()` 返回的 `JudgeExecutionResult.usage`（由 `AgentRunResult.usage` 逐 batch 汇总）在 `execute_run()` 中折进 `judge_batches["judge"]` 和 run 总量。

并发安全性依赖于累加只发生在 supervisor 的单一 await 边界（`wait_for_session_result()` 返回后），不是在 reviewer task 内部，因此无需额外锁。

judge 目前聚合为单一 `"judge"` 条目，理由是 batch 只是 context window 拆分，不是独立工作单元，也不要求稳定 batch id。本轮只替换了 batch 内的执行入口，该聚合结构未变。

已覆盖测试：`test_review_run_state_add_usage_accumulates_counters`、`test_reviewer_and_judge_state_accumulate_usage`、`test_judge_usage_aggregates_every_batch`、`test_judge_usage_resets_between_runs`、`test_execute_run_aggregates_agent_usage_into_run_state`、`test_execute_run_judge_success_marks_batch_completed`（断言 judge usage 同时进入 batch 与 run）。

### Judge batch 通过 AgentRunner 执行（2026-09-20 落地）

执行链路（`review/output/judge.py` + `agent/tools/review_judge.py`）：

```text
ReviewJudge.judge_dimensions -> _split_batches -> _judge_batch
  -> AgentRunSpec(tools=[SubmitJudgeVerdictsTool], tool_choice=submit_verdicts,
                  terminal_tools={submit_verdicts}, terminal_retry_limit=5,
                  temperature=0, max_tokens=config.max_tokens,
                  context_window_tokens=config.context_window_tokens, error_message=None)
  -> AgentRunner.run()（共享 coordinator/plan 的 runner/provider/model）
  -> JudgeVerdictReceiver.submit(verdicts)
  -> (verdicts, AgentRunResult.usage)
  -> JudgeExecutionResult.verdicts / stats / usage / error
```

- `_judge_batch()` 只在 `AgentRunResult.stop_reason == "completed"` **且** receiver 收到合法提交时算成功；timeout、provider 异常、`terminal_tool_failed`、`max_iterations`、只输出 prose、未调用 `submit_verdicts`、参数始终非法都算 batch 失败。
- 失败 batch 的候选由 `_run_batches()` 标记 `needs_confirmation` 并写入有界 `error`，其他 batch 继续执行；`asyncio.CancelledError` 不在捕获范围内，会向上传播。
- 失败 batch 已消耗的 usage 仍会计入总量（2026-09-20 修复）：`_judge_batch()` 抛出的 `_JudgeBatchError` 携带 `usage`（run 正常结束时取 `AgentRunResult.usage`），timeout 由 `_JudgeUsageObserver` 逐轮快照兜底，`_run_batches()` 从异常取回后合并。`bound_child_error()` 对空消息异常（如裸 `TimeoutError`）会返回空串，因此失败原因有 `AI judge batch failed (<Type>)` 兜底，避免 supervisor 把空 error 读成 `completed`。
- `judge_dimensions()` 不再抛普通异常：provider/terminal/timeout 错误转成 `JudgeExecutionResult.error` 加候选 `needs_confirmation`；只有取消继续向上抛出。
- `JudgeExecutionResult` 是不可变值对象。上一轮的 `last_stats`/`last_usage`/`last_error` 可变字段接口已删除，finalizer 与 supervisor 只消费返回值。
- Judge 与 plan 共用 `self.runner`/`self.model`：`AgentLoop._build_review_judge()` 不再解析 `review.judge.model_preset` 或构建独立 preset snapshot。`ReviewJudgeSettings.model_preset` 字段仍保留在配置 schema 中（兼容旧配置），但不会被 Judge 读取。
- Judge batch 不共享持久化 session、checkpoint、injection、workspace 或用户权限；`max_tool_result_chars` 使用 `judge.py` 内的固定小上限（该 run 唯一的工具只返回短确认串）。

`ReviewFinalizer.apply_judge()` 消费并返回 `JudgeExecutionResult`；`ReviewOrchestrator.execute_run()` 在调用前置 `judge_batches["judge"]` 为 `running`，随后按该结果写回：

- `result.usage` 折进 batch 与 `ReviewRunState`；
- `result.error` 非空 → batch=`error` + 有界原因，候选仍由 finalizer 标记 `needs_confirmation`，**不**把整个 run 升为 `error`；
- `result.error` 为空 → batch=`completed`（含全部 `needs_confirmation` 与“无候选”的空结果），`result.stats` 非空时写回统计；
- 删除对 `judge.last_usage` / `last_stats` / `last_error` 的读取。

`ReviewJudgeSettings.model_preset` 字段仍在 `nanoreview/config/schema.py` 中保留（兼容旧配置文件），但 `AgentLoop._build_review_judge()` 已不读取它。

已覆盖测试（`tests/review/test_judge.py`）：`test_judge_batch_runs_through_shared_runner_with_terminal_spec`（runner/spec 契约与无 session/checkpoint/injection/permission）、`test_judge_single_batch_failure_does_not_block_other_batches`、`test_judge_prose_response_marks_candidates_needs_confirmation`、`test_judge_invalid_tool_arguments_are_retried_then_succeed`、`test_judge_permanently_invalid_arguments_fail_the_batch`、`test_judge_cancellation_propagates`、`test_judge_ignores_configured_model_preset`、`test_judge_disabled_setting_yields_no_judge`、`test_judge_execution_result_is_immutable`；`tests/review/test_orchestration.py`（`test_execute_run_judge_success_marks_batch_completed` / `..._failure_marks_batch_error` / `..._no_candidates_does_not_call_provider`）、`tests/review/test_finalizer.py`（`test_apply_judge_returns_stats_usage_and_bounded_error` / `test_apply_judge_escalated_failure_reports_bounded_error`）已改为新契约并继续通过。

### Reviewer/Judge 取消与失败终结（本轮落地）

`AgentLoop._finalize_review_run()`（`agent/loop.py`）在 `/stop` 或任务失败把 run 置为终态时，同步终结仍在 `pending`/`running` 的子工作：

- `STOPPED` → 未完成 reviewer/judge 置 `stopped`（带“review stopped…”有界原因），已 `completed` 的 reviewer 保持其终态与 usage；
- `ERROR` → 未完成 reviewer/judge 置 `error`（带“review failed…”有界原因）。

`ReviewState` 模型相应收紧：`ReviewerRunState.status` 含 `stopped`；`JudgeBatchState` 新增 `error` 字段并支持 `running`/`stopped`；新增 `bound_child_error()`（300 字符上限）供 judge/orchestrator 复用有界原因。

已覆盖测试：`test_stopped_run_marks_inflight_reviewer_and_judge_stopped`、`test_error_run_marks_inflight_reviewer_and_judge_error`（test_review_gate）；`test_judge_batch_state_transition_and_bounded_error`、`test_bound_child_error_collapses_and_bounds_reason`、`test_reviewer_stopped_status_distinct_from_completed`、`test_run_state_terminal_locks_phase_and_metadata`（test_review_state）。

### Reviewer 终态状态映射（本轮修复）

修复前 `ReviewOrchestrator._dispatch_and_collect()` 在收集 reviewer 结果时无条件写入 `reviewer.status = "completed"`，不读取 `metadata["subagent_status"]`：reviewer 以 `error` 结束时 run state 仍报告成功，属于与恢复无关的既有状态错误。

当前行为（`agent/orchestration.py`）：

- 读取 `metadata["subagent_status"]`；只有 `ok` 写入 `completed`，其余（含缺失状态）写入 `error`，并保存有界错误原因（`reviewer_failure_reason()`，上限 300 字符，与 finalizer 的 `_incomplete_reason` 口径一致；该 helper 已移入 `review/output/finalizer.py` 供两处复用）。
- 失败路径（2026-09-19 修复）：非 `ok` 终态会把失败原因显式传给 `finalizer.ingest_subagent_output(..., failure_reason=...)`；finalizer 将该维度直接标记为 `incomplete`（带原因），**不再解析失败 reviewer 的 raw 输出**。修复前，失败 reviewer 返回的合法 JSON（如空 findings）会被解析成 `no_findings`，最终报告显示 "No actionable issues found" 而不是 incomplete，与新验收标准冲突。hook 路径 `ingest_messages()` 对显式非 `ok` 状态做同样处理（缺失状态保留 lenient 行为以兼容旧持久化消息）。
- 成功和失败都保留原有 finalizer ingestion、usage 汇总（reviewer 级 + run 级）和 `result_callback` 行为；失败额外记录 `review.dispatch.reviewer_failed` 日志。
- 不增加 reviewer 外层自动重试。报告通过 `Review incomplete` 显式标注不完整。
- judge 聚合注释中“稳定 batch id / 可重入”的表述已删除。

已覆盖测试：`test_failed_reviewer_is_recorded_as_error_and_report_is_incomplete`、`test_missing_subagent_status_is_not_silently_treated_as_success`（含报告不完整与 "No actionable issues found" 不出现断言）、`test_failed_reviewer_with_valid_json_is_reported_incomplete`（非 ok 状态 + 合法 JSON 不得判为 no_findings）、finalizer 侧 `test_ingest_runner_message_with_failed_status_is_incomplete`、`test_execute_run_aggregates_agent_usage_into_run_state`（成功路径仍为 `completed`）。

### Review session 消息门禁

当前门禁位于 `AgentLoop.run()`、`_dispatch()` 和 `_state_command()`：

- 内存中存在 review run 时，普通消息不会进入 pending queue；
- 已完成或失败状态会拒绝普通后续消息；
- `/status`、`/stop` 和内部 subagent/system event 保留；
- `/new` 会清理 review gate 和相关 metadata；
- session metadata gate 覆盖进程重启后没有内存 run 的场景。

已覆盖测试：`tests/agent/test_review_gate.py`（3 例）——终态 session 的普通消息返回拒绝响应且内存与磁盘 session messages 都不增加；`running` metadata 在没有内存 run 时同样被 gate；gate 对内部事件、无 review metadata 的 session 和非法状态值保持放行。

## 尚未满足原始验收标准的部分

> 说明：更早一轮的目标调整已删除“同进程恢复”相关验收项，因此原有“同进程恢复未实现”缺口、result reference、completed reviewer replay、稳定 batch id 和重复派发等待办**不再是缺口**。2026-09-20 的 Judge→AgentRunner 迁移已完成，原“Judge 尚未统一到 AgentRunner”缺口同样关闭。下面只保留仍然有效的差距。

### 1. run-level compression 尚未实现

`AgentRunner._prepare_messages()` 当前仍是 orphan tool result 处理、microcompact、tool result budget 和 hard trim。尚未实现原交接文档要求的：

- 每个 run 独立 `RunCompressionState`；
- 60% 异步压缩；
- 80% 同步压缩；
- frozen/compress/active 消息区；
- 压缩失败保留原 working history；
- 压缩后仍超限时返回 `context_compression` stop reason；
- working messages 与 `AgentRunResult.messages` 分离。

### 2. supervisor 状态机尚未真正收敛

当前通用外层状态机仍是：

```text
RESTORE -> COMPACT -> COMMAND -> BUILD -> RUN -> SAVE -> RESPOND -> DONE
```

review phase 目前嵌在 `_run_agent_loop()` 内，并非独立的：

```text
PREPARE -> PLAN -> REVIEW -> FINALIZE -> SAVE -> RESPOND -> DONE
```

后续迁移必须保持 generic chat 行为不回归，同时将 review supervisor 的阶段边界、取消和 checkpoint 从 `ReviewOrchestrator` 移到 `AgentLoop`；迁移不涉及任何恢复语义。

## 与恢复无关的现状说明（明确非目标）

以下行为是**当前的最终设计**，不是待修复缺口：

- 取消、异常或进程重启后不恢复 reviewer/judge 工作。取消/异常把运行中的 state 终结为 `stopped/error` 并保持 session gate；进程重启后 metadata gate 能阻止普通消息，但不会重建 supervisor。
- generic `runtime_checkpoint` 只恢复普通 Agent 的消息/工具上下文，本来就不包含 reviewer/judge 结果；不打算扩展它承载 review 工作单元。
- `SubagentManager` 对同一 session/dimension 的已完成任务拒绝再次 spawn 的限制保留；由于不再有恢复/重复派发需求，不需要 run-scoped task identity。
- 没有 `/resume`、自动 workflow 重入、result reference、attempt count 或 per-batch 持久化字段。
- 将来若出现明确的“中断后继续”产品需求，单独设计 OCR 式的跨运行 resume（显式用户入口、输入身份校验、持久化 manifest），不复用本轮删除的同进程方案。OCR 的实际机制见 `.agents/reference-summary.md` 的 “open-code-review 的跨运行 resume（已核查）”：`--resume <session-id>` 显式入口 + `~/.opencodereview/sessions/<repo>/<session-id>.jsonl` 持久化重放 + `RunManifest`/`ValidateResume` 身份校验。NanoReview 当前不采用该机制。

## 当前消息门禁的边界说明

产品规则仍是“一次 review session 只执行一次，完成后拒绝普通消息”。实现上必须继续区分三类消息：

1. 普通用户消息：运行中和终态都拒绝，不写入 session/history/pending queue；
2. `/status`、`/stop`、`/new`：走控制路径；
3. subagent/system result：允许进入当前 review supervisor 的内部收集路径。

两条门禁路径的实际行为已核对：

- 内存 gate（`agent/loop.py:1517-1530`）在 `run()` 的入站分支直接 `continue`，消息不进 pending queue、不进 session，这条路径是干净的。
- 注册 gate（`_dispatch()`，`agent/loop.py:1598-1610`）在任何 await 之前完成 review run 注册，因此同一 session 的并发 review 提交无法竞态穿过门禁；已存在 run 时直接返回 gate 响应，不落 session。
- metadata gate（`agent/loop.py:2224-2228`）位于 `_state_command()` 开头，命中后 `ctx.outbound = gate_response; return "shortcut"`。它在 `self.commands.dispatch()` **之前**返回，因此不会走到 `2240-2246` 那段 `_persist_user_message_early()` + `add_message("assistant", …)` + `sessions.save()` 的 shortcut 持久化逻辑，被拒绝的普通消息不会写入 session。

这条 gate 的“不持久化”属性完全依赖它位于 `_state_command()` 中 command dispatch 之前的位置。任何把 gate 下移、或把持久化逻辑上移的重构都会静默破坏该不变量，因此本轮补了 `tests/agent/test_review_gate.py` 作为保护：metadata 处于终态的 session 收到普通消息后返回 gate 响应，且内存与磁盘的 `session.messages` 都不增加。后续重构该位置前必须先看这条测试。

## 测试与工作区注意事项

- `tests/webui/test_review_report_api.py` 存在于当前工作区（本文件早前版本记录该文件被删除，与实际不符，已更正）。report API 测试正常参与 `pytest` 运行。
- 命令口径按 `AGENTS.md`：WebUI 使用 `bun run test` / `bun run build`，不是 `npm`。
- WebUI `bun run test` 当前为 `30 passed`（`parse-report.test.ts` 20 项 + `review-report.test.ts` 10 项）。此前记录的 Vite 产物超过 500 kB warning 未在本轮重新验证，仍按非功能阻塞处理。
- Python 全量测试基线见上方「当前基线」（现为 `426 passed`）。本轮新增覆盖：judge batch 走共享 runner 的 spec 契约、terminal retry（prose / 非法参数）、单 batch 失败不阻断其他 batch、取消传播、`model_preset` 被忽略、`apply_judge` 返回 stats/usage/error。历史轮次已覆盖：取消/`/stop` 把运行中 reviewer/judge 终结为 `stopped/error`（test_review_gate 2 例）、judge batch 状态归集（`running -> completed/error`、无候选、timeout）、部分失败 reviewer 的 completed/error + incomplete 报告，以及运行终态锁 phase。仍未覆盖：supervisor 阶段边界（迁移后 orchestrator 收敛在 `AgentLoop` 时的阶段/取消/checkpoint 行为）、run-level compression。
- `ruff` 现存 47 个既有告警与 `bb99aafd` 一致，评估新改动时应对比基线而不是要求全量清零；本轮改动文件为 `All checks passed!`。
- 任何后续修改都必须同时检查 `.agents/architecture.md`、`.agents/security.md`、`.agents/budget.md`、session persistence 和 WebUI/API wire contract。

## 后续推荐顺序

已落地（已包含在当前基线提交 `bb99aafd` 或本轮工作区改动）：

- ~~修复 artifact 终态写入时序，并补一致性测试。~~
- ~~实现 coordinator/reviewer/judge usage 汇总到 `ReviewRunState`。~~
- ~~补 metadata gate 的“不持久化”回归测试（`tests/agent/test_review_gate.py`）。~~
- ~~修复 reviewer 终态状态映射并补 error 路径测试。~~
- ~~从目标与验收基线中删除同进程恢复，并同步 addendum、`AGENTS.md` 和 `.agents/` 约束。~~
- ~~将 Judge batch 迁移到 `AgentRunner.run()`，删除 `ReviewJudge.last_usage` 临时读取点，usage 改由 `AgentRunResult.usage` 提供。~~

待办（按依赖关系排序）：

1. ~~状态正确性测试与修复~~（2026-09-19 完成）：已用测试锁定 judge 状态/usage 归集、取消/`/stop` 终结运行中 reviewer/judge、部分失败 incomplete 报告、运行终态锁 phase、metadata gate 不持久化及 reviewer 终态错误映射。剩余与状态无关的 orchestration 收敛见第 3 步。
2. ~~将 Judge batch 迁移到 `AgentRunner.run()`~~（2026-09-20 完成）：batch 执行改为 `AgentRunner.run()` + `submit_verdicts` terminal tool + `JudgeVerdictReceiver`，保留候选收集、batch 拆分、聚合 usage/stats 和 verdict contract；`last_*` 可变字段接口已删除，usage 由 `AgentRunResult.usage` 经 `JudgeExecutionResult.usage` 提供。剩余：run-level compression 未接入（见“尚未满足原始验收标准的部分”第 1 项）。
3. **实现并验证 run-level compression**：先在 `AgentRunner._prepare_messages()` 周围引入每次 run 独立的 `RunCompressionState`，再实现 60% 异步压缩、80% 同步压缩、frozen/compress/active 分区和 `context_compression` 终止语义。必须先补单元测试，覆盖压缩成功、压缩失败保留 working history、超限终止、取消时清理后台 task，以及 `AgentRunResult.messages` 不被压缩结果污染。
4. **分阶段收敛 supervisor**：以 `ReviewOrchestrator.execute_run()` 的现有调用链为迁移清单，在 `AgentLoop` 中逐步落地 `PREPARE -> PLAN -> REVIEW -> FINALIZE -> SAVE -> RESPOND -> DONE`。每迁移一个阶段就补阶段边界、取消、artifact、metadata 和失败可见性测试；迁移完成前保留 compatibility shell，禁止在 shell 中增加新职责。
5. **清理遗留并复核产品链路**：在生产调用方和测试全部迁移后删除 `ReviewOrchestrator` 死入口及无调用方的 generic chat 能力，同时复核 WebUI/API wire contract、文档和完整测试。此阶段仍不引入任何恢复语义。

6. **再评估预处理与预算**：待 compression 和 supervisor 收敛后，基于真实运行数据复核 `preprocessor.py` 的证据预算、`context_window_tokens`、并发和超时边界；只有发现稳定的预算缺口才修改 `.agents/budget.md` 与配置契约，并为每个调整补测试。不要在核心迁移前重复调整预算公式。

顺序理由：状态和 Judge 迁移已经完成；compression 是所有 agent 共用的运行时基础，应先于 supervisor 阶段迁移。supervisor 收敛后才能准确判断哪些 generic chat 和预算路径仍有调用方，最后再做清理和预算复核。任何一步都不得引入 resume 语义。

## 交接不变量

后续实现不得破坏以下不变量：

- 一个 review session 至多一个 review run；
- report、findings、verdicts、run state 使用同一 input fingerprint；
- 普通拒绝消息不污染 review history；
- 不持久化 asyncio task、provider client、lock、callback 或压缩 coroutine；
- artifact API 不泄露绝对路径；
- reviewer 成功必须写入 `completed`，失败必须写入 `error` 且带非空有界原因；reviewer/judge 的失败必须显式进入不完整报告或 needs-confirmation，不得静默视为成功，也不自动重跑；
- `/stop` 必须能取消 supervisor、所有 child agent 和未来的 compression task；
- WebUI 重连后能读取 phase、run id、status 和最终 report（不承诺恢复运行中的工作）；
- 代码中不出现 `/resume`、自动 workflow 重入、result reference 或跨运行恢复路径。
