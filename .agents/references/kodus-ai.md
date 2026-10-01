# kodus-ai

文档入口：`C:\Users\Administrator\Desktop\kodus-ai 文档`，指向项目 `docs/`，不是独立代码源。

## kodus-ai 的 review 与对话（已核查）

- Root: `C:\Users\Administrator\Desktop\kodus-ai`
- Commit: `1df08e5`
- Scope: GitHub 评论分流、Git 对话 use-case、conversation provider/prompt/store、review stage/provider/core adapter、agent-harness runner。
- Entry points: `libs/platform/infrastructure/webhooks/github/githubPullRequest.handler.ts:460/615`、`libs/platform/application/use-cases/codeManagement/chatWithKodyFromGit.use-case.ts:2160/2452`、`libs/agents/infrastructure/services/agents/conversation/conversationAgent.ts:228/234/505`、`libs/code-review/infrastructure/agents/core/core-agent-loop.adapter.ts:65/148/211`。
- Behavior: review 命令由 webhook 入队审查任务；非 review 的 Kody mention 或符合条件的线程回复进入对话 use-case。对话是独立 ConversationAgentSpec，review 是分类 finder + verifier；两者共用 `AiSdkAgentRunner -> LLM.run`，不是同一 agent 实例在两个模式间续跑。
- Contracts: 共用 `AgentSpec`、`AgentRunInput`、`RunState`、工具与 policy 契约。模型任务路由分别为 `conversation`、`codeReview`（规则审查另有 `kodyRulesReview`），因此模型也可分别配置。
- Current mapping: 本条仅核查参考项目，不定义 NanoReview 目标或迁移方案。
- Differences: review 输入包含 changedFiles、PR/commit 信息、callGraph、memoryRules、previousDecisions 等，并接入窗口预算、分批、压缩与 overflow retry；对话重建 PR 描述、原 suggestion/diff、评论回复，另从 Mongo conversation store 取最近 10 条 user/assistant 消息，作为引用文本组装进 prompt。对话不接续 reviewer 的 RunState/tool transcript，且当前 spec 未接入 review 的 CompressionPolicy。
- Risks/tests: 本次为源码核查，未运行测试或服务。相关测试包括 `codeCommentMarkers.spec.ts`、`conversation-prompt.spec.ts`、`mongo-conversation-store.spec.ts`、`ai-sdk-agent-runner.spec.ts`；conversationAgent.ts 的 thread 接口注释仍称仅用于日志，与实际 history load/persist 不一致，以执行代码为准。
- Last checked: 2026-10-01

## 上下文压缩（已核查）

- Commit: `1df08e5`；2026-10-01 仅阅读源码和测试，未运行测试或服务。
- 入口：`libs/agent-harness/infrastructure/policies/compression.policy.ts:18`、`compression/context-window-compressor.ts:72`、`compression/context-compressor.ts:329/513`；review 接入为 `libs/code-review/infrastructure/agents/core/core-agent-loop.adapter.ts:120/156`。
- `CompressionPolicy.prepareStep` 调用注入的 Compressor、替换当次模型请求窗口并输出 `context.compress` 计数事件；策略不内嵌于 Runner，也不承担跨轮 conversation 摘要。
- ContextWindowCompressor 按 token 估算消息及 system/tool overhead；超过窗口 70% 软触发，默认预留 8%。先截短工具结果（最近 4 个消息位置中的结果约 3000 字符，旧结果约 400），仍超预算时更强截短、删除较早完整轮次，最后进一步截短剩余内容。属于确定性裁剪，不调用 LLM 生成语义摘要，预算保证仍受估算与不可缩内容限制。
- 底层支持从 allToolCalls 构造调查回顾，但当前标准压缩器调用 `compressMessages(modelMsgs, [])`，没有接入该回顾记录；以实际接入而非注释为准。
- review 的 `OverflowRecoveringRunner` 对实际超窗错误按原窗口 60% 收紧后重跑该 pass 一次；该逻辑位于 review 层，不是通用 Harness 的副作用恢复协议。
- ConversationAgent 当前 policies 只有 WriteGate/WriteTruth，没有 CompressionPolicy。每轮从存储加载最近 10 条 user/assistant 消息，并与重建的 PR/comment/diff 上下文组装为引用文本；Mongo store 保留最近 100 条消息。未发现这条链路的持续语义摘要；保存原用户消息和最终回答，不续接 review 工具 transcript。
- 相关测试：`compression.policy.spec.ts`、`context-window-compressor.spec.ts`、`context-compressor.spec.ts`、`ai-sdk-agent-runner.compression.e2e.spec.ts`、`overflow-recovering-runner.spec.ts`。
