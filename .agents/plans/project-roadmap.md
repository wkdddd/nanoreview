# NanoReview 项目长期规划

更新时间：2026-10-22

本文记录已确认的产品方向、架构目标、后续顺序与范围边界，不代表全部已实现。当前任务与验收见 [code-adjustment-plan.md](code-adjustment-plan.md)；现有实现契约见 `.agents/constraints/`。

## 产品定位

NanoReview 是以代码审查为入口的个人多智能体系统。用户明确发起一次本地 review，流程结束后在同一 session 讨论报告、debug、修复代码并执行验证。

项目面向 Agent 开发求职，重点展示多智能体审查、上下文工程、工具执行边界和可复现的效果证据。投入周期为一个月，按优先级推进，不预先承诺完成全部方向。Nanobot、Kodus 等项目只提供参考，不替代本项目需求。

## 已确认目标与边界

- 产品审查输入仅为本地 Git diff，单一 `target` 定义完整审查边界；无 Git、读取失败或空 diff 在准入时失败。不提供远端 PR、远端仓库获取或全仓审查入口。
- diff 预处理不恢复全仓读取、AST/语义切块或 related 补充；也不做文件语言分类，任何路径的 diff 都按文本证据审查。独立 RAG 模块与 tree-sitter 依赖保留。
- review 由用户显式触发；请求校验并注册后建立 session。不创建纯对话 session，Conversation Agent 不自主触发 review。
- 同 session 首版仅一次 review。目标内 `scope` 收窄不接用户入口，保留为待评估方向；已有准入校验与快照能力不因未开放入口而删除。
- review 到达 `DONE` 后才开放对话；此前普通消息与非控制命令不进入历史或 pending queue，`/status`、`/stop` 可用，`/new` 被拒绝且不清空 session。
- `DONE` 要求资源清理完成、终态及最终 report 或有界失败结果已持久化；status 可为 `completed`、`error`、`stopped`。清理或保存失败保持门禁，并明确交付错误；报告交接失败须说明可用结果与覆盖缺口。
- 对话可读取代码、修改文件、执行命令和测试，修复发生在原仓库，不自动创建 worktree。应用 workspace 存放 session/report，目标 root 作为文件路径与命令 cwd 的基准。
- 不恢复中断的 review、命令或模型任务；保留历史最终 report、对话历史与中断标记，重启后可以开始新的对话 turn。

## 架构目标

- `SessionCoordinator` 位于 `nanoreview/agent/coordinator.py`，负责准入、路由、串行调度、控制、门禁和最终发布，不另设 `SessionRuntime`。ReviewLoop 保留 review 阶段编排，ConversationLoop 管理完整对话 turn。
- coordinator 创建并持有每 run 黑板，独占写入；ReviewLoop 通过窄接口请求更新。必要运行状态迁入黑板后删除旧 `ReviewRunState` 可变容器，不维护第二套同步状态机。API、CLI、WebUI 读取领域投影，不从展示文本推断终态。
- reviewer 不读写黑板，只接收 assignment 冻结输入；Planner、Judge 可只读当前 run 的授权视图。review 各角色完成后返回结构化结果，经校验后由 coordinator 写入，不提供中途共享草稿。Conversation Agent 不访问黑板，仅获取最终 report。
- 保留 reviewer 结果全部完成写入、validator、Judge、清理及终态持久化的阶段与门禁。移除 terminal tool 提交协议，保留输出业务校验、有界纠正与收尾预算；对话继续自由文本输出。
- Review 与 Conversation 共用 Runner、Provider、工具实现、压缩、usage 和取消能力，独立 prompt、消息上下文、ToolRegistry、tool context、执行 profile 与权限。review 的 planning、evidence、validation、Judge batching、finalizer 留在领域层，Runner 不承担 session 路由或 WebUI 协议。
- 权限在工具层执行：每 turn 解析 WorkspaceScope，review 固定 restricted；保留角色工具隔离、路径限制、命令 guard、sandbox 和通用联网边界，不引入逐工具 approval 或风险分级授权。应用层 guard 不等于操作系统隔离。
- 共同规则在 conversation turn、planner/reviewer run、Judge batch 开始时读取，内部迭代保持快照。上下文治理统一为**请求级压缩**（`ContextGovernor`，唯一预算公式，首包同样校验），跨轮只保留 session checkpoint（`Consolidator` 负责落盘与回放折叠），不复制第二套 Runner 或恢复 AutoCompact 产品路径。
- 模型参数按角色冻结：一次 run 从准入到最后一个 iteration 使用同一个 `LLMRuntime`（provider/model/generation/window），角色可逐项覆盖对话 agent，跨 provider 需显式声明。长期记忆由对话压缩产出的 journal 与每周一次 Dream 收敛维护，review 角色不参与写入。
- API、CLI、WebUI 共用领域流程；transport 只负责协议解析、交付与状态读取。不持久化 task、provider、lock、callback、future 或用于恢复执行的 runtime checkpoint。

## 报告与对话交接

- 最终 report artifact 是审查结论权威来源，findings 保留稳定的 report-local ID。session 关联 run、report 与对话历史，不接收 reviewer/Judge 内部 transcript、模型推理或完整工具 payload。
- 首次对话以可重放 handoff 注入完整最终 report，保留来源、run_id 和 report_ref；超出模型窗口时拒绝该 turn 并说明原因，不用自动摘要代替。没有最终 report 时仅交接已有最终结果、结束状态、覆盖缺口和有界错误。
- report 参与后续 Consolidator 压缩，保留 finding 标识、结论、用户决定、修复和验证进展，原 artifact 始终可核对。Conversation Agent 不改写报告，不维护 finding 引用 sidecar、自动关闭或重新审查通过状态，也不新增文件/shell 持久化审计。

## 后续顺序

| 顺序 | 目标 |
|---|---|
| 当前 | 统一上下文治理、角色模型配置与 Dream 长期记忆，具体范围和验收见实施计划；旧节点未验收项另列，不视作文档清理后已完成。 |
| 后续 | 后端闭环验收：API/CLI 覆盖 review、报告交接、对话修复与测试，以及取消、部分修改、持久化、重启读取和错误交付。 |
| 再后 | 审查效果评测：验证误报、已知问题检出及调整前后收益。 |
| 最后 | 前端适配与演示：状态、报告、流式对话、控制、错误、刷新和重连；包含 reviewer mode 三态适配与 approval 残留清理。 |

后端阶段先核对 API/event、CLI、测试、文档和前端消费者，记录待适配项；前端实现留到最后，再完成完整工作流验收。usage 与 hooks 属于维护项，只有影响正确性、排障或评测时才修正。检索、分块和证据分配的进一步优化由评测结果驱动，另行确认。

## 审查效果评测

- 主基线复用 [AACR-Bench 数据集](https://huggingface.co/datasets/Alibaba-Aone/aacr-bench) 与 [evaluation 框架](https://github.com/alibaba/aacr-bench/tree/main/evaluation)，只取 PR diff 作为产品审查输入。固定数据版本及校验值、框架与 NanoReview commit、模型、提示词、权限和参数；额外模型调用总预算不超过人民币 100 元。
- 保留 `data -> review -> result -> evaluate` 流程与标准 JSONL，提供 NanoReview adapter/result converter。报告语义与行号 Precision/Recall/F1、评论数、参考问题与匹配数、成功/缺失/超时/失败样本、耗时和 token；优先评估误报及人工确认的有效问题比例，Recall 仅指给定 diff 下已标注问题的检出，不代表全仓召回。
- 固定真实 Judge 配置并保存逐条匹配，人工复核关键和边界样本；Mock 只验证流水线，LLM Judge 不单独作为真值。不预设多智能体优于单 Agent，不采用外部静态成绩代替本次评测。
- 固定 seed 从小子集开始，在预算内扩展；每次使用独立 run_id，保留基线和调整后对照。交付版本化 manifest、可复现命令、逐样本结果、汇总指标、失败清单和人工抽查记录，说明标注、Judge 与覆盖限制。外部观测平台不替代 scorer 或人工核验。
- 少量原生 whole-repository cases 仅作独立离线补充验证，使用独立 manifest，保留专家确认的 finding、位置、严重性和证据；不接产品入口、不修改原始数据、不与 diff 主指标混合。

## 待评估方向

- 同 session 再次 review、多报告管理及对话显式请求重新 review。
- 目标内 scope 收窄；接入前补齐执行期传递与端到端测试，不把现有准入能力当作已开放功能。
- 更细的修复状态、finding 关闭与验证结果模型。
- COMMON_RULES 拆分为 review/conversation 规则。
- 更强工作区隔离或自动 worktree。
- 报告与历史的长期归档、搜索和跨 session 关联。
- 模型路由、成本预算与更细压缩策略（角色运行时与请求级压缩已落地，这部分指按成本/能力选模型与更细的裁剪策略）。
- ReviewLoop、MessageBus、WebUI transcript/trace 在当前黑板改造范围之外的进一步收敛，先核对调用方。
- general 绕过 Planner、直接派发及 Kodus finder/verifier 流程。

这些方向不自动纳入当前任务，确认后再形成实施计划。顺序调整不改变产品目标；与已确认短期计划冲突时同步本文件，不保留并列旧口径。

## 明确不做

- 首版不支持同 session 多次 review、多报告管理、纯对话入口、自主 review、通用定时任务或 `/resume`。唯一例外是长期记忆的每周 Dream 收敛（固定周日 03:00 Asia/Shanghai、不补跑、只写 `memory/MEMORY.md`），它不是通用调度器，也不扩展到 review 或对话 turn。
- 不自动重执行可能产生副作用的对话 turn，不直接照搬参考项目的截短或超窗重跑策略。
- 不因产品专用化删除仍有消费者的通用 session、历史、Consolidator 或对话工具；删除组件前核对用途。
- 不把未合并候选分支当作目标架构或实施基线。

## 长期验收方向

- 用户可完成“显式 review → 终态 → 同 session 讨论、修复、验证”的闭环，状态、权限、上下文和失败边界清楚。
- report、历史与压缩在长对话、刷新、重连和重启后保持一致；本地路径、角色工具、命令限制、取消和部分修改可验证，不残留确认等待或有效前端 approval 入口。
- 审查效果有可复现案例与人工核验依据，调整收益和局限可通过对照解释。
- 未来增加多次 review 时，无须推翻 session、run、report、turn 的基本关联。
