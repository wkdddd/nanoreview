# Budget and Context

当前实现入口：`config/schema.py`、`review/planning/preprocessor.py`、`agent/context_governance.py`、`agent/llm_runtime.py`、`agent/runner.py`、`agent/memory.py`、`agent/dream.py`。默认值与公式以代码为准。

## 预算边界

- evidence budget、单次输出、上下文窗口、执行轮数、并发和超时分别约束不同资源；不能互相替代。
- 当前没有按累计 token、金额或日/月周期实现的本地总配额。`prefetch_budget_chars` 仅约束预处理的上下文文本。
- 预处理跳过原因必须进入报告覆盖说明；工具输出裁剪需保留足够诊断信息。
- 上下文估算优先使用 Provider counter，其次 `tiktoken`，最后约 4 字符/token；估算不等于计费 usage。

## 上下文治理

- **一个预算公式**：`input_token_budget(window, max_output) = window - max_output - 1024`（`agent/context_governance.py`）。governor、Judge 分批、review 回放窗口、handoff 首包检查都调用它——预判与执行必须同源，否则「计划内的批次」会在发包时被拒。
- `AgentRunSpec` 只有一份 `messages`（原始 transcript，只追加不改写）；请求由 `ContextGovernor.prepare_request` 每轮重建：首部连续 `system`/`developer` 原样、被压缩区间换成一条带标记的数据消息、其余按序保留。
- 三级降级：摘要 accepted 区间 → 替换**未发送**的超大 `read_file` 结果 → 抛 `ContextWindowExceededError`（run 以 `context_limit` 结束、error 非空、请求未发出）。首个请求同样校验，不存在「首包不检查」的例外。
- 可摘要范围由调用方声明：对话 turn 传 `1 + len(history)`；reviewer/Judge 传 `0`，其授权输入不可摘要，超窗即失败。这是「授权证据不得静默丢弃」的落地点。
- 摘要失败或空响应退化为有界原文 checkpoint（`raw_archive`，两端保留、省略中间）；checkpoint 只由持久化 `_last_summary` 回到后续上下文（含重启后），token 探针与真实 prompt 必须用同一份。
- 新的整理摘要先纳入当前 `_last_summary` 再落盘，不整体覆盖；`"(nothing)"` 是**替换**检查点（删除 key），`None` 保留旧值。
- 长期记忆读取有界：Dream 每次最多 20 条、每条 1000 字符；`write_memory` 内容上限 64_000 字符，超限要求模型自行收敛后重写。

## Usage

- Runner 累计可见模型响应和压缩请求 usage；失败、超时和丢弃摘要不应丢失已经观察到的用量，未知用量不虚构。
- 请求级压缩的摘要用量由 runner 自己在 run 结束时并入 `AgentRunResult.usage`（幂等，只并一次），角色侧的 hook 不再单独上报压缩用量。
- Provider 内部重试未单独汇总；回放窗口折叠（`Consolidator.fold_replay_overflow`）直接调用 Provider，其 usage 不在 run 统计内。
- Judge 通过 Runner 执行，失败 batch 仍保留已消耗 usage；请求超时使用已观察到的批内快照。
- 当前 `AgentLoop._total_usage`/WebSocket `/usage` 是进程观测，未完整包含 review 与会话整理用量，不代表账单。

## Review run 预算契约

上下文窗口、reviewer 输出与 Judge 参数都来自 **run 准入时冻结的 `LLMRuntime`**，不再有全局固定窗口（原 `REVIEW_CONTEXT_WINDOW_TOKENS` / `REVIEWER_MAX_OUTPUT_TOKENS` 已删除）：

```text
context_window_tokens = reviewer_runtime.context_window_tokens
reviewer_max_output_tokens = reviewer_runtime.max_tokens
reviewer_model_request_limit = 30（最终答案必须是结构化 findings；无角色级 wall clock）
reviewer/judge 输出纠正配额 = 3（output_retry_limit）
judge_model_request_limit = 30
evidence_token_budget = 150_000（diff evidence 总额，仍由 review 配置控制）
diff evidence 单文件阈值 = 8_000（`<threshold` 一个完整 unit；`>=threshold` 按 hunk 切分）
```

- 实际生效值写入 run snapshot：`budget` 段是 `review_budget_contract(runtime)`，`models` 段是 reviewer/Judge 的 `describe()`。
- **Judge 分批与 governor 共用预算公式**：`available_per_batch = input_token_budget(window, reserved_output) - header_tokens`。预判比执行宽松会让批次在发包时以 `context_limit` 失败，而不是被切小。
- review 输入只有本地 diff：evidence 就是 changed hunk，预处理不枚举仓库、不读完整源码、不做语义切块，因此不存在 related 补充预算、`max_file_chars` 截断标记或按文件规模分级的三态决策；单个超大 hunk 无法安全切分时整块记为 skipped（`token_limit_exceeded`）。
- **没有 Planner 模型调用**：review 的模型开销只有 reviewer 与 Judge 两段。证据预处理按 frozen 顺序保留，放不下的 unit 记为 skipped（`budget_exhausted`）并随 snapshot 与 report coverage 可见，**不再按风险排序**。每个选中的 reviewer 接收全部接纳 evidence。
- reviewer 的授权 evidence 是固定输入，不参与压缩：reviewer run 的 `accepted_message_count=0`，信封放不下时显式 `context_limit` 失败，不摘要授权证据，也不假定压缩能裁剪它们。
- 结构正确性由 `AgentRunSpec.output_contract` + `output_retry_limit` 保证：被拒的答案不进 `final_content`、不置 `structured_output`，只追加一条带具体原因的纠正消息继续同一 run；配额耗尽以 `invalid_output` 收场。因此「预算内一定提交成功」不是保证，「被拒绝不能冒充成功」才是。
- **最后一次请求不再开放工具**：结构化 run 的 `iteration == max_iterations - 1` 由 `_is_final_structured_turn()` 判定，该轮不给工具并明确要求直接给答案。该轮被拒同样是 `invalid_output`，不会获得额外纠正轮。
- reviewer 内重复 `read_file`/`grep` 按单 run ledger 抑制（相同规范化范围 + 内容未变时返回短提示），`force` 不可绕过；这是上下文效率控制，不是累计 token 硬上限，也不跨 reviewer/review run 共享。

## 变更检查

预算改动核对配置映射、evidence 策略、Runner/Subagent 限制、上下文治理与停止原因（`context_limit` / `invalid_output` / `tool_error`）、Provider usage、WebUI 展示及对应测试。新增总配额须定义作用域、计量来源、并发扣减与超限行为。
