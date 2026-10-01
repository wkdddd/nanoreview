# ReviewAgent Multi-Agent Architecture Handoff

更新时间：2026-09-19

> 文档角色：本文件是 ReviewAgent 的目标调整文档和验收基线，描述应达到的产品边界、架构、重试边界和实施顺序。它不是当前实现状态报告；当前代码事实、已落地能力和未完成缺口统一记录在 `.claude/plans/reviewagent-multi-agent-handoff-implementation-addendum.md`。除非产品目标发生明确变化，不要为了匹配现状改写本文件；若目标调整，必须在 addendum 中同步记录影响。

> 明确非目标：本版本不提供任何 review 恢复能力——既不恢复进程内已中断的 reviewer/judge 工作，也不支持进程重启后 resume。`ReviewRunState` 不是恢复协议，只服务运行状态、phase、usage、取消、session metadata 和 artifact 可观测性。将来只有出现明确的“中断后继续”产品需求时，才单独设计带显式用户入口、输入身份校验和持久化 manifest 的跨运行 resume；不复用被删除的同进程方案。

## 目标与产品边界

NanoReview 定位为 ReviewAgent，而不是通用聊天 Agent。

- 一个 session 只执行一次 review task，最终输出一份 review report。
- review 完成后拒绝同一 session 的后续用户消息，不支持用户追问或在原 session 重新开始。
- 一次 review 内可以运行多个 agent：coordinator、多个 reviewer、judge。
- agent 的单次 LLM/tool 执行统一由 `AgentRunner.run()` 完成。
- supervisor 负责审查流程编排、agent 生命周期、取消、状态迁移和持久化，不负责恢复。

## 目标制定时的实现基线（历史参考）

以下实现描述保留用于说明目标为何需要这些调整；它不是当前代码状态。当前状态以 implementation addendum 的最新核对为准。

### 外层状态机

`nanoreview/agent/loop.py` 当前定义了：

```text
RESTORE -> COMPACT -> COMMAND -> BUILD -> RUN -> SAVE -> RESPOND -> DONE
```

状态循环和转换表位于 `nanoreview/agent/loop.py:78-196`、`1743-1818`。

- `RESTORE`：创建/恢复 session，恢复 runtime checkpoint 和未完成用户 turn。
- `COMPACT`：调用 `AutoCompact.prepare_session()`，处理 idle session 的归档摘要。
- `COMMAND`：处理 `/stop`、`/status`、`/new` 等命令。
- `BUILD`：构造通用 history、初始消息、工具上下文和回调。
- `RUN`：调用 `_run_agent_loop()`。
- `SAVE`：保存 assistant/tool 消息、媒体、延迟和 session。
- `RESPOND`：构造 outbound，发布 review report stream。
- `DONE`：退出状态循环并记录 trace。

### ReviewOrchestrator

`nanoreview/agent/orchestration.py:84-175` 的 `ReviewOrchestrator.execute()` 当前是一个黑盒流程，内部依次完成：

```text
collect plan
-> derive reviewer limits
-> dispatch reviewers
-> collect reviewer results
-> finalizer validation
-> judge
-> report finalize
```

`AgentLoop._run_agent_loop()` 在 `nanoreview/agent/loop.py:1117-1147` 直接调用它。

因此当前架构是：

```text
AgentLoop
  -> RUN
      -> _run_agent_loop()
          -> ReviewOrchestrator.execute()  # 黑盒
```

### AgentRunner

`nanoreview/agent/runner.py:282-760` 是单个 agent 的 LLM/tool 循环：

- 每轮请求模型；
- 执行工具；
- 处理 terminal tool；
- provider retry；
- checkpoint；
- 注入消息；
- 返回 `AgentRunResult`。

每轮请求前调用 `runner._prepare_messages()`（`runner.py:304`），当前只包含：

```text
drop/backfill orphan tool results
-> microcompact
-> tool result budget
-> _snip_history hard trim
```

当前没有 LLM 摘要式 run-level compression。

### Agent 类型的当前差异

- coordinator：在 `ReviewOrchestrator._collect_plan()` 中使用 `AgentRunner.run()`。
- reviewer：在 `SubagentManager._run_subagent()` 中使用独立的 `AgentRunner.run()`。
- judge：当前在 `ReviewJudge._judge_batch()` 中直接调用 provider，尚未使用 `AgentRunner`。

## 已确认的目标架构

### 不再保留独立 Workflow 抽象

`ReviewOrchestrator` 应直接合并进 `ReviewAgentLoop`，不再新增 `ReviewWorkflow`。

理由：

- 产品只有一种 review workflow；
- workflow 和 loop 在本项目中都是同一条审查流程的控制器；
- 再增加一层 workflow 只会延续当前 `RUN -> Orchestrator.execute()` 的黑盒问题。

保留的模块只应是纯业务组件，例如：

- `review/planning/`：目标和 evidence 准备；
- `review/output/validator.py`：finding 校验；
- `review/output/report.py`：报告渲染；
- `review/source/`：源码获取。

这些模块不拥有 run 生命周期和状态迁移。

### 简化后的 supervisor 状态机

一次性 review 建议使用：

```text
PREPARE
  -> PLAN
  -> REVIEW
  -> FINALIZE
  -> SAVE
  -> RESPOND
  -> DONE
```

状态职责：

- `PREPARE`：合并当前 `BUILD` 和 `prepare_code_review_context()`，生成 review target、evidence、policy 和 dimensions。
- `PLAN`：保持现有 LLM coordinator plan 生成逻辑，运行 coordinator agent 并接收 `submit_review_plan`。
- `REVIEW`：执行 dispatch、collect、validate，并运行 judge agents。
- `FINALIZE`：生成最终 findings、coverage、warnings、usage 和 report。
- `SAVE`：把最终报告保存为独立 review artifact，session 只保存 artifact 引用和必要状态。
- `RESPOND`：发布 WebUI 状态和最终 report。
- `DONE`：取消/清理剩余 task、compression job、lock 和临时资源。

以下内容不再作为外层状态：

- `COMPACT`：跨 turn session compact 不再是主要需求，改为每个 agent run 内部 compact。
- `COMMAND`：仅保留 `/status` 和 `/stop`，通过 supervisor 的 out-of-band 控制路径处理；其他命令删除或按 ReviewAgent 语义调整。
- `BUILD`：并入 `PREPARE`。
- `RUN`：保留为 `AgentRunner.run()` 的执行方法，不再作为 supervisor 的黑盒状态。
- `RESTORE`：不再作为独立外层状态。本版本不提供任何 resume（进程内或跨进程）；取消或异常把 run 终结为 `stopped`/`error`，不重建 supervisor，也不重跑已完成的 reviewer/judge 工作。

如果需要保留状态 trace，可继续记录上述 7 个 supervisor phase，但不需要为每个 agent 的 LLM 轮次再复制一套外层状态。

### Review 状态内部顺序

`REVIEW` 状态内部是显式顺序，而不是再次调用一个整体 orchestrator：

```text
dispatch reviewers
-> wait/collect reviewer results
-> validate findings
-> split judge batches
-> run JudgeAgent for each batch
```

reviewer 仍然可以并发；supervisor 负责限制并发、收集结果，并把终端状态写入 `ReviewRunState`。

通用 `spawn` 工具保留，用于扩展 agent 能力。固定 reviewer dimensions 仍由 supervisor
程序化 dispatch；额外 agent 只能由获得相应 profile scope 的 agent 创建，并继续受并发、
递归深度、工具权限和总运行限制约束。

## Judge Agent 改造

`ReviewJudge` 应保留 candidate 收集、batch 拆分和统计，但每个 batch 的模型执行改为：

```text
JudgeAgent
  -> AgentRunner.run()
  -> submit_verdicts tool
  -> JudgeVerdictReceiver
  -> ReviewJudgeVerdict
```

每个 judge batch 使用独立 `AgentRunSpec`：

- `terminal_tools={"submit_verdicts"}`；
- `tool_choice=submit_verdicts`；
- 允许多轮工具调用和 terminal retry，但必须受该 batch 的 `max_iterations` 和 retry limit 约束；
- `context_window_tokens`；
- `provider_retry_mode`；
- usage sink；
- run-level compression state。

`ReviewJudge` 不再直接调用 `provider.chat_with_retry()`。

batch 不需要稳定 ID：batch 只是**一次运行内**的 context-window 拆分单元，不是可恢复的持久化工作单元，也不要求跨调用可复现。迁移只替换 batch 内的执行入口，保留现有候选收集、batch 拆分和聚合统计；当前聚合的 `judge_batches["judge"]` usage/stats 结构保持不动。

当前 judge 已经按 context window 分 batch，因此第一阶段不必让 judge 处理很长的历史；使用 `AgentRunner` 的主要收益是统一 retry、terminal tool、usage、checkpoint 和运行日志。

## 重试与失败边界（与恢复无关）

本版本只保留以下三类“重试/控制”，它们都不是 resume，也不改变失败可见性：

- **模型请求重试**：Provider 层按 `provider_retry_mode` 重试单次请求。
- **terminal retry**：`AgentRunner` 在一次 run 内对 terminal tool 提交失败重试，受 `terminal_retry_limit` 和 `max_iterations` 约束。
- **超时控制**：按 agent/ batch 的 timeout 上限终止该次执行。

明确不做的事：

- 不增加 `/resume`、自动 workflow 重入、result reference 或跨进程恢复；
- reviewer/judge 失败不自动重跑；失败必须显式进入不完整结果或 `needs_confirmation`，部分 reviewer 失败时仍然产出明确标注不完整的报告；
- `/stop` 是终态取消，不恢复当前 review。

## 每个 agent 的 run-level compact

compact 应放入 `AgentRunner.run()` 的每次循环，而不是外层 `COMPACT` 状态。

所有以下 agent 自动获得同一能力：

```text
coordinator -> AgentRunner.run()
reviewer    -> AgentRunner.run()
judge       -> AgentRunner.run()
```

### 参考 OpenCodeReview

参考版本：`C:\Users\Administrator\Desktop\open-code-review`，commit `e95bdda`。

相关实现：

- `internal/llmloop/loop.go:RunPerFile`：每轮 LLM/tool loop。
- `internal/llmloop/loop.go:addNextMessage`：每轮追加消息并检查 token。
- `internal/llmloop/compression.go`：三段式压缩和异步任务。

其策略：

```text
60% token：启动后台压缩
80% token：立即同步压缩
仍超限：受控停止当前 run
```

三段消息区：

```text
frozen zone + compress zone + active zone
```

- frozen：系统消息和初始任务 envelope；
- compress：较早的完整 assistant/tool rounds，交给 LLM 摘要；
- active：最近的完整 rounds，原样保留。

NanoReview 不能简单照搬“前两条消息冻结”，应冻结所有 system 消息和初始 review task，压缩较早的完整 tool rounds，保留最近完整 rounds。

### NanoReview 实现要求

在 `AgentRunner.run()` 中为每次调用创建独立的 `RunCompressionState`：

```python
RunCompressionState(
    pending_task=None,
    snapshot_length=0,
    pending_result=None,
)
```

每轮请求前：

1. 尝试应用已经完成的异步压缩；
2. 估算当前 prompt token；
3. 超过 hard threshold 时同步压缩；
4. 再向 provider 发请求。

assistant/tool 消息追加后：

1. 再次估算 token；
2. 超过 80% 时同步压缩；
3. 介于 60%-80% 时启动异步压缩；
4. 压缩失败时保留原消息；
5. 压缩后仍超限时返回明确的 `context_compression` stop reason。

压缩只修改当前 run 的 working messages，不应改变 caller 用于保存的完整 `AgentRunResult.messages` 边界。

## ReviewRunState 的边界

`ReviewRunState` 是运行期可观测性对象，不是恢复协议。它记录：

- run id、session key、input fingerprint；
- phase、status、reviewer/judge 聚合 usage 与 stats、warnings；
- report artifact 引用。

必须在业务边界更新（evidence/target 准备完成、coordinator plan 接收成功、每个 reviewer terminal result、finalizer 完成、report artifact 写入成功），但这些边界只用于状态展示和审计，不用于重入流程。

不得持久化或跨边界传递：

- `asyncio.Task`；
- provider client/connection；
- callback、lock、semaphore；
- in-flight compression coroutine；
- 任意 Python 局部变量。

也不得为“将来可能恢复”预留 `result_ref`、attempt count、run-scoped task key 或 per-batch 持久化字段。取消或异常发生后异步 compact 直接丢弃，不重跑、不保存。最终报告完成后，session 只保存 `report_ref`、run id、最终状态和必要审计字段。

## 当前代码需要收敛的方向

### `ReviewAgentLoop` 保留

- bus 入站和 WebUI 出站；
- cancel/timeout；
- save/done；
- `ReviewRunState`（运行状态、phase、usage、artifact 可观测性，不含恢复语义）；
- provider/model runtime update；
- agent tree 和并发管理；
- skills 加载和 reviewer skill contract；
- usage/warnings/trace。

### 可以删除或移出的通用聊天能力

需在实现阶段逐项确认并删除：

- generic chat prompt/history replay；
- 跨 turn `AutoCompact` TTL archive；
- 普通聊天 response 分支；
- reviewer 结果伪装成普通 inbound message 的桥接逻辑；
- SOUL/personality 注入；
- 图片/附件处理路径；
- 与 ReviewAgent 无关的通用 personalization 和长期 memory。

`spawn` 不删除，作为 agent 扩展能力保留，由 profile scope、并发限制和递归深度控制。
通用命令只保留 `/status` 和 `/stop`，其他命令删除或按 ReviewAgent 语义调整。

不要在未检查 WebUI/API 调用链前直接删除 `MessageBus`、session metadata 或 progress event。

## 推荐实施顺序

1. 状态正确性测试与修复：先用测试锁定现有不变量（reviewer/judge 状态映射、metadata gate 不持久化、artifact 终态一致性），并修复与目标不符的状态错误。
2. 将 Judge 迁移到 `AgentRunner.run()`：batch 内执行改为 `JudgeAgent -> AgentRunner -> submit_verdicts`，保留候选收集、batch 拆分和聚合统计。
3. 在 `AgentRunner.run()` 中实现 OpenCodeReview 风格的 60%/80% run-level compression。
4. 将 supervisor 迁移到 `AgentLoop`：把 `ReviewOrchestrator` 的阶段职责并入 `ReviewAgentLoop`，外层状态机收敛为 `PREPARE/PLAN/REVIEW/FINALIZE/SAVE/RESPOND/DONE`，最后删除旧 orchestrator。
5. 删除确认无调用方的遗留通用聊天能力，并同步 WebUI/API、文档和测试链路。

`ReviewRunState`、独立 report artifact 引用、usage 汇总属于第 1 步之前的既有基线，不再是待办；其实际落地状态以 implementation addendum 为准。

## 验收标准

- 一个 review session 只允许一个 review run；完成后拒绝后续输入。
- 终态 session 收到普通消息时返回拒绝响应，且内存和持久化 session messages 都不增加。
- reviewer 成功时状态为 `completed`；失败时状态为 `error` 且带非空的有界错误原因，报告显式标注不完整。
- coordinator、reviewer、judge 都通过 `AgentRunner.run()` 执行。
- 每个 agent 的长 run 在 60% 时可后台压缩，在 80% 时同步压缩。
- 压缩失败不会丢弃完整 working history，也不会覆盖有效结果。
- reviewer/judge 的 token usage 可汇总到 review run。
- `/stop` 能取消 supervisor 和所有 child agent。
- WebUI 能读取 phase、run id、status 和最终报告；不承诺恢复运行中的 reviewer/judge 工作。
- report、findings、verdicts 和 run state 的 input fingerprint 一致。
- 代码中不存在 `/resume`、自动 workflow 重入、result reference 或跨进程恢复路径。

## 已确认的产品决策

- review session 完成后拒绝后续用户消息。
- 不做任何 review 恢复：不提供 `/resume`、不自动重入 workflow、不保存 result reference、不支持跨进程 resume。
- `/stop` 是终态取消，不恢复当前 review；进程异常或重启后不恢复 reviewer/judge 工作，metadata 只用于 gate 和状态展示。
- reviewer 失败允许 review 以不完整报告结束，不把整个 run 自动升级为 `error`。
- 现有 Provider 重试、`AgentRunner` terminal retry 和超时控制保持不变。
- 保留现有 LLM coordinator plan 生成逻辑。
- judge batch 允许多轮工具调用和 terminal retry。
- 保留 skills；删除 SOUL/personality 和图片/附件路径。
- 通用命令只保留 `/status` 和 `/stop`，其他命令删除或调整。
- 保留通用 `spawn` 作为 agent 扩展工具。
- review report 保存为独立 artifact，session 只保存 artifact 引用。
