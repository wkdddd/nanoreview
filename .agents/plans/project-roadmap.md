# NanoReview 项目长期规划

更新时间：2026-10-01

本文记录已确认的产品方向、影响演进的架构原则、暂定执行顺序、待评估方向和明确不做的能力。它不是当前代码状态，也不是具体实施任务。当前代码调整另见 [code-adjustment-plan.md](code-adjustment-plan.md)；稳定模块契约归入 `.agents/constraints/`。

## 产品定位

NanoReview 是以代码审查为入口的个人多智能体代码审查系统。用户先明确提交一次本地 review，流程结束后在同一个 session 中讨论报告、debug、修改代码并执行验证。

Review Agent 与 Conversation Agent 是两套 Agent，共用分层 Harness，但拥有独立的 prompt、上下文、工具注册表、权限和结果契约。

## 已确认目标

- 审查输入只支持本地代码仓库和本地 diff，不支持远端 PR 或远端仓库获取。
- 首版以单一 `target` 作为唯一审查输入：一个 target 对应一次审查的完整边界，不提供「目标内子路径范围」的选择。
- 目标内子路径收窄（`scope`）不接通任何用户入口，保留为未来扩展；准入侧已有的校验、归一化与快照能力不删除，接入时按待评估方向处理。
- review 必须由用户通过明确入口触发，Conversation Agent 首版不能自主调用 review。
- review 请求通过校验并被接受、注册后 session 即成立，无须等待 review 完成；不允许创建纯对话 session。
- review 运行期间不开放普通讨论：普通消息与非控制命令被拒绝且不写入历史或 pending queue；`/status`、`/stop` 和权限响应可用；review session 内 `/new` 被拒绝且不清空 session。
- review 无论 `completed`、`error` 还是 `stopped`，在资源清理完成且最终 report 或有界失败结果持久化后，都进入同 session 对话。
- Conversation Agent 可以读取代码、修改文件、执行命令和运行测试，修复直接发生在原审查仓库，不自动创建 worktree。
- 两套 Agent 使用独立 `ToolRegistry`；工具实现可以共享，但注册表实例、工具权限和执行 profile 隔离。
- 完整保留 `ReviewRunState`，供 session detail、API 和 WebUI 读取；Conversation Agent 主要使用 report 和对话 history。
- report 以带来源标识的 `review_context` 索引消息进入 `Session.messages`，至少关联 `source=review_agent`、`run_id` 和 `report_ref`；完整 report artifact 是权威来源。
- Conversation Agent 首次上下文注入完整 report：system directive 标注来源与只读规则，完整 report 以可重放的 handoff 消息写入历史；report 超出首次对话模型窗口时拒绝该 turn 并给出原因，不用自动摘要替代；后续 report 参与 `Consolidator` 压缩。
- `Consolidator` 按 token 自动触发，首版移除 `AutoCompact` 产品路径，沿用现有 `last_consolidated` 机制。
- 对话首次上下文注入完整最终 report；没有最终报告时注入实际已有结果、结束状态、覆盖缺口和有界错误。
- report 可以参与后续上下文压缩，压缩需保留 finding 标识、结论、用户决定、修复与验证进展；原报告 artifact 始终可核对。
- 首版同 session 不支持再次 review，不提供多报告管理；数据关系保留未来扩展空间。
- 不恢复中断的 review、命令或模型任务；重启后可以读取历史和结果并开始新的对话 turn。
- Kodus、Nanobot 和旧 handoff 文档只提供参考，最终架构和决策以 NanoReview 为准。

## 长期架构原则

- ReviewLoop 管理一次 review 生命周期；Conversation Agent 管理一次对话执行；会话层负责准入、路由、串行执行和控制。
- 现有 `ReviewRunState` 是 review 状态的权威模型；session 关联 run、report、导航信息和对话历史，不建立第二套 review 状态机。
- ReviewRunState、report artifact、对话历史和模型工作上下文各有明确用途；前端不从展示文本推断终态，reviewer/Judge 内部 transcript 不直接变成对话历史。
- 两套 Agent 共用 `AgentRunner`、Provider、ToolRegistry、权限执行、压缩、usage 和取消，但不共享可变消息、tool context 或任务状态。
- 两套 Agent 的角色边界、session/run 关联、权限隔离、结果契约和报告交接入口在第 1 阶段确定并冻结；Conversation Agent 的具体执行能力在第 3 阶段实现。
- review 的 planning、evidence、validation、Judge batching 和 finalizer 保持在 review 领域；Runner 不承担 review、session 路由或 WebUI 协议。
- 权限按 Agent role 隔离：review 使用审查 profile，conversation 使用修复 profile；共享工具实现不等于共享工具权限。
- 应用 workspace 存放 session/report；目标仓库 root 是文件工具路径基准和命令默认 cwd。路径 guard、命令规则、sandbox 和 approval 独立生效。
- 单次 Agent run 使用 Runner compression；跨轮对话使用 Consolidator。压缩策略可按 Agent 区分，但不复制第二套 Runner。
- report 是审查结论权威来源；修复与验证关联原 run/finding，不改写原报告或自动标记重新审查通过。
- 不持久化 task、provider、lock、callback、future 或足以自动恢复执行的 runtime checkpoint。
- API、CLI、WebUI 使用同一领域流程；transport 只负责协议解析、交付和状态读取，不在 channel 中复制 review 逻辑。

## 暂定执行顺序

以下顺序用于组织首版目标的落地，允许根据代码核查、验证结果和用户优先级拆分、合并或调整，不代表节点已经完成或可以直接开始实施。每轮选定一个具体节点后，再将方案与验收条件写入 `code-adjustment-plan.md`；尚未明确的需求或产品行为必须先询问用户，不自行补全。

| 顺序 | 阶段与预期结果 | 规划理由 |
|---|---|---|
| 1 | 本地 review 准入：统一仓库与 diff 校验，按单一 `target` 确定审查边界；接受并注册后立即持久化 session；确定 ReviewAgent 与 Conversation Agent 的角色、session/run 关联、权限隔离和报告交接契约；为 Conversation Agent 预留独立执行 profile 与上下文入口；拒绝远端输入、纯对话创建、重复 review 和运行期普通讨论。 | 先固定 session 成立时机、一次 review 的边界和双 Agent 的交接契约，为生命周期迁移和各入口提供一致基础；本阶段不实现 Conversation Agent 的修改代码、执行命令和测试能力。 |
| 2 | ReviewLoop 与终态收尾：迁移一次 review 的生命周期，复用现有编排和领域组件；完整保留并持久化 `ReviewRunState`；成功、错误、停止均清理资源并保存报告或有界失败结果。 | 对话交接依赖可靠的终态和可读取结果；先统一收尾，避免仍有子任务运行或结果尚未落盘时开放对话。 |
| 3 | Conversation Agent 修复能力：实现第 1 阶段预留的独立 prompt、上下文、工具注册表和修复权限；在原审查仓库讨论、修改和验证，并把修复结果关联回原 run/finding。 | 终态门禁解除与报告交接已在第 1 阶段落地，本阶段补齐隔离后的修复执行能力，形成最小用户闭环，避免把 review 工具、内部 transcript 或应用 workspace 误用于修复。 |
| 4 | 长对话治理：让报告与后续历史参与 token Consolidator，沿用 `last_consolidated`；移除 AutoCompact 产品调用路径；验证压缩、刷新和重启后的上下文一致性。 | 报告交接及对话历史契约稳定后，才能明确压缩输入和保留信息，减少上下文丢失或重复注入。 |
| 5 | 完整闭环验收与遗留收敛：统一 API、CLI、WebUI 的状态读取和交付；验证重连、重启、取消、权限确认及部分修改；核对消费者后评估旧入口与组件的清理范围。 | 通过完整工作流核验跨模块一致性，再决定删除范围，避免过早移除仍承担历史回放、控制或展示职责的实现。 |

执行时保留以下依赖与验证边界：

- 当前阶段优先完成后端调整，暂不修改 `review-webui/`；前端功能、布局、交互及契约适配统一留待后续安排。
- 各阶段同步调整受影响的后端 API/event、CLI、测试和文档；仍须核对前端消费者并记录待适配项，前端适配完成后再进行完整闭环验收。
- 开放对话必须以资源清理、结果持久化以及权限和上下文隔离完成为前提，不能只依据 review 状态字段；持久化失败须明确可见。
- 首次完整报告注入的 token 预算检查已在第 1 阶段落地：超窗时拒绝该 turn 并提示原因，不用自动摘要替代，也不等待后续长对话压缩兜底。
- 不恢复中断任务的边界随生命周期及对话迁移落实；保留已完成历史和中断记录，核对 checkpoint 与自动重试调用方，避免重复执行有副作用的工具。
- 暂定顺序的调整不改变已确认目标，也不自动纳入待评估方向；涉及产品范围变化时先与用户确认。
- 当`.agents\plans\project-roadmap.md`和`.agents\plans\code-adjustment-plan.md`以后者短期plan为准并同步调整roadmap，但禁止静默修改，必须向用户指出修改位置和理由


## 待评估方向

- 同一 session 再次发起 review 和多报告管理。
- review 完成后由对话显式请求重新 review 的交互形式。
- 目标内 `scope` 收窄（按单文件或子目录限定审查范围）：准入侧的校验、归一化、快照写入与错误码已保留可用，但 CLI 与 WebUI 均无输入入口，且执行期尚未消费该 scope，目前不产生任何用户可见行为；接入前必须先补齐执行期传递链路并覆盖端到端测试。
- 更细粒度的修复状态、finding 关闭和验证结果模型。
- 更强的工作区隔离或自动 worktree 流程。
- 报告与对话历史的长期归档、搜索和跨 session 关联。
- 对话和 review 的模型路由、成本预算及更细的压缩策略。
- ReviewLoop、旧 AgentLoop、MessageBus 和 WebUI transcript/trace 的最终删除范围，须在调用方迁移后评估。

待评估方向不是首版承诺；确定后再形成独立的短期代码调整计划。

## 明确不做

- 首版不支持远端 PR、GitHub 获取或远端仓库拉取。
- 首版不允许 Conversation Agent 自主触发 review。
- 首版不支持同 session 多次 review、多报告管理或 `/resume`。
- 不把 reviewer/Judge 的完整 transcript、模型推理或完整工具 payload 注入对话历史。
- 不因产品专用化删除通用 session、历史、Consolidator、AutoCompact 或对话工具；删除前必须核对消费者。
- 不把 Kodus 的确定性截短和超窗重跑策略直接当作 NanoReview 的完整压缩方案，尤其不自动重执行可能产生副作用的对话 turn。
- 不把未合并候选分支当成目标架构或实施基线。

## 长期验收方向

- 用户能完成“明确发起 review -> review 终态 -> 同 session 讨论、修复和验证”的完整闭环。
- review 和 conversation 的状态、权限、上下文和失败边界清晰可见且互不污染。
- report、历史和压缩在长对话、刷新、重连和重启场景下保持一致。
- 本地仓库边界、确认策略、取消和部分修改行为可验证。
- 未来增加多次 review 时无需推翻 session、run、report 和 turn 的基本关联模型。
