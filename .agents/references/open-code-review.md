# open-code-review

以下保留原核查记录；2026-10-01 确认本地 HEAD 未变。本次仅整理格式，未重新核查源码或运行测试，具体行为的核查日期见各节。

主要入口：`internal/scan/`、`internal/diff/`、`internal/llmloop/`、`internal/session/`、`internal/delegate/`；CLI 位于 `cmd/opencodereview/`。

## open-code-review 的 LLM loop（已核查）

- Root: `C:\Users\Administrator\Desktop\open-code-review`
- Commit: `e95bdda`
- Scope: `internal/llmloop`、`internal/agent`、`internal/scan`
- Entry points: `internal/llmloop/loop.go:RunPerFile`、`internal/llmloop/loop.go:Runner`、`internal/agent/agent.go:Agent.Run`、`internal/agent/agent.go:Agent.executeSubtask`、`internal/scan/agent.go:Agent.executeSubtask`
- Behavior: `llmloop.Runner.RunPerFile` 为每个文件维护一段增长中的对话，在有界轮数内请求 LLM、顺序执行返回的工具调用、追加 assistant/tool 消息；`task_done` 成功即完成，否则可因最大轮数、连续空结果或上下文压缩失败停止。最大轮数耗尽后会进行一次只允许 `code_comment`/`task_done` 的 grace round。
- Contracts: `llmloop.Deps` 注入 LLM client、工具注册表、模板、会话、diff 定位器和评论收集器；Runner 聚合 token/warning/tool-call 统计并等待后台压缩。上下层通过 `(completed, MainLoopStop, error)` 判断完成和停止原因。
- Current mapping: 本条仅记录参考项目的执行与编排事实，不定义 NanoReview 的 Loop 拆分或迁移方案。
- Differences: 参考项目没有与 NanoReview 等价的全局消息 `AgentLoop`；`internal/agent.Agent` 是 diff-review 的产品层编排器，按文件并发 dispatch，先可选 PLAN_TASK，再调用共享 `llmloop.Runner`，最后做 review filter。`internal/gitcmd/runner.go` 仅是 Git 子进程 runner，不是 agent loop。
- Risks/tests: loop 对工具错误、空工具结果、取消、三段式上下文压缩、异步评论处理和后台任务 join 有专门测试（`internal/llmloop/*_test.go`）；迁移其设计时需保留停止原因、会话记录和压缩并发边界。
- Last checked: 2026-09-03

## open-code-review 的预算控制（已核查）

- Root: `C:\Users\Administrator\Desktop\open-code-review`
- Commit: `e95bdda`
- Scope: `internal/agent`、`internal/llmloop` 的 token 预算与上下文压缩机制
- Entry points: `internal/agent/estimate.go:estimateDiffFileTokens`、`internal/agent/agent.go:625`（滚动预算闸门）、`internal/llmloop/loop.go:345`（`resp.Usage` atomic 累加）、`internal/llmloop/compression.go:19-22`（60%/80% 三段压缩阈值）
- Behavior: dispatch 循环在获取并发槽前做逐文件预算前瞻（已用 + 下一文件估算 > 预算即停止调度，在途 worker 不取消，超限有界）；实际用量由每个 LLM 响应的 `resp.Usage` atomic 累加，闸门基于实测值而非估算；上下文在 60% 窗口异步后台压缩、80% 同步立即压缩（LLM 摘要而非丢弃）。预算用尽是受控覆盖截断（recordWarning + failed(budget) 归因），不是 run 失败。
- Contracts: `MaxTokensBudget` 为 opt-in 配置，默认 0 表示不限制；压缩阈值常量以 `PromptTokenLimit` 单点定义供 agent/scan 预检共享。
- Current mapping: 本条仅记录参考项目的预算机制，不代表 NanoReview 已实现累计 token 配额。
- Differences: OCR 按文件并行，预算用尽停止调度会缩小覆盖面；NanoReview 按维度分派，静默丢弃维度会改变审查语义。证据输入预算与实际消耗配额是不同概念，不能直接套用逐文件准入策略。
- Risks/tests: 本次未运行测试；复用机制时需核对调度覆盖、在途任务及压缩并发边界，原记录未列出本节具体测试入口。
- Last checked: 2026-09-26（NanoReview 侧新增 run-level compression，OCR 侧事实未变）

## open-code-review 的跨运行 resume（已核查）

- Root: `C:\Users\Administrator\Desktop\open-code-review`
- Commit: `e95bdda`
- Scope: `internal/session/resume.go`、`internal/session/resume_identity.go`、`internal/session/persist.go`、`cmd/opencodereview/review_cmd.go`、`cmd/opencodereview/scan_cmd.go`
- Entry points: `cmd/opencodereview/shared_flags.go:201/237`（`--resume` flag）、`cmd/opencodereview/review_cmd.go:165`（`validateResumeIdentity`）、`internal/session/resume.go:107`（`LoadReviewResumeState`）、`internal/session/resume_identity.go:50`（`ResumeState.ValidateResume`）
- Behavior: resume 是**显式 CLI 入口**而非自动重入——`ocr review --resume <session-id>` / `ocr scan --resume <session-id>`。会话以 JSONL 持久化在 `$HOME/.opencodereview/sessions/<encoded-repo-path>/<session-id>.jsonl`；resume 时重放该文件构建只读 checkpoint 索引（`ResumeState`，item 按 diff fingerprint 记录已完成文件及其评论），并从中复用已完成的 file-level 工作单元。`--preview` 与 `--resume` 互斥；review 的 resume 还要求 `--from/--to` 或 `--commit`，不支持 workspace resume。
- Contracts: `session_end` 记录携带冻结的 `RunManifest`（父 run 的 coverage 快照）。`Manifest` 为 nil 表示父 run 的输入身份不可校验，而不是“没做工作”；`Closed` 区分被中断的父 run 与正常关闭。`ValidateResume(ResumeRequest)` 会在 `agent.New` 之前校验 repo/branch/model/review mode/diff 范围等输入身份，身份不符直接拒绝且不落任何持久化；新 session 记录 `ResumedFrom`/`ResumeLineage` 血缘。
- Current mapping: 本条仅记录外部 resume 设计，不表示 NanoReview 支持跨运行任务恢复。
- Differences: OCR 的 resume 是面向“中断后继续”的产品能力，依赖显式用户入口、持久化 manifest、逐文件 fingerprint 和输入身份校验；这类能力必须作为独立设计落地（显式入口 + 身份校验 + manifest），不能与进程内状态对象混为一谈。
- Risks/tests: 参考入口不等于本项目支持该能力；复用 resume 方案需核对身份校验、持久化与跨运行一致性测试。
- Last checked: 2026-09-19

## open-code-review 的审查质量评测（已核查）

- Root: `C:\Users\Administrator\Desktop\open-code-review`
- Commit: `e95bdda`
- Public benchmark: README 和 `pages/src/components/BenchmarkSection.tsx` 宣称 AACR-Bench 包含 50 个开源仓库、200 个真实 Pull Request、10 种编程语言、80+ 名资深工程师交叉验证和 1,505 个标注的 ground-truth issues；README 链接到 Hugging Face 数据集 `Alibaba-Aone/aacr-bench`。
- Metrics: 展示 `precision`、`recall`、`F1`，以及平均耗时和平均 token；前 3 个指标用于衡量发现已知问题的准确性与覆盖率，后 2 个指标用于衡量运行成本和延迟。
- Evidence boundary: 当前 commit 中能确认数据集说明、前端静态结果表和 benchmark 截图，但没有发现完整的数据生成、执行、评分脚本或 CI eval harness。因此这些数字是公开 benchmark 结果展示，不能视为在 NanoReview 本地复现的结果；AACR-Bench 的具体标注协议和评分细节仍需以数据集及其发布说明为准。
- Reusable design for NanoReview: 以真实 PR/仓库快照构造 case，保存人工确认的 golden findings；运行审查 agent 后，将 candidate findings 与 golden findings 配对，计算 precision/recall/F1，并同时记录耗时、输入输出 token 和模型身份。外部 Hugging Face 数据集可以作为补充数据源，但应先转换为 NanoReview 的固定 case schema 并保留来源、版本和许可信息。
- Limitations: 不能只复制展示表格或把 README 的总体数字当作本项目验证结果；若要声称可复现，需要补充 case manifest、运行器、finding matcher、结果落盘和固定模型/评测版本。
- Last checked: 2026-10-06
