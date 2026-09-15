# ReviewAgent Multi-Agent 实施后交接补充

更新时间：2026-09-15

本文件基于 `.claude/plans/reviewagent-multi-agent-handoff.md`，记录该方案执行后的实际代码状态、已知缺口和后续实施边界。原交接文档仍是目标架构的基线；本文件只补充当前实现事实，不替代原文。

## 当前基线

- 基线提交：`4d169e46 feat: ReviewAgent 收敛为一次性 review run 并持久化独立 report artifact`，新增 `nanoreview/agent/review_state.py`、report API、WebUI report fetch 和对应测试。
- 当前工作区在该提交之上另有未提交改动（22 个文件，`git diff --stat` 为 `+307/-122`），其中与本方案直接相关的是 `agent/loop.py`、`agent/orchestration.py`、`agent/review_state.py`、`review/output/judge.py`、`utils/helpers.py` 及三个对应测试文件。这部分改动已经解决了本文件早前记录的两个缺口（artifact 终态时序、usage 汇总），详见「已落地能力」。
- 工作区其余改动（`tools/registry.py`、`providers/base.py`、`cli/commands.py`、`utils/tool_hints.py`、`skills/playwright/SKILL.md`、`pyproject.toml`、文档类）属于其他批次，不在本方案范围内，不要顺手合并或回滚。
- 当前工作区验证结果（2026-09-15 实测）：`python -m pytest -q` 为 `393 passed`；`cd review-webui && bun run test` 为 `30 passed`。
- `python -m ruff check nanoreview/` 当前报 `47 errors`，与基线提交 `HEAD` 完全一致，全部为既有问题（18×E402、11×I001、9×W293 等），不是本批次引入。本方案改动涉及的文件中只有 `utils/helpers.py` 一处 `I001`，该问题在 `HEAD` 版本同样存在。不要在本方案的改动中顺带批量修复它们。

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

### Artifact 终态写入时序（工作区改动，已修复）

早前的实现先在 `run_state.status == running` 时构造 artifact，再把 run state 置为 `completed`，导致落盘 artifact 的 `status` 与 session metadata/API 外层状态不一致。

当前 `build_report_artifact()`（`agent/review_state.py:310`）新增显式 `status: ReviewRunStatus | None` 参数，`(status or state.status).value` 中 state 自身只作 fallback；`agent/loop.py:1351` 在序列化前显式传入 `status=ReviewRunStatus.COMPLETED`。artifact 写入失败时不留下 artifact，run 本身降级为 `error`。

已覆盖测试：`test_build_report_artifact_status_overrides_running_state`、`test_build_report_artifact_can_record_a_failed_run`。

### ReviewRunState usage 汇总（工作区改动，已实现）

新增共享 `merge_token_usage()`（`utils/helpers.py`），并在三层挂上累加入口：

- `ReviewerRunState.add_usage()`（`agent/review_state.py:83`）、`JudgeBatchState.add_usage()`（`:97`）、`ReviewRunState.add_usage()`（`:130`）；
- coordinator：`_collect_plan()` 在 plan accepted 后写入 `run_state.add_usage(result.usage)`；
- reviewer：`_dispatch_and_collect()` 从 `metadata["subagent_usage"]` 读取，同时写入 reviewer 级和 run 级；
- judge：`ReviewJudge.last_usage` 按 batch 用 `merge_token_usage` 累加，每次 `judge_dimensions()` 开头重置，避免跨 run 串读；orchestrator 侧把它折进 `judge_batches["judge"]` 和 run 总量。

并发安全性依赖于累加只发生在 supervisor 的单一 await 边界（`wait_for_session_result()` 返回后），不是在 reviewer task 内部，因此无需额外锁。

judge 目前聚合为单一 `"judge"` 条目，理由是 batch 只是 context window 拆分，不是独立恢复单元。这一点在迁移到 JudgeAgent 并引入稳定 batch id 时需要重新设计（见缺口 1、2）。

已覆盖测试：`test_review_run_state_add_usage_accumulates_counters`、`test_reviewer_and_judge_state_accumulate_usage`、`test_judge_usage_aggregates_every_batch`、`test_judge_usage_resets_between_runs`、`test_execute_run_aggregates_agent_usage_into_run_state`。

### Review session 消息门禁

当前门禁位于 `AgentLoop.run()`、`_dispatch()` 和 `_state_command()`：

- 内存中存在 review run 时，普通消息不会进入 pending queue；
- 已完成或失败状态会拒绝普通后续消息；
- `/status`、`/stop` 和内部 subagent/system event 保留；
- `/new` 会清理 review gate 和相关 metadata；
- session metadata gate 覆盖进程重启后没有内存 run 的场景。

## 尚未满足原始验收标准的部分

### 1. 同进程恢复尚未实现

原始交接文档要求：

```text
已完成 reviewer/batch：复用现有结果
未完成 reviewer/batch：从最近业务边界重新运行
```

当前实现只是把 reviewer/judge 状态写入 `ReviewRunState`，但 `ReviewOrchestrator._dispatch_and_collect()` 每次仍从全部 assignments 开始，未根据 `reviewers[dimension].status` 或 `judge_batches` 跳过已完成单元，也没有从保存的 `ReviewRunState` 重新进入 review pipeline 的恢复入口。

后续实现必须：

- 为 reviewer result 保存可复用的结构化 result reference 或内存结果；
- dispatch 前过滤 `completed` reviewer；
- 对未完成或失败 reviewer 重新构造 task envelope；
- judge batch 使用稳定 batch id，已完成 batch 不重复调用；
- 恢复时重新创建 asyncio task/provider 请求，不持久化运行时对象；
- 增加取消、超时、部分完成和重复派发测试。

### 2. Judge 尚未统一到 AgentRunner

`ReviewJudge._judge_batch()` 仍直接调用 `provider.chat_with_retry()`。原始目标要求：

```text
JudgeAgent -> AgentRunner.run() -> submit_verdicts -> JudgeVerdictReceiver
```

需要保留现有候选收集、context window batch 和统计逻辑，仅替换 batch 内执行入口，并接入 terminal retry、usage、checkpoint、取消和 run-level compression。

### 3. run-level compression 尚未实现

`AgentRunner._prepare_messages()` 当前仍是 orphan tool result 处理、microcompact、tool result budget 和 hard trim。尚未实现原交接文档要求的：

- 每个 run 独立 `RunCompressionState`；
- 60% 异步压缩；
- 80% 同步压缩；
- frozen/compress/active 消息区；
- 压缩失败保留原 working history；
- 压缩后仍超限时返回 `context_compression` stop reason；
- working messages 与 `AgentRunResult.messages` 分离。

### 4. supervisor 状态机尚未真正收敛

当前通用外层状态机仍是：

```text
RESTORE -> COMPACT -> COMMAND -> BUILD -> RUN -> SAVE -> RESPOND -> DONE
```

review phase 目前嵌在 `_run_agent_loop()` 内，并非独立的：

```text
PREPARE -> PLAN -> REVIEW -> FINALIZE -> SAVE -> RESPOND -> DONE
```

后续迁移必须保持 generic chat 行为不回归，同时将 review supervisor 的阶段边界、取消、恢复和 checkpoint 从 `ReviewOrchestrator` 移到 `AgentLoop`。

## 当前消息门禁的边界说明

产品规则仍是“一次 review session 只执行一次，完成后拒绝普通消息”。实现上必须继续区分三类消息：

1. 普通用户消息：运行中和终态都拒绝，不写入 session/history/pending queue；
2. `/status`、`/stop`、`/new`：走控制路径；
3. subagent/system result：允许进入当前 review supervisor 的内部收集路径。

两条门禁路径的实际行为已核对：

- 内存 gate（`agent/loop.py:1517-1530`）在 `run()` 的入站分支直接 `continue`，消息不进 pending queue、不进 session，这条路径是干净的。
- 注册 gate（`_dispatch()`，`agent/loop.py:1598-1610`）在任何 await 之前完成 review run 注册，因此同一 session 的并发 review 提交无法竞态穿过门禁；已存在 run 时直接返回 gate 响应，不落 session。
- metadata gate（`agent/loop.py:2224-2228`）位于 `_state_command()` 开头，命中后 `ctx.outbound = gate_response; return "shortcut"`。它在 `self.commands.dispatch()` **之前**返回，因此不会走到 `2240-2246` 那段 `_persist_user_message_early()` + `add_message("assistant", …)` + `sessions.save()` 的 shortcut 持久化逻辑，被拒绝的普通消息不会写入 session。

但这里存在一个真实风险，必须在后续改动中保护：这条 gate 的“不持久化”属性完全依赖它位于 `_state_command()` 中 command dispatch 之前的位置。任何把 gate 下移、或把持久化逻辑上移的重构都会静默破坏该不变量，而当前测试没有任何断言能捕捉到。

`tests/` 中目前没有针对 metadata gate 的用例（`grep` 只在 `tests/agent/test_review_state.py:63`、`tests/webui/test_review_report_api.py` 中出现 `ReviewMetaKey.STATUS`，均非 gate 行为测试）。后续必须补一条测试：metadata 处于终态的 session 收到普通消息后，返回 gate 响应且 `len(session.messages)` 不增加。

## 测试与工作区注意事项

- `tests/webui/test_review_report_api.py` 存在于当前工作区（本文件早前版本记录该文件被删除，与实际不符，已更正）。report API 测试正常参与 `pytest` 运行。
- 命令口径按 `AGENTS.md`：WebUI 使用 `bun run test` / `bun run build`，不是 `npm`。
- WebUI `bun run test` 当前为 `30 passed`（`parse-report.test.ts` 20 项 + `review-report.test.ts` 10 项）。此前记录的 Vite 产物超过 500 kB warning 未在本轮重新验证，仍按非功能阻塞处理。
- `ruff` 现存 47 个既有告警与 `HEAD` 一致，评估新改动时应对比基线而不是要求全量清零。
- 任何后续修改都必须同时检查 `.agents/architecture.md`、`.agents/security.md`、`.agents/budget.md`、session persistence 和 WebUI/API wire contract。

## 后续推荐顺序

已完成（当前工作区，待提交）：

- ~~修复 artifact 终态写入时序，并补一致性测试。~~
- ~~实现 coordinator/reviewer/judge usage 汇总到 `ReviewRunState`。~~

待办：

1. 补 metadata gate 的“不持久化”回归测试，锁定当前正确但脆弱的行为（低成本，先做，防止后续迁移静默破坏不变量）。
2. 设计 `ReviewRunState` 的可恢复 result reference 和稳定 batch id，补同进程恢复及重复派发测试。这一步会把现在聚合的单一 `"judge"` usage 条目拆成 per-batch，需同步调整 usage 归集。
3. 将 Judge batch 迁移到 `AgentRunner.run()`，保持现有 batch 拆分和 verdict contract；迁移后 `ReviewJudge.last_usage` 这个临时读取点应删除，usage 改由 `AgentRunResult.usage` 提供。
4. 在 `AgentRunner.run()` 接入 60%/80% run-level compression。
5. 将 review supervisor phase 从 `ReviewOrchestrator` 逐步迁移到 `AgentLoop`，最后删除旧 orchestrator。
6. 确认并删除无调用方的 generic chat 能力，完成 WebUI/API、文档和测试链路收敛。

顺序理由：第 2 步定义了恢复单元的边界，第 3 步的 batch 执行入口必须落在这些边界上，否则 JudgeAgent 迁移完还要为恢复再改一次。第 1 步与其余各步无耦合，可独立先行。

## 交接不变量

后续实现不得破坏以下不变量：

- 一个 review session 至多一个 review run；
- report、findings、verdicts、run state 使用同一 input fingerprint；
- 普通拒绝消息不污染 review history；
- 不持久化 asyncio task、provider client、lock、callback 或压缩 coroutine；
- artifact API 不泄露绝对路径；
- reviewer/judge 的失败必须显式进入 error 或 needs-confirmation，不得静默视为成功；
- `/stop` 必须能取消 supervisor、所有 child agent 和未来的 compression task；
- WebUI 重连后能读取 phase、run id、status 和最终 report。
