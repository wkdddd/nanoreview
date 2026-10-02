# Architecture

## 当前链路

本节基于主工作区 `1675aeb5`；候选分支与目标方案不等于已落地实现。

`channels -> MessageBus -> AgentLoop(SessionCoordinator) -> AgentRunner / ReviewLoop -> MessageBus -> channels`

- `agent/loop.py`：消息与 turn 编排、session/context、取消和结果交付；review 与 conversation 的准入、路由和门禁已委托给 `session/coordinator.py`。
- `agent/review_loop.py`：一次 review run 的生命周期、状态迁移、终态持久化和结构化结果；执行委托 `agent/orchestration.py`。
- `session/coordinator.py`：进程级路由与门禁（准入调用、session 路由、命令门禁、review→conversation 交接与索引）；不调用模型、不执行工具、不建立独立持久化状态机。
- `agent/conversation_loop.py`：conversation 阶段的输入/输出契约占位，无执行。
- `agent/orchestration.py`：当前实际执行计划、reviewer 调度/收集与 finalizer/Judge，并非空壳；其迁移设计见计划。
- `agent/runner.py`：单个 agent 的模型/工具循环、运行内压缩、停止原因和 usage。
- `agent/subagent.py`：子代理任务生命周期；`agent/review_state.py`：run 状态、fingerprint 与报告 artifact。
- `review/`：`admission.py` 准入边界，`result.py` 终态结果与交接渲染，`input/`、`planning/`、`source/`、`output/` 输入、证据、源码、finding 校验、Judge 与报告领域逻辑。
- `session/`：历史持久化与回放；`agent/context.py`、`memory.py`、`autocompact.py`：提示上下文与会话整理。
- `channels/`：协议、交付和重试；`providers/`：模型调用适配；`agent/tools/`：能力与权限。
- `review-webui/`：展示与交互；`templates/`、`skills/`：模型行为契约。

仅有 review 处于 `running` 时门禁普通消息；review 进入终态且资源清理、结果持久化完成后，同 session 立即开放对话。长期目标见 `.agents/plans/project-roadmap.md`，当前代码调整计划见 `.agents/plans/code-adjustment-plan.md`。

## 本地 review 准入

- `review/admission.py` 是 WebUI、结构化 API、CLI 共用的准入边界，transport 只做协议解析、交付和状态读取；`AgentLoop.admit_review` 是各入口调用的唯一入口。
- 准入按「校验 → 快照 → 注册」一次完成：`ReviewAdmissionService.admit` 校验本地目标与 scope、采集相对 `HEAD` 的净 diff（`review/input/local_git.py`）、写入输入快照（`review/input/snapshot.py`），并持久化 session 导航 metadata；`AgentLoop` 随后注册 `ReviewRunState`。
- 拒绝是原子的：抛出 `ReviewAdmissionError`（稳定 `code` + HTTP `status`），不写 session、不建 run、不落快照、不留历史。重复提交同一 session 返回 `duplicate_review`。
- 执行只读 `review_run_id` 对应的 run；准入 turn 以 `_review_admitted` 标记放行。
- 远端 GitHub 目标本轮仍走既有计划路径，不做本地校验。
- 前端适配未完成：WebSocket 准入拒绝复用既有 `error` 事件并附带 `code`/`field`，WebUI 表单渲染结构化错误与输入回填留待前端节点。

## Review / Conversation 运行时边界

- 一个 session 首版最多一个 review run；review 只能由用户经准入入口触发，Conversation Agent 不得自主发起 review，也不允许创建纯 conversation session。
- 转入门禁：普通消息与非控制命令仅在 review `running` 时被拒，且不写入历史或 pending queue；`/status`、`/stop`、权限响应可用；review session 内 `/new` 被拒绝且不清空 session。
- 只有携带 `_review_admitted` 且匹配 live `running` run 的 turn 进入 review 管线。session 在 review 结束后仍保留 `review_target` metadata，因此持久化 target 本身不足以重入 review。
- `ReviewLoop` 是 run 状态、report artifact 与终态 metadata 的唯一写入者；`SessionCoordinator` 只读结果、写 `review_context` 索引、切换路由；`AgentRunner` 不写 review 状态。
- 阶段判断从 live `ReviewRunState`、session metadata、report artifact 和交接索引推导，不新增 session phase 字段。无 live executor 的持久化 `running` 会被一次性规范化为终态 `error`，不 resume、不重跑。
- 交接三态：`complete`（artifact 已落盘且无缺口）、`partial`（artifact 已落盘但存在缺口）、`failed`（无可用 artifact，绝不渲染为完整成功）。交接不重试、不自动补齐、不重跑；已落盘 report 仍可查看。
- 首次对话注入完整 report：system directive 说明来源与只读规则，完整 block 作为带 `injected_event=review_handoff` 的 assistant 消息写入历史（可重放、不重复注入）。完整 report 超出首次对话模型窗口时拒绝该 turn 并给出原因，不用自动摘要替代。交接失败时仍进入对话，但必须说明失败、可用结果与覆盖缺口。
- report artifact 与 `ReviewRunState` 是权威来源，Conversation Agent 不得改写；`review_context` 只是索引。
- conversation 按 session 串行、最多 20 条待处理消息；`/stop` 取消当前与排队 turn 且保留已发生的修改不自动回滚；重启后不执行未完成队列。
- 不新增第三个 Agent、通用 `BaseLoop` 或完整独立的 session 状态机。

## 模块边界

- 编排拥有生命周期和状态迁移，领域组件拥有业务算法；避免多个对象竞争写入同一状态。
- Runner 不承担 review、session 路由或 WebUI 协议；transport 细节留在 adapter。
- 持久化明确状态与历史各自的权威来源；前端不通过展示文本推断业务终态。
- 跨边界变更同时核对接口、调用方、持久化和最近的测试。具体迁移与产品限制只写入所属计划。
