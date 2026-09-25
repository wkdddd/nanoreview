# Agent Guidelines

本文件是本仓库中 AI 编码代理的首要工作约束。当程序事实和约束冲突时，优先考虑程序并更新约束。此文件应保持精简，避免包含冗余或与其他约束文件重复的信息。

`.agents/` 包含按主题拆分的补充说明：开始工作时先阅读本文件；任务涉及架构、安全或运行时行为时，再阅读对应主题文件。根文件只保留入口级约束和仓库定位，专题文件负责展开具体规则；两者不得冲突。

## 项目定位

NanoReview 是基于 nanobot 演进的个人多智能体代码审查系统，主体为 Python，配套 React/TypeScript WebUI。涉及消息链路、模块归属和跨边界改动时，阅读 `.agents/architecture.md`；涉及扩展点、抽象与最小改动时，阅读 `.agents/design.md`。

## 常用命令

在已启用项目 Python 环境的前提下执行：

```bash
# Python 测试与静态检查
pytest
pytest tests/agent/test_codereview.py -v
ruff check nanoreview/

# WebUI
cd review-webui && bun run dev
cd review-webui && bun run build
cd review-webui && bun run test

# Gateway
nanoreview gateway
```

在 Windows/PowerShell 中执行命令前设置 UTF-8：

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```

## 测试规范

- Python 目标版本为 3.11+；pytest 使用 `asyncio_mode = "auto"`。
- 测试放在 `tests/`，目录结构应镜像被测模块。
- 改动共享行为、跨模块契约、配置、提示词、工具权限、渠道协议或用户可见 WebUI 行为时，补充或更新最贴近的测试。
- 前端或后端接口变更必须检查完整调用链：API/消息事件、WebUI 状态、配置、模板、文档与测试。
- 不做无关重构，不批量格式化，不回滚或覆盖已有用户改动。
- 日志记录关键状态变化和错误上下文，避免循环内高频噪音；不要记录密钥、令牌、完整会话或不必要的敏感元数据。

## 参数命名规范

- 参数名应表达数据的职责和语义，不要仅因代码位于 review 模块就统一添加 `review_` 前缀。
- review 模块内部的函数、方法和数据结构优先使用简洁语义名，例如：`target`、`target_type`、`action`、`query`、`root`、`path`、`scope`、`depth`、`dimensions`。只有同一作用域存在歧义或确实需要区分不同领域对象时，才使用领域前缀。
- `review_` 前缀保留给跨模块或持久化的 review 命名空间键，例如会话 metadata、WebUI/API 事件和其他稳定 wire contract：`review_target`、`review_action`。私有的临时 metadata 可使用 `_review_` 前缀。
- 工具和公共函数的新参数应按调用语义命名；review 专用工具中的“审查查询”使用 `query`，除非同一接口中同时存在多种查询而必须区分。已有外部参数名属于稳定契约，不能只为统一风格随意改名；如需修改，必须检查完整调用链、文档和测试。
- 名称应体现类型和约束：布尔值使用 `is_`、`has_`、`include_` 或 `enable_`；数量使用 `*_count`、`*_limit`；路径使用 `*_path` 或 `*_root`；标识符使用 `*_id`；类型使用 `*_type`。避免使用无语义的 `data`、`info`、`value` 或缩写。

## 协作要求

- Python 使用 `pathlib.Path`，异步路径使用 `async`/`await`，不得在事件循环中执行长时间阻塞操作。
- 在实现新模块或功能时，项目的日志打印需要完整（错误信息和关键节点的成功info）,但不要泛滥，方便调试
- 调整代码时不需要兼容旧配置或旧参数，除非用户明确要求。
- 先取证，后判断：开始前阅读相关代码、测试、配置和专题约束；结论应引用具体文件、行号或命令结果，不凭猜测补全信息,证据或需求不清楚时询问用户，不得自行猜测。
- 保持范围清晰：只修改完成任务所必需的文件和行为；保留用户已有改动，不回滚、覆盖或顺手重构无关内容。
- 检查完整链路：涉及 API、消息、配置、提示词、工具权限或 WebUI 时，检查生产者、消费者、状态流转、测试和文档。
- 优先复用现有设计：遵循项目已有模式，采用最小但可维护的方案；如存在取舍，说明影响和选择理由。
- 编辑前说明意图：明确将修改的位置、目标和验证方式；重要模块都需要添加注释，保证代码可读性。
- 关注工程风险：至少检查输入校验、权限边界、异常处理、异步阻塞、性能、日志和可观测性；不得记录密钥、令牌或完整会话。
- 验证与交付透明：优先运行最贴近改动的测试、静态检查或构建；交付时说明改动、验证结果、未执行项目及原因，并列出剩余风险。
- 文档分层与同步门槛：先判断信息归属，再决定是否编辑文档；禁止为了“保持一致”批量复制同一条约束。
  - `AGENTS.md` 只记录跨任务、长期有效的代理工作规则。除非用户明确要求调整仓库规则，或反复出现的代理行为问题需要建立长期规则，不得把单个功能的目标、非目标、验收项、实现状态或临时决策写入这里。
  - `.agents/*.md` 只记录对应主题的长期架构、安全、设计、调试或预算契约。只有代码或稳定接口确实改变了该契约，且原文因此失真时才最小更新；仅仅阅读、引用或遵守某项任务要求不构成更新理由。
  - `.claude/plans/`、任务说明和 implementation addendum 承载产品目标、任务级非目标、验收基线和当前进展。此类内容默认只更新所属计划/进展文档，不向 `AGENTS.md` 或 `.agents/` 复制。
  - 例如任务提出“本版本不提供 review 恢复”时，这属于任务级非目标，默认只留在目标/进展文档；只有某个专题文件原有的能力描述因此变成事实错误时，才最小修正那一处，不能把该句复制到其他文件。
  - `.agents/reference-summary.md` 只在参考项目事实、核查范围或映射发生变化时更新；不要用它记录本仓库的临时决策。
  - 编辑任何约束或参考文件前，必须确认：事实是什么、唯一权威文件是哪一个、现有文字是否已经失真、是否有明确的跨文件同步理由。现有文字仍准确时停止扩散；确需同步时逐文件说明不同消费者需要的内容，禁止整段镜像。
- 统一编码：输出、文件写入、命令和字符串均使用 UTF-8。
- 需要commit时的日志信息需要完整但不啰嗦，不要使用"auto commits"等无价值信息

## 项目具体说明

- Architecture constraints：`.agents/architecture.md`
- Security boundaries：`.agents/security.md`
- Common gotchas：`.agents/gotchas.md`
- Debug constraints：`.agents/debug.md`
- Design principle:`.agents/design.md`
- Budget and token controls：`.agents/budget.md`

## ReviewAgent 目标与进展文档

- `.claude/plans/reviewagent-multi-agent-handoff.md` 是 ReviewAgent 的目标调整文档和验收基线。涉及 supervisor、`reviewstate`、状态映射与失败可见性、AgentRunner、Judge 或上下文压缩的设计时，先按该文档确定目标行为；不要把其中的“当前实现事实”当成现状，因为该文档描述的是应达到的架构。
- `.claude/plans/reviewagent-multi-agent-handoff-implementation-addendum.md` 是上述目标的最新实施进展文档。开始相关任务前必须阅读，并以代码、测试和命令结果核对其“当前”结论；它记录实际已落地能力、缺口、风险、验证结果和下一步，不替代目标文档。
- 只有代码、测试或目标/验收状态发生变化时，才更新 implementation addendum：更新日期、HEAD/工作区基线、已落地能力、未满足验收项、测试/检查结果和后续边界。仅阅读、讨论或调整代理规则不需要修改它；不得保留已经失效的“当前”描述，也不得把历史工作区状态继续当作现状。
- ReviewAgent 的具体产品目标、非目标和验收项以目标文档及 implementation addendum 为唯一来源；不要为了提醒代理而把同一条产品约束重述到 `AGENTS.md` 或 `.agents/`。目标文档与实际代码不一致时，不要为了“同步”而改写所有文档；按文档角色在 addendum 记录缺口，只有明确目标变化或现有专题契约已失真时才编辑对应权威文件。
- 涉及稳定接口、状态机、metadata、artifact、WebUI/API 或失败与重试语义的改动，除更新 addendum 外，还要同步检查最近的 `.agents/` 约束、生产者/消费者、测试和文档链路。

## 参考项目open-code-review

需要借鉴open-code-review 时，参考项目地址为 `C:\Users\Administrator\Desktop\open-code-review`；先读取索引摘要 [`.agents/reference-summary.md`](.agents/reference-summary.md)。

任务涉及对应说明时，先阅读相关文件再执行。若对应约束文件与 `AGENTS.md` 冲突，以 `AGENTS.md` 为准；只在当前任务依赖且专题文件已失真时最小修正，不为消除重复而批量同步。
