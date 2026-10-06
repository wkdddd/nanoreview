# Architecture

## 当前链路

本节基于主工作区 `850aeb6b` 之上的当前实现（含 Conversation Agent 收敛改动：`AgentLoop` 已并入 `SessionCoordinator` 并删除）；候选分支与目标方案不等于已落地实现。

`channels -> MessageBus -> SessionCoordinator -> AgentRunner / ReviewLoop -> MessageBus -> channels`

- `agent/coordinator.py`：进程运行时与唯一决策点——MessageBus 接收/发送、per-session 串行与有界 pending 队列、命令分发、取消调度、review/conversation 路由与门禁、handoff 值对象（`ReviewHandoff`）与其唯一一次 session 写入（`SessionCoordinator.consume_handoff`）、结果发布。自身不调用模型、不执行工具、不写普通对话历史：review turn 整体交给 `ReviewLoop`，conversation turn 交给 `ConversationLoop`。
- `agent/review_loop.py`：一次 review run 的唯一 supervisor——准备、计划、reviewer/Judge 执行、用户消息与报告持久化、资源清理与终态写入，内部按 `PREPARE -> PLAN -> REVIEW -> FINALIZE -> CLEANUP -> DONE` 顺序执行；同时拥有状态迁移和结构化结果。
- `agent/conversation_loop.py`：一个完整 conversation turn——读取 session/历史、调用注入的 handoff consumer 完成交接写入、构建 frozen/working 上下文与 per-turn 核心 `ToolRegistry`、单次 `AgentRunner` 运行、历史持久化与回复组装；不含 `local_review`。构造时必需 `handoff_consumer`，`ReviewHandoff` 只在 `TYPE_CHECKING` 下导入。连接 MCP 后把代理工具注册进该轮注册表。
- `agent/runner.py`：单个 agent 的模型/工具循环、运行内压缩、停止原因和 usage；不感知 review 业务，完整未截断工具结果只对调用方经 `AgentRunSpec.preserve_tool_result_tools` 显式声明的工具保留，默认不保留。
- `agent/subagent.py`：子代理任务生命周期；`agent/review_state.py`：run 状态、fingerprint 与报告 artifact。
- `review/`：`admission.py` 准入边界，`result.py` 终态结果与交接渲染，`input/`、`planning/`、`source/`、`output/` 输入、证据、源码、finding 校验、Judge 与报告领域逻辑。
- `session/`：历史持久化与回放；`agent/context.py`、`memory.py`：提示上下文与会话整理（会话整理只由 `Consolidator` 按 token 触发）。
- `channels/`：协议、交付和重试；`providers/`：模型调用适配；`agent/tools/`：能力与 workspace scope。
- `review-webui/`：展示与交互；`templates/`、`skills/`：模型行为契约。

## MCP 边界

- `agent/tools/mcp.py` 自本机 nanobot `432421bc` 提取适配，不产生对 nanobot 包或目录的运行依赖。恢复 stdio、SSE、Streamable HTTP 三种 transport，以及 tools/resources/prompts、图片结果和 headers 鉴权；OAuth、热重载、插件 MCP 配置与管理界面不在本轮。
- `MCPProvider` 由 `SessionCoordinator` 独立装配，自持专用 `ToolRegistry` 与连接。**MCP 工具不注册到 `coordinator.tools`**，因此 planner、reviewer、Judge 及 `reviewer_execution_profiles()` 构造的 subagent 注册表均不可见。
- `ConversationLoop` 每轮先完成连接准备，再向该轮独立注册表注册 `MCPToolProxy`。代理只保存工具**定义快照**，执行时经 `Provider.resolve_wrapper()` 查找活包装器，因此服务端重连后已进入的 turn 也会走新连接，不会继续使用失效 session。
- 连接由创建它的 task 负责关闭（`_OwnedMCPConnection`，受 AnyIO cancel scope 约束）。单服务连接准备限时 `CONNECT_TIMEOUT_SECONDS`（30 秒），超时或失败只清理该服务并继续其他服务，未连接服务在下一轮重试；连接准备失败不使 conversation turn 失败。
- `SessionCoordinator.aclose()` 是幂等关闭入口，顺序固定为：停止准入 → 取消并等待活动 turn 与子任务 → 清空 pending queue → 排空后台任务 → 关闭 MCP。Gateway、CLI 与 API cleanup 统一调用，SDK 调用方需显式关闭。`/stop` 与 API 请求取消通过 `task_is_cancelling()` 区分外部取消，不重试被取消的 MCP 调用。

当前代码在 review `running` 时拒绝普通消息和非控制命令；只有清理完成且终态 metadata 已成功落盘（phase `done`）才发布 live `DONE` 并路由到 conversation。清理失败、再次取消或终态保存失败时 run 保持 `running`、门禁不开放，并向 turn 返回有界错误；已准入且停在 `PREPARE` 的 run 同样收尾为 `stopped`/`error` 及原因。失败原因与结果摘要随终态持久化，供重启后读取；无 live executor 的持久化 `running` 仍一次性规范化为 `error`。长期目标见 `.agents/plans/project-roadmap.md`。

## 本地 review 准入

- `review/admission.py` 是 WebUI、结构化 API、CLI 共用的准入边界，transport 只做协议解析、交付和状态读取；`SessionCoordinator.admit_review` 是各入口调用的唯一入口。
- 准入按「校验 → 快照 → 注册」一次完成：`ReviewAdmissionService.admit` 校验本地目标与 scope、采集相对 `HEAD` 的净 diff（`review/input/local_git.py`）、写入输入快照（`review/input/snapshot.py`），并持久化 session 导航 metadata；`SessionCoordinator` 随后注册 `ReviewRunState`。
- 拒绝是原子的：抛出 `ReviewAdmissionError`（稳定 `code` + HTTP `status`），不写 session、不建 run、不落快照、不留历史。重复提交同一 session 返回 `duplicate_review`。
- 执行只读 `review_run_id` 对应的 run；准入 turn 以 `_review_admitted` 标记放行。
- review 输入只有本地一种：`ReviewTargetType` 为 `{"auto", "local"}`，无 GitHub source/tool/cache/metadata/evidence 入口；远端 GitHub URL 不特判，退化为普通本地路径参与校验。
- 前端适配未完成：WebSocket 准入拒绝复用既有 `error` 事件并附带 `code`/`field`，WebUI 表单渲染结构化错误与输入回填留待前端节点。

## Review / Conversation 运行时边界

- 一个 session 首版最多一个 review run；review 只能由用户经准入入口触发，Conversation Agent 不得自主发起 review，也不允许创建纯 conversation session。
- 当前转入行为（待按计划调整）：普通消息与非控制命令在 review `running` 时被拒，且不写入历史或 pending queue；`/status`、`/stop` 可用；review session 内 `/new` 被拒绝且不清空 session。
- 只有携带 `_review_admitted` 且匹配 live `running` run 的 turn 进入 review 管线。session 在 review 结束后仍保留 `review_target` metadata，因此持久化 target 本身不足以重入 review。
- `/stop` 先取消活动 turn task 与 subagent，再兜底收尾：取消后若 session 仍有 live `running` review run（典型是 turn 已结束但清理/终态保存失败留下的残留），补一次 `finalize(STOPPED)` 并如实报告结果；无残留 run 时 `finalize` 是 no-op，不影响纯 conversation session。取消发生在 turn 尚未进入 review 管线（还在等 session lock 或并发 gate）时，已准入的 `PREPARE` run 也被收尾为 `stopped`，不被静默丢弃。
- `ReviewLoop` 是 run 状态、report artifact 与终态 metadata 的唯一写入者；`SessionCoordinator` 只读结果、写 `review_context` 索引、切换路由；`AgentRunner` 不写 review 状态。
- review 固定按 `PREPARE -> PLAN -> REVIEW -> FINALIZE -> CLEANUP -> DONE` 顺序推进，每步只更新 `ReviewRunState.phase`，不引入状态转移表或通用 `BaseLoop`。成功、错误和 `/stop` 三种出口都先经过 `CLEANUP` 再在 `DONE` 写终态：正常为 `completed`、致命失败为 `error`、取消为 `stopped`。清理未确认完成或终态保存失败时不发布 `DONE`：run 保持 `running`、门禁保持关闭，并把有界错误交给 turn（`finalize()` 此时返回 `None`）。report stream 等 transport 事件由上层交付，不属于 review phase。
- `REVIEW` 阶段内部固定为「并发 dispatch reviewer → 全部收齐 → 校验 finding → 按 context window 切候选批 → 逐批判定」：reviewer 受并发上限约束并行执行；Judge 只在所有 reviewer 终态、候选集完整后才运行。Judge 的“批”是对已收齐候选池按 token 预算的切分，不是 reviewer 到达流；各批串行执行，批间 verdicts 合并与顺序无关。
- 阶段判断从 live `ReviewRunState`、session metadata、report artifact 和交接索引推导，不新增 session phase 字段。终态 metadata 先落盘、后发布 live `DONE`，因此“可路由”等价于“已持久化”。无 live executor 的持久化 `running` 会被一次性规范化为终态 `error`，并持久化有界中断原因，不 resume、不重跑。规范化在 `pending_handoff()` 里先于 `_review_settled()` 执行（`result()` 提前调用），保证重启后第一条普通消息就修复 orphan，而非永远卡在 `running`。规范化的 `save()` 失败必须回滚 metadata（缓存不提前发布 `error/done`），保持磁盘与缓存一致，留待下次读重试。
- 终态 metadata 除 run id/status/phase/fingerprint/report ref 外，还持久化有界 `review_summary`（report 摘要或失败原因）与 `review_error`（非 `completed` 终态的原因），供重启后读取；不持久化 child transcript 或其他中间状态。
- 交接三态：`complete`（artifact 已落盘且无缺口）、`partial`（artifact 已落盘但存在缺口）、`failed`（无可用 artifact，绝不渲染为完整成功）。交接不重试、不自动补齐、不重跑；已落盘 report 仍可查看。
- 首次对话注入完整 report：system directive 说明来源与只读规则，完整 block 作为带 `injected_event=review_handoff` 的 assistant 消息写入历史（可重放、不重复注入）。写入由 `SessionCoordinator.consume_handoff` 执行，turn 顺序固定为「加载 session → 恢复中断历史 → 执行 token Consolidator → 调用 handoff consumer（处理 review handoff）→ 读取历史 → 构建模型上下文」；写入失败向上传播且不进入 Runner。完整 report 超出首次对话模型窗口时拒绝该 turn 并给出原因，不用自动摘要替代。review 完成 `DONE` 后，交接失败仍可进入对话，但必须说明失败、可用结果与覆盖缺口。
- report artifact 与 `ReviewRunState` 是权威来源，Conversation Agent 不得改写；`review_context` 只是索引。
- conversation 按 session 串行、最多 20 条待处理消息；`/stop` 取消当前与排队 turn 且保留已发生的修改不自动回滚；重启后不执行未完成队列。
- 不新增第三个 Agent、通用 `BaseLoop` 或完整独立的 session 状态机。

## 模块边界

- 编排拥有生命周期和状态迁移，领域组件拥有业务算法；避免多个对象竞争写入同一状态。
- Runner 不承担 review、session 路由或 WebUI 协议；transport 细节留在 adapter。
- 持久化明确状态与历史各自的权威来源；前端不通过展示文本推断业务终态。
- 跨边界变更同时核对接口、调用方、持久化和最近的测试。具体迁移与产品限制只写入所属计划。
