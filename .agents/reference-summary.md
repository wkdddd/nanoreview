# 本地参考项目摘要与低 Token 工作流

本文件是重复借鉴本地项目时的入口摘要。先读本文件，再按任务读取参考项目源码；不要默认全量读取参考仓库。

## 默认参考项目

- Root: `C:\Users\Administrator\Desktop\open-code-review`
- Project: `open-code-review`，成熟的开源代码审查 Agent（Go）
- Commit checked: `e95bdda`
- Primary index: `AGENTS.md`、`skills/open-code-review/SKILL.md`
- Core review packages: `internal/scan/`、`internal/diff/`、`internal/llmloop/`、`internal/session/`、`internal/delegate/`
- CLI entry: `cmd/opencodereview/`
- UI/integration: `pages/`、`extensions/vscode/`、`plugins/open-code-review/`

## 使用规则

1. 先确认参考项目绝对路径、当前 commit 和本次目标。
2. 先搜索路径和符号，再读取命中文件的必要行段：优先使用 `rg -n`、`rg --files` 和带行号的局部读取。
3. 只把与当前目标直接相关的源码、配置、测试和文档加入上下文；不要粘贴整个仓库或完整大文件。
4. 本文件已有的事实不重复调查，除非参考项目的 commit/hash 已变化或源码与摘要矛盾。
5. 发现可复用结论后，更新本文件或对应专题摘要，并记录来源路径和 commit；不要把临时推测写成事实。
6. 参考项目中的密钥、令牌、会话内容和不必要的个人信息不得复制到摘要或 prompt。

## NanoReview 当前项目锚点

| 领域 | 主要入口 | 读取目的 |
| --- | --- | --- |
| 消息链路 | `nanoreview/channels/`、`nanoreview/bus/`、`nanoreview/agent/loop.py` | 渠道入站、会话上下文、通用运行时事件 |
| LLM 与工具 | `nanoreview/agent/runner.py`、`nanoreview/agent/tools/`、`nanoreview/providers/` | 模型调用、工具契约、权限和 Provider 适配 |
| 代码审查 | `nanoreview/review/`，尤其是 `review/orchestration.py` | 目标标准化、计划、子代理、结果校验和报告生成 |
| 审查子域 | `review/input/`、`review/planning/`、`review/source/`、`review/output/` | 输入、策略、源码获取、报告校验与渲染 |
| 持久化 | `nanoreview/session/`、`nanoreview/utils/webui_transcript.py`、`nanoreview/utils/subagent_trace.py` | 会话、回放、WebUI transcript 和子代理 trace |
| WebUI | `review-webui/src/`、`nanoreview/channels/websocket.py` | API/WebSocket wire contract、状态和界面调用链 |
| 行为契约 | `nanoreview/templates/`、`nanoreview/skills/` | 系统提示词、工具说明和运行时技能 |
| 回归测试 | `tests/`（镜像 `nanoreview/`） | 查找最接近的行为、契约和跨层测试 |

## 跨层调用链

### 普通消息

`channels -> bus -> AgentLoop -> AgentRunner -> providers/tools -> bus -> channels`

`AgentLoop` 和 `AgentRunner` 保持通用；渠道、Provider、工具和 WebUI 行为应留在各自边界。

### 代码审查

`WebSocket/API -> review input normalization -> review orchestration -> planning -> subagents -> validation/finalizer -> WebUI report`

审查专用的协调、分派、收集和最终化当前由 `review/orchestration.py` 承担，但它已被标记为迁移中的 legacy compatibility shell；目标 owner 是 `agent/loop.py` 的 supervisor 生命周期（见 `.agents/architecture.md`）。迁移完成前不要在 legacy orchestrator 中新增能力，也不要复制它的黑盒入口。

## open-code-review 的 LLM loop（已核查）
- Root: `C:\Users\Administrator\Desktop\open-code-review`
- Commit: `e95bdda`
- Scope: `internal/llmloop`、`internal/agent`、`internal/scan`
- Entry points: `internal/llmloop/loop.go:RunPerFile`、`internal/llmloop/loop.go:Runner`、`internal/agent/agent.go:Agent.Run`、`internal/agent/agent.go:Agent.executeSubtask`、`internal/scan/agent.go:Agent.executeSubtask`
- Behavior: `llmloop.Runner.RunPerFile` 为每个文件维护一段增长中的对话，在有界轮数内请求 LLM、顺序执行返回的工具调用、追加 assistant/tool 消息；`task_done` 成功即完成，否则可因最大轮数、连续空结果或上下文压缩失败停止。最大轮数耗尽后会进行一次只允许 `code_comment`/`task_done` 的 grace round。
- Contracts: `llmloop.Deps` 注入 LLM client、工具注册表、模板、会话、diff 定位器和评论收集器；Runner 聚合 token/warning/tool-call 统计并等待后台压缩。上下层通过 `(completed, MainLoopStop, error)` 判断完成和停止原因。
- Current mapping: NanoReview 的 `nanoreview/agent/runner.py:AgentRunner.run` 是最接近的单会话工具循环；`nanoreview/agent/loop.py:AgentLoop` 还额外承担 bus、会话恢复、状态机、并发入站消息和出站响应；审查专用协调在 `nanoreview/agent/orchestration.py`（迁移中的 legacy shell，目标 owner 为 `agent/loop.py`）。
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
- Current mapping: NanoReview 无等价总预算闸门（有意删除了 orchestration 的 `_admit_assignments` 越权准入）；证据侧总量由 `review.evidence_token_budget`（受 `EvidenceBudget.from_options` 的 usable 窗口钳制）控制，单请求尺寸由 `context_window_tokens`（主 agent 与 subagent 均接通 runner 裁剪）控制；run 级上下文压缩已落地于 `nanoreview/agent/compression.py` 与 `AgentRunner.run()`，采用 60% 异步 / 80% 同步的三段语义，按单次 run 独立状态管理（详见 `.agents/budget.md`）。
- Differences: OCR 按文件并行（同质、可截断、N 个由数据驱动），预算用尽即 break 只损失覆盖面；NanoReview 按维度并行（异质、语义互补、4 个固定），静默丢维度会破坏用户显式选择或 planner 决策，故 OCR 的"预算用尽即 break"不可移植。若日后需要成本上限，应按 OCR 做法独立加 opt-in 的实测闸门（Usage 累加 + 默认 0 不限制），而非复用证据预算项。
- Risks/tests: NanoReview 已实现 run-level 压缩式上下文管理（`nanoreview/agent/compression.py` + `AgentRunner.run()`，2026-09-26），采用与 OCR 同源的 60%/80% 三段语义，但按 run 独立状态实现、不复用 OCR 的后台任务管理；reviewer 长会话仍有 runner 硬裁剪兜底。两者机制差异见下方 `Current mapping`。
- Last checked: 2026-09-26（NanoReview 侧新增 run-level compression，OCR 侧事实未变）

## open-code-review 的跨运行 resume（已核查）
- Root: `C:\Users\Administrator\Desktop\open-code-review`
- Commit: `e95bdda`
- Scope: `internal/session/resume.go`、`internal/session/resume_identity.go`、`internal/session/persist.go`、`cmd/opencodereview/review_cmd.go`、`cmd/opencodereview/scan_cmd.go`
- Entry points: `cmd/opencodereview/shared_flags.go:201/237`（`--resume` flag）、`cmd/opencodereview/review_cmd.go:165`（`validateResumeIdentity`）、`internal/session/resume.go:107`（`LoadReviewResumeState`）、`internal/session/resume_identity.go:50`（`ResumeState.ValidateResume`）
- Behavior: resume 是**显式 CLI 入口**而非自动重入——`ocr review --resume <session-id>` / `ocr scan --resume <session-id>`。会话以 JSONL 持久化在 `$HOME/.opencodereview/sessions/<encoded-repo-path>/<session-id>.jsonl`；resume 时重放该文件构建只读 checkpoint 索引（`ResumeState`，item 按 diff fingerprint 记录已完成文件及其评论），并从中复用已完成的 file-level 工作单元。`--preview` 与 `--resume` 互斥；review 的 resume 还要求 `--from/--to` 或 `--commit`，不支持 workspace resume。
- Contracts: `session_end` 记录携带冻结的 `RunManifest`（父 run 的 coverage 快照）。`Manifest` 为 nil 表示父 run 的输入身份不可校验，而不是“没做工作”；`Closed` 区分被中断的父 run 与正常关闭。`ValidateResume(ResumeRequest)` 会在 `agent.New` 之前校验 repo/branch/model/review mode/diff 范围等输入身份，身份不符直接拒绝且不落任何持久化；新 session 记录 `ResumedFrom`/`ResumeLineage` 血缘。
- Current mapping: NanoReview **当前不采用**该机制。没有任何 resume 入口：`ReviewRunState` 只是进程内运行期可观测性（status/phase/usage/cancel/artifact ref），不是恢复协议；进程异常或重启后不恢复 reviewer/judge 工作，session metadata 的 review 状态只用于 gate 和状态展示。
- Differences: OCR 的 resume 是面向“中断后继续”的产品能力，依赖显式用户入口、持久化 manifest、逐文件 fingerprint 和输入身份校验；这类能力必须作为独立设计落地（显式入口 + 身份校验 + manifest），不能与进程内状态对象混为一谈。
- Risks/tests: 若日后 NanoReview 需要 resume，必须一次性补齐用户入口、输入身份校验、持久化 manifest 和跨运行一致性测试；不要复用任何“进程内恢复”式的半成品，也不要把 `result_ref` 之类字段提前预埋进现有 wire contract。
- Last checked: 2026-09-19

## 摘要记录格式

为每个参考项目或功能建立一条短记录，使用以下字段：

```markdown
## <参考项目或功能名>
- Root: `C:\absolute\path`
- Commit: `<git commit or content hash>`
- Scope: `<本次调查范围>`
- Entry points: `<path:symbol>, ...`
- Behavior: `<用 1-3 句描述实际行为>`
- Contracts: `<API/event/config/schema>`
- Current mapping: `<NanoReview 对应路径或“无”>`
- Differences: `<已确认差异>`
- Risks/tests: `<安全、错误处理、性能和测试缺口>`
- Last checked: `<YYYY-MM-DD>`
```

## 增量刷新

当参考项目更新时，先比较上次记录的 commit：

```powershell
git -C C:\path\to\reference-project diff --name-only <OLD_COMMIT> <NEW_COMMIT>
```

只重新读取变化文件及其直接消费者。若没有变化，直接复用摘要并说明“参考项目未变化”。没有 Git 时，使用文件清单和内容 hash 作为替代；不要因为无法取得 hash 就复制全库。

## 推荐的最小输出

参考项目筛查结果只返回：

1. 相关文件路径和符号。
2. 每个文件一句职责。
3. 与 NanoReview 的已确认差异。
4. 需要修改的边界、测试位置和未确认问题。

除非用户明确要求，不输出大段源码、重复背景或与目标无关的目录清单。
