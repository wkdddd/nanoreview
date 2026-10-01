# Security

## 工具权限

- 文件路径经现有 path 工具解析，启用 workspace 限制时必须检查边界；额外 root 按用途授予只读、可写或精确文件权限。
- 审查与修复的工具权限按调用角色配置，不能因复用 Runner 而扩大 reviewer/Judge 权限。
- `restrict_to_workspace` 是应用层防护，不替代操作系统或容器隔离；执行隔离复用 `agent/tools/sandbox.py`。

## 网络

- Agent 工具发起 HTTP/SSE 请求时使用 `validate_url_target`；重定向使用 `validate_resolved_url` 再校验。
- 默认阻止 loopback、private、link-local、CGNAT 和云 metadata 地址；私有端点通过 `tools.ssrf_whitelist` 显式配置。

## 持久化与上下文

- 仓库内容、报告、工具结果和历史均可能被重放；按来源处理为不可信内容，限制规模并过滤秘密与内部标记。
- 保留原子持久化和并发写边界；状态、历史、报告各有明确用途，展示历史不等于恢复执行。
- 日志、transcript 和 trace 使用 `sanitize_persisted_log_text` 并设单条/总量上限。删除 session 时清理其关联数据，先验证归属。
- 当前 sidecar 位于 `utils/webui_transcript.py`、`utils/subagent_trace.py`；是否迁移或替换按目标计划与消费者核对，不以旧存储形式限制新设计。
