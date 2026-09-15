# Budget and Token Controls

本文档汇总 NanoReview 中与预算、token 消耗和执行上限相关的稳定约定。它记录的是当前实现，不构成新的运行时配置。

## Scope

项目目前控制的是单次请求和单次审查的输入规模、输出规模、上下文长度、执行轮数、并发数和超时；没有按累计 token、金额、日/月周期实现的本地总配额。

## Configuration Sources

主要配置位于 `nanoreview/config/schema.py`：

- `agents.defaults.max_tokens`：主 Agent 的默认单次输出上限，默认 `8192`。
- `agents.defaults.context_window_tokens`：主 Agent 和 Subagent 共用的上下文窗口，默认 `65536`。
- `agents.defaults.max_tool_iterations`：主 Agent 单次运行最大工具/模型轮数，默认 `200`。
- `agents.defaults.max_tool_result_chars`：工具结果字符上限，默认 `16000`。
- `agents.defaults.max_concurrent_subagents`：SubagentManager 全局并发上限，默认 `1`。
- `agents.defaults.context_block_limit`：可选的显式上下文块上限。
- `agents.defaults.max_messages`：会话历史回放消息数上限，默认 `120`。
- `agents.defaults.consolidation_ratio`：上下文压缩后的目标比例，默认 `0.5`。

Review 专用配置：

- `review.evidence_token_budget`：被接受的主证据 token 上限，默认 `100000`。
- `review.subagent_evidence_budget_chars`：单个 Subagent 证据任务字符预算，默认 `24000`。
- `review.prefetch_budget_chars`：Coordinator evidence manifest 字符上限，默认 `16000`。
- `review.prefetch_dense_backfill_limit`：主证据单元数量的病态输入保护上限，默认 `256`。
- `review.max_concurrent_subagents`：单次 Review 请求期望的并发上限，默认 `4`；实际值还受 SubagentManager 全局上限约束。
- `review.judge.max_tokens` / `review.judge.timeout_seconds`：AI Judge 单次输出和超时，默认分别为 `2048` 和 `60` 秒。

## Review Evidence Budget

实现位于 `nanoreview/review/planning/preprocessor.py` 的 `EvidenceBudget.from_options`。先从模型上下文窗口扣除固定保留量：

```text
usable_input_tokens = context_window
                     - 2000  # system prompt
                     - 3000  # tool descriptions
                     - 4000  # output reserve
                     - 10%   # safety margin
```

随后派生：

```text
evidence_budget = min(configured evidence_token_budget, usable_input_tokens)
task_cap        = min(max(400 / 2, subagent_evidence_budget_chars / 4), usable_input_tokens)
chunk_cap       = max(400, min(task_cap / 2, 4000))
direct_cap      = min(evidence_budget, max(1600, usable_input_tokens / 4))
related_budget  = max(400, evidence_budget / 4)
```

规模判断：

- `direct`：总输入不超过 `direct_cap`，尽量整文件保留。
- `chunked`：超过 `direct_cap` 但不超过 `evidence_budget`，按语义切块并按优先级筛选。
- `oversized`：超过 `evidence_budget`，切块后仍只接受预算内的高优先级单元。

单元本身无法放入 chunk 时记录 `token_limit_exceeded`；整体预算耗尽时记录 `budget_exhausted`。被跳过的单元会进入 Review 报告的未审查摘要。

`prefetch_budget_chars` 只限制 manifest 文本长度，超出时截断 manifest；它不是实际 evidence token 总配额。

## Agent and Subagent Execution

`AgentRunner` 通过 `AgentRunSpec` 执行硬限制：

- 主 Agent 使用 `max_iterations`；Subagent Review 任务由 `agent/orchestration.py` 按输入大小派生 `10..30` 轮。
- Review Subagent 固定使用 `max_tokens=2048`、`timeout_seconds=180`。
- Coordinator 的 `submit_review_plan` 最多尝试 `5` 次，整个 planner run 最多 `7` 轮。
- 实际 Review 并发是 `min(review.max_concurrent_subagents, SubagentManager.max_concurrent_subagents)`。
- 主 Agent 的会话回放预算约为 `context_window_tokens - provider.max_tokens - 1024`；`context_block_limit` 非空时覆盖该计算。
- 工具结果先按字符上限裁剪，再参与后续上下文治理。

当模型响应因 `finish_reason=length` 被截断时，Runner 最多做 `3` 次长度恢复；空响应最多重试 `2` 次。这些重试都会产生额外模型调用。

## Context Compaction

`Consolidator` 会估算未压缩会话的 prompt token 数。当其达到输入预算时，按安全的 user-turn 边界归档旧消息，目标压缩到输入预算的 `consolidation_ratio`。`AutoCompact` 还可按 `session_ttl_minutes` 对空闲会话做主动归档。

上下文估算优先使用 Provider 的 token counter，其次使用 `tiktoken`；不可用时回退到约 4 字符/token 的保守估算。Review 预处理器本身使用同样量级的确定性字符估算。

## Usage Accounting

Provider 返回的 usage 会在 `AgentRunner` 中按每次模型响应累计到 `AgentRunResult.usage`，包括重试和长度恢复请求。`AgentLoop` 保存：

- `_last_usage`：最近一次主 Agent run。
- `_total_usage`：当前进程生命周期内主 Agent run 的累计值。

WebSocket `/usage` 暴露 process 级 `usage` 和 `last_usage`。Subagent 的 usage 会放在结果 metadata 的 `subagent_usage` 中，但不会额外合并进全局总量。

当前 Judge 调用和会话压缩调用直接访问 Provider，也没有合并到 `AgentLoop._total_usage`。因此这些字段适合做运行观测，不等同于完整账单。

## Change Rules

涉及预算或 token 行为的改动，至少同步检查：

1. 配置 schema 和 camelCase 映射。
2. `preprocessor.py` 的预算推导、跳过原因和报告输出。
3. `AgentRunner` / `SubagentManager` 的轮数、输出、超时和上下文裁剪。
4. Provider usage 解析、`AgentLoop` 累计和 WebSocket `/usage` 契约。
5. 对应的 `tests/review`、`tests/agent` 和 WebUI 类型/状态。

不要把估算 token 当作 Provider 最终计费 token；新增总配额时必须明确作用域（进程、会话、Review 或 Provider）、计量来源、并发下的原子扣减和超限行为。
