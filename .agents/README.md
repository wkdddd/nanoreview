# Agent Reference Index

根目录 `AGENTS.md` 是本仓库 AI 编码代理的首要约束。本目录将高风险或高上下文主题拆分，避免根文件膨胀：

- `architecture.md`：模块职责、消息链路和变更影响范围。
- `design.md`：架构边界、最小改动和抽象原则。
- `security.md`：文件系统、网络、执行和持久上下文安全边界。
- `debug.md`：运行日志类型、路径、关联方式和调试排查约束。
- `gotchas.md`：Windows、配置变量、模板、格式化与上下文回放陷阱。
- `reference-summary.md`：本地参考项目的入口索引、摘要格式、增量刷新和低 Token 筛查流程。

任务涉及对应主题时，先阅读相关文件再修改。若本目录与根 `AGENTS.md` 冲突，以根文件为准，并在同一次改动中消除冲突。
