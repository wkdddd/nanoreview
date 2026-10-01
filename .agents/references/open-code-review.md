# open-code-review

本地路径：`C:\Users\Administrator\Desktop\open-code-review`。核查 commit：`e95bdda`；2026-10-01 确认 HEAD 未变，具体行为的核查日期见各节。

主要入口：`internal/scan/`、`internal/diff/`、`internal/llmloop/`、`internal/session/`、`internal/delegate/`；CLI 位于 `cmd/opencodereview/`。

## open-code-review 的 LLM loop（已核查）

- Scope: `internal/llmloop`、`internal/agent`、`internal/scan`
- Entry points: `internal/llmloop/loop.go:RunPerFile`、`internal/llmloop/loop.go:Runner`、`internal/agent/agent.go:Agent.Run`、`internal/agent/agent.go:Agent.executeSubtask`、`internal/scan/agent.go:Agent.executeSubtask`
- Behavior: `llmloop.Runner.RunPerFile` 为每个文件维护一段增长中的对话，在有界轮数内请求 LLM、顺序执行返回的工具调用、追加 assistant/tool 消息；`task_done` 成功即完成，否则可因最大轮数、连续空结果或上下文压缩失败停止。最大轮数耗尽后会进行一次只允许 `code_comment`/`task_done` 的 grace round。
- Contracts: `llmloop.Deps` 注入 LLM client、工具注册表、模板、会话、diff 定位器和评论收集器；Runner 聚合 token/warning/tool-call 统计并等待后台压缩。上下层通过 `(completed, MainLoopStop, error)` 判断完成和停止原因。
- Differences: 参考项目没有与 NanoReview 等价的全局消息 `AgentLoop`；`internal/agent.Agent` 是 diff-review 的产品层编排器，按文件并发 dispatch，先可选 PLAN_TASK，再调用共享 `llmloop.Runner`，最后做 review filter。`internal/gitcmd/runner.go` 仅是 Git 子进程 runner，不是 agent loop。
- Risks/tests: loop 对工具错误、空工具结果、取消、三段式上下文压缩、异步评论处理和后台任务 join 有专门测试（`internal/llmloop/*_test.go`）；迁移其设计时需保留停止原因、会话记录和压缩并发边界。
- Last checked: 2026-09-03

## open-code-review 的预算控制（已核查）

- Scope: `internal/agent`、`internal/llmloop` 的 token 预算与上下文压缩机制
- Entry points: `internal/agent/estimate.go:estimateDiffFileTokens`、`internal/agent/agent.go:625`（滚动预算闸门）、`internal/llmloop/loop.go:345`（`resp.Usage` atomic 累加）、`internal/llmloop/compression.go:19-22`（60%/80% 三段压缩阈值）
- Behavior: dispatch 循环在获取并发槽前做逐文件预算前瞻（已用 + 下一文件估算 > 预算即停止调度，在途 worker 不取消，超限有界）；实际用量由每个 LLM 响应的 `resp.Usage` atomic 累加，闸门基于实测值而非估算；上下文在 60% 窗口异步后台压缩、80% 同步立即压缩（LLM 摘要而非丢弃）。预算用尽是受控覆盖截断（recordWarning + failed(budget) 归因），不是 run 失败。
- Contracts: `MaxTokensBudget` 为 opt-in 配置，默认 0 表示不限制；压缩阈值常量以 `PromptTokenLimit` 单点定义供 agent/scan 预检共享。
- Differences: OCR 按文件并行，预算用尽停止调度会缩小覆盖面；NanoReview 按维度分派，静默丢弃维度会改变审查语义。证据输入预算与实际消耗配额是不同概念，不能直接套用逐文件准入策略。
- Last checked: 2026-09-26（NanoReview 侧新增 run-level compression，OCR 侧事实未变）

## open-code-review 的跨运行 resume（已核查）

- Scope: `internal/session/resume.go`、`internal/session/resume_identity.go`、`internal/session/persist.go`、`cmd/opencodereview/review_cmd.go`、`cmd/opencodereview/scan_cmd.go`
- Entry points: `cmd/opencodereview/shared_flags.go:201/237`（`--resume` flag）、`cmd/opencodereview/review_cmd.go:165`（`validateResumeIdentity`）、`internal/session/resume.go:107`（`LoadReviewResumeState`）、`internal/session/resume_identity.go:50`（`ResumeState.ValidateResume`）
- Behavior: resume 是**显式 CLI 入口**而非自动重入——`ocr review --resume <session-id>` / `ocr scan --resume <session-id>`。会话以 JSONL 持久化在 `$HOME/.opencodereview/sessions/<encoded-repo-path>/<session-id>.jsonl`；resume 时重放该文件构建只读 checkpoint 索引（`ResumeState`，item 按 diff fingerprint 记录已完成文件及其评论），并从中复用已完成的 file-level 工作单元。`--preview` 与 `--resume` 互斥；review 的 resume 还要求 `--from/--to` 或 `--commit`，不支持 workspace resume。
- Contracts: `session_end` 记录携带冻结的 `RunManifest`（父 run 的 coverage 快照）。`Manifest` 为 nil 表示父 run 的输入身份不可校验，而不是“没做工作”；`Closed` 区分被中断的父 run 与正常关闭。`ValidateResume(ResumeRequest)` 会在 `agent.New` 之前校验 repo/branch/model/review mode/diff 范围等输入身份，身份不符直接拒绝且不落任何持久化；新 session 记录 `ResumedFrom`/`ResumeLineage` 血缘。
- Differences: OCR 的 resume 是面向“中断后继续”的产品能力，依赖显式用户入口、持久化 manifest、逐文件 fingerprint 和输入身份校验；这类能力必须作为独立设计落地（显式入口 + 身份校验 + manifest），不能与进程内状态对象混为一谈。
- Risks/tests: 参考入口不等于本项目支持该能力；复用 resume 方案需核对身份校验、持久化与跨运行一致性测试。
- Last checked: 2026-09-19
