# Security

## 工具权限

- 文件路径经现有 path 工具解析，启用 workspace 限制时必须检查边界；额外 root 按用途授予只读、可写或精确文件权限。
- 审查与修复的工具权限按调用角色配置，不能因复用 Runner 而扩大 reviewer/Judge 权限。
- `restrict_to_workspace` 是应用层防护，不替代操作系统或容器隔离；执行隔离复用 `agent/tools/sandbox.py`。

## 网络

- Agent 工具发起 HTTP/SSE 请求时使用 `validate_url_target`；重定向使用 `validate_resolved_url` 再校验。
- 默认阻止 loopback、private、link-local、CGNAT 和云 metadata 地址；私有端点通过 `tools.ssrf_whitelist` 显式配置。
- DNS 解析是阻塞调用，须经 `asyncio.to_thread` 移出事件循环。
- DNS pin 改的是进程全局 `socket.getaddrinfo`，互斥必须用线程级锁并按嵌套计数，只有最外层恢复原解析器；每事件循环各持一把 `asyncio.Lock`（弱引用键），不可用单个模块级 `asyncio.Lock` —— 它会绑定到第一个 await 它的 loop，之后换 loop 直接抛 `bound to a different event loop`。回归见 `tests/security/test_network_guards.py`。
- 环境代理（`HTTP_PROXY`/`HTTPS_PROXY`）与 SSRF 白名单必须一致：被 `tools.ssrf_whitelist` 放行的 loopback 目标要同时豁免代理，否则白名单只对「探测/首个请求」生效，后续请求仍被送进代理。症状是本地 MCP 服务器的 SSE 消息端点被代理回 `404`，只有 `/sse` 流连得上。`httpx_env_proxy_mounts` 与 `env_proxy_applies_to_url` 必须用同一套豁免规则（httpx mount 只按 host 匹配，不接受 CIDR）。

## MCP

- MCP 的 HTTP/SSE 请求逐次校验，包括重定向目标和 SSE 后续请求；`PinnedDNSAsyncTransport` 在校验与连接之间固定 DNS 结果，防止校验与使用分属不同解析。私有端点同样需要 `tools.ssrf_whitelist`，本地 HTTP MCP 服务器必须显式放行。
- `mcpServers.headers` 常含凭据。日志与错误只输出脱敏 URL（`_redact_url` 去掉凭据、query 与 path），不记录完整 URL、header 值和工具返回内容；工具内容按 `RequestContext.log_content` 控制。
- 配置了 OAuth（`auth: oauth`）时明确报错并拒绝连接，不静默忽略后按无鉴权连接。
- MCP 工具**不触发逐工具 approval 确认**，即使 `approval_enabled=true`。MCP 外部服务也不受本地文件工具的 workspace guard 或 shell sandbox 约束，访问范围须在服务启动参数及其自身权限中限制。
- 沿用上游自动重试：瞬时故障会重试一次，**可能重复执行写入操作**。不提供恰好执行一次的保证，也不回滚已发生的操作。
- 连接在进程内共享，因此服务端会话状态也共享；`cwd` 取自 MCP 配置，不随 Conversation 的目标仓库切换。

## 持久化与上下文

- 仓库内容、报告、工具结果和历史均可能被重放；按来源处理为不可信内容，限制规模并过滤秘密与内部标记。
- 保留原子持久化和并发写边界；状态、历史、报告各有明确用途，展示历史不等于恢复执行。
- 日志、transcript 和 trace 使用 `sanitize_persisted_log_text` 并设单条/总量上限。删除 session 时清理其关联数据，先验证归属。
- 当前 sidecar 位于 `utils/webui_transcript.py`、`utils/subagent_trace.py`；是否迁移或替换按目标计划与消费者核对，不以旧存储形式限制新设计。
