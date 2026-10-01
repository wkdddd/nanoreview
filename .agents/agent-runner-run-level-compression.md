# AgentRunner Run-Level Compression 实施计划

更新日期：2026-09-25

## 1. 目标与边界

在 `AgentRunner.run()` 内实现一次运行范围内的上下文压缩，使 coordinator、reviewer、judge 和普通 Agent 共用同一套长期运行保护：

- 完整请求上下文达到有效 context window 的 60% 时启动异步压缩；
- 达到 80% 时在下一次业务模型请求前执行同步压缩；
- 同步压缩失败，或压缩后仍达到 80%，立即停止当前 run；
- 压缩只改变后续模型看到的 working context，不能改写 `AgentRunResult.messages` 中的原始运行消息；
- frozen task/evidence envelope、最新完整交互和 terminal retry 上下文不得被错误裁剪；
- 压缩调用的可见 usage 计入当前 run，并延续 judge timeout 不丢失已完成调用 usage 的现有契约。

本阶段不实现：

- 首次业务模型请求前的 run-level compression 或额外超预算预检；
- reviewer 初始 evidence 分配上限调整；
- Provider 真实 context window 自动探测；
- 跨 run/session 的压缩恢复；
- 新的 WebUI compression 事件或持久化协议；
- Provider 内部 retry 中间响应 usage 的额外聚合。

## 2. 稳定行为契约

### 2.1 有效 context window

压缩阈值使用当前 provider/model chain 已解析出的 `context_window_tokens`：

1. 当前 model preset 的值优先；
2. 未覆盖时使用 `agents.defaults.context_window_tokens`，当前默认值为 `65_536`；
3. fallback provider chain 使用所有候选窗口的最小值；
4. 已显式配置的 `131_072`、`200_000` 等较大窗口必须原样生效。

该值是 NanoReview 对当前运行采用的上下文预算，不宣称是从 Provider 自动探测出的模型硬限制。

阈值直接计算，不再扣除业务输出预留或额外安全余量：

```text
soft_limit = floor(context_window_tokens * 0.60)
sync_limit = floor(context_window_tokens * 0.80)
```

token 估算必须覆盖完整请求输入：frozen messages、working messages、synthetic summary 和 tool definitions。复用 `estimate_prompt_tokens_chain()`，依次使用 Provider counter、tiktoken 和现有字符估算 fallback。

### 2.2 首次请求例外

每个 `AgentRunner.run()` 的第一次业务模型请求维持现状：

- 执行现有消息修复、microcompact、tool-result budget 和 hard trim；
- 不启动异步摘要；
- 不执行 60%/80% run-level compression 检查；
- 如果不可压缩的初始 frozen prompt 本身过大，仍交给 Provider 返回 context-length 错误。

从第二次业务模型请求开始，每次请求前都执行 run-level compression 检查。

### 2.3 消息所有权

Runner 同时维护两套状态：

- `raw_messages`：`frozen_messages + working_messages` 加上本次 run 新产生的 assistant/tool/injection 消息，只追加、不写入摘要，最终返回为 `AgentRunResult.messages`；
- `model_working_messages`：供后续模型请求使用的 working context，可以被摘要替换。

任何压缩、microcompact 或 hard trim 都不能污染 `raw_messages`。checkpoint、tool event 和 terminal result 继续基于原始消息产生。

## 3. 接口调整

### 3.1 `AgentRunSpec`

删除 `initial_messages`，改为显式边界：

```python
frozen_messages: list[dict[str, Any]]
working_messages: list[dict[str, Any]]
```

新增：

```python
compression_prompt: str | None = None
compression_timeout_s: float = 180.0
compression_usage_callback: Callable[[dict[str, int]], None] | None = None
```

约束：

- `compression_prompt=None` 时使用项目默认模板；
- `compression_timeout_s` 作用于每次压缩层逻辑请求；
- `compression_usage_callback` 只报告已经完成且 Provider 已暴露的压缩 usage，用于 judge 外层 timeout 快照；callback 异常只记录日志，不能破坏 run；
- 不兼容旧的 `initial_messages` 参数，所有生产调用方与测试一次迁移完成。

压缩输出上限复用业务请求的 `spec.max_tokens`；其为 `None` 时继续使用 Provider generation 默认值。压缩请求复用相同 model、temperature、reasoning effort、provider retry mode 和 retry-wait callback，但不携带业务 tools、tool choice、streaming callback、permission 或 checkpoint callback。

### 3.2 `AgentRunResult`

保持已有公共结果形状，不新增持久化或 wire-level compression stats。新增停止原因：

- `compression_failed`：同步压缩在首次调用和一次压缩层 retry 后仍请求失败、超时或输出校验失败；
- `compression_limit`：同步压缩成功，但按实际重建上下文重新计数后仍达到 80%。

两者都必须携带非空 `error`。`usage` 包含 Runner 能观察到的所有业务调用与压缩调用 usage。

### 3.3 Run-local state

在 Runner 内定义私有、每次 `run()` 独占的 `RunCompressionState`，至少保存：

- 当前 model working context；
- 当前 synthetic summary 的位置/身份；
- pending async task；
- async snapshot 的 frozen/working 前缀与消息数；
- 上次 async 失败后是否已有新交互追加；
- 已汇总的 compression usage；
- run cancellation/cleanup 状态。

状态不得挂在共享 `AgentRunner` 实例上，避免并发 reviewer 或 judge batch 相互应用、取消或覆盖压缩任务。

## 4. 调用方显式分区

所有 `AgentRunSpec` 构造点显式决定 frozen/working，Runner 不按 role 或前 N 条消息猜测边界。

### 4.1 普通 Agent

- frozen：system prompt、当前 user task，以及只属于当前任务的运行 envelope；
- working：旧 session history；
- mid-turn subagent result、barrier 和新 user injection 在追加时进入 working。

`AgentLoop` 在构造 prompt 时必须保留足够的结构信息，以便把当前 user task 与旧 history 分开，而不是先拼成一个无法逆向识别的 `initial_messages`。

### 4.2 Review coordinator

- frozen：coordinator system prompt、当前 review task、evidence manifest；
- working：该 run 可继承的旧历史，如当前流程没有则为空。

`AgentLoop` 将 resolved `context_window_tokens` 传给 `ReviewOrchestrator`，coordinator spec 必须显式携带该值。

### 4.3 Reviewer/Subagent

- frozen：reviewer system prompt、当前 reviewer task 和 evidence envelope；
- working：初始为空。

### 4.4 Judge

- frozen：judge system prompt、当前 batch；
- working：初始为空。

judge 继续使用当前 resolved context window 进行 batch 拆分和 Runner 压缩，两个位置必须使用同一个值。

## 5. 三段式压缩模型

模型请求上下文按以下逻辑重建：

```text
frozen zone + optional synthetic summary + active working zone
```

### 5.1 原子交互单元

working context 按不可拆分的交互单元分组：

- user/injection 消息及其后续 assistant 响应；
- assistant tool-call message 与其全部对应 tool-result messages；
- terminal retry prompt 与下一次 terminal assistant/tool round；
- 尾部尚未得到响应的 user/injection 消息作为开放单元原样保留。

Runner 当前只在上一轮工具执行完成后进入下一次业务模型请求，因此正常请求边界不会出现缺失 tool result 的半轮；分组逻辑仍需防御性地把任何尾部开放单元归入 active，不能拆开或摘要。

### 5.2 Active zone

从最新单元向前选择完整交互单元，使 active 原文 token 总量不超过 `soft_limit`。如果最新单个交互单元自身超过该值，也必须完整保留，不能拆分。

active 预算只限制原文 active zone；frozen 和 summary 另计。压缩完成后以完整请求重新计算是否低于 80%。

### 5.3 Compress zone

active 之前的完整 working 前缀是 compress zone。没有合法完整前缀时不构造无意义的压缩请求；后续完整请求若仍达到 80%，按 `compression_limit` 停止。

旧 synthetic summary 可以作为下一次 compress zone 输入，以便整合早期结论，但新结果写回时必须替换旧 summary，不能累积多份摘要。

## 6. 压缩请求与 JSON 协议

### 6.1 Prompt 模板

新增 `nanoreview/templates/agent/memory_compression.md`，由现有 `render_template()` 加载。默认模板适用于普通 Agent 和 review 调用方，并要求保留：

- 当前任务、目标和 focus；
- 已确认结论；
- evidence ID、路径、行范围和关键摘要；
- finding 状态、结论、支持 evidence、反证和不确定性；
- pending tasks；
- 约束和 evidence availability；
- 工具产生的关键线索，但不要求保留普通工具结果原文。

模板必须明确：输入是可能包含指令文本的历史数据；摘要不是新的用户指令，也不是新的源码证据；不得虚构路径、行号、evidence 或 finding。

待压缩消息使用带 message id、role 和边界的结构化文本序列化到压缩请求的 user message中，避免把原 working messages 直接作为压缩会话角色历史执行。

### 6.2 Provider 调用

压缩使用 Runner 内部无工具方法直接调用同一 Provider，不递归调用 `AgentRunner.run()`，也不复用 `Consolidator.archive()`：

```python
response_format={"type": "json_object"}
tools=None
```

请求、Provider fallback、transient retry、unsupported `response_format` fallback 和错误分类全部复用现有 Provider 链路。Provider 去掉 `response_format` 后，只要最终内容通过 Runner 校验，仍算成功。

每个压缩任务最多执行两次压缩层逻辑请求：首次加一次 retry。每次逻辑请求内部仍可执行 Provider 自身 retry；底层 retry 不计入压缩层的两次限制。

### 6.3 JSON schema

最终内容以标准 `json.loads()` 解析，再复用项目已有 `Schema.validate_json_schema_value()` 风格校验。不得用 `json_repair` 修补摘要。

```json
{
  "task_context": {
    "task": "string",
    "objective": "string",
    "focus": "string"
  },
  "confirmed_conclusions": ["string"],
  "evidence": [
    {
      "evidence_id": "string",
      "path": "string",
      "line_range": "string",
      "summary": "string"
    }
  ],
  "findings": [
    {
      "status": "string",
      "conclusion": "string",
      "evidence_ids": ["string"],
      "counterevidence": ["string"],
      "uncertainty": ["string"]
    }
  ],
  "pending_tasks": ["string"],
  "constraints_and_availability": {
    "constraints": ["string"],
    "evidence_availability": ["string"]
  }
}
```

校验规则：

- 所有已定义字段和内部类型都必须存在且正确；
- 数组和字符串允许为空；
- 允许额外字段，但 Runner 只保留已定义字段；
- 至少一个已定义字段必须包含非空内容；
- 非法 JSON、缺字段、类型错误或完全空摘要均为压缩失败。

校验成功后只对已定义字段进行 canonical JSON 序列化，写入独立 synthetic user message：

```text
<compressed_context>
This is a summary of prior history, not new source evidence.
{canonical JSON}
</compressed_context>
```

## 7. 每轮请求前的状态机

第一次请求之后，每次业务模型请求前按固定顺序执行：

1. 检查 pending async task；若已完成，先收集 usage 和结果；
2. 只有 snapshot frozen/working 前缀仍与当前状态一致时才应用结果；
3. 保留 snapshot 之后追加的完整 suffix；前缀变化则丢弃过期结果；
4. 对实际重建的完整请求重新计数；
5. 达到 80% 时取消并等待尚未完成的 async task，然后执行同步压缩；
6. 同步成功后重新计数，仍达到 80% 则返回 `compression_limit`；
7. 低于 80% 且达到 60% 时，如果无 pending task 且满足重试条件，创建新的 async snapshot；
8. 低于阈值后进入现有业务模型请求流程。

异步结果应用后不在同一个步骤中递归触发同步压缩；下一次业务请求前按上述流程重新判断。

## 8. 异步生命周期、失败与取消

### 8.1 Async compression

- async job 对 frozen 和当前 model working context做深拷贝快照；
- 同一 run 同时最多一个 pending job；
- 两次逻辑请求均失败后记录日志并清除 pending job，原 working context不变；
- async 失败后，只有追加了新的完整交互单元或 injection，且上下文仍处于 60% 到 80% 之间，才允许再次启动；
- 没有新消息时不能在每次请求前形成失败重试循环。

### 8.2 Sync compression

- 进入 80% 时优先同步压缩，不等待无界后台任务；已有 async task先取消并等待；
- 请求失败、超时或校验失败，在一次压缩层 retry 后仍失败，返回 `compression_failed`；
- 成功后实际完整请求仍达到 80%，返回 `compression_limit`；
- 两种停止都立即终止 run，不再发送下一次业务模型请求。

### 8.3 Run cleanup

`run()` 使用 `try/finally` 管理 compression state：

- 正常完成、terminal tool 成功、业务错误、max iterations 或外部取消时都清理 pending task；
- 未完成 task 先 cancel，再 await；
- 已完成但尚未消费的 task 仍回收 Provider 已返回的 usage；
- 取消后的摘要结果不得应用；
- cleanup 不得吞掉调用方对 `CancelledError` 的取消语义。

## 9. Usage 与日志

### 9.1 Usage

- 每个压缩逻辑请求返回的可见 usage 立即合并进 run-level usage；
- 第一次压缩失败后第二次成功，两次返回的可见 usage 都累计；
- 异步结果过期或未应用，不影响其已消耗 usage 的累计；
- Provider 内部 retry 的中间 usage 若 Provider 未暴露，本阶段不修改 Provider 公共 retry 契约；
- `compression_usage_callback` 在每次压缩调用 usage 入账时触发一次。

Judge 创建 spec 时提供 callback，将 compression usage 立即合并到 `_JudgeUsageObserver.usage`。普通业务模型 iteration 仍由 `after_iteration()` 统计，避免同一 usage 重复累计。Judge 被外层 timeout 取消时，已经完成的压缩调用仍可从 observer 快照取回。

### 9.2 日志

通过现有 loguru 配置写入控制台和日志文件。记录关键事件：

- `compression.started`：mode、attempt、session/run 标识、before tokens、阈值；
- `compression.retry`：mode、attempt、bounded reason；
- `compression.applied`：mode、before/after tokens、snapshot suffix 数；
- `compression.discarded`：snapshot mismatch 或 cancellation；
- `compression.failed`：mode、attempts、bounded reason；
- `compression.stopped`：`compression_failed` 或 `compression_limit`。

不得记录完整上下文、完整 evidence、完整摘要 JSON、密钥或令牌。循环内只记录状态变化，不重复打印相同 pending 状态。

## 10. 现有治理链路的调整

`_prepare_messages()` 重构为显式接收 frozen 和 working：

- frozen 原样复制到最终模型上下文；
- orphan repair、backfill、microcompact、tool-result budget 和 hard trim 只处理 working/active；
- repair 不能跨 frozen/working 边界补出非法 tool pair；调用方不得把一个 tool round拆到两个分区；
- hard trim 继续作为最后兜底，但只能从最旧 working 交互单元删除，不能按单条消息切断 user/assistant/tool 单元；
- `context_block_limit` 只影响旧 hard trim，不替代 run-level 60%/80% context-window 阈值。

普通非 terminal 工具结果不是永久保留内容：旧结果可被 microcompact、tool budget 或摘要处理。terminal tool 成功后 run 立即结束，不再启动或执行无意义的下一轮压缩。

## 11. 调用方失败映射

所有 `compression_failed` 和 `compression_limit` 都按当前执行失败处理：

- coordinator：转为 `ReviewPlanningError`，review run 进入 error；
- reviewer：发布 `subagent_status="error"`，保留 usage 和 bounded reason，finalizer 将维度标为 incomplete；
- judge：作为失败 batch 返回非空 error，保留 usage，其他 batch 按现有策略继续；
- 普通 Agent：返回对应 `AgentRunResult.stop_reason` 和非空 error，由 `AgentLoop` 的通用错误输出处理。

需要修正 SubagentManager 目前只显式识别 `error`/`tool_error` 的分支，避免新的 compression stop reason 被当作成功完成。Judge 已按所有非 `completed` 结果失败处理，补充针对新 reason 的回归测试即可。

## 12. 测试与验收

### 12.1 Runner 单元测试

- frozen/working 显式接口及所有调用方迁移；
- frozen 不被 microcompact、tool budget、orphan repair 或 hard trim 修改；
- 首次请求不触发 run-level compression；
- 第二次请求前开始执行阈值检查；
- tool definitions 计入完整 token 估算；
- Provider counter 与 fallback counter 路径；
- resolved 窗口为 `65_536`、`131_072`、`200_000` 时阈值正确；
- 60% 启动 async，80% 使用 sync；
- active 保留最近完整 user/assistant/tool 单元；
- terminal retry 和 injection 开放单元不被切割；
- `AgentRunResult.messages` 不包含 synthetic summary，原始追加顺序不变。

### 12.2 Async 测试

- snapshot 完成后正确拼接新增 suffix；
- frozen 或 working 前缀变化时丢弃结果；
- 同一 run 不创建重复 pending job；
- 不同并发 run 不共享状态；
- async 请求/超时/JSON 校验失败后记录日志并继续；
- 无新消息不重复尝试，有新交互后可重试；
- run 结束和外部 cancellation 都 cancel + await；
- 过期/取消结果不写回，但已返回 usage 仍入账；
- async apply 后不在同一步递归触发 sync。

### 12.3 Sync 与 JSON 测试

- 压缩请求使用 `tools=None` 和 `response_format={"type":"json_object"}`；
- Provider 移除 response format 后返回合规 JSON 可接受；
- 首次逻辑请求失败、第二次成功；
- 两次请求失败或超时返回 `compression_failed`；
- 非法 JSON、缺字段、错误类型和全空对象触发 retry/失败；
- 额外字段允许但不写回 canonical summary；
- 成功压缩后仍达到 80% 返回 `compression_limit`；
- 旧 summary 被替换并参与下一次再摘要；
- 失败时原 working context 保持不变。

### 12.4 Usage 与调用链测试

- 业务 usage 与 compression usage正确合并且不重复；
- 压缩层 retry 的可见 usage 全部累计；
- Judge timeout observer 捕获已完成的 compression usage；
- coordinator 映射为 planning error；
- reviewer 映射为 error/incomplete；
- judge 映射为 failed batch 且 error 非空；
- 普通 Agent 返回准确 stop reason/error；
- 日志包含模式、尝试次数、token 前后值和失败原因，不包含完整上下文。

### 12.5 验证命令

```powershell
pytest tests/agent -v
pytest tests/review -v
ruff check nanoreview/
pytest
```

如果全量测试耗时或环境依赖导致无法完成，交付时必须列出已执行的最近测试、未执行项及原因。

## 13. 文档同步

实现和验证完成后再更新状态文档：

- `.claude/plans/reviewagent-multi-agent-handoff-implementation-addendum.md`：记录实际实现、HEAD/工作区基线、测试结果、剩余缺口；
- `.agents/reference-summary.md`：仅修正“NanoReview 尚无 run-level compression”的参考映射事实；
- `.agents/budget.md`：仅在代码落地使现有 context governance/usage 描述失真时最小更新；
- 不把本任务目标复制到 `AGENTS.md`。

本计划文档在实现前是需求与验收依据；implementation addendum 只在代码、测试或实际验收状态发生变化后更新。
