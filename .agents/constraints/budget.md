# Budget and Context

当前实现入口：`config/schema.py`、`review/planning/preprocessor.py`、`agent/runner.py`、`agent/compression.py`、`agent/memory.py`。默认值与公式以代码为准。

## 预算边界

- evidence budget、单次输出、上下文窗口、执行轮数、并发和超时分别约束不同资源；不能互相替代。
- 当前没有按累计 token、金额或日/月周期实现的本地总配额。`prefetch_budget_chars` 仅约束 manifest 文本。
- 预处理跳过原因必须进入报告覆盖说明；工具输出裁剪需保留足够诊断信息。
- 上下文估算优先使用 Provider counter，其次 `tiktoken`，最后约 4 字符/token；估算不等于计费 usage。

## 上下文治理

- `AgentRunSpec` 显式传入 `frozen_messages` 与 `working_messages`；治理只处理 working，不跨分区配对或裁剪。
- run-level compression 按单次 run 独立：60% 异步、80% 同步；当前从第二次业务请求开始检查。首个请求与冻结内容的超窗风险仍须由调用方检查。
- 压缩只改变模型工作上下文，不改变 `AgentRunResult.messages` 原始历史；重建保留 summary、最近完整交互单元与压缩期间追加的 suffix。
- assistant tool-call 及其 tool results 不可拆分；未完成交互保留在活动区。异步结果必须校验 snapshot，过期结果不得覆盖新上下文。
- 同步压缩失败重试一次，仍失败为 `compression_failed`；成功后仍超限为 `compression_limit`。错误非空，必须向调用方和用户可见。
- run 内压缩不负责跨轮历史管理；当前 `Consolidator` 按 token 提供会话整理，后续调整需分别核对回放预算与 run 预算。
- 会话整理隐藏的历史只经由持久化 `_last_summary` 回到后续上下文（含重启后）；它同时是 token 探针与真实 prompt 的摘要来源，两者必须一致，不得只计入预算而不注入。
- 新的整理摘要必须先纳入当前 `_last_summary` 再落盘，不得整体覆盖；同一次调用的多个 chunk 与跨轮连续整理都累积到同一份，否则更早被隐藏的决策和未完成事项会从上下文消失。
- 整理请求必须为新 chunk 保留预算份额，且超预算时两端都保留、只省略中间：新 chunk 即将被 `last_consolidated` 隐藏，丢了就永久丢失，其尾部又紧邻回放窗口边界。降级为 raw 摘要时同样处理（本项目没有 raw history sidecar 可兜底）。
- 推进 `last_consolidated` 后必须落盘；摘要缺失（`None`）保留旧检查点，模型返回 `"(nothing)"` 则是**替换**检查点——删除 `_last_summary`，不得留存旧值继续注入它已不再覆盖的上下文。
- `"(nothing)"` 会删除检查点，必须在 Consolidator prompt 中明确定义为仅当旧检查点和新 chunk 都没有可保留信息时使用；不可把未声明的模型文本解释为控制信号。

## Usage

- Runner 累计可见模型响应和压缩请求 usage；失败、超时和丢弃摘要不应丢失已经观察到的用量，未知用量不虚构。
- Provider 内部重试未单独汇总；会话整理直接调用 Provider，其 usage 不在 run 统计内。
- Judge 通过 Runner 执行，失败 batch 仍保留已消耗 usage；超时使用已观察到的批内快照。
- 当前 `AgentLoop._total_usage`/WebSocket `/usage` 是进程观测，未完整包含 review 与会话整理用量，不代表账单。

## Review run 预算契约

Review 管线（Planner / reviewer / Judge）使用固定 `200_000` tokens 上下文窗口，不随 Conversation Agent 配置或 provider 探测变化（`agent/review_loop.py::REVIEW_CONTEXT_WINDOW_TOKENS`）：

```text
context_window_tokens = 200_000
planner_manifest_budget = 80_000（上下文窗口 40%）
evidence_token_budget = 150_000（主）/ related = 37_500（四分之一，单层补充）
diff evidence 单文件阈值 = 8_000（`<threshold` 一个完整 unit；`>=threshold` 沿用语义/hunk 切分）
reviewer_max_output_tokens = 8_192
reviewer_model_request_limit = 30（最后一次请求用于 review_submit）
reviewer_timeout_seconds = 180
```

- review 输入只有本地 diff：evidence 以 changed hunk 为主，related 只作一层补充。文件读取超 `max_file_chars` 时追加截断标记，不得把截断内容当作完整文件 evidence。
- Planner 只读一个结构化 manifest（`review/planning/manifest.py`），其 references 与 skipped/omitted 说明共享 80k 预算；超限时按 frozen 顺序保留前缀并记录被省略的 id/数量/原因，**不再按风险排序**。manifest 的 version、`input_mode` 与 token counter 随 `EvidenceManifest.snapshot_payload()` 持久化到 review snapshot。
- Planner 输入分 `direct`/`paged`：`direct` 内联全部 evidence 全文；`paged` 只渲染索引，内容由 `list_review_diff`/`read_review_diff` 分页读取，单次响应预算对齐运行期 `max_tool_result_chars`，避免 reader 声称已读后被 `AgentRunner` 再截断。有界读取的三条硬规则：请求的 ID 不得被隐藏丢弃（超预算部分显式列出 ID）；单个 unit 超过页面预算时必须在正文内标注“未完整展示”，绝不返回无标记的半个 patch；reference excerpt 上限（40k 字符）不得低于单文件完整 diff unit 的最大规模（`DIFF_UNIT_TOKEN_THRESHOLD * 4`），否则 direct 模式会静默丢 unit 尾部。
- Planner 请求上限 `24`（`_PLANNER_MAX_ITERATIONS`），分诊需要多次有界读取 + 多条 decision + finish；不强制 `tool_choice`，由 `finish_review_triage` 的 reserved terminal iteration 保证收尾。散文回合配额 `prose_retry_limit=8` 与 terminal 提交配额（5）分开计。reviewer 的 30 次不变。
- reviewer frozen task（授权 evidence + related evidence）是固定输入，不参与工作历史压缩；容量不足必须显式失败或记录 coverage 缺口，不得静默丢弃授权 evidence，也不得假定压缩能裁剪 frozen task。
- 「最后一次请求用于 review_submit」由 `AgentRunner` 的 reserved terminal iteration 强制执行：终局迭代（`iteration == max_iterations - 1`）只暴露 terminal tool，单一 terminal tool 再经 `tool_choice` 强制，仍在同一 request 预算内。Judge（`submit_verdicts`）本就每轮强制；Planner 靠 terminal 保留轮收尾；其他 terminal-tool run 同样获得一次保证提交。可用 `AgentRunSpec.reserve_terminal_iteration=False` 关闭。
- reviewer 内重复 `read_file`/`grep` 按单 run ledger 抑制（相同规范化范围 + 内容未变时返回短提示），`force` 不可绕过；这是上下文效率控制，不是累计 token 硬上限，也不跨 reviewer/review run 共享。

## 变更检查

预算改动核对配置映射、evidence 策略、Runner/Subagent 限制、压缩分区与停止原因、Provider usage、WebUI 展示及对应测试。新增总配额须定义作用域、计量来源、并发扣减与超限行为。
