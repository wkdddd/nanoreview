# Budget and Context

当前实现入口：`config/schema.py`、`review/planning/preprocessor.py`、`agent/context_governance.py`、`agent/llm_runtime.py`、`agent/runner.py`、`agent/memory.py`、`agent/dream.py`。默认值与公式以代码为准。

## 预算边界

- 单次请求预算、单次输出、上下文窗口、执行轮数、并发和超时分别约束不同资源；不能互相替代。
- 当前没有按累计 token、金额或日/月周期实现的本地总配额。`prefetch_budget_chars` 仅约束预处理的上下文文本。
- 预处理跳过原因必须进入报告覆盖说明；工具输出裁剪需保留足够诊断信息。
- 上下文估算优先使用 Provider counter，其次 `tiktoken`；两者都不可用时，review 的预算预检按「非 ASCII 字符各计 1 token、其余 3 字符/token」的上界估算（中文按 4 字符/token 会低估约 3 倍，让超窗请求通过检查）。reviewer 与 Judge 共用 `helpers.py::estimate_prompt_tokens_with_fallback` 这一条策略，各角色不再自己实现回退。估算不等于计费 usage。

## 上下文治理

- **一个预算公式**：`input_token_budget(window, max_output) = window - max_output - 1024`（`agent/context_governance.py`）。governor、Judge 请求预检、review 回放窗口、handoff 首包检查都调用它——预判与执行必须同源，否则「计划内的请求」会在发包时被拒。
- `AgentRunSpec` 只有一份 `messages`（原始 transcript，只追加不改写）；请求由 `ContextGovernor.prepare_request` 每轮重建：首部连续 `system`/`developer` 原样、被压缩区间换成一条带标记的数据消息、其余按序保留。
- 三级降级：摘要 accepted 区间 → 替换**未发送**的超大 `read_file` 结果 → 抛 `ContextWindowExceededError`（run 以 `context_limit` 结束、error 非空、请求未发出）。首个请求同样校验，不存在「首包不检查」的例外。
- 可摘要范围由调用方声明：对话 turn 传 `1 + len(history)`；reviewer/Judge 传 `0`，其授权输入不可摘要，超窗即失败。这是「授权证据不得静默丢弃」的落地点。
- 摘要失败或空响应退化为有界原文 checkpoint（`raw_archive`，两端保留、省略中间）；checkpoint 只由持久化 `_last_summary` 回到后续上下文（含重启后），token 探针与真实 prompt 必须用同一份。
- 新的整理摘要先纳入当前 `_last_summary` 再落盘，不整体覆盖；`"(nothing)"` 是**替换**检查点（删除 key），`None` 保留旧值。
- 长期记忆读取有界：Dream 每次最多 20 条、每条 1000 字符；`write_memory` 内容上限 64_000 字符，超限要求模型自行收敛后重写。

## Usage

- Runner 累计可见模型响应和请求级压缩的 usage；失败、超时和丢弃摘要不应丢失已经观察到的用量，未知用量不虚构。
- 请求级压缩的摘要用量由 runner 在 run 结束时并入 `AgentRunResult.usage`（幂等，只并一次）；角色侧 hook 不再单独上报压缩用量。
- Provider 内部重试未单独汇总；回放窗口折叠（`Consolidator.fold_replay_overflow`）直接调用 Provider，其 usage 不在 run 统计内。
- Judge 通过 Runner 执行，失败或超时的 run 仍保留已观察到的 usage 快照（`_JudgeUsageObserver`）；`budget_rejected` 时没有模型调用，因此不产生 usage。
- 当前 `AgentLoop._total_usage`/WebSocket `/usage` 是进程观测，未完整包含 review 与会话整理用量，不代表账单。

## Review run 预算契约

预算对象统一由 `review/budget.py::ReviewBudget` 表达，但其中的窗口与输出上限来自 **review 准入时冻结的 `LLMRuntime`**（`ReviewBudget.from_runtimes`），不再有全局固定窗口（原 `REVIEW_CONTEXT_WINDOW_TOKENS` / `REVIEWER_MAX_OUTPUT_TOKENS` 已删除）；只有两段 reserve 仍来自 `review` 配置。`agent/review_loop.py::review_budget_contract()` 是该契约的唯一投影。预算对象只构建一次并向下传递（`_ReviewInputs.budget` / `ReviewJudgeConfig.budget`），judge 不再把标量重新换回预算：

```text
context_window_tokens = reviewer_runtime.context_window_tokens（review.reviewer.contextWindowTokens，未设则继承对话 agent）
reviewer_output_tokens = reviewer_runtime.max_tokens           （review.reviewer.maxTokens，未设则继承）
judge_output_tokens   = judge_runtime.max_tokens              （review.judge.maxTokens；须装下全部候选裁决）
reviewer_context_read_reserve_tokens = 32_000                 （review.reviewerContextReadReserveTokens）
safety_margin_tokens = 4_000                                  （review.budgetSafetyMarginTokens）
reviewer_model_request_limit = 30（最后若干次仍属 reviewer，最终答案必须是结构化 findings；无角色级 wall clock）
reviewer/judge 输出纠正配额 = 3（`output_retry_limit`；无 planner）
```

- 实际生效值写入 run snapshot：`budget` 段是 `review_budget_contract(budget)`，`models` 段是 reviewer/Judge runtime 的 `describe()`，因此 `budget` 里的数字与 `models` 里的 model 永远对得上。
- 单次请求的判据只有一条：`prompt + 输出预留 + 预留（上下文读取 + 安全余量）<= context_window_tokens`（`RequestBudget.fits`）。reviewer 的输出预留是 `reviewer_runtime.max_tokens`；Judge 的输出预留是 `judge_runtime.max_tokens`（`judge_output_covers` 已先保证它装得下全部裁决）：调高只会占掉更多输入空间；调低会把更多窗口让给输入，但低于裁决所需时直接拒绝。
- **reviewer 输入完整、超限整体拒绝**：预处理对每个改动文件只产出一个完整 unit（`_patch_units`，无 token 阈值、不按 hunk 切分），`prefetch` 的 `excerpt` 就是完整 diff 文本，不做字符截断。`_require_reviewer_request_budget` 在 `start_assignments` 之前逐个所选 reviewer 计算首请求（system prompt + 完整 diff + 工具 schema 常量），任一超限即抛 `ReviewPlanningError`：**零个 reviewer 启动**，run 落 `error`，错误文案含维度、所需 token、`describe()` 与调参建议。不得以删文件、裁片段或少跑一个 reviewer 代替拒绝。
- **Judge 一次裁决全部候选**：候选集合仍是 validator 的 `accepted` + `uncertain`（排除 `rejected`），不再分批、不再跨批合并，`ReviewJudgeStats` 无 `batches`。裁决前依次检查两件事：① 每个候选的裁决 skeleton（真实 candidate id）+ 每候选 48 token reason 预留必须装进 `judge_runtime.max_tokens`；② 完整 prompt + 该输出预留 + 安全余量必须装进窗口。任一不满足即 `budget_rejected=True`、`verdicts={}`（不伪造裁决）、不调用模型；上层据此把 run 落 `error`，并持久化带 `### DRAFT - Not Adjudicated` 的草稿报告（reviewer/validator 结果仍可读）。结构纠错重试属同一批，不计作第二次裁决。
- 预处理只跳过真正无内容或不可审的路径：`empty_patch`（无 diff 文本）与证据收集器给出的文件级原因（如 `binary_file`、`filtered`）。`token_limit_exceeded` / `budget_exhausted` 已随预算裁剪一起删除；报告 coverage 只反映这些真实缺口，不再有「因预算跳片」的局部覆盖语义。
- **没有 Planner 模型调用**：review 的模型开销只有 reviewer 与 Judge 两段。每个选中的 reviewer 接收全部接纳 evidence。
- reviewer 的授权 evidence 是固定输入，不参与压缩：reviewer run 的 `accepted_message_count=0`，信封放不下时显式 `context_limit` 失败，不摘要授权证据，也不假定压缩能裁剪它们。
- **不再有保留的 terminal 提交轮**：review 全链路移除 terminal tool 协议后，`reserve_terminal_iteration` / `terminal_retry_limit` / `prose_retry_limit` 一并删除。结构正确性改由 `AgentRunSpec.output_contract` + `output_retry_limit` 保证：被拒的答案不进 `final_content`、不置 `structured_output`，只追加一条带具体原因的纠正消息继续同一 run；配额耗尽以 `invalid_output` 收场。因此「预算内一定提交成功」不再是保证，取而代之的是「被拒绝不能冒充成功」。reviewer 请求上限 30，Judge `_JUDGE_MAX_ITERATIONS = 3 + 3`。
- **但最后一次请求不再开放工具**：结构化 run 的 `iteration == max_iterations - 1` 由 `_is_final_structured_turn()` 判定，该轮不给工具并明确要求直接给答案，避免预算被一次无法回收的工具调用耗尽后停在与内容无关的 `max_iterations`。该轮被拒同样是 `invalid_output`，不会获得额外纠正轮。
- reviewer 内重复 `read_file`/`grep` 按单 run ledger 抑制（相同规范化范围 + 内容未变时返回短提示），`force` 不可绕过；这是上下文效率控制，不是累计 token 硬上限，也不跨 reviewer/review run 共享。

## 变更检查

预算改动核对配置映射、evidence 策略、Runner/Subagent 限制、上下文治理与停止原因（`context_limit` / `invalid_output` / `tool_error`）、Provider usage、WebUI 展示及对应测试。新增总配额须定义作用域、计量来源、并发扣减与超限行为。
