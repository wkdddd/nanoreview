# 当前代码调整计划

更新时间：2026-10-04

## 当前节点：roadmap 第 3 阶段 Conversation Agent

实施 Conversation Agent 的完整对话 turn，将 session 调度与双 Agent 协调收敛到 `SessionCoordinator`，完整迁移并删除 `AgentLoop`。本节点按已确认方案实施；当前代码状态仍以工作区为准。

### 已确认职责与边界

- 将 `SessionCoordinator` 从 `nanoreview/session/coordinator.py` 移至 `nanoreview/agent/coordinator.py`，保留类名并删除旧模块及其导入路径；不再单独建立 `SessionRuntime`。
- `SessionCoordinator` 作为 session 核心入口，承接 MessageBus 收发、session 锁与最多 20 条的待处理队列、命令与权限响应、取消调度、review/conversation 路由与门禁、handoff 准备及最终结果发布。进入 review turn 后，它不构造或保存 review 上下文，只接收并发布 `ReviewLoop` 结果（包括报告分块推流）；它可读取 session metadata、维护 review 索引，但不承担普通对话 turn 的历史构造、追加和保存。按职责拆分私有 helper。
- Coordinator 调用 `ReviewLoop` 或 `ConversationLoop`，不把 Agent 执行算法并入自身。`ReviewLoop` 负责完整的 review turn：从 session 构造 `ContextBuilder` + `COMMON_RULES` 上下文，保存用户消息和报告，管理 review 生命周期、run 状态和 report artifact，并返回结果；`AgentRunner` 继续执行单个模型/工具 run。
- `ConversationLoop` 拥有完整对话 turn：读取 session/history、写入并消费 handoff、构造上下文与工具、调用 `AgentRunner`、错误与取消收尾、保存历史及组装回复。它使用现有 `SessionManager`，不建立第二套存储。
- review 未完成清理、终态及结果持久化前继续拒绝普通消息；完成 `DONE` 后才路由到 conversation。首次 handoff 注入完整 report 或既有的有界失败上下文，报告只读且超出上下文预算时拒绝该 turn。
- Review Agent 与 Conversation Agent 保持独立 prompt、上下文、`ToolRegistry` 和权限执行 profile；Conversation Agent 沿用通用 core（含 `message`、`spawn` 和已注册插件），排除 review 专属工具，权限行为沿用现有 `approval_enabled`。工作区解析与路径限制复用 nanobot，目标仓库 root 作为文件路径基准和默认命令 cwd。
- 以现有继承 nanobot 的 `AgentLoop` 为迁移起点，复用其消息注入、流式输出、保存和交付策略；本次删除 `nanoreview/agent/loop.py`、`AgentLoop` 类及其导出、导入，不保留兼容门面，不引入正式 conversation 状态机。
- `/stop` 取消当前 turn 和队列，不回滚已发生修改；进程重启后不恢复或重跑未完成 turn。

### ConversationLoop 具体实现契约

采用 nanobot 式单轮处理接口。Coordinator 先串行准入，再调用 ConversationLoop；一个 session 可执行多个 turn，每个 turn 调用一次 Runner，每个 run 内可多次调用模型和工具。核心接口如下，执行配置及现有回调按需显式传入，名称可按现有实现微调：

```python
async def process_message(
    self,
    msg: InboundMessage,
    *,
    session_key: str,
    turn_id: str,
    target_root: Path,
    handoff: ReviewHandoff | None = None,
) -> OutboundMessage | None:
    ...
```

- 删除占位的 `ConversationTurnRequest`、`ConversationTurnResult`，不新增外部 `ConversationTurnContext` 或回调包装类。输入复用 `InboundMessage`，Runner 输出复用 `AgentRunResult`，最终回复复用 `OutboundMessage`。
- 内部使用私有 `_TurnContext`，保存本轮 Session、history、消息分区、工具注册表、Runner 结果、瞬时 finding 引用和回复状态。每次调用独立创建；不把本轮可变状态保存在共享 Loop 实例中，不持久化该对象。
- Coordinator 在 session 锁内完成路由、门禁及 handoff 准备，传入已确定的 session/turn 标识、目标 root 和一致的模型/预算配置。它负责读取权威 report 并准备只读 handoff；ConversationLoop 不决定是否开放 conversation，也不再次读取 report artifact。
- ConversationLoop 通过 `SessionManager` 取得 Session。首次 handoff 在完整报告预算检查通过后，由 Loop 写入可重放的历史消息和已消费标记并保存，再读取 history；超窗拒绝不得消费 handoff。后续 turn 不重复注入。
- 使用 `ContextBuilder.build_partitioned_messages()`：system prompt、共同规则、技能和当前 user/media 位于 frozen 区，history 位于 working 区；handoff directive 追加到 frozen system message。不得注入完整 reviewer/Judge transcript，保留既有历史整理接线，长对话治理调整仍留待后续节点。
- 为本轮创建独立 `ToolRegistry`，通过现有 `ToolLoader` 加载 core，仅排除 `local_review`、`github_review`。`submit_review_plan`、`submit_verdicts` 的实际名称均为 `_plugin_discoverable=False`，本来不会自动加载；`review_submit` 只在 reviewer scope 中，不进入 core。绑定 request context、工作区及 `FileStateStore.for_session(session_key)`；`message`/`spawn` 的发送和子任务依赖由统一装配接入，不新增 MessageBus 消费循环。
- 组装 `AgentRunSpec` 调用共享 Runner。模型/工具迭代、运行内压缩、usage 和逐工具 permission callback 保留在 Runner；权限响应、进度/流式发布和 pending injection 通过现有回调接入 Coordinator。
- 复用 nanobot 的 pending 注入策略：执行中消费的追加消息进入当前 run，未消费消息继续由 Coordinator 调度。历史增量包含这些用户消息及新增 assistant/tool 消息，不重复保存当前首条用户消息、frozen 内容或旧 history；增量边界留在 Loop 内部。
- Loop 保存本轮 user 消息、增量及必要的错误/停止内容，执行既有裁剪、清洗、file cap 和 `SessionManager.save()`，然后组装回复。保留 message 工具已发送时的重复回复抑制、媒体交付和流式内容替换行为。
- 正文实时流式发送，stream segment 结束不等于 turn 成功。保存失败须明确报错并阻止成功终态；正常返回 `None` 可表示工具已发送、无需再发正文，不得据此推断失败。usage、stop reason 等通过内存回调或现有 turn 生命周期事件交给 Coordinator，Coordinator 发布最终回复和 turn_end。
- 取消时复用现有 runtime checkpoint：由 ConversationLoop 保存可用历史，并在下一个 turn 补齐中断 turn 的占位历史，不重跑该 turn；在 `finally` 中清理本轮绑定后传播取消。Coordinator 负责取消队列、关闭交付和释放调度资源。收尾保存失败须可见，不能吞掉取消；这不新增恢复 checkpoint，也不回滚修改。
- 私有 helper 按读取、构造、执行、保存、回复和清理组织流程，不引入状态转移表、通用 `BaseLoop` 或 `ConversationRunState`。拒绝一次工具权限只产生 denied 结果供模型继续，`/stop` 才停止整轮。

因此一次普通对话 turn 的实际顺序固定为：

```text
Coordinator.receive
  -> route/gate/lock/queue
  -> prepare read-only handoff + resolve turn configuration
  -> ConversationLoop.process_message(InboundMessage)
       -> load Session + persist one handoff + read history
       -> build ContextBuilder partition
       -> create core-only tools + bind FileStates/permissions
       -> AgentRunner.run(AgentRunSpec)
       -> collect turn output + persist history + SessionManager.save
       -> assemble OutboundMessage or None
       -> finally cleanup
  -> publish outbound/turn_end
```

API/CLI 的 `process_direct()` 归 Coordinator：转换为 `InboundMessage` 后等待 session 锁串行执行，返回自己的回复，不注入正在运行的 turn，再调用两个 Loop；ConversationLoop 不提供绕过调度的直接入口。

- Coordinator 将 system/subagent 消息作为内部事件放过门禁；conversation 阶段交给 `ConversationLoop.process_message`，按 nanobot 方式以 assistant 角色运行一个 turn；review 阶段继续走 `result_callback` 和队列。

### 共享规则文件

- 将 workspace 根目录的 `SOUL.md` 和模板 `templates/SOUL.md` 改为 `COMMON_RULES.md`，通过 `ContextBuilder.BOOTSTRAP_FILES` 读取；不再回退读取旧 `SOUL.md`。`MemoryStore.read_soul` 当前没有调用方，不作为 SOUL 接入点；是否清理无调用 API 需随调用方核对。
- 重写模板内容，使其约束 Review Agent 与 Conversation Agent 共同遵守的代码规范、证据要求和审查原则；去掉“只能分析、不能修改代码”及“收到 review target 立即开始 review”等角色专属规则。未来可另增 `REVIEW_RULES.md` 与 `CONVERSATION_RULES.md`。
- Conversation 和 planner 每次通过 `ContextBuilder.BOOTSTRAP_FILES` 在 turn/run 开始读取一次 `COMMON_RULES.md`；reviewer 的 `prompt_builder` 和 Judge 的 `_system_prompt` 也分别在每个 run/batch 新增读取接线。内部模型迭代保持该快照；后启动的执行读取最新文件。缺失或读取失败时跳过附加规则并记录警告。固定系统指令及安全、权限、报告只读边界优先；不记录规则 hash。Consolidator 等历史维护调用不注入该规则。
- 已有 workspace 中改过的 `SOUL.md` 内容不自动迁移到 `COMMON_RULES.md`；迁移或重写需显式处理。保留 `MemoryStore` 作为可扩展的 memory 文件抽象，更新 memory skill 文档及相关测试，移除对 `SOUL.md` 的说明。

### Conversation 历史与观测

- Session history 是对话回放来源，不维护独立 conversation sidecar，不新增文件修改或 shell 命令的持久化审计。
- 不为审计增加 Git 前后快照、FileStates turn 写路径收集或 shell 退出码解析；保留现有工具事件、进度、错误日志及历史中的工具交互。
- turn/session/run 标识用于日志和事件关联；usage、停止原因沿用既有观测契约，不新增持久化审计字段。
- 本决定不删除既有 WebUI transcript、subagent trace 等存储；其消费者和清理仍沿用现有机制。不持久化 task、future、lock 或可恢复执行的 runtime 状态；现有 runtime checkpoint 仅保存历史补全信息，不用于恢复执行。
- 删除状态机后默认不再生成 `turn_trace` 字段；`websocket.py` 和 WebUI `types.ts` 的现有消费者记录为前端待适配项，本节点不修改 `review-webui/`。

### Finding 引用

- 为最终 report 中的 confirmed findings 按最终展示顺序生成稳定的 report-local ID（如 `F001`），写入 Markdown、report artifact 和 `ReviewResult.findings`；内部 rejected/uncertain candidates 不生成面向用户的 ID。
- 收集本轮实际消费的用户消息中明确出现、且存在于当前 report 的 finding ID，允许多个并去重；无效 ID 忽略，不从标题、路径、修复 diff 或模型输出推断。
- `finding_ids` 只保留在本轮内存和日志，不保存结构化 turn/finding 关联，不写入 Session metadata；用户原文中的 ID 仍作为普通历史内容保留。
- 引用不建立 finding 修复状态机，不自动关闭 finding，不改写原 report，也不表示修复已验证或重新审查通过。

### 实施顺序

1. 迁移 coordinator 模块及其测试和导入方；删除 `nanoreview/session/coordinator.py` 旧路径，并确认所有 API、CLI、channel 与服务装配入口均使用 `nanoreview.agent.coordinator.SessionCoordinator`。
2. 将 `AgentLoop` 的装配、MessageBus 主循环、session 串行和队列、命令、取消调度、权限响应、双 Agent 路由迁入 Coordinator；把 review turn 的 `ContextBuilder`/`COMMON_RULES` 上下文构造、用户消息和报告保存、结果返回收敛到 `ReviewLoop`，Coordinator 只发布结果（包括报告分块推流），不引入第二个 runtime 类。
3. 将完整对话 turn 迁入 ConversationLoop：InboundMessage 输入、内部 `_TurnContext`、handoff 历史写入、上下文与工具、Runner 调用、历史保存、取消收尾及 OutboundMessage 输出。删除占位 Request/Result 类，复用 nanobot 的注入、工作区、流式和交付策略。
4. 通过 `ContextBuilder.BOOTSTRAP_FILES` 将 SOUL 文件和模板接入 `COMMON_RULES.md`，显式接入 reviewer `prompt_builder` 与 Judge `_system_prompt`；不假定旧 workspace 的 `SOUL.md` 内容自动迁移，验证内部迭代不刷新。
5. 为最终 report findings 增加稳定 ID，接入多个显式 finding 引用的瞬时收集和日志；不增加独立 sidecar 或文件/shell 审计。
6. 迁移 API、CLI、channel、包导出及测试，完整删除 `nanoreview/agent/loop.py`、`AgentLoop` 及旧 coordinator 导入路径，不保留兼容门面；记录 `turn_trace` 消失对应的前端待适配项。本节点不修改 `review-webui/`。

当前核查的迁移影响面为 9 个源文件和 8 个测试文件，重点核对 `test_review_gate`（21 处引用）与 `test_loop_modes`（19 处引用）。每个实施步骤单独提交；步骤完成后运行受影响测试并保持 pytest 通过，最后运行全量 pytest。

### 测试与验收

- Coordinator 路由只在 review `DONE` 后开放 conversation；review handoff 只注入一次，普通消息门禁、队列上限、命令、权限响应、取消与 outbound 行为保持一致。
- `AgentLoop` 类、模块、导出及旧 coordinator 导入路径全部移除；API/CLI 和总线入口共用 Coordinator 调度。ConversationLoop 独立完成对话历史读取、handoff 消费、保存和回复组装，Coordinator 不重复追加历史。
- 多轮对话回放连续；注入消息只保存一次，未消费消息继续调度；`None` 返回和 message 工具发送不会导致重复回复或误判失败。system/subagent 消息在 conversation 阶段按 assistant turn 执行，在 review 阶段按 `result_callback` 和队列交付。
- Conversation Agent 只收到自己的工具注册表、目标工作区和权限配置；仅 `local_review`/`github_review` 从 core 排除，`submit_review_plan`/`submit_verdicts` 不因 discoverability 被自动加载，`review_submit` 不进入 core；core 的 message/spawn 行为保持一致，拒绝单次权限不强制结束 turn。
- `COMMON_RULES.md` 按 ContextBuilder、reviewer `prompt_builder` 和 Judge `_system_prompt` 在指定 turn/run/batch 开始读取，内部迭代不刷新，下一次执行读取最新文件；缺失/不可读不会中断调用，旧 `SOUL.md` 不自动迁移或读取。
- 流式输出可先于保存，成功终态必须在保存后；保存失败明确可见，取消通过现有 runtime checkpoint 保留部分历史，并在下一个 turn 补齐中断 turn 的占位历史且不重跑；已发生修改保留并清理绑定后传播取消。
- 不生成 conversation sidecar 或新增文件/shell 审计；Finding ID 在 report Markdown、artifact、ReviewResult 和 Web/API 状态中一致；多个有效引用只留在内存和日志，不新增持久化关联，report 内容及 finding 状态不被 Conversation Agent 修改。
- 删除状态机后默认不输出 `turn_trace`，记录 `websocket.py` 与 WebUI `types.ts` 的前端待适配项；session 删除沿用既有关联数据清理，重启只回读已保存历史和结果，不恢复未完成执行或队列。运行最近的 coordinator/conversation、Runner 取消、report/result、session 和入口相关 pytest，并执行全量 pytest。ruff 验收以不新增错误为准；当前已有 47 个错误均不在本节点涉及文件内，不要求本次清零。

### 本节点之外

`REVIEW_RULES.md`、`CONVERSATION_RULES.md` 的角色专用规则拆分；更细的工具权限与 approval 策略；finding 修复状态、自动关闭或重新审查；多次 review/多报告、自动 worktree、scope 用户入口；长对话 Consolidator/AutoCompact 收敛及 WebUI 交互改造均不在本节点实现。
