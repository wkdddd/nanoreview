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
- 验证：合入 `main` 后全量 `pytest` 为 `1119 passed / 7 skipped`（7 项为未配置 `NANOREVIEW_TEST_DATABASE_URL` 的 PostgreSQL 集成用例）；`ruff check` 在新增文件上 0 项（`registry.py`/`sandbox.py` 的 3 项为既有问题）；`git diff --check` 通过。
- 环境：本机没有 PostgreSQL 服务，也没有 MSVC（`chroma-hnswlib` 无 Python 3.13 轮子）；测试用独立 `.venv` 与 `.pytest-tmp`。

