# NanoReview 项目长期规划

更新时间：2026-10-06

本文记录已确认的产品方向、影响演进的架构原则、暂定执行顺序、待评估方向和明确不做的能力。它不是当前代码状态，也不是具体实施任务。当前代码调整另见 [code-adjustment-plan.md](code-adjustment-plan.md)；稳定模块契约归入 `.agents/constraints/`。

## 产品定位

NanoReview 是以代码审查为入口的个人多智能体代码审查系统。用户先明确提交一次本地 review，流程结束后在同一个 session 中讨论报告、debug、修改代码并执行验证。

Review Agent 与 Conversation Agent 是两套 Agent，共用分层 Harness，但拥有独立的 prompt、上下文、工具注册表、权限和结果契约。

后续迭代面向 Agent 开发求职，以多智能体审查为项目特点，重点深化上下文工程、工具执行边界和任务效果证据。投入周期为一个月，按优先级推进，不预先承诺全部阶段在周期内完成。

## 已确认目标

- 审查输入只支持本地代码仓库和本地 diff，不支持远端 PR 或远端仓库获取。
- 首版以单一 `target` 作为唯一审查输入：一个 target 对应一次审查的完整边界，不提供「目标内子路径范围」的选择。
- 目标内子路径收窄（`scope`）不接通任何用户入口，保留为未来扩展；准入侧已有的校验、归一化与快照能力不删除，接入时按待评估方向处理。
- review 必须由用户通过明确入口触发，Conversation Agent 首版不能自主调用 review。
- review 请求通过校验并被接受、注册后 session 即成立，无须等待 review 完成；不允许创建纯对话 session。
- review 完成 `DONE` 后才开放同 session 对话；此前普通消息与非控制命令被拒绝，不写入历史或 pending queue。`/status`、`/stop` 可用；review session 内 `/new` 被拒绝且不清空 session。
- `DONE` 表示资源清理完成，终态及最终 report 或有界失败结果已持久化；最终 status 可以是 `completed`、`error` 或 `stopped`。清理或保存失败必须向用户返回必要错误，并保持对话门禁；失败原因应持久化供重启后读取。完成 `DONE` 后，报告交接失败也必须说明错误、可用结果与覆盖缺口。
- Conversation Agent 可以读取代码、修改文件、执行命令和运行测试，修复直接发生在原审查仓库，不自动创建 worktree。
- 两套 Agent 使用独立 `ToolRegistry`；工具实现可以共享，但注册表实例、工具权限和执行 profile 隔离。
- 第 4 阶段参照 nanobot 简化工具调用，已移除逐工具 approval、全局与 session 确认开关、确认请求/响应链路和 GitHub 远程 review 输入，未新增风险分级授权系统。权限只在工具调用层处理：每个 turn 解析一次 `WorkspaceScope`，review 侧固定 `restricted`。角色工具隔离、路径限制、命令 guard 和已有 sandbox 保留；通用联网能力（模型 HTTP/OAuth、`web_search`、`web_fetch`、MCP 三种 transport 与 SSRF 校验）保留。
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

- ReviewLoop 管理一次 review 生命周期；ConversationLoop 管理完整对话 turn，包括历史读取与保存、上下文、执行和回复组装；会话层负责准入、路由、串行执行和控制。
- 现有 `ReviewRunState` 是 review 状态的权威模型；session 关联 run、report、导航信息和对话历史，不建立第二套 review 状态机。
- ReviewRunState、report artifact、对话历史和模型工作上下文各有明确用途；前端不从展示文本推断终态，reviewer/Judge 内部 transcript 不直接变成对话历史。
- 两套 Agent 共用 `AgentRunner`、Provider、ToolRegistry 实现、工具执行、压缩、usage 和取消，但不共享可变消息、tool context 或任务状态。
- 两套 Agent 的角色边界、session/run 关联、权限隔离、结果契约和报告交接入口在第 1 阶段确定并冻结；Conversation Agent 的具体执行能力在第 3 阶段实现。
- review 的 planning、evidence、validation、Judge batching 和 finalizer 保持在 review 领域；Runner 不承担 review、session 路由或 WebUI 协议。
- 权限按 Agent role 隔离：review 使用审查 profile，conversation 使用修复 profile；共享工具实现不等于共享工具权限。
- 应用 workspace 存放 session/report；目标仓库 root 是文件工具路径基准和命令默认 cwd。路径 guard、命令规则和 sandbox 独立生效，移除 approval 不扩大角色工具或路径权限；应用层 guard 不等于操作系统隔离。
- 单次 Agent run 使用 Runner compression；跨轮对话使用 Consolidator。压缩策略可按 Agent 区分，但不复制第二套 Runner。
- report 是审查结论权威来源，最终 findings 保留稳定的 report-local ID，供用户和模型讨论具体问题。Conversation Agent 不收集、不记录 finding 引用；不维护 conversation sidecar，不新增文件/shell 持久化审计或 turn/finding 关联，不建立修复状态、不自动关闭 finding、不改写报告或自动标记重新审查通过。
- `SessionCoordinator` 是 session 调度与双 Agent 协调入口，位于 `nanoreview/agent/`；它拥有消息循环、串行、队列、控制、路由门禁、handoff 值对象与其唯一一次 session 写入，以及最终发布，不另设 `SessionRuntime`。ConversationLoop 使用现有 SessionManager 完成对话历史，并在固定位置回调 coordinator 完成交接写入，ReviewLoop 负责 review 生命周期。
- ConversationLoop 复用 nanobot 的完整 turn 编排，采用 `InboundMessage -> OutboundMessage | None` 和内部 turn context；消息注入、流式输出、工作区和通用 core 工具沿用已有策略，排除 review 专属工具，不引入正式 conversation 状态机。第 3 阶段完整迁移并删除 AgentLoop 类、模块及导入，不保留兼容门面。
- 共同规则按 conversation turn、planner/reviewer run、Judge batch 在执行开始时读取，内部迭代保持快照；角色专属 prompt、权限和报告只读边界独立生效。
- 不持久化 task、provider、lock、callback、future 或足以自动恢复执行的 runtime checkpoint。
- API、CLI、WebUI 使用同一领域流程；transport 只负责协议解析、交付和状态读取，不在 channel 中复制 review 逻辑。

## 暂定执行顺序

截至本次核查，第 1–3 阶段已完成，生命周期 hooks 与 MCP 也已落地，不再作为后续待开发节点。此处的完成记录不替代本次或后续实施的验证。权限简化与上下文编排分为两个独立阶段，先权限、后上下文。

第 4 阶段的具体实施方案、受影响文件和验收条件见 `code-adjustment-plan.md`；本文件只记录长期阶段目标，不重复短期实施清单。后续阶段在选定实施节点后，沿用同一计划文件补充对应方案。

| 顺序 | 状态 | 阶段与预期结果 |
|---|---|---|
| 1 | 已完成 | 本地 review 准入：统一仓库与 diff 校验、单一 `target`、session 注册和双 Agent 交接契约。 |
| 2 | 已完成 | ReviewLoop 与终态收尾：持久化 `ReviewRunState`，成功、错误和停止均完成清理与结果保存，`DONE` 后才开放对话。 |
| 3 | 已完成 | Conversation Agent：完成 SessionCoordinator 与 ConversationLoop 迁移，删除 AgentLoop 和旧 coordinator 模块；接入 COMMON_RULES 和稳定 finding ID，不收集 finding 引用。 |
| 4 | 已完成 | 工具权限与远程 review 边界收敛：删除逐工具 approval、配置与 session 开关、确认请求和响应链路及 GitHub 远程 review 接入；改为每 turn 一次 `WorkspaceScope` 解析，review 侧固定 restricted；保留角色工具隔离、路径限制、命令 guard、已有 sandbox 与全部通用联网能力。 |
| 5 | 已完成 | 执行上下文编排：梳理规则、报告、历史、摘要和工具定义的组装与预算；收敛 Consolidator 与 run 内压缩职责，沿用 `last_consolidated`，移除 AutoCompact 产品调用路径；验证累计摘要、信息保留、历史裁剪与重启后的上下文一致性。 |
| 6 | 待实施 | 后端闭环验收：通过 API/CLI 验证 review、报告交接、对话修复和测试验证，以及取消、部分修改、持久化、重启后读取和错误交付；核对消费者后收敛遗留代码。 |
| 7 | 待实施 | 审查效果评测：复用 [AACR-Bench 数据集](https://huggingface.co/datasets/Alibaba-Aone/aacr-bench) 和其 [evaluation 评测框架](https://github.com/alibaba/aacr-bench/tree/main/evaluation) 作为主基线，接入 NanoReview 的结构化审查结果；以降低误报、证明审查有效性为主，代码调整带来的改善为辅助，额外模型调用总预算不超过人民币 100 元。 |
| 8 | 最后实施 | 前端适配与演示闭环：统一 WebUI 的状态、报告、流式对话、控制和错误展示，移除 approval 残留入口，完成刷新、重连与完整工作流验收。 |

第 5 阶段限定为执行上下文编排，不同时调整仓库检索、代码分块或 reviewer 证据分配策略；这些效果优化在评测暴露问题后另行讨论。第 7 阶段的数据集适用性、案例规模和对照方案在实施前核查确定，不预设多智能体优于单 Agent，也不把已知问题检出率等同于整个仓库的完整召回率。

### 第 7 阶段评测方案

- **评测基线**：固定 AACR-Bench 数据集版本、数据快照校验值、评测框架 commit、NanoReview commit、模型版本、提示词、工具权限和运行参数。AACR-Bench 的 PR 样本保留完整仓库上下文，可作为 NanoReview repository-level review 的第一批可复现基线；不直接采用 OpenCodeReview 页面中的静态结果表。
- **复用方式**：保留 AACR-Bench 的 `data -> review -> result -> evaluate` 流程和标准 JSONL schema，新增 NanoReview reviewer adapter/result converter，将 NanoReview finding 统一为文件路径、行区间、diff side、描述和严重性等字段。必要的 NanoReview 全仓库审查案例另建独立 manifest，不修改 AACR-Bench 原始数据。
- **主要指标**：正式报告语义 Precision、Recall、F1，以及行号 Precision、Recall、F1；同时报告生成评论数、参考问题数、匹配数、成功/缺失/超时/失败样本数、耗时和 token。主结论优先看 Precision、误报率和人工确认的有效问题比例，Recall 只解释为对数据集已标注问题的检出率。
- **裁判与人工核验**：Mock Judge 只用于验证流水线；正式结果使用固定的真实 Judge 配置，并保存逐条匹配明细。对关键样本和 Judge 边界样本进行人工复核，记录 Judge 误判、位置偏差和标注覆盖不足，不把 LLM Judge 单独当作真值。
- **运行与对照**：先用固定 seed 的小规模子集完成 adapter、结果解析和评分校验，再在预算内扩展样本。每次实验使用独立 `run_id`，至少保留 NanoReview 基线与代码调整后的对照运行；外部评测平台可用于 trace、耗时和 token 记录，但不替代 AACR-Bench scorer 和人工核验。
- **补充验证**：AACR-Bench 不能覆盖没有 diff 的完整目录审查，因此在主基线之外维护少量 NanoReview 原生 whole-repository cases。每条案例必须有专家确认的 finding、位置、严重性和证据；该轨道单独报告，不与 AACR-Bench 的 PR 指标混合。

第 7 阶段交付物包括：版本化数据与运行 manifest、NanoReview reviewer adapter、可复现的评测命令、逐样本结果与匹配明细、汇总指标、失败与缺失样本清单、人工抽查记录，以及对误报改善和已知问题检出的结论。评测结论必须注明 AACR-Bench 的标注范围、Judge 依赖和样本覆盖限制。

token usage 统计完善和额外 hooks 建设降为维护项，不再设置独立阶段；保留已有观测能力，只有影响执行正确性、排障或评测时才做必要修正。

执行时保留以下依赖与验证边界：

- 第 4、5 阶段基于已完成的双 Agent 能力独立实施、分别验证；第 6 阶段先验收现有后端闭环，第 7 阶段再评测，第 8 阶段完成前端适配。
- 后续具体实现优先复用 nanobot 的现有项目逻辑，并遵守 NanoReview 已确认的双 Agent 与报告交接边界。
- 第 4–7 阶段优先后端，不修改 `review-webui/`；前端功能、交互及契约适配统一留到第 8 阶段。
- 各阶段同步调整受影响的后端 API/event、CLI、测试和文档；仍须核对前端消费者并记录待适配项，前端适配完成后再进行完整闭环验收。
- 对话准入以 review 完成 `DONE` 为前提；清理、终态和结果持久化必须完成，权限与上下文须隔离。清理、持久化和交接失败须明确可见，不能把内存终态当作已落盘证明。
- 首次完整报告注入的 token 预算检查已在第 1 阶段落地：超窗时拒绝该 turn 并提示原因，不用自动摘要替代，也不等待后续长对话压缩兜底。
- 不恢复中断任务的边界随生命周期及对话迁移落实；保留已完成历史和中断记录，核对 checkpoint 与自动重试调用方，避免重复执行有副作用的工具。
- 暂定顺序的调整不改变已确认目标，也不自动纳入待评估方向；涉及产品范围变化时先与用户确认。
- roadmap 与已确认的短期计划冲突时，以短期计划为准并同步 roadmap，须向用户说明位置与理由；空短期计划不引入新任务。


## 待评估方向

- 同一 session 再次发起 review 和多报告管理。
- review 完成后由对话显式请求重新 review 的交互形式。
- 目标内 `scope` 收窄（按单文件或子目录限定审查范围）：准入侧的校验、归一化、快照写入与错误码已保留可用，但 CLI 与 WebUI 均无输入入口，且执行期尚未消费该 scope，目前不产生任何用户可见行为；接入前必须先补齐执行期传递链路并覆盖端到端测试。
- 更细粒度的修复状态、finding 关闭和验证结果模型。
- 将共用 `COMMON_RULES.md` 继续拆分为 `REVIEW_RULES.md` 与 `CONVERSATION_RULES.md`。
- 更强的工作区隔离或自动 worktree 流程。
- 报告与对话历史的长期归档、搜索和跨 session 关联。
- 对话和 review 的模型路由、成本预算及更细的压缩策略。
- ReviewLoop、MessageBus 和 WebUI transcript/trace 的后续收敛范围，须核对调用方；AgentLoop 的完整迁移与删除已完成。


待评估方向不是首版承诺；确定后再形成独立的短期代码调整计划。

## 明确不做

- 首版不支持远端 PR、GitHub 获取或远端仓库拉取。
- 首版不允许 Conversation Agent 自主触发 review。
- 首版不支持同 session 多次 review、多报告管理或 `/resume`。
- 首版不支持定时及自动化能力
- 不把 reviewer/Judge 的完整 transcript、模型推理或完整工具 payload 注入对话历史。
- 不因产品专用化删除通用 session、历史、Consolidator 或对话工具；已确认移除 AutoCompact 产品调用路径，组件删除范围仍须核对消费者。
- 不把 Kodus 的确定性截短和超窗重跑策略直接当作 NanoReview 的完整压缩方案，尤其不自动重执行可能产生副作用的对话 turn。
- 不把未合并候选分支当成目标架构或实施基线。

## 长期验收方向

- 用户能完成“明确发起 review -> review 终态 -> 同 session 讨论、修复和验证”的完整闭环。
- review 和 conversation 的状态、权限、上下文和失败边界清晰可见且互不污染。
- report、历史和压缩在长对话、刷新、重连和重启场景下保持一致。
- 本地仓库边界、角色工具隔离、命令限制、取消和部分修改行为可验证；移除 approval 后不残留后端确认等待或有效前端确认入口。
- 审查效果有可复现案例与人工核验依据，优先减少误报，并记录已知问题检出情况；后续策略调整能够通过对照解释收益与局限。
- 未来增加多次 review 时无需推翻 session、run、report 和 turn 的基本关联模型。
