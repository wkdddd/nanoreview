# Agent Guidelines

本文件是本仓库中 AI 编码代理的首要工作约束。

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

## 实现约定

- Python 使用 `pathlib.Path`，异步路径使用 `async`/`await`，不得在事件循环中执行长时间阻塞操作。
- 在实现新模块或功能时，项目的日志打印需要完整（错误信息和关键节点的成功info）,但不要泛滥，方便调试
- 调整代码时不需要兼容旧配置或旧参数，除非用户明确要求。

## 参数命名规范

- 参数名应表达数据的职责和语义，不要仅因代码位于 review 模块就统一添加 `review_` 前缀。
- review 模块内部的函数、方法和数据结构优先使用简洁语义名，例如：`target`、`target_type`、`action`、`query`、`root`、`path`、`scope`、`depth`、`dimensions`。只有同一作用域存在歧义或确实需要区分不同领域对象时，才使用领域前缀。
- `review_` 前缀保留给跨模块或持久化的 review 命名空间键，例如会话 metadata、WebUI/API 事件和其他稳定 wire contract：`review_target`、`review_action`、`review_mode_variant`。私有的临时 metadata 可使用 `_review_` 前缀。
- 工具和公共函数的新参数应按调用语义命名；review 专用工具中的“审查查询”使用 `query`，除非同一接口中同时存在多种查询而必须区分。已有外部参数名属于稳定契约，不能只为统一风格随意改名；如需修改，必须检查完整调用链、文档和测试。
- 名称应体现类型和约束：布尔值使用 `is_`、`has_`、`include_` 或 `enable_`；数量使用 `*_count`、`*_limit`；路径使用 `*_path` 或 `*_root`；标识符使用 `*_id`；类型使用 `*_type`。避免使用无语义的 `data`、`info`、`value` 或缩写。

## 协作要求
- Python 使用 `pathlib.Path`，异步路径使用 `async`/`await`，不得在事件循环中执行长时间阻塞操作。
- 在实现新模块或功能时，项目的日志打印需要完整（错误信息和关键节点的成功info）,但不要泛滥，方便调试
- 调整代码时不需要兼容旧配置或旧参数，除非用户明确要求。
- 先取证，后判断：开始前阅读相关代码、测试、配置和专题约束；结论应引用具体文件、行号或命令结果，不凭猜测补全信息。
- 保持范围清晰：只修改完成任务所必需的文件和行为；保留用户已有改动，不回滚、覆盖或顺手重构无关内容。
- 检查完整链路：涉及 API、消息、配置、提示词、工具权限或 WebUI 时，检查生产者、消费者、状态流转、测试和文档。
- 优先复用现有设计：遵循项目已有模式，采用最小但可维护的方案；如存在取舍，说明影响和选择理由。
- 编辑前说明意图：明确将修改的位置、目标和验证方式；**每个模块**都需要添加注释，保证代码可读性。
- 关注工程风险：至少检查输入校验、权限边界、异常处理、异步阻塞、性能、日志和可观测性；不得记录密钥、令牌或完整会话。
- 验证与交付透明：优先运行最贴近改动的测试、静态检查或构建；交付时说明改动、验证结果、未执行项目及原因，并列出剩余风险。
- 及时更新约束：若新增或修改了稳定接口、配置、工作流或项目规则，同步更新相关文档或 `.agents/` 专题说明。
- 统一编码：输出、文件写入、命令和字符串均使用 UTF-8。

## 项目具体说明

- Architecture constraints：`.agents/architecture.md`
- Security boundaries：`.agents/security.md`
- Common gotchas：`.agents/gotchas.md`
- Debug constraints：`.agents/debug.md`
- Design principle:`.agents/design.md`

## 参考项目open-code-review

需要借鉴open-code-review 时，参考项目地址为 `C:\Users\Administrator\Desktop\open-code-review`；先读取索引摘要 [`.agents/reference-summary.md`](.agents/reference-summary.md)。

任务涉及对应说明时，先阅读相关文件再执行。若对应约束文件与 `AGENTS.md` 冲突，以 `AGENTS.md` 为准，并在同一次改动中消除冲突。

