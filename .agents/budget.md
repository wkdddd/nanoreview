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
- `agents.defaults.consolidation_ratio`：会话级 `Consolidator` 归档旧消息后的目标比例，默认 `0.5`（与 run-level compression 的 60%/80% 阈值无关）。

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
- `AgentRunSpec` 以 `frozen_messages`（冻结任务/证据信封）+ `working_messages`（活动区）显式分区取代旧的 `initial_messages`；run-level compression 的提示词、超时和用量回调分别为 `compression_prompt`、`compression_timeout_s`（默认 `180`）、`compression_usage_callback`。

当模型响应因 `finish_reason=length` 被截断时，Runner 最多做 `3` 次长度恢复；空响应最多重试 `2` 次。这些重试都会产生额外模型调用。

## Context Compaction

NanoReview 有两套互补的上下文管理：

**run-level compression（2026-09-26 落地，2026-09-27 / 2026-09-28 校正）**：`AgentRunner.run()` 内按每次 run 独立的 `RunCompressionState`（`nanoreview/agent/compression.py`）管理。有效上下文窗口的 `60%`（`soft_limit`）触发后台异步压缩，`80%`（`sync_limit`）触发同步压缩；从第二轮请求起才做 60%/80% 检查，首个业务请求不检查——但首个请求就已按 `frozen verbatim + 治理后的 working` 构造：governance（孤儿修复、回填、microcompact、工具结果预算、hard trim）只处理 working，绝不跨 frozen/working 边界配对或裁剪，frozen 自身超窗时整段交给 Provider 报错。压缩只改模型工作上下文，不改 `AgentRunResult.messages`（原始只追加历史，不含合成摘要）。消息按三段组织：`frozen_messages`（冻结任务/证据信封）+ 可选合成摘要消息（`<compressed_context>…</compressed_context>`，role `user`）+ `working_messages`。工作区在压缩时按原文 token 分为 `compress_prefix`（交给摘要的完整交互单元前缀）与 `active_prefix`（原样保留的最新完整单元），重建恒为 `frozen + summary + active_prefix + suffix`：active zone 与压缩期间追加的 suffix 都不会被丢弃，旧的合成摘要只会被新摘要替换。交互单元按确定性规则分组（`split_units`，run-level compression 与 hard trim 共用）：user/injection 与其后的直接 assistant 响应同属一单元，assistant tool-call 及其全部 tool 结果不可拆分，无前置 user 的后续 tool 轮各自成单元，未被应答的 user 或缺失 tool 结果的开放轮整体保留为最新活动单元。异步摘要写回后不在同一步递归进入同步压缩，重新计数留到下一次业务请求前；异步失败后仅当 working revision 前进（有新的完整轮次或 injection）才允许重试，同一 revision 不重复请求。同步压缩首次失败后重试一次，仍失败 → run 以 `compression_failed` 结束；同步成功但重建请求仍 ≥80% → run 以 `compression_limit` 结束，两者 `error` 均非空。压缩停止的可见性：streaming 输出不给 `compression_failed`/`compression_limit` 标记 `_streamed`（错误必须由 channel 实际发送）；普通 Agent 的 `final_content` 用 `spec.error_message`；coordinator 映射为 `ReviewPlanningError`，reviewer 记为 error/incomplete，judge 记为带 usage 的 failed batch。压缩请求的 `max_tokens` 只在 `AgentRunSpec.max_tokens` 有值时传递，否则省略该参数以使用 provider generation 默认值（没有固定兜底上限）。压缩调用消耗的 usage 计入该 run。运行状态只属于单次 run，不共享给并发 reviewer/judge。

**session-level consolidation**：`Consolidator` 估算未压缩会话的 prompt token 数，达到输入预算时按安全的 user-turn 边界归档旧消息，目标压缩到输入预算的 `consolidation_ratio`；`AutoCompact` 还可按 `session_ttl_minutes` 对空闲会话做主动归档。会话压缩调用仍直接访问 Provider。

上下文估算优先使用 Provider 的 token counter，其次使用 `tiktoken`；不可用时回退到约 4 字符/token 的保守估算。Review 预处理器本身使用同样量级的确定性字符估算。

## Usage Accounting

Provider 返回的 usage 会在 `AgentRunner` 中按每次模型响应累计到 `AgentRunResult.usage`，包括重试和长度恢复请求。run-level compression 调用消耗的 usage 同样计入该 run（`run()` 结束时并入）；压缩 Provider 返回后立即记录其可见 usage——error response、空响应、首次失败后成功、被丢弃的过期异步任务，以及 judge 超时观察者都会累计；Provider 抛异常或超时未暴露 usage 时不虚构计数，Provider 内部重试的 usage 不单独汇总。judge 的压缩用量经 `compression_usage_callback` 折进 `JudgeExecutionResult.usage`，超时也不丢失已完成轮次的压缩用量。`AgentLoop` 保存：

- `_last_usage`：最近一次主 Agent run。
- `_total_usage`：当前进程生命周期内主 Agent run 的累计值。

WebSocket `/usage` 暴露 process 级 `usage` 和 `last_usage`。Subagent 的 usage 会放在结果 metadata 的 `subagent_usage` 中，但不会额外合并进全局总量。

Judge 自 2026-09-20 起不再直接访问 Provider：每个 judge batch 通过共享 `AgentRunner.run()` 执行，usage 由 `AgentRunResult.usage` 经 `JudgeExecutionResult.usage` 折进 `JudgeBatchState` 和 `ReviewRunState`。失败的 batch 也必须计入已消耗的 usage（run 结束的失败取 `AgentRunResult.usage`，超时取批内逐轮快照），不得因为失败而丢弃；失败时写入的 `error` 必须非空，否则调用方会把 batch 读成 `completed`。**会话级**压缩调用（`Consolidator`/`AutoCompact`）仍直接访问 Provider，其用量不在 run 统计内；**run-level** 压缩用量则随所属 run 计入。review（含 judge）用量仍未合并到 `AgentLoop._total_usage`。因此这些字段适合做运行观测，不等同于完整账单。

## Change Rules

涉及预算或 token 行为的改动，至少同步检查：

1. 配置 schema 和 camelCase 映射。
2. `preprocessor.py` 的预算推导、跳过原因和报告输出。
3. `AgentRunner` / `SubagentManager` 的轮数、输出、超时和上下文裁剪。
4. run-level compression 的阈值（`nanoreview/agent/compression.py`）、分区、停止原因和用量计入。
5. Provider usage 解析、`AgentLoop` 累计和 WebSocket `/usage` 契约。
6. 对应的 `tests/review`、`tests/agent`（含 `test_runner_compression.py`）和 WebUI 类型/状态。

不要把估算 token 当作 Provider 最终计费 token；新增总配额时必须明确作用域（进程、会话、Review 或 Provider）、计量来源、并发下的原子扣减和超限行为。
