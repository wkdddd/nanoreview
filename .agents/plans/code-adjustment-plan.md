# 当前代码调整计划

更新时间：2026-10-01

## 当前状态

已确认第一轮实施节点：**本地 review 准入与一次性 session 边界**。

## 目标节点

- 对应长期规划：`project-roadmap.md` 第一阶段“本地 review 准入”。
- 用户可见结果：用户通过 WebUI、CLI `review` 或结构化 API 明确发起一次本地 review；输入校验通过后立即成立一个 review session，并在 review 完成、错误或停止且结果持久化后进入同 session 对话。
- 本轮包含：统一入口准入、Repo/Diff 本地输入校验、scope 过滤、完整输入快照、session/run 注册与持久化、运行期门禁、结构化错误、CLI/WebUI/API 契约对齐。
- 本轮不包含：远端 PR 或远端仓库、普通对话触发 review、同 session 再次 review、多报告管理、`/resume`、外部 patch 或 commit range 输入、同仓库跨 session 互斥、Conversation Agent 实现、长对话 Consolidator 调整。

## 已确认产品决策

- Repo 模式允许非 Git 的本地文件或目录；Diff 模式必须位于 Git 仓库。
- Repo scope 支持仓库根、子目录和单文件；文件只审查该文件，目录递归审查目录内容。
- Diff scope 支持仓库根、子目录和单文件，按范围过滤当前工作区变更。
- Diff 只审查相对于 `HEAD` 的最终工作区净变化，包含 staged、unstaged 和 untracked；不支持外部 `.patch`/`.diff` 或 ref 区间。
- 允许读取 scope 外的关联上下文，但 finding 必须归属于选定 scope；Diff finding 还必须由本次变更引起。
- CLI 相对路径按调用命令时的 cwd 解析；WebUI/API 首版要求服务端本地绝对路径。
- 只允许专用入口触发 review：WebUI 提交、CLI `review`、结构化 API；普通消息不触发。
- 每次 CLI `review` 创建新 session；不同 session 可并发审查同一仓库，不做仓库级互斥。
- 空 Diff 在准入阶段拒绝，不创建 session。
- 准入校验全部通过后才注册并持久化 session/run；拒绝请求不写历史、不创建 session。
- 用户提交时保存完整输入内容快照，review 严格针对该快照执行。
- review 运行期间普通消息和非控制命令拒绝且不写历史、不进入 pending queue；`CommandRouter` 保留为 `/status`、`/stop`、权限确认及后续命令扩展入口。
- review 完成、错误或停止，并在资源清理及最终报告或有界失败结果持久化后，开放同 session 对话。

## 当前代码事实与缺口

- `planner.py` 当前接受文件和目录，非 Git 目录也可进入计划；不存在路径不会在准入阶段拒绝。
- `evidence.py` 当前分别读取 staged、unstaged 和 untracked patch，尚未形成相对于 `HEAD` 的单一净变化快照。
- WebSocket/CLI 当前会先写 session metadata；`AgentLoop._dispatch` 才在运行时注册内存 review run，CLI `process_direct` 绕过该注册链路。
- 当前重复 review 门禁主要依赖进程内 `_review_runs`；需要统一检查持久化 session，并防止新请求覆盖既有 review metadata。
- 当前 `/new` 会清空 session 并解除 review 门禁；review session 内必须拒绝 `/new`，新 review 通过专用入口创建新 session。
- 当前门禁按 session 加锁，不限制不同 session 对同一仓库的并发；该并发语义保留。

## 调整方案

### 统一准入与输入模型

- 增加共享 review admission/domain service，由 WebUI/API、CLI 和消息入口调用；transport 只负责协议解析、交付和状态读取。
- admission 输入包含 `session_id`（若由入口生成）、绝对 target、target type、action、scope、focus 和调用 cwd；输出稳定的 accepted session/run metadata 或结构化 rejection。
- Repo admission 校验目标存在、文件/目录类型和 scope；Diff admission 校验 Git 根、目标范围和相对于 `HEAD` 的非空净变化。
- CLI 先按调用 cwd 将相对路径解析为绝对路径；WebUI/API 拒绝相对路径。
- 生成完整输入快照，至少包含目标文件内容、Git HEAD、范围内的净 diff、Repo 目标内容和必要的路径/摘要 metadata；快照成为 review 执行输入。
- scope 采用精确文件/目录路径过滤，不引入多选或 glob；关联读取可超出 scope，但输出归属校验必须在 finalizer/Judge 边界执行。

### Session、run 与持久化时序

- 校验、快照和重复 review 检查完成后，在同一准入边界内创建 session、生成 run id、持久化 review metadata 和输入快照，再投递执行任务。
- 已存在 review session 的新 review 请求返回稳定重复提交错误，不修改原 target、scope、run 或历史。
- `ReviewRunState` 继续作为运行状态权威模型；session metadata 保存导航、输入快照引用、run id、状态、阶段、错误和报告引用。
- 资源清理、报告/有界失败结果和终态 metadata 持久化完成后，才解除普通对话门禁。
- 不恢复中断任务；重启后读取历史、快照和终态结果，但不自动重新执行。

### 运行期命令与错误

- `CommandRouter` 负责控制命令路由；运行期间允许 `/status`、`/stop` 和权限确认，其余命令与普通消息返回结构化门禁错误。
- review session 内 `/new` 拒绝且不清空 session；`/new` 仅在创建新 session 的专用流程中生效。
- 准入失败返回稳定错误码、可读信息和必要字段：路径不存在/类型错误、非 Git Diff、空 Diff、scope 无匹配变更、重复 review、相对路径非法、快照失败等。
- WebUI 展示准入错误并保持当前输入可修正；CLI 返回非零退出码；API 使用对应 4xx 响应。

### API、事件、CLI、WebUI 与测试

- 统一 review request/accepted/rejected、run state、snapshot reference 和 terminal result 的结构化字段；不让前端从展示文本推断终态。
- WebUI 提交成功后使用新 session；拒绝消息不写入 session history 或 pending queue。
- CLI `review` 不再固定复用 `cli:review`，每次命令建立独立 session，并复用统一 admission。
- 定向测试覆盖 Repo/Diff 输入矩阵、路径解析、scope 过滤、净 diff、完整快照、空 diff、重复提交、跨入口一致性、运行期门禁、`/new`、错误码和终态开放对话。

## 状态、权限、取消、并发与错误边界

- 不同 session 对同一仓库允许并发；不新增仓库锁或排队。
- 每个 review 在接受时生成并保存输入快照；执行只读取该快照，不因后续工作区变化改写本次审查对象。
- `/stop` 取消执行并进入 `stopped`；错误进入 `error`；两者都必须完成资源清理并持久化有界结果后开放对话。
- 快照、状态、报告和历史分离保存，保留原子写入、归属校验、大小限制和敏感信息清理。

## 验收与交付

- 定向测试：新增或更新 `tests/agent/test_review_gate.py`、`tests/agent/test_codereview.py`、`tests/review/test_prefetch.py` 及 API/WebUI review request 测试。
- 全量或构建验证：运行受影响 Python 测试、`ruff check nanoreview/`；WebUI 变更时运行 `bun run test` 和 `bun run build`。
- 完成后同步：将已落地的稳定准入契约归入相关 `.agents/constraints/`，本计划验收后清空供下一轮使用。
- 剩余风险：完整快照的存储规模、快照与报告 artifact 的生命周期、旧 transcript/trace 消费者迁移、现有终态门禁与对话交接实现仍需在后续阶段核对。
