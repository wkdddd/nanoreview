# Architecture

## 当前链路

本节基于主工作区 `850aeb6b` 之上的当前实现（含 Conversation Agent 收敛改动：`AgentLoop` 已并入 `SessionCoordinator` 并删除）；候选分支与目标方案不等于已落地实现。

`channels -> MessageBus -> SessionCoordinator -> AgentRunner / ReviewLoop -> MessageBus -> channels`

- `agent/coordinator.py`：进程运行时与唯一决策点——MessageBus 接收/发送、per-session 串行与有界 pending 队列、命令分发、取消调度、review/conversation 路由与门禁、handoff 值对象（`ReviewHandoff`）与其唯一一次 session 写入（`SessionCoordinator.consume_handoff`）、结果发布。自身不调用模型、不执行工具、不写普通对话历史：review turn 整体交给 `ReviewLoop`，conversation turn 交给 `ConversationLoop`。
- `agent/review_loop.py`：一次 review run 的唯一 supervisor——准备、计划、reviewer/Judge 执行、用户消息与报告持久化、资源清理与终态写入，内部按 `PREPARE -> PLAN -> REVIEW -> FINALIZE -> CLEANUP -> DONE` 顺序执行；同时拥有状态迁移和结构化结果。
- `agent/conversation_loop.py`：一个完整 conversation turn——读取 session/历史、调用注入的 handoff consumer 完成交接写入、构建 frozen/working 上下文与 per-turn 核心 `ToolRegistry`、单次 `AgentRunner` 运行、历史持久化与回复组装；只注册通用 workspace 工具，不存在仓库级 reader 工具（`local_review` 已整体删除）。构造时必需 `handoff_consumer`，`ReviewHandoff` 只在 `TYPE_CHECKING` 下导入。连接 MCP 后把代理工具注册进该轮注册表。
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
- 准入按「校验 → 快照 → 注册」一次完成：`ReviewAdmissionService.admit` 校验本地目标与 scope、采集相对 `HEAD` 的净 diff（`review/input/local_git.py`，含 staged/unstaged/untracked）、写入输入快照（`review/input/snapshot.py`），并持久化 session 导航 metadata；`SessionCoordinator` 随后 `ReviewBoardStore.create(admission)` 建立该 run 的黑板。
- review action 只有 `diff`：`normalize_review_action` 默认 `diff`，显式 `repo` 按 `invalid_action` 拒绝；无 Git 仓库、无法读取 diff、空 diff 分别在准入阶段映射为稳定错误码（`not_a_git_repo` / `diff_unavailable` / `empty_diff`），不启动 Planner/reviewer/Judge。CLI `review` 的 `--action` 默认值同步为 `diff`。
- 拒绝是原子的：抛出 `ReviewAdmissionError`（稳定 `code` + HTTP `status`），不写 session、不建 run、不落快照、不留历史。重复提交同一 session 返回 `duplicate_review`。
- 执行只读 `review_run_id` 对应的 run；准入 turn 以 `_review_admitted` 标记放行。
- review 输入只有本地一种：`ReviewTargetType` 为 `{"auto", "local"}`，无 GitHub source/tool/cache/metadata/evidence 入口；远端 GitHub URL 不特判，退化为普通本地路径参与校验。
- 准入快照是审查的**权威输入**，不只是审计记录：PREPARE 读回该 run 的 snapshot（`ReviewSnapshotStore.read`），把其中的净 diff 作为 `ReviewMetaKey.FROZEN_DIFF`（私有的进程内键，`{"patches", "skipped"}`）交给 prefetch，`ReviewEvidenceService.dispatch` 据此**不再读取当前工作树**——准入后用户改动/提交/回滚都不会改变被审内容；快照缺失时降级为读活工作树并记 warning，不失败 run。
- **changed-file 边界同样取自快照**：`_resolve_execution_inputs` 把 snapshot 的 `changed_files` 送入 `_ReviewInputs.changed_files`，最终进入 `validator` 的 diff 边界校验（未修改文件中的 finding 判 `uncertain` 而非 accepted），并由黑板的 `changed_files` 进入 report coverage。该键从不进入 reviewer metadata。
- 净 diff 的统一形态是 **`HEAD -> worktree`**：`collect_net_diff` 与 `ReviewEvidenceService.local_changed_patches` 都用 `git diff HEAD`，staged 的中间态（`HEAD -> index`）不再与 `index -> worktree` 拼接，reviewer 不会同时看到中间态与最终态。
- **预处理只消费 diff**：`review/planning/preprocessor.py` 只做 patch 变换——patch/hunk 切分（单文件 `8_000` token 阈值）→ 按 frozen 顺序保留、放不下的记为 skipped → 稳定 `ev-NNN` ID → preview/manifest。它不枚举仓库、不读取完整源码、不做 AST 解析或语义切块，也没有 inventory、上下文 overlap 与 import/caller/test 的 related 补充；不对文件做语言分类，任何路径的 diff 都按文本证据处理（没有 `unsupported_code_type` 跳过）；`ReviewEvidenceService.dispatch` 对非 `diff` action 直接抛 `ValueError`，不保留全仓读取入口。diff 之外的上下文由 reviewer 在自己的 `restricted` scope 内定向 `read_file`/`grep` 取用，那是工具权限能力，不是预处理产物。
- PLAN 完成后，`ReviewLoop` 把最终 Planner manifest（budget/version/input_mode/retained/omitted）、预算契约与分诊审计（`planner_triage`）回写同一 run 的 snapshot（`ReviewSnapshotStore.augment`）；report artifact 另带 `snapshot_ref` 与 `coverage`（changed/skipped files、manifest、triage 覆盖、per-reviewer 状态/usage/重复读取、incomplete dimensions），不复制源码或完整会话。snapshot 回写失败只记日志，不改变 run 终态。
- 前端适配未完成：WebSocket 准入拒绝复用既有 `error` 事件并附带 `code`/`field`，WebUI 表单渲染结构化错误与输入回填留待前端节点。

## review run 黑板与结构化输出

一个 review run = 一块黑板（`agent/review_board.py`）。`ReviewBoardStore` 由 `SessionCoordinator` 持有并注入 `ReviewLoop`；store 是唯一写入者，`ReviewLoop` 只能通过 `ReviewBoardWriter` 请求写入、通过 `ReviewBoardView` 读取。

- 生命周期：`admit()` → `store.create(admission)`；`/new` → `store.reset()`（丢弃板并清掉 session 上的 run 标识）；`coordinator._release_unstarted_run()` 在 `_dispatch` 与 `process_direct`（含优先级命令分支）的 finally 调用，按 `msg.metadata["_review_admitted"]` 只释放本 turn 自己那个仍停在 `PREPARE` 的 run，并同步清除持久标识——只丢板会留下一个无人能 settle 的 `running` run，使该 session 之后所有提审被当作重复。不传 run_id 的路径必须显式给出归属，避免误删同一 session 里「刚被受理、消息还在队列」的新 run。
- 写接口（全部 async，全部返回前先过 run 锁）：`advance(phase)`、`record_accepted_input(...)`、`record_triage(triage)`、`start_assignments(assignments)`、`reviewer_started(dimension, evidence_assigned=)`、`reviewer_settled(dimension, status=, error=, usage=, duplicate_reads=, duplicate_searches=)`、`record_dimension(result)`、`judge_started(batch_id)`、`judge_settled(batch_id, status=, error=, stats=, usage=)`、`record_judged(dimensions, stats)`、`add_warning(message)`、`record_summary(text)`、`add_usage(usage)`、`record_report(report_markdown=, findings=, verdicts=)`、`persist_metadata()`、`settle(status)`。
- `record_summary` 只承载有界原因（stop/error/未启动就被取消）：没产出 report 的 run 也要在 session metadata 里解释自己，而 `metadata_payload` 的 `error` 只在 `error`/`stopped` 且已有 warning 时才写；后续 `record_report` 会用 report 摘要覆盖它。
- 不变量：① `record_dimension` 按 dimension 替换而非追加；② `reviewer_settled` 到 `completed` 是终态，重复 settle 不改状态但 usage 仍累加，因此一个 assignment 不会被重复计数；③ `record_judged` 一次性替换候选集，report 与落盘 verdicts 读同一版本；④ `settle` 先持久化 session metadata、成功后才把 run 标成终态并写 `DONE`，失败抛 `ReviewBoardPersistenceError` 并把 run 留在 `running`；⑤ `settle` 到非 `completed` 时把仍 `pending`/`running` 的 child 记为 `error`/`stopped`，审计里不出现"已关闭的 run 下有活 child"。
- 结构化输出契约在 `review/contracts.py`，由 `agent/output_contract.py::OutputContract` 承载，纯函数、无状态：每次 `validate()` 重新解析、重新校验，被拒的尝试不可能因为复用对象而在下一次变成通过。

## Review / Conversation 运行时边界

- 一个 session 首版最多一个 review run；review 只能由用户经准入入口触发，Conversation Agent 不得自主发起 review，也不允许创建纯 conversation session。
- 当前转入行为（待按计划调整）：普通消息与非控制命令在 review `running` 时被拒，且不写入历史或 pending queue；`/status`、`/stop` 可用；review session 内 `/new` 被拒绝且不清空 session。
- 只有携带 `_review_admitted` 且匹配 live `running` run 的 turn 进入 review 管线。session 在 review 结束后仍保留 `review_target` metadata，因此持久化 target 本身不足以重入 review。
- `/stop` 先取消活动 turn task 与 subagent，再兜底收尾：取消后若 session 仍有 live `running` review run（典型是 turn 已结束但清理/终态保存失败留下的残留），补一次 `finalize(STOPPED)` 并如实报告结果；无残留 run 时 `finalize` 是 no-op，不影响纯 conversation session。取消发生在 turn 尚未进入 review 管线（还在等 session lock 或并发 gate）时，已准入的 `PREPARE` run 也被收尾为 `stopped`，不被静默丢弃。
- **黑板是每个 run 唯一的可变权威记录，由 `SessionCoordinator` 唯一写入**：`agent/review_board.py::ReviewBoardStore` 持有 `ReviewRunBoard`，每次写入都过该 run 的 `asyncio.Lock` 串行化，写入完成后返回冻结投影 `ReviewBoardView`；锁从不跨模型或工具调用持有（只在 agent run 返回之后记结果）。
- `ReviewLoop` 只有阶段编排权：它拿到 `ReviewBoardWriter`（窄写请求接口，只能请求 coordinator 更新）与 `ReviewBoardView`（冻结只读投影，reviewer/judge 状态是不可变快照、集合是 tuple），**没有**可直接修改黑板的引用。report artifact、终态 metadata 与 `review_context` 索引的写入也都经 store。
- 角色权限：reviewer 既不读也不写黑板，只拿本次 assignment 的冻结输入；Planner 与 Judge 只读本 run 授权数据；Conversation Agent 从不接触黑板，只拿最终 report。`AgentRunner` 不写 review 状态。
- 黑板统一承载运行状态，因此旧的 `ReviewRunState` 可变容器与重复结果存储已删除；黑板**不是**恢复执行的 checkpoint（重启后只有终态 session metadata 被恢复，持久化 `running` 且无 live 黑板一律规范化为 `error`，不 resume、不重跑）。
- review 固定按 `PREPARE -> PLAN -> REVIEW -> FINALIZE -> CLEANUP -> DONE` 顺序推进，每步只更新黑板的 `phase`，不引入状态转移表或通用 `BaseLoop`。成功、错误和 `/stop` 三种出口都先经过 `CLEANUP` 再在 `DONE` 写终态：正常为 `completed`、致命失败为 `error`、取消为 `stopped`。清理未确认完成或终态保存失败时不发布 `DONE`：run 保持 `running`、门禁保持关闭，并把有界错误交给 turn（`finalize()` 此时返回 `None`）。report stream 等 transport 事件由上层交付，不属于 review phase。
- `REVIEW` 阶段内部固定为「并发 dispatch reviewer → 全部收齐 → 校验 finding → 按 context window 切候选批 → 逐批判定」：reviewer 受并发上限约束并行执行；Judge 只在所有 reviewer 终态、候选集完整后才运行。Judge 的“批”是对已收齐候选池按 token 预算的切分，不是 reviewer 到达流；各批串行执行，批间 verdicts 合并与顺序无关。
- **仓库级 reader 工具（`local_review`）已从代码库整体删除**：工具本身、`ReviewToolBase`、`review/source/local.py::LocalRepoReader` 与 `skills/rag` 一并移除，`_CONVERSATION_DENIED_TOOLS` 随之取消。Review 流程的模型工具集因此天然不含它：Planner 只注册 `list_review_diff`/`read_review_diff`（仅在输入为 paged 形态时），四类 reviewer subagent 只有 `grep`/`list_dir`/`read_file`，Judge 注册空工具表；证据由 `ReviewEvidenceService` 作为 ReviewLoop 的内部依赖直接提供（prefetch 与 Planner manifest 用同一份授权 evidence），不依赖任何工具 wrapper 注册。ReviewLoop 的 coordinator/reviewer prompt 与 task 也不建议模型调用任何仓库 reader 工具。
- **Planner 阶段是真实分诊，不是 assignment 提交**：`review/planning/triage.py::TriageReceiver` 仍是唯一业务契约，但 Planner 不再增量提交——它的**最终答案**是一个 `{"decisions": [...]}` JSON 对象，由 `review/contracts.py::planner_contract` 逐条喂进一个**每次尝试新建**的 `TriageReceiver`（一条决策可覆盖多个 evidence 与多个维度、每个 evidence 只能分诊一次），全部接受后 `finish()`；`review_loop._collect_plan` 按维度聚合出 assignment，Planner 不维护第二份 assignment。覆盖状态（`assigned`/`dismissed`/`unexamined`）由程序从 decisions 派生，不由模型自报；未读或未上报的 evidence 记录 `unexamined` 且**不自动补派**。
- **无自动补派**：不因静态关键词、`risk_level` 或 `unexamined` evidence 增加 reviewer。`special`/`general` 下用户所选维度必须被至少一条 decision 命名，否则整个 decision set 被拒——用户选择靠拒绝 payload 强制，程序不给 reviewer 加塞 evidence。
- **Planner 输入分两种形态，由证据总量决定，不由单文件 8k 阈值决定**：`ReviewEvidenceBundle.input_mode` 由预处理 mode 给出；`direct` 内联全部 evidence 全文，`paged` 只渲染索引并由 Planner 经受限 `list_review_diff`/`read_review_diff` 分页读取。这两个工具只由 frozen evidence 构造、无文件系统访问，不能读任意路径、不能创建 evidence、不能越出准入 diff。reader 的分页预算对齐运行期 `max_tool_result_chars`，避免 `AgentRunner` 在 reader 声称已读后再截断页面。
- **review 全链路不再有 terminal tool 提交协议**：Planner、reviewer、Judge 都是自由回合，最终答案必须是契约要求的一个 JSON 对象。`AgentRunSpec.output_contract` + `output_retry_limit` 承担全部校验与纠正：解析失败时把具体原因（走 `OutputContract.repair_prompt`）作为一条 user 消息追加进同一 run 的上下文，模型可在同一 run 内修正；配额耗尽后 `stop_reason="invalid_output"`、`output_error` 非空、`structured_output is None`。旧的 `terminal_tools` / `terminal_retry_limit` / `prose_retry_limit` / `reserve_terminal_iteration` / `terminal_attempts` / `terminal_error` 已全部删除。Planner 请求上限 24（`_PLANNER_MAX_ITERATIONS`）、输出纠正配额 5（`_PLANNER_OUTPUT_RETRY_LIMIT`）；reviewer 请求上限 30、Judge `_JUDGE_MAX_ITERATIONS = 3 + 3`（纠正配额 3 加 3 轮余量）。
- **最后一次请求留给答案**：`AgentRunner._is_final_structured_turn()`（`iteration == max_iterations - 1` 且 run 有 output contract）在该轮不暴露工具，并把 `FINAL_STRUCTURED_ANSWER_PROMPT` 同时写进 raw `messages` 与 `state.working`（后者才是 `_model_context` 真正发送的内容）。该轮被拒时直接以 `invalid_output` 收场，不再追加一轮发不出去的纠正提示，也不能落回 `max_iterations` 掩盖真实原因。Conversation（无契约）不保留任何轮次。
- **`risk_hints` 已从运行模型完全删除**：静态关键词表、检测函数、`_score_unit` 评分、`CodeUnit`/`EvidenceReference`/manifest/prefetch 的字段与渲染全部移除；evidence 按 frozen diff 稳定顺序截尾而非按风险排序，`matched` 仅作展示元数据，不参与排序或路由。
- **Planner 分诊结论不是 finding**：黑板的 `triage` 与 snapshot `planner_triage` 只做运行审计（排障/评测）；decision、`risk_level`、`rationale` 不进入 report findings。规划主动不派任何 reviewer 时，报告显式输出 Triage Outcome 而非 `incomplete`，run 仍以 `completed` 结算并落 report artifact。
- **reviewer 的最终答案仍然由结构决定**：`review/contracts.py::reviewer_contract` 解析 `{"findings": [...]}` 并复用原提交工具的字段契约（`severity` 必须在 `critical|high|medium|low`、`line` 为整数或 null、`details` 必须是对象、文本字段不能为空）。一次被拒只说明哪一条 finding 的哪个字段不合法，模型在同一个 AgentRun 内改一处再答；空 `findings` 是否成立由程序判断（需要 `assigned_evidence` 或一次成功的 `read_file`/`grep`），模型自报不算。
- reviewer run 自带去重 ledger（每个 subagent 一个独立 `FileStates`）：相同规范化文件范围/检索签名且内容未变时返回短提示而非全文，`force` 不可绕过，状态不跨 reviewer 或 review run 共享；用于定位重复探索，不新增累计 token 上限。
- 阶段判断从 live 黑板投影、session metadata、report artifact 和交接索引推导，不新增 session phase 字段。终态 metadata 先落盘、后发布 live `DONE`，因此“可路由”等价于“已持久化”。无 live executor 的持久化 `running` 会被一次性规范化为终态 `error`，并持久化有界中断原因，不 resume、不重跑。规范化在 `pending_handoff()` 里先于 `_review_settled()` 执行（`result()` 提前调用），保证重启后第一条普通消息就修复 orphan，而非永远卡在 `running`。规范化的 `save()` 失败必须回滚 metadata（缓存不提前发布 `error/done`），保持磁盘与缓存一致，留待下次读重试。
- 终态 metadata 除 run id/status/phase/fingerprint/report ref 外，还持久化有界 `review_summary`（report 摘要或失败原因）与 `review_error`（非 `completed` 终态的原因），供重启后读取；不持久化 child transcript 或其他中间状态。
- 交接三态：`complete`（artifact 已落盘且无缺口）、`partial`（artifact 已落盘但存在缺口）、`failed`（无可用 artifact，绝不渲染为完整成功）。交接不重试、不自动补齐、不重跑；已落盘 report 仍可查看。
- 首次对话注入完整 report：system directive 说明来源与只读规则，完整 block 作为带 `injected_event=review_handoff` 的 assistant 消息写入历史（可重放、不重复注入）。写入由 `SessionCoordinator.consume_handoff` 执行，turn 顺序固定为「加载 session → 恢复中断历史 → 执行 token Consolidator → 调用 handoff consumer（处理 review handoff）→ 读取历史 → 构建模型上下文」；写入失败向上传播且不进入 Runner。完整 report 超出首次对话模型窗口时拒绝该 turn 并给出原因，不用自动摘要替代。review 完成 `DONE` 后，交接失败仍可进入对话，但必须说明失败、可用结果与覆盖缺口。
- report artifact 与黑板是权威来源，Conversation Agent 不得改写；`review_context` 只是索引。
- conversation 按 session 串行、最多 20 条待处理消息；`/stop` 取消当前与排队 turn 且保留已发生的修改不自动回滚；重启后不执行未完成队列。
- CLI 交互入口：`cli/commands.py` 的 `review` 在准入成功后于同一 `cli:review:<id>` session 执行 review，终态后默认进入交互对话（`agent` 的交互模式复用同一 `_run_cli_session`）；交互阶段以 `session_key_override` 固定到该 session，因此命令路由、review 门禁和 report handoff 与其他 channel 一致。`--no-chat`、stdin/stdout 非终端或无控制台时的 `--fail-on` 调用保持一次性执行。review 报告经 `process_direct` 返回值交给 CLI 渲染（CLI 不请求 bus 报告流）。
- 不新增第三个 Agent、通用 `BaseLoop` 或完整独立的 session 状态机。

## 模块边界

- 编排拥有生命周期和状态迁移，领域组件拥有业务算法；避免多个对象竞争写入同一状态。
- Runner 不承担 review、session 路由或 WebUI 协议；transport 细节留在 adapter。
- 持久化明确状态与历史各自的权威来源；前端不通过展示文本推断业务终态。
- 跨边界变更同时核对接口、调用方、持久化和最近的测试。具体迁移与产品限制只写入所属计划。
