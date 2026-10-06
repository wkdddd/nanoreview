# kodus-ai

文档入口：`D:\GitHub\kodus-ai`，原有的c盘文档已被清理

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

## kodus-ai 的审查质量评测（已核查）

- Root: `D:\GitHub\kodus-ai`
- Commit: `1df08e5`
- Finder recall 评测：`evals/investigation/run-recall.js` 使用真实的 generalist prompt 和 agent loop，通过 deterministic tool replay 重放已录制的工具结果，并以每个 PR 的 golden comments/known bugs 作为真值。`recall-assertion.js` 和 `recall-judge.js` 对 golden finding 与 agent finding 做语义匹配，输出 recall、precision、F1、fair-recall 和 loop-fidelity；每个 case 还保留 `goldenResults`，可以定位发现、误报和漏报。
- 评测归因：`fair-recall` 排除 replay 没有提供给 agent 的代码所造成的伪漏报；`loop-fidelity` 统计工具调用是否落在 replay 服务覆盖范围内。后者不足时，结果应标记为可信度受限，而不能直接判成审查质量失败。
- 真实 PR benchmark：`scripts/benchmark/benchmark-create.sh` 创建测试 PR 并生成 manifest，生产 worker 在 GitHub/Azure DevOps 上执行审查；`benchmark-evaluate.sh` 从 MongoDB 读取 suggestions，与 `prs-benchmark.json` 的 golden comments 比较。评测会过滤过短、`discarded-by-safeguard` 和 `shouldIncludeInBenchmarkEvaluation` 判定排除的结果，再由 `judge-sonnet.js` 对每个 golden/candidate 做 N×M 语义判断，使用 greedy 1:1 matching 防止一个 candidate 重复命中多个 golden，按 severity threshold 输出 TP、FP、FN、precision、recall、F1、仓库统计以及每个 PR 的 found/missed/noise 和 `match-matrix.json`。
- Judge 约束：judge 的模型、reasoning effort 和版本属于评测身份；judge 变化时历史 floor 不能直接比较。LLM judge 结果应和人工标注、规则或测试证据一起保留，不能把 judge 当作唯一真值。
- Trace-context A/B：`evals/trace-context` 在同一 diff 和工具结果上分别运行 baseline（无 Trace decision）与 trace（有 Trace decision），分类 useful recall、useful suppression、neutral、harmful suppression 和 context-induced false positive。rollout gate 要求 harmful suppression 为 0、instruction injection 成功数为 0、non-golden finding 不显著增加；空 decision pack 时还要求 prompt block 字节级缺失。
- CI 分层：PR workflow 的 `eval:wiring` 使用 fake LLM，只确认 harness 驱动真实引擎，不宣称质量通过；nightly workflow 固定模型、judge、case set，当前 finder recall 配置为 30 个 PR，低于 floor 时触发 confirmation run；Tier-0 周期性运行生产模型 smoke/eval。`gate.js` 等脚本区分 `0`（通过）、`1`（质量或门禁失败）和 `2`（基础设施或未测量），避免把 infra failure 误报为质量通过。
- Reusable design for NanoReview: 先建立可版本化的 golden finding 数据集和 case manifest，再提供本地 deterministic replay harness；统一输出 candidate/golden 匹配明细、TP/FP/FN、precision/recall/F1、fair-recall、工具调用覆盖率、耗时和 token。将 wiring smoke、固定小规模质量集、nightly 扩展集和人工复核分层，并固定 agent/judge/model/reasoning effort 后再比较版本。
- Differences and limits: Kodus 的真实 benchmark 依赖 GitHub/Azure DevOps、Docker、MongoDB、Postgres、worker 和模型 API，成本与运行时间较高；NanoReview 当前没有等价的 eval harness、gold 数据结构或统一 scorer。Kodus 的流程可作为实现参考，数据、阈值和产品门禁不能直接当作 NanoReview 的目标。
- Last checked: 2026-10-06
