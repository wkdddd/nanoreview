# 当前代码调整计划

更新时间：2026-11-01

## 当前节点：PR-only 审查、PostgreSQL 与 grep 内核复用

状态：部分实施。grep 内核、PostgreSQL 持久化地基、GitHub PR 输入层与 `pr_context` 已合入 `main`；准入、审查链路、后端入口与旧链路删除未实施。

### 目标与边界

- 仅支持 GitHub PR 审查：仓库链接 → 分页查看 PR → 选定 PR → 固定版本审查 → 报告与只读讨论。支持公共仓库及服务端统一 Token 可访问的私有仓库；不做 OAuth、多用户凭据、GitHub Enterprise、GitHub 写入或代码修复。
- 通过 GitHub API 按固定 SHA 获取元数据、diff 和所需代码，不 clone、不整仓下载。删除本地审查入口、git diff HEAD 采集、工作目录降级及修复分支；保留通用 Runner、任务并发、取消、清理和终态门禁。
- PostgreSQL 存储会话、消息、仓库、PR、审查运行、快照、发现、报告及必要证据/任务结果；不处理前端和 RAG。保留后端 HTTP、CLI、Gateway/WebSocket 的接口与事件验证。
- 旧 JSONL/JSON 原文件保留归档，不导入、不删除、不继续作为业务读写回退；新接口只展示数据库中的新数据。
- 每个 reviewer 一次接收本次接纳的整体输入；Judge 一次接收全部候选及有界证据包，不按文件、hunk 或候选拆成多个审查/裁决批次。预处理证据单元、工具分页不等于模型输入分批。超预算明确记录缺口，不静默分批或截断后宣称完整。
- reviewer、Judge 的模型最大迭代次数均为 30；工具次数、读取量和输出预算独立计数，不能把迭代次数等同于工具调用次数。保留最终结构化答案轮。
- 不修改用户维护的 ROADMAP.md；其他已有改动只在确有必要时局部合并，不覆盖或回滚。

### 数据与执行契约

- 流程：异步准入 → PR 快照准备 → 程序预处理/assignments → reviewer 并发执行 → validator → Judge 有限补读与裁决 → 报告、清理与终态事务。不重新引入 LLM Planner。
- PR 当前状态与快照分开。快照冻结 base/head 仓库、PR 编号、base_sha/head_sha/merge_base_sha、元数据、changed_files、patch、旧新路径/行映射、输入指纹和准备缺口。新侧读 head，旧侧读 merge-base；正确处理 fork、删除和重命名。
- PR 文件详情分页获取；准备前后复核版本，变化时重取一次，仍变化则失败。缺失 patch 优先由固定版本文本重建，无法获取时明确缺口；不能改读默认分支或把读取失败当作空变更。
- 预处理保留纯程序 diff 解析、hunk 定位与预算裁剪，输出接纳/跳过单元及原因；每个选定 reviewer 接收全部接纳证据。整体输入无法满足预算时明确未完成，不建立多个输入批次。
- reviewer 输出统一为 severity、title、location(file_id/side/start_line/end_line)、evidence_refs、impact、recommendation；severity 保留 critical/high/medium/low。程序生成候选 ID、SHA、来源和真实代码片段，模型只引用已提供的证据 ID。
- validator 校验引用存在性、版本、路径/行范围、变更关联及实际证据，不依赖本地 Path，不静默改正为另一个位置。
- Judge 输入包含全部候选、对应真实 diff、代码证据、validator 结果和覆盖缺口；复用 Runner，开放有限 pr_context 补读。输出 accept/reject/needs_confirmation、理由、additional_evidence_refs 和重复候选关联；仅裁决既有候选，新增证据再次确定性校验。整体输入超预算或关键补读失败时保留待确认，不分批补判。
- 报告区分确认发现、待确认项、执行结果和覆盖情况；只有证据/候选裁决及覆盖条件满足时才可表达“未发现问题”。空 findings、工具成功、执行结束均不自动代表完整覆盖。
- coordinator 保留每 run 运行态的唯一写入边界；黑板不是 checkpoint。最终 findings、report 和 run 终态同一 PostgreSQL 事务提交，随后发布终态事件。

### grep：复用 nanobot，建立共享纯文本内核

参考本机 C:/Users/Administrator/Desktop/nanobot，已核对 HEAD a4f2e98d8a4a7f0670a2f17ddb967755404d2934；这是本地参考版本，不声明为上游最新版本。

- 复用来源：nanobot/agent/tools/search.py 的 GrepTool、必要 glob/扫描预算 helper，以及 nanobot/agent/tools/_search_content.py 的 ContentPage。保留复制代码的 MIT 版权与许可。不要整体替换 NanoReview filesystem/Tool/Runner。
- 将内容匹配抽成最小共享核心，输入为路径、带行号的纯文本行流、匹配配置；负责固定字符串/正则、大小写、上下文合并、长行保留命中、跨文件 offset、结果数量/长度裁剪和续页位置，不包含目录遍历、网络、SHA、权限或数据库逻辑。
- 更新通用本地 GrepTool：复用 nanobot 的受限扫描、先检查文件大小、asyncio.to_thread、threading.Event 协作取消和 checkpoint。保留本项目 ToolResult、_FsTool、运行时路径 scope 和 ReviewReadLedger 的必要契约；测试 worker 中的 scope/contextvars 传播。
- 对齐纯文本参数 pattern/path/glob/type/case_insensitive/fixed_strings/output_mode/context_before/context_after/head_limit/offset；采用 content、前后各 5 行、head_limit=250 的默认值。output_mode 支持 content/files_with_matches/count；head_limit=0 仍受总输出预算约束，不复制旧隐藏兼容参数。
- 通用扫描初始默认沿用参考版本：30 秒、500000 路径、12000 字符输出；取消/超预算明确可见。这是协作式扫描预算，不宣称能强制打断单次 Python 正则调用。
- 不迁移 PDF/Office/表格解析、pages 参数、FindFilesTool、RgTool、外部 rg 依赖或整套 nanobot 文档读取基础设施。
- 本地与 PR 适配器共享同一匹配/分页实现；PR 内容不能借用本地 _resolve/os.walk 访问宿主目录，不建立另一套远端正则实现。

### pr_context：固定 SHA 读取与有界搜索

- 单一 Tool 按 action=diff/read/list/search 分发；宿主绑定 snapshot_id、仓库、SHA、角色策略和预算，不接受模型自行指定 URL、Token、任意 SHA 或角色权限。
- diff 读快照，read 按 old/new 读取固定版本文本，list 查询固定版本文件树；search 将 query/scope 等参数映射到共享 grep 核心。
- 搜索流程：固定 SHA 文件树 → 按路径/glob/type 稳定选择有限候选 → 命中缓存或异步获取缺失正文 → 共享内核匹配 → 生成可追溯证据。树截断时按预算补取子树，未补齐必须记录缺口；不使用默认分支 Code Search 冒充 PR 版本搜索。
- 搜索默认 fixed_strings=true，显式选择时才使用正则；scope 为仓库相对候选路径/目录。缓存身份包含仓库、SHA、路径；分页状态绑定快照、查询、模式、候选集合和稳定路径顺序，不沿用本地 mtime 排序。
- 搜索结果记录 searched_files、未读取/失败原因、候选与结果是否截断及续页位置。零匹配只表示实际搜索范围内无匹配；不能声称全仓不存在。目录查询和空搜索不算读取到代码证据。
- reviewer 开放全部四项；Judge 开放 diff/read/search，并绑定当前候选相关的有限路径范围。只读续聊绑定所选 run 的快照；不注册 shell、写文件或任意写入工具。
- 初始角色工具上限：每 reviewer 24 次，Judge 每次整体裁决 6 次；单次代码正文最多 200 行/8 KiB，每 run 额外源码最多 128 文件/10 MiB。命中缓存仍计工具调用，网络读取单独计账；额度可配置并冻结到 run。共享缓存不共享可变权限。
- 返回状态、来源版本/位置、实际内容、evidence_id、截断与续页信息；有效片段和 Judge 补读证据进入数据库，报告可复核。

### PostgreSQL 与后端入口

- 使用 SQLAlchemy async + asyncpg + Alembic；关系字段承载身份、外键、状态、时间和查询，带 schema_version 的 JSONB 保存复杂载荷。ORM 与 Agent DTO 分开，独立并发任务不共享 AsyncSession。
- 主表覆盖 sessions/messages、repositories/pull_requests、review_runs/review_snapshots、review_findings/review_reports，补充必要证据与 reviewer 结果。会话绑定一个 PR，可多次运行；消息关联所选 run，旧报告不随 PR 更新漂移。
- 请求幂等键只防重复提交；同一 SHA 允许手动重审。替换 SessionManager 与快照/report 的文件存取，同时 await CLI、Gateway、ConversationLoop、摘要合并与启动/关闭生命周期中的读写；工作区规则文件和 RAG 保持原职责。
- 新增 database.url、github.token，支持 NANOREVIEW_DATABASE_URL/NANOREVIEW_GITHUB_TOKEN 服务端覆盖；Token 不进入业务表、prompt、日志或报告。Alembic 显式迁移，启动检查 schema，不用 create_all 代替迁移。
- 公共接口：POST /api/repositories/resolve；GET /api/repositories/{id}/pull-requests（默认 open、page_size=30）；POST /api/review-runs（repo/PR/session/request_id，返回 202 与 run/session ID）；GET /api/review-runs/{id}、GET .../{id}/report、POST .../{id}/cancel。
- 独立 HTTP 服务与 Gateway/WebSocket 共用异步领域服务及业务接口注册；现有 session/report/code-context/delete 消费者同步改为数据库/快照，不能只改一个服务入口。CLI 使用 nanoreview review --repo <url> --pr <number>，删除本地审查参数。
- 首期单实例、有界并发，不加 Redis/Celery、多 worker 或 checkpoint resume。排队记录持久化；重启将遗留运行标记 interrupted，不自动恢复执行。
- status：queued/running/succeeded/failed/cancelled/interrupted；phase：prepare/preprocess/review/validate/judge/finalize/cleanup。保持后台准备、取消传播、usage、清理与持久化成功后开放讨论的顺序。

### 实施顺序与验收

1. **确定持久化与运行边界。** 核对现有计划/配置/调用方，建立 PostgreSQL 模型、迁移、异步仓储和生命周期；保留用户修改，仅在后续实施需要时局部同步 .agents 内的目标与现状文档。
2. **先迁移通用 grep 内核。** 复用已锁定 nanobot 的纯文本实现及相关测试，适配本项目结果、scope 和 ledger；先通过本地工具回归，再由 PR 层接入，避免建立两套搜索逻辑。
3. **实现 PR 准备与上下文适配。** 完成仓库/PR 分页、固定版本快照、路径树和内容缓存；pr_context.search 复用共享 matcher，不 clone、不读默认分支降级。
4. **改造审查数据流。** 切换预处理、reviewer schema/工具、validator 和 Judge 整体输入/补读；最大模型轮数 30，保持取消、输出纠正与证据覆盖语义。
5. **接通后端并移除旧分支。** 适配 CLI、HTTP、Gateway/WebSocket、报告与只读续聊；删除本地准入/采集/工作区降级及修复链路，不修改前端或 RAG。
6. **执行验收并收敛文档。** 最终结果/报告/终态事务提交后通知；按真实代码更新 architecture/security/budget/debug/gotchas 和受影响的 .agents 目标文档，不提前写成已实现。

### 测试与验证计划（尚未执行）

- grep：固定字符串/正则/大小写、glob/type、content/files/count、上下文合并、跨文件分页不重不漏、输出裁剪后可前进、长行命中可见；worker scope、协作取消、时间/路径/输出预算与现有去重隔离。
- 迁移参考测试：nanobot tests/tools/test_search_tools.py；本项目测试放在 tests/agent/tools/ 对应位置，保留 test_reviewer_read_dedup.py 的重复查询、内容变化及 run 隔离覆盖；验证同一 matcher 对本地/远端相同文本结果一致。
- PR：公共/私有权限、分页、fork、删除/重命名、准备期间更新、缺失 patch；固定 SHA 不读默认分支，候选/读取预算、缓存、树截断、API 失败、零匹配覆盖语义和续页状态。
- 审查：整体输入与最大 30 轮、有效/无效证据、空 findings、Judge 补读及权限、全部候选整体接收、超预算待确认、取消、清理和终态门禁；删除旧“GitHub 能力必须不存在”断言，替换为快照/角色边界。
- 持久化：真实 PostgreSQL 空库迁移、CRUD/摘要、同 SHA 重审与请求防重、重启历史读取/中断标记、最终报告与终态事务一致性。禁止用 SQLite 替代 PostgreSQL 集成验收。
- 后端闭环：CLI、HTTP、WebSocket 的仓库/PR 列表 → 审查 → 报告 → 只读讨论及取消；不运行前端构建/测试，不调整 RAG。
- 验证顺序：最近 grep/契约单测 → PostgreSQL/provider 集成 → 审查与后端闭环 → ruff check nanoreview/ → 全量 pytest → git diff --check。不改 RAG，既有相关回归仍需通过；环境缺项和既有失败单独说明，不当作通过。

## 实施进展（2026-11-01 合入 `main`）

- 已完成并验证：`agent/tools/search_core.py` 抽取 nanobot `a4f2e98d` 纯文本匹配/分页内核；`agent/tools/search.py` 改为 content 默认、5 行上下文、`head_limit=250`、30 秒/500000 路径/12000 字符协作预算、`asyncio.to_thread` + `threading.Event` 取消，删除 `max_matches`/`max_results`，保留 scope、`ToolResult` 与重复检索 ledger。`tests/agent/tools/test_search_tools.py` 32 项与 `test_reviewer_read_dedup.py` 通过。
- 已完成（未接入链路）：`storage/` 的 SQLAlchemy 模型、Alembic `0001_initial`、schema 头部校验、异步仓储与终态单事务；`review/input/github.py`、`review/input/pr_snapshot.py`、`review/source/pr_context.py`、`agent/tools/pr_context.py`。对应单测 40 项通过；PostgreSQL 集成用例在未设置 `NANOREVIEW_TEST_DATABASE_URL` 时跳过。
- 未实施：准入改为 PR 目标、reviewer/Judge 工具替换为 `pr_context`、validator 去掉本地 Path、SessionManager 与报告/快照存储迁移到 PostgreSQL、HTTP/CLI/Gateway 入口、删除本地审查与修复链路。
- 验证：全量 `pytest` 为 `1 failed / 1067 passed / 7 skipped`；唯一失败 `tests/channels/test_review_submission.py::test_accepted_submission_carries_admitted_marker` 断言已删除的 `SessionCoordinator._review_runs`，属既有基线失败，与本轮改动无关。`ruff check` 新增文件 0 项，`registry.py`/`sandbox.py` 的 3 项为既有问题。
- 环境：本机没有 PostgreSQL 服务，也没有 MSVC（`chroma-hnswlib` 无 Python 3.13 轮子）；测试用独立 `.venv` 与 `.pytest-tmp`。

## 已实施节点（待验收）

### 统一上下文治理、角色模型配置与 Dream 长期记忆（2026-10-31，待验收）

状态：已实施，待验收（worktree `nanoreview-ctx`，分支 `workbuddy/context-governance-dream`，基线 `7c000b1d`）。不恢复 Planner，不合并其他分支的在途重构。

实施内容：

1. **角色运行时**：新增 `agent/llm_runtime.py`（不可变 `LLMRuntime` + 字段级 `RoleModelOverrides` + `resolve_role_runtime`）。`review.reviewer` / `review.judge` 逐项覆盖，未设字段继承对话 agent；跨 provider 必须显式 `provider`，经 `make_role_provider_factory` 创建。review 准入时一次性冻结 reviewer/Judge runtime，写入 run snapshot 的 `models` 与 `budget` 段，中途 `/model` 不影响已启动审查。
2. **请求级上下文治理**：新增 `agent/context_governance.py`。唯一预算公式 `input_token_budget = window - max_output - 1024`；每轮重建请求（首部 system/developer 原样、压缩区间换成带标记的数据消息）；首包同样校验；三级降级（摘要 → 替换未发送的超大 `read_file` → `context_limit` 失败）；`AgentRunResult.compaction` 只统计被摘要覆盖的会话消息数。
3. **Consolidator 收敛**：只保存请求级压缩产出的 checkpoint（`persist_checkpoint`）并折叠回放窗口溢出（`fold_replay_overflow`），不再按 token 扫描，也不再有 turn 前后两套整理。
4. **Dream 长期记忆**：新增 `agent/dream.py`。`memory/history.jsonl` + cursor（只由对话压缩追加）、独立临时 run（对话模型 + 普通轮数，仅 `read_memory`/`write_memory` 两个工具）、暂存后宿主原子写回 `memory/MEMORY.md` 再推进 cursor、失败/取消不提交、合法无改动也推进。固定周日 03:00 Asia/Shanghai，窗口一个 tick、不补跑、当周标识持久化、每 workspace 互斥；`/dream` 手动入口进命令表与帮助；新增 `dream.enabled`（默认开，只关自动调度）。MEMORY 只在对话 turn 开始时读一次快照，审查角色看不到。

删除项（不要恢复）：`agent/compression.py` 全部（frozen/working 分区、`split_units`、60%/80% 软同步限、异步快照校验）、`AgentDefaults.consolidation_ratio`、`REVIEW_CONTEXT_WINDOW_TOKENS` / `REVIEWER_MAX_OUTPUT_TOKENS` / `REVIEWER_TIMEOUT_SECONDS`、`SubagentManager` 的角色级 `asyncio.timeout`、Judge 的 `asyncio.wait_for` 与固定 `temperature=0`/输出/窗口覆盖、`Consolidator.get_lock`/`maybe_consolidate_by_tokens`/`_persist_last_summary`。

验证：合并到 `main` 后全量 `pytest` `1058 passed`（本节点新增 `test_context_governance.py` 17 项、`test_llm_runtime.py` 14 项、`test_dream.py` 34 项、`test_long_term_memory_wiring.py` 11 项、`/dream` 命令断言 1 项）；`ruff check nanoreview/ tests/` 44 项（基线 45，`tests/` 0 项）；`git diff --check` 通过。删除 `tests/agent/test_runner_compression.py`、`tests/agent/test_subagent_reasoning_config.py`，其余测试随接口迁移。

遗留：WebUI 不新增 Dream 配置表单（本节点不做前端）；`ReviewRoutingMode` 的 Auto 文案适配仍是既有前端待办。

### 完整输入与统一请求预算（2026-10-31，待验收）

状态：已实施，待验收。生产代码、测试迁移与约束文档（`budget.md`、`architecture.md`）已同步。

- **统一预算入口**：新增 `review/budget.py::ReviewBudget`（`RequestBudget` + `from_review_config`），`ReviewConfig` 用 `context_window_tokens` / `reviewer_max_output_tokens` / `reviewer_context_read_reserve_tokens` / `budget_safety_margin_tokens` 替代 `evidence_token_budget`（后者已删除，传入被忽略）。`REVIEW_CONTEXT_WINDOW_TOKENS` / `REVIEWER_MAX_OUTPUT_TOKENS` 常量删除，`review_budget_contract()` 改为投影 7 个字段。
- **reviewer 完整输入 + 超限拒绝**：`_patch_units` 一个改动文件一个完整 unit（删除 8k 阈值切片、超大 hunk 跳过、`_apply_main_budget` 贪心挑片、`EvidenceBudget`、`mode`/`accepted_tokens`）；`prefetch` 的 `excerpt=unit.text` 不再截到 40k。`_require_reviewer_request_budget` 在 `start_assignments` 之前逐个所选 reviewer 校验首请求（system prompt + 完整 diff + 工具 schema 常量），任一超限抛 `ReviewPlanningError`：零 reviewer 启动、run `error`。
- **Judge 一次裁决**：删除分批、跨批合并与 `ReviewJudgeStats.batches`；裁决前先查输出上限（裁决 skeleton + 每候选 48 token reason 预留 ≤ `judge.max_tokens`），再查整体请求是否装进窗口。不满足即 `budget_rejected=True`、`verdicts={}`、不调用模型；`ReviewFinalizer` 把候选全标 `needs_confirmation`，报告输出 `### DRAFT - Not Adjudicated` 草稿，`ReviewLoop` 落 run `error` 但保留可读报告（`ReviewLoopOutcome(stop_reason="error", produces_report=True)`）。结构纠错重试仍是同一批。
- **coverage 语义收敛**：skipped reason 只剩 `empty_patch` 与证据收集器的文件级原因（`binary_file` / `filtered` 等），`token_limit_exceeded` / `budget_exhausted` 删除；报告「Scope Not Reviewed」文案同步。
- 结构收敛（用户复核后）：① 删掉只用于日志的第二次输入估算（`_estimate_input_tokens` 读目标文件+拼 diff+再估一次），预检把已测得的 `prompt_tokens` 按维度返回，日志与 dispatch 复用；② 预算对象只建一次：`ReviewJudgeConfig.budget` 直接持有 `ReviewBudget`，删除 `context_window_tokens`/`max_tokens`/`safety_margin_tokens` 三个标量、`_review_budget()` 重建与 `_resolve_context_window()` 兜底，`from_review_config` 改为对已校验 `ReviewConfig` 的纯投影（去掉 `Any/getattr/_int_option`）；③ reviewer/judge 共用 `helpers.py::estimate_prompt_tokens_with_fallback`（tiktoken → 上界），各角色不再自写回退；④ `ReviewLoop.execute` 的两个失败出口与成功出口合并成「先定 status+reason，再走一次 cleanup/settle」，`ReviewFinalizer` 四处「未裁决」出口合并为 `_unjudged()`；⑤ `judge_request` 去掉恒不生效的 `max(cap, verdict)` 与 `verdict_tokens` 参数；⑥ `SessionCoordinator.review_config` 归一化为 `ReviewConfig()`，后续读真实字段而非 getattr 默认值。
- 复审后补齐（两轮子 agent 审查）：① 无 tokenizer 时按「非 ASCII 1 token/字符」估算上界（`utils/helpers.py::estimate_text_tokens_upper_bound`，reviewer/judge/日志估算共用），否则中文 diff 被低估约 3 倍而通过预检；② 草稿 run 的 report artifact 改为写入 `status=error` 且 `warnings` 含拒绝原因（`record_report(status=)`），不再出现 artifact 说 completed、board 说 error；③ `judge.maxTokens` 默认 2_048 → 8_192（一次裁决全部候选后 2 048 只够约 23 个候选）；④ 删除无消费者的 `ReviewMetaKey.DIFF_CONTEXT_WINDOW_TOKENS`；⑤ 修正预算用例：窗口大于固定预留、只有 payload 能触发拒绝，并补同类对照用例与「中文无 tokenizer 仍拒绝」用例；⑥ 补配置→预算接线断言（judge 窗口/cap/margin、reviewer limits、snapshot 的 budget 记录）。
- 验证：`pytest tests/` 为 `25 failed / 1005 passed`（25 项仍是本机禁止派生子进程的既有 git 子进程基线：admission 10、review_gate 10、rag 3、api 1、channels 1）；`ruff check nanoreview/` 45 项（与基线同一组错误，改动文件未新增；`utils/helpers.py` 的 I001 为基线既有）、`tests/` 0 项（基线 3 项 I001 已消除）；`git diff --check` 通过。新增覆盖：`test_review_budget.py`（10）、`test_preprocessor.py`（24，含「多 hunk 不拆」「超阈值不跳」「无预算切分 API」「estimate_tokens 契约」）、`test_judge.py`（含 payload 驱动的超限拒绝与对照、窗口小于自身预留、输出上限不足、中文兜底、纠错不计第二批）、`tests/utils/test_helpers.py`（4，上界估算）、`test_review_loop.py::test_every_reviewer_receives_the_whole_diff_untouched`、`::test_an_over_budget_reviewer_rejects_the_run_before_any_starts`、`::test_a_reviewer_request_that_fits_is_dispatched`、`::test_a_cjk_diff_without_a_tokenizer_still_rejects`、`::test_an_unserviceable_judge_settles_the_run_as_a_draft`。
- **待适配（前端）**：WebUI 未展示 judge 分批（已无该概念），本次无需改动；若后续要呈现「草稿」态，需前端消费 report 的 DRAFT 段与 run `error`（artifact 的 `status` 现已如实为 `error`）。

## 已实施节点（已验收）

### 移除 Planner，改为手动选择 Reviewer（2026-10-28）

- Planner 的 LLM 分诊整体移除：用户在准入时用 `focus` 直接选 reviewer（缺省/`null`/空列表 → 只跑 `general`），五个 reviewer 可任意组合、含全选。`auto/special/general` 三态路由语义删除，显式 `auto` 视为非法 reviewer 名称。
- 执行链路改为 `冻结 diff → 预处理 → 程序生成 assignments → Reviewer 并发审查 → 校验 → Judge → 报告`：每个选中的 reviewer 接收全部接纳证据（`evidence.references`），不经过 Planner manifest 二次筛选；assignment 数量上限（4）已移除，并发上限（默认 4）保留，全选五项时排队执行。
- 删除仅服务 Planner 的模块：`review/planning/manifest.py`、`review/planning/triage.py`、`agent/tools/review_plan.py` 及对应测试。`plan` 进度阶段保留，含义改为程序生成 assignments；前端消费的阶段名、状态、报告引用与汇总 usage 不变。
- 新 snapshot 与报告不再写 Planner 专属数据，改记预处理接纳、任务分配与预算跳过；报告去掉 `Mode:` 行与 `Triage Outcome` 段，只列所选 Reviewer。
- **前端暂不修改**：现有 Auto 将启动 general，Custom 继续支持多选；Auto 文案暂时不准确，记录为后续前端事项。
- 验收口径：新审查不再产生 Planner 模型调用；默认 general、五项自由组合生效；现有证据准备与审查能力保留，前端现有提交流程可继续使用。
- 遗留：当前确认节点（run 黑板与结构化输出收敛）里对 Planner 角色的描述需按「无 Planner」重写，尚未实施。

### run 黑板与结构化输出收敛（2026-10-25，已验收）

状态：已验收（2026-10-25）。生产代码、测试迁移与约束文档均已完成，经四轮 subagent 复审收敛。全量 `pytest` 为 `25 failed / 1042 passed`，25 项全部是本机禁止派生子进程导致的 git 子进程环境失败（admission 10、rag 3、channels 1、review_gate 10、api 1），属既有基线；`ruff check nanoreview/` 45 项、`tests/` 0 项（均优于或等于基线）；`git diff --check` 通过。复审修复项：`coordinator.result()` 的 running 守卫、`/new` 的持久标识清理、stop/error 出口的 summary、结构化 run 的终局答案轮（含提示双写与被拒即 `invalid_output`）、`drop_unstarted` 的 run 归属与持久标识清理、`process_direct` 各返回路径的 run 释放、judge 空 verdicts 的降级语义与提示一致、reviewer findings 一次报全字段问题。既有节点的待验收与后续事项另列，不自动纳入本节点范围。

#### 已确认边界

- `nanoreview/agent/coordinator.py` 创建并持有每个 run 的黑板，是唯一写入者。`ReviewLoop` 保留阶段编排，通过注入的窄接口请求更新，不持有可直接修改的黑板对象。
- agent 仅在完成后返回结构化结果；校验成功后由 coordinator 写入黑板。不提供 agent 中途写入、共享草稿或 reviewer 互评。
- reviewer 不读写黑板，仅获得本次 assignment 的冻结输入。Planner、Judge 可通过受限查询接口只读当前 run 的授权数据，其结果也交由 coordinator 写入。
- 对话 agent 不读黑板，只获取最终 report；保留现有 review 完成门禁。子审查 agent 结果全部完成写入是必要条件，仍须经过 validator、Judge、清理和终态持久化后才能开放对话。
- 黑板统一承载必要运行状态，迁移后删除旧 `ReviewRunState` 可变容器及重复结果存储。不恢复中断任务，不把黑板做成恢复执行的 checkpoint；保留历史最终 report 读取和遗留 running 的中断标记。
- 移除 review 全链路的 terminal tool 提交协议，改为解析最终结构化输出。ConversationLoop 保持自由文本输出，探索类工具及角色原有输入权限不因本次调整扩大。

#### 数据与职责

| 数据 | 产生方 | 生效方式 |
|---|---|---|
| 输入引用、计划、assignments、任务状态、usage、错误、phase/status | 准入与执行宿主 | 请求 coordinator 更新 |
| triage decisions | Planner 最终返回 | 纯校验通过后由 coordinator 写入并派生 assignments |
| reviewer 原始结果、候选及来源 | reviewer 最终返回 | 纯校验通过后由 coordinator 写入；失败、取消和超时记录独立状态 |
| 证据与位置校验结果 | 程序 validator | coordinator 写入，不覆盖原始来源 |
| verdicts、理由、判定状态 | Judge 最终返回 | 对冻结候选版本校验后由 coordinator 写入 |
| 最终 findings、coverage、report 引用与终态 | review 收尾流程 | coordinator 更新，按现有契约生成最终 report 并发布终态 |

黑板按 run/session 绑定，字段只有一个可变权威；只读接口返回有界视图，不泄露可变对象引用。数据类型、纯校验与查询模型可独立放置，coordinator 负责持有、写入和注入，不把所有业务逻辑堆入同一文件。最终 report 是已结算输出，不是第二份运行状态。

#### 实施步骤

1. **定义黑板与最终输出契约。** 核对 `review_state.py`、准入、Loop、API 和 report 消费者，列出必要字段及读写归属；定义 Planner、reviewer、Judge 的最终结果 schema，复用现有归一化与业务校验。合法空 findings、执行失败及未完成必须可区分。
2. **建立 coordinator 写入边界。** 创建每 run 黑板，向 ReviewLoop 注入只读视图与更新接口；准入注册、任务状态、usage、校验结果、取消及收尾均通过 coordinator 更新。并发更新在 coordinator 内串行化；结果与对应任务状态一致发布，锁不跨模型或工具调用持有。
3. **替换 Runner 完成协议。** 使用现有 `response_format`/`final_content` 入口，增加通用最终结果解析和校验；合法结果才完成，失败反馈具体错误并在同 run 中有界纠正。校验器不得修改黑板。保留最后一轮收尾预算、超时、取消、压缩和 usage；截断输出不得当成功，也不能沿用普通文本“继续”来拼接 JSON。
4. **迁移三个 review 角色。** Planner 通过授权只读接口读取 frozen diff，最终一次返回 decisions，替换增量 terminal 提交；reviewer 返回 findings，替换 `review_submit`；Judge 从全部 reviewer 完成并经 validator 处理的冻结候选视图读取数据，最终返回 verdicts。保留分诊覆盖、空发现证据检查、候选归一化及缺失判定的既有语义。
5. **切换消费者并清理旧链路。** API、coverage、report、门禁与清理读取黑板投影后，删除 `ReviewRunState` 旧容器、`_review_runs` 别名及重复累积结果的 receiver/finalizer 状态；删除 review 结果经工具事件、completion JSON、`InboundMessage`、session metadata 反复搬运的路径。完成通知仅唤醒宿主，不携带完整结果；核对调用方后清理 terminal 工具、Runner 专属配置、分支和诊断字段，保留仍有用途的通用能力及观测事件。
6. **验证外部闭环并同步文档。** 保留最终 report artifact、首次 report handoff、历史 report 读取和中断归一化；对话仍不接入黑板。实施后按真实代码更新 architecture、security、budget、debug、gotchas 及 roadmap 中受影响的现状或目标，不提前写成已实现。

Provider 原生 schema 支持不一致：OpenAI-compatible 设置格式参数可能切换调用分支，Anthropic 有 schema 映射，Codex provider 当前忽略格式参数，基类也可能降级重试。复用当前支持路径，支持时发送原生约束；其余通过输出契约和本地校验处理，不承诺所有 provider 都保证严格生成。

#### 验收与验证

- 权限：只有 coordinator 能改变黑板；reviewer 无查询/写入入口；Planner、Judge 仅只读当前 run 的授权数据；对话 agent 只接收最终 report。
- 完成协议：非法 JSON、字段或业务数据可在同 run 中有限纠正；普通散文、输出截断及失败不算成功；合法零发现保留已有证据要求；校验失败不写入有效结果。
- 并发与阶段：多个 reviewer 完成时不丢结果、不重复计数；部分结果不提前启动 Judge 或开放对话；失败/取消仍明确记录，不能伪装成无发现；Judge 判定与最终报告使用同一候选版本。
- 生命周期：report 生成、清理、终态保存及对话门禁保持现有顺序；保存或清理失败不提前开放；重启后可读历史最终 report，遗留 running 能标记中断且不重跑 agent。
- 回归：Planner 分诊及 evidence 覆盖、validator、Judge 降级、usage、报告/API/CLI 与对话交接保持可用；迁移 terminal 测试时保留等价行为覆盖，删除旧状态测试前先迁移必要契约。

按步骤运行最近测试，改名或删除测试文件时同步迁移覆盖：

```powershell
pytest tests/agent/tools/test_runner_tool_errors.py tests/agent/test_runner_compression.py tests/providers/test_anthropic_provider.py tests/providers/test_modelscope_provider.py
pytest tests/review/test_reviewer_profiles.py tests/review/test_judge.py tests/review/test_finalizer.py tests/review/test_validator.py tests/review/test_report.py tests/review/test_quality.py
pytest tests/agent/test_review_loop.py tests/agent/test_review_state.py tests/agent/test_loop_modes.py tests/agent/test_coordinator.py tests/agent/test_conversation_loop.py tests/agent/test_review_gate.py
pytest tests/review/test_admission.py tests/review/test_result.py tests/api/test_review_admission.py tests/channels/test_review_submission.py tests/webui/test_review_report_api.py tests/cli/test_review_chat.py
ruff check nanoreview/
pytest
git diff --check
```

另补黑板所有权/角色权限及结构化输出契约测试；遵循本文件既有“实施 → 自查 → codex 循环复审”流程。历史验证记录不作为本节点验证结果。
## 其他待验收与后续事项

- diff-only 预处理清理（2026-10-22 追加：移除 `classify_path` 语言分类与 `unsupported_code_type` 跳过，任何路径的 diff 都按文本证据处理）、reviewer 三态/general 均已实施，待验收；原 Planner 分诊随节点移除作废。
- WebUI 的 reviewer mode 三态适配、Dream 配置表单与草稿态展示留待已确认的前端节点。
- `tests/agent/rag/test_rag_review.py` 的 local_changed_summary 用例依赖 git 子进程，在本机沙箱中属既有环境失败。

