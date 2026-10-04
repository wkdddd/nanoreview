# 文档索引

根 `AGENTS.md` 规定协作方式；本目录按信息用途分层，按任务读取，不默认全量加载。

## 约束

`constraints/` 记录稳定模块边界与排查入口：

- [architecture.md](constraints/architecture.md)：当前链路与职责。
- [security.md](constraints/security.md)：工具、网络与持久上下文。
- [design.md](constraints/design.md)：复用、抽象与变更边界。
- [budget.md](constraints/budget.md)：预算、上下文治理与 usage。
- [debug.md](constraints/debug.md)：日志路径与关联方式。
- [gotchas.md](constraints/gotchas.md)：平台、配置与迁移陷阱。

## 规划

- [project-roadmap.md](plans/project-roadmap.md)：长期产品目标、架构原则、待评估方向和明确不做；确定产品方向时先读。
- [code-adjustment-plan.md](plans/code-adjustment-plan.md)：当前已选定的具体代码调整节点；没有确认节点时保持为空。


新目标不等于当前实现。进展须核对 HEAD/工作区，候选分支单独记录；历史测试结果不得冒充本次验证。需求冲突先按已确认的新目标解释，未确认处询问用户。

### 历史规划参考

以下文档从旧 `.claude/plans/` 恢复，专门用于 review workflow 调整时回顾既有设计、实施顺序和已验证的取舍：

- [agent-runner-run-level-compression.md](agent-runner-run-level-compression.md)：`AgentRunner` run-level compression 的历史设计与验收细节。

这些文档是调整 review workflow 的参考资料，不是当前产品目标、代码事实或待执行任务的权威来源。当前方向以 [project-roadmap.md](plans/project-roadmap.md) 为准，当前实施任务以 [code-adjustment-plan.md](plans/code-adjustment-plan.md) 为准；实现状态仍须以代码、测试和当前约束文件核对。

## 参考

| 项目摘要 | 参考用途 | 已核查 commit |
|---|---|---|
| [nanobot](references/nanobot.md) | 对话、session、上下文与 Runner | `432421bc` |
| [open-code-review](references/open-code-review.md) | 审查流程、LLM loop、预算与显式 resume | `e95bdda` |
| [kodus-ai](references/kodus-ai.md) | 审查与对话分流、共享执行内核 | `1df08e5` |

按问题选择项目摘要，再定位相关源码；不默认加载全部摘要或仓库。路径、核查范围与日期见各文件。参考文件只记录外部事实和必要差异，不定义产品目标；本项目职责见约束，候选分支取舍见计划。

参考更新与历史笔记保留规则见根 `AGENTS.md` 的“约束文档入口”。核查时仅重读变化文件及直接消费者；无 Git 时使用内容 hash。摘要保留路径、版本、范围、入口、行为/契约、差异和验证局限，不复制密钥、会话或无关源码。
