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

审查专用的协调、分派、收集和最终化由 `review/orchestration.py` 负责，不要把这些逻辑下沉到通用 `AgentLoop`。

## open-code-review 的 LLM loop（已核查）
- Root: `C:\Users\Administrator\Desktop\open-code-review`
- Commit: `e95bdda`
- Scope: `internal/llmloop`、`internal/agent`、`internal/scan`
- Entry points: `internal/llmloop/loop.go:RunPerFile`、`internal/llmloop/loop.go:Runner`、`internal/agent/agent.go:Agent.Run`、`internal/agent/agent.go:Agent.executeSubtask`、`internal/scan/agent.go:Agent.executeSubtask`
- Behavior: `llmloop.Runner.RunPerFile` 为每个文件维护一段增长中的对话，在有界轮数内请求 LLM、顺序执行返回的工具调用、追加 assistant/tool 消息；`task_done` 成功即完成，否则可因最大轮数、连续空结果或上下文压缩失败停止。最大轮数耗尽后会进行一次只允许 `code_comment`/`task_done` 的 grace round。
- Contracts: `llmloop.Deps` 注入 LLM client、工具注册表、模板、会话、diff 定位器和评论收集器；Runner 聚合 token/warning/tool-call 统计并等待后台压缩。上下层通过 `(completed, MainLoopStop, error)` 判断完成和停止原因。
- Current mapping: NanoReview 的 `nanoreview/agent/runner.py:AgentRunner.run` 是最接近的单会话工具循环；`nanoreview/agent/loop.py:AgentLoop` 还额外承担 bus、会话恢复、状态机、并发入站消息和出站响应；审查专用协调在 `nanoreview/review/orchestration.py`。
- Differences: 参考项目没有与 NanoReview 等价的全局消息 `AgentLoop`；`internal/agent.Agent` 是 diff-review 的产品层编排器，按文件并发 dispatch，先可选 PLAN_TASK，再调用共享 `llmloop.Runner`，最后做 review filter。`internal/gitcmd/runner.go` 仅是 Git 子进程 runner，不是 agent loop。
- Risks/tests: loop 对工具错误、空工具结果、取消、三段式上下文压缩、异步评论处理和后台任务 join 有专门测试（`internal/llmloop/*_test.go`）；迁移其设计时需保留停止原因、会话记录和压缩并发边界。
- Last checked: 2026-09-03

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
