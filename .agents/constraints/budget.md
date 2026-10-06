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

## Usage

- Runner 累计可见模型响应和压缩请求 usage；失败、超时和丢弃摘要不应丢失已经观察到的用量，未知用量不虚构。
- Provider 内部重试未单独汇总；会话整理直接调用 Provider，其 usage 不在 run 统计内。
- Judge 通过 Runner 执行，失败 batch 仍保留已消耗 usage；超时使用已观察到的批内快照。
- 当前 `AgentLoop._total_usage`/WebSocket `/usage` 是进程观测，未完整包含 review 与会话整理用量，不代表账单。

## 变更检查

预算改动核对配置映射、evidence 策略、Runner/Subagent 限制、压缩分区与停止原因、Provider usage、WebUI 展示及对应测试。新增总配额须定义作用域、计量来源、并发扣减与超限行为。
