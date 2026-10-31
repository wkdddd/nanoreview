# 当前代码调整计划

更新时间：2026-10-31

## 当前节点：统一上下文治理、角色模型配置与 Dream 长期记忆

状态：已实施，待验收（worktree `nanoreview-ctx`，分支 `workbuddy/context-governance-dream`，基线 `7c000b1d`）。不恢复 Planner，不合并其他分支的在途重构。

实施内容：

1. **角色运行时**：新增 `agent/llm_runtime.py`（不可变 `LLMRuntime` + 字段级 `RoleModelOverrides` + `resolve_role_runtime`）。`review.reviewer` / `review.judge` 逐项覆盖，未设字段继承对话 agent；跨 provider 必须显式 `provider`，经 `make_role_provider_factory` 创建。review 准入时一次性冻结 reviewer/Judge runtime，写入 run snapshot 的 `models` 与 `budget` 段，中途 `/model` 不影响已启动审查。
2. **请求级上下文治理**：新增 `agent/context_governance.py`。唯一预算公式 `input_token_budget = window - max_output - 1024`；每轮重建请求（首部 system/developer 原样、压缩区间换成带标记的数据消息）；首包同样校验；三级降级（摘要 → 替换未发送的超大 `read_file` → `context_limit` 失败）；`AgentRunResult.compaction` 只统计被摘要覆盖的会话消息数。
3. **Consolidator 收敛**：只保存请求级压缩产出的 checkpoint（`persist_checkpoint`）并折叠回放窗口溢出（`fold_replay_overflow`），不再按 token 扫描，也不再有 turn 前后两套整理。
4. **Dream 长期记忆**：新增 `agent/dream.py`。`memory/history.jsonl` + cursor（只由对话压缩追加）、独立临时 run（对话模型 + 普通轮数，仅 `read_memory`/`write_memory` 两个工具）、暂存后宿主原子写回 `memory/MEMORY.md` 再推进 cursor、失败/取消不提交、合法无改动也推进。固定周日 03:00 Asia/Shanghai，窗口一个 tick、不补跑、当周标识持久化、每 workspace 互斥；`/dream` 手动入口进命令表与帮助；新增 `dream.enabled`（默认开，只关自动调度）。MEMORY 只在对话 turn 开始时读一次快照，审查角色看不到。

删除项（不要恢复）：`agent/compression.py` 全部（frozen/working 分区、`split_units`、60%/80% 软同步限、异步快照校验）、`AgentDefaults.consolidation_ratio`、`REVIEW_CONTEXT_WINDOW_TOKENS` / `REVIEWER_MAX_OUTPUT_TOKENS` / `REVIEWER_TIMEOUT_SECONDS`、`SubagentManager` 的角色级 `asyncio.timeout`、Judge 的 `asyncio.wait_for` 与固定 `temperature=0`/输出/窗口覆盖、`Consolidator.get_lock`/`maybe_consolidate_by_tokens`/`_persist_last_summary`。

验证：`pytest` 全量 `1030 passed`（含新增 `test_context_governance.py` 17 项、`test_llm_runtime.py` 14 项、`test_dream.py` 34 项、`test_long_term_memory_wiring.py` 11 项、`/dream` 命令断言 1 项）；`ruff check nanoreview/ tests/` 44 项（基线 45，`tests/` 0 项）；`git diff --check` 通过。删除 `tests/agent/test_runner_compression.py`、`tests/agent/test_subagent_reasoning_config.py`，其余测试随接口迁移。

遗留：WebUI 不新增 Dream 配置表单（本节点不做前端）；`ReviewRoutingMode` 的 Auto 文案适配仍是既有前端待办。

## 既有节点待验收与后续事项

- diff-only 预处理清理（2026-10-22 追加：移除 `classify_path` 语言分类与 `unsupported_code_type` 跳过，任何路径的 diff 都按文本证据处理）、reviewer 三态/general、Planner diff 分诊均已实施，待验收。
- 原 Planner 节点已整体移除（2026-10-28），其 golden diff replay 缺口随节点一并作废。
- WebUI 的 reviewer mode 三态适配留待已确认的前端节点。

## 已实施节点：移除 Planner，改为手动选择 Reviewer（2026-10-28）

- Planner 的 LLM 分诊整体移除：用户在准入时用 `focus` 直接选 reviewer（缺省/`null`/空列表 → 只跑 `general`），五个 reviewer 可任意组合、含全选。`auto/special/general` 三态路由语义删除，显式 `auto` 视为非法 reviewer 名称。
- 执行链路改为 `冻结 diff → 预处理 → 程序生成 assignments → Reviewer 并发审查 → 校验 → Judge → 报告`：每个选中的 reviewer 接收全部接纳证据（`evidence.references`），不经过 Planner manifest 二次筛选；assignment 数量上限（4）已移除，并发上限（默认 4）保留，全选五项时排队执行。
- 删除仅服务 Planner 的模块：`review/planning/manifest.py`、`review/planning/triage.py`、`agent/tools/review_plan.py` 及对应测试。`plan` 进度阶段保留，含义改为程序生成 assignments；前端消费的阶段名、状态、报告引用与汇总 usage 不变。
- 新 snapshot 与报告不再写 Planner 专属数据，改记预处理接纳、任务分配与预算跳过；报告去掉 `Mode:` 行与 `Triage Outcome` 段，只列所选 Reviewer。
- **前端暂不修改**：现有 Auto 将启动 general，Custom 继续支持多选；Auto 文案暂时不准确，记录为后续前端事项。
- 验收口径：新审查不再产生 Planner 模型调用；默认 general、五项自由组合生效；现有证据准备与审查能力保留，前端现有提交流程可继续使用。
- 遗留：当前确认节点（run 黑板与结构化输出收敛）里对 Planner 角色的描述需按「无 Planner」重写，尚未实施。

## 已实施节点：完整输入与统一请求预算（2026-10-31）

状态：已实施，待验收。生产代码、测试迁移与约束文档（`budget.md`、`architecture.md`）已同步。

- **统一预算入口**：新增 `review/budget.py::ReviewBudget`（`RequestBudget` + `from_review_config`），`ReviewConfig` 用 `context_window_tokens` / `reviewer_max_output_tokens` / `reviewer_context_read_reserve_tokens` / `budget_safety_margin_tokens` 替代 `evidence_token_budget`（后者已删除，传入被忽略）。`REVIEW_CONTEXT_WINDOW_TOKENS` / `REVIEWER_MAX_OUTPUT_TOKENS` 常量删除，`review_budget_contract()` 改为投影 7 个字段。
- **reviewer 完整输入 + 超限拒绝**：`_patch_units` 一个改动文件一个完整 unit（删除 8k 阈值切片、超大 hunk 跳过、`_apply_main_budget` 贪心挑片、`EvidenceBudget`、`mode`/`accepted_tokens`）；`prefetch` 的 `excerpt=unit.text` 不再截到 40k。`_require_reviewer_request_budget` 在 `start_assignments` 之前逐个所选 reviewer 校验首请求（system prompt + 完整 diff + 工具 schema 常量），任一超限抛 `ReviewPlanningError`：零 reviewer 启动、run `error`。
- **Judge 一次裁决**：删除分批、跨批合并与 `ReviewJudgeStats.batches`；裁决前先查输出上限（裁决 skeleton + 每候选 48 token reason 预留 ≤ `judge.max_tokens`），再查整体请求是否装进窗口。不满足即 `budget_rejected=True`、`verdicts={}`、不调用模型；`ReviewFinalizer` 把候选全标 `needs_confirmation`，报告输出 `### DRAFT - Not Adjudicated` 草稿，`ReviewLoop` 落 run `error` 但保留可读报告（`ReviewLoopOutcome(stop_reason="error", produces_report=True)`）。结构纠错重试仍是同一批。
- **coverage 语义收敛**：skipped reason 只剩 `empty_patch` 与证据收集器的文件级原因（`binary_file` / `filtered` 等），`token_limit_exceeded` / `budget_exhausted` 删除；报告「Scope Not Reviewed」文案同步。
- 结构收敛（用户复核后）：① 删掉只用于日志的第二次输入估算（`_estimate_input_tokens` 读目标文件+拼 diff+再估一次），预检把已测得的 `prompt_tokens` 按维度返回，日志与 dispatch 复用；② 预算对象只建一次：`ReviewJudgeConfig.budget` 直接持有 `ReviewBudget`，删除 `context_window_tokens`/`max_tokens`/`safety_margin_tokens` 三个标量、`_review_budget()` 重建与 `_resolve_context_window()` 兜底，`from_review_config` 改为对已校验 `ReviewConfig` 的纯投影（去掉 `Any/getattr/_int_option`）；③ reviewer/judge 共用 `helpers.py::estimate_prompt_tokens_with_fallback`（tiktoken → 上界），各角色不再自写回退；④ `ReviewLoop.execute` 的两个失败出口与成功出口合并成「先定 status+reason，再走一次 cleanup/settle」，`ReviewFinalizer` 四处「未裁决」出口合并为 `_unjudged()`；⑤ `judge_request` 去掉恒不生效的 `max(cap, verdict)` 与 `verdict_tokens` 参数；⑥ `SessionCoordinator.review_config` 归一化为 `ReviewConfig()`，后续读真实字段而非 getattr 默认值。
- 复审后补齐（两轮子 agent 审查）：① 无 tokenizer 时按「非 ASCII 1 token/字符」估算上界（`utils/helpers.py::estimate_text_tokens_upper_bound`，reviewer/judge/日志估算共用），否则中文 diff 被低估约 3 倍而通过预检；② 草稿 run 的 report artifact 改为写入 `status=error` 且 `warnings` 含拒绝原因（`record_report(status=)`），不再出现 artifact 说 completed、board 说 error；③ `judge.maxTokens` 默认 2_048 → 8_192（一次裁决全部候选后 2 048 只够约 23 个候选）；④ 删除无消费者的 `ReviewMetaKey.DIFF_CONTEXT_WINDOW_TOKENS`；⑤ 修正预算用例：窗口大于固定预留、只有 payload 能触发拒绝，并补同类对照用例与「中文无 tokenizer 仍拒绝」用例；⑥ 补配置→预算接线断言（judge 窗口/cap/margin、reviewer limits、snapshot 的 budget 记录）。
- 验证：`pytest tests/` 为 `25 failed / 1005 passed`（25 项仍是本机禁止派生子进程的既有 git 子进程基线：admission 10、review_gate 10、rag 3、api 1、channels 1）；`ruff check nanoreview/` 45 项（与基线同一组错误，改动文件未新增；`utils/helpers.py` 的 I001 为基线既有）、`tests/` 0 项（基线 3 项 I001 已消除）；`git diff --check` 通过。新增覆盖：`test_review_budget.py`（10）、`test_preprocessor.py`（24，含「多 hunk 不拆」「超阈值不跳」「无预算切分 API」「estimate_tokens 契约」）、`test_judge.py`（含 payload 驱动的超限拒绝与对照、窗口小于自身预留、输出上限不足、中文兜底、纠错不计第二批）、`tests/utils/test_helpers.py`（4，上界估算）、`test_review_loop.py::test_every_reviewer_receives_the_whole_diff_untouched`、`::test_an_over_budget_reviewer_rejects_the_run_before_any_starts`、`::test_a_reviewer_request_that_fits_is_dispatched`、`::test_a_cjk_diff_without_a_tokenizer_still_rejects`、`::test_an_unserviceable_judge_settles_the_run_as_a_draft`。
- **待适配（前端）**：WebUI 未展示 judge 分批（已无该概念），本次无需改动；若后续要呈现「草稿」态，需前端消费 report 的 DRAFT 段与 run `error`（artifact 的 `status` 现已如实为 `error`）。

## 当前确认节点：run 黑板与结构化输出收敛

状态：已验收（2026-10-25）。生产代码、测试迁移与约束文档均已完成，经四轮 subagent 复审收敛。全量 `pytest` 为 `25 failed / 1042 passed`，25 项全部是本机禁止派生子进程导致的 git 子进程环境失败（admission 10、rag 3、channels 1、review_gate 10、api 1），属既有基线；`ruff check nanoreview/` 45 项、`tests/` 0 项（均优于或等于基线）；`git diff --check` 通过。复审修复项：`coordinator.result()` 的 running 守卫、`/new` 的持久标识清理、stop/error 出口的 summary、结构化 run 的终局答案轮（含提示双写与被拒即 `invalid_output`）、`drop_unstarted` 的 run 归属与持久标识清理、`process_direct` 各返回路径的 run 释放、judge 空 verdicts 的降级语义与提示一致、reviewer findings 一次报全字段问题。既有节点的待验收与后续事项另列，不自动纳入本节点范围。

### 已确认边界

- `nanoreview/agent/coordinator.py` 创建并持有每个 run 的黑板，是唯一写入者。`ReviewLoop` 保留阶段编排，通过注入的窄接口请求更新，不持有可直接修改的黑板对象。
- agent 仅在完成后返回结构化结果；校验成功后由 coordinator 写入黑板。不提供 agent 中途写入、共享草稿或 reviewer 互评。
- reviewer 不读写黑板，仅获得本次 assignment 的冻结输入。Planner、Judge 可通过受限查询接口只读当前 run 的授权数据，其结果也交由 coordinator 写入。
- 对话 agent 不读黑板，只获取最终 report；保留现有 review 完成门禁。子审查 agent 结果全部完成写入是必要条件，仍须经过 validator、Judge、清理和终态持久化后才能开放对话。
- 黑板统一承载必要运行状态，迁移后删除旧 `ReviewRunState` 可变容器及重复结果存储。不恢复中断任务，不把黑板做成恢复执行的 checkpoint；保留历史最终 report 读取和遗留 running 的中断标记。
- 移除 review 全链路的 terminal tool 提交协议，改为解析最终结构化输出。ConversationLoop 保持自由文本输出，探索类工具及角色原有输入权限不因本次调整扩大。

### 数据与职责

| 数据 | 产生方 | 生效方式 |
|---|---|---|
| 输入引用、计划、assignments、任务状态、usage、错误、phase/status | 准入与执行宿主 | 请求 coordinator 更新 |
| triage decisions | Planner 最终返回 | 纯校验通过后由 coordinator 写入并派生 assignments |
| reviewer 原始结果、候选及来源 | reviewer 最终返回 | 纯校验通过后由 coordinator 写入；失败、取消和超时记录独立状态 |
| 证据与位置校验结果 | 程序 validator | coordinator 写入，不覆盖原始来源 |
| verdicts、理由、判定状态 | Judge 最终返回 | 对冻结候选版本校验后由 coordinator 写入 |
| 最终 findings、coverage、report 引用与终态 | review 收尾流程 | coordinator 更新，按现有契约生成最终 report 并发布终态 |

黑板按 run/session 绑定，字段只有一个可变权威；只读接口返回有界视图，不泄露可变对象引用。数据类型、纯校验与查询模型可独立放置，coordinator 负责持有、写入和注入，不把所有业务逻辑堆入同一文件。最终 report 是已结算输出，不是第二份运行状态。

### 实施步骤

1. **定义黑板与最终输出契约。** 核对 `review_state.py`、准入、Loop、API 和 report 消费者，列出必要字段及读写归属；定义 Planner、reviewer、Judge 的最终结果 schema，复用现有归一化与业务校验。合法空 findings、执行失败及未完成必须可区分。
2. **建立 coordinator 写入边界。** 创建每 run 黑板，向 ReviewLoop 注入只读视图与更新接口；准入注册、任务状态、usage、校验结果、取消及收尾均通过 coordinator 更新。并发更新在 coordinator 内串行化；结果与对应任务状态一致发布，锁不跨模型或工具调用持有。
3. **替换 Runner 完成协议。** 使用现有 `response_format`/`final_content` 入口，增加通用最终结果解析和校验；合法结果才完成，失败反馈具体错误并在同 run 中有界纠正。校验器不得修改黑板。保留最后一轮收尾预算、超时、取消、压缩和 usage；截断输出不得当成功，也不能沿用普通文本“继续”来拼接 JSON。
4. **迁移三个 review 角色。** Planner 通过授权只读接口读取 frozen diff，最终一次返回 decisions，替换增量 terminal 提交；reviewer 返回 findings，替换 `review_submit`；Judge 从全部 reviewer 完成并经 validator 处理的冻结候选视图读取数据，最终返回 verdicts。保留分诊覆盖、空发现证据检查、候选归一化及缺失判定的既有语义。
5. **切换消费者并清理旧链路。** API、coverage、report、门禁与清理读取黑板投影后，删除 `ReviewRunState` 旧容器、`_review_runs` 别名及重复累积结果的 receiver/finalizer 状态；删除 review 结果经工具事件、completion JSON、`InboundMessage`、session metadata 反复搬运的路径。完成通知仅唤醒宿主，不携带完整结果；核对调用方后清理 terminal 工具、Runner 专属配置、分支和诊断字段，保留仍有用途的通用能力及观测事件。
6. **验证外部闭环并同步文档。** 保留最终 report artifact、首次 report handoff、历史 report 读取和中断归一化；对话仍不接入黑板。实施后按真实代码更新 architecture、security、budget、debug、gotchas 及 roadmap 中受影响的现状或目标，不提前写成已实现。

Provider 原生 schema 支持不一致：OpenAI-compatible 设置格式参数可能切换调用分支，Anthropic 有 schema 映射，Codex provider 当前忽略格式参数，基类也可能降级重试。复用当前支持路径，支持时发送原生约束；其余通过输出契约和本地校验处理，不承诺所有 provider 都保证严格生成。

### 验收与验证

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
