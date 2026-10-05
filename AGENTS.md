# Agent Guidelines

NanoReview 是基于 nanobot 演进的个人多智能体代码审查系统，主体为 Python，配套 React/TypeScript WebUI。本文件规定 AI 与用户的协作方式；产品目标以 `.agents/plans/` 为准。

此项目作为agent开发求职的简历项目，需要保持一定的技术广度，功能调整时可以向用户建议最新的agent开发技术

## 工作方式

- 对约束文档调整时应该简洁凝练，不要使用大段冗余啰嗦的约束prompt
- 先阅读相关代码、测试、配置及专题约束，再提出结论；引用文件、行号或命令结果。需求或证据不清楚时及时询问用户，不自行补全。
- 产品方向由用户决定。参考项目只提供依据，不替代本项目需求；已确认的决定无需反复询问。
- 不确定的问题*必须*询问用户且由用户做出决策；可使用`/grill-me skill`来拷问用户模糊点
- 编辑前说明位置、目的和验证方式。只修改必要内容，保留用户已有改动，不做无关重构、批量格式化或回滚。
- 优先复用现有实现。删除代码前核对调用方和目标用途，不因其来自通用 agent 就认定无用。
- 涉及 API、事件、配置、提示词、权限或 WebUI 时，检查生产者、消费者、状态、测试和文档完整链路。
- Python 使用 `pathlib.Path` 和 `async`/`await`，避免事件循环中的长时间阻塞；文件、输出和命令使用 UTF-8。
- 注释说明重要边界和复杂逻辑；日志覆盖关键成功节点与错误上下文，避免高频噪音、密钥和完整会话。
- 检查输入校验、权限、异常、取消、并发、性能和可观测性。除非用户要求，不兼容旧配置或旧参数。
- 运行最贴近改动的验证；交付说明改动、结果、未执行项及原因、剩余风险。提交信息说明实际变更，不使用无意义的自动提交描述。
- 对此nanoreview项目代码审的查过程中不要考虑极端情况，尽量考虑真实运行环境中可能发生的bug

## 命名与测试

- 参数按职责命名，避免无语义的名称和缩写；review 内部优先使用 `target`、`action`、`query`、`scope`。`review_` 用于跨模块或持久化命名空间，临时私有 metadata 可用 `_review_`。
- 布尔值使用 `is_`、`has_`、`include_`、`enable_`；数量使用 `*_count`、`*_limit`，路径使用 `*_path`、`*_root`，标识符使用 `*_id`，类型使用 `*_type`。稳定外部参数不可只为统一风格改名。
- Python 3.11+，pytest 使用 `asyncio_mode = "auto"`；`tests/` 镜像被测模块。共享行为、跨模块契约或用户可见行为变更应更新最近的测试。

## 约束文档入口

以下路径相对 `.agents/`。`constraints/` 只约束 NanoReview 实现，`plans/` 记录用户确认的目标与实施安排，`references/` 只提供外部事实和历史取舍。

| 文件 | 职责范围 | 边界 |
|---|---|---|
| `README.md` | 文档索引与阅读导航。 | 不替代专题文档。 |
| `constraints/architecture.md` | 当前链路、模块职责、状态与持久化契约。 | 不写待实施架构。 |
| `constraints/security.md` | 工具权限、路径/网络隔离、上下文与存储安全。 | 不决定产品范围。 |
| `constraints/design.md` | 复用、抽象、删除、迁移与配置原则。 | 不写外部笔记或实施清单。 |
| `constraints/budget.md` | 资源限制、上下文压缩、停止原因与 usage。 | 不把未实现配额当作现有能力。 |
| `constraints/debug.md` | 日志路径、标识关联与排查方法。 | 不存单次故障流水或完整会话。 |
| `constraints/gotchas.md` | 平台、编码、配置与迁移易错点。 | 不重复专题契约。 |
| `plans/project-roadmap.md` | 长期方向、阶段、待评估与明确不做。 | 目标不等于已实现。 |
| `plans/code-adjustment-plan.md` | 已确认的当前节点、步骤、验收与进展，可为空。 | 不加入未确认任务。 |
| `mcp-usage.md` | MCP 配置示例与行为边界。 | 不记录产品目标。 |
| `references/nanobot.md` | 对话、session、上下文与 Runner 参考。 | 不定义本项目架构。 |
| `references/open-code-review.md` | 审查流程、LLM loop、预算与 resume 参考。 | 外部能力不等于本项目能力。 |
| `references/kodus-ai.md` | review/对话分流、共享内核与压缩参考。 | 外部方案不等于本项目目标。 |

先读索引，再按影响范围必读具体约束；实施前同时核对短期计划、代码和测试，涉及产品方向时读 roadmap。参考与历史规划按需读取，不作为当前任务或实现依据；历史验证不替代本次验证。

本文件只管协作与文档管理；各文档按职责更新，不跨文件复制要求。代码决定实现事实，用户确认决定目标；冲突须说明并按已确认决定同步计划，不静默改目标。仅调整规则不改实施进展。

## 常用命令

在已启用项目 Python 环境后执行：

```bash
pytest
ruff check nanoreview/
cd review-webui
bun run dev
bun run build
bun run test
```

Gateway：`nanoreview gateway`。Windows/PowerShell 执行命令前设置 UTF-8：

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```
