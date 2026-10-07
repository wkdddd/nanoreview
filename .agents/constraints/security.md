# Security

## 工具权限

权限只在工具调用层处理，不做逐工具 approval、不做用户确认回环；每个 turn 由 workspace scope 一次性决定访问范围。

- 文件路径经现有 path 工具解析，启用 workspace 限制时必须检查边界；额外 root 按用途授予只读、可写或精确文件权限。
- Conversation Agent 每 turn 解析一个 `WorkspaceScope`（`agent/tools/workspace_scope.py`，`access_mode` 为 `restricted`/`full`），来源优先级 message metadata → session metadata → 全局配置，非法 payload 整体回退到配置默认值；解析后 turn 内不可变，不逐工具等待确认。
- turn 内每次重建工具调用上下文都必须沿用同一 scope。每批工具前会刷新 request context（让中途注册的工具，如重连的 MCP，拿到上下文），刷新只带 channel/chat/metadata，**scope 必须由调用方显式补回**；漏传会把 scope 重置为 `None`，后续 filesystem/shell/message 调用随即回退到构造时的 `restrict_to_workspace`（默认 `full`），使 `restricted` 在第一批工具后失效。
- 带 `workspace_scope` 键但 payload 非法的层级会使整条解析链终止并回退配置默认，不再向下一层取用；只有该层**未声明**该键时才继续向下。非法声明不得继承更低层的更宽 scope。
- 默认 `ToolsConfig.restrict_to_workspace=False` 即 `full`；`True` 即 `restricted`。别名 `restrict`/`full-access` 归一化为规范值。
- ReviewLoop、planner、reviewer、Judge 固定使用 `restricted` scope（`review_workspace_scope()`），不接受会话 metadata 放宽；审查与修复的工具权限按调用角色配置，不能因复用 Runner 而扩大 reviewer/Judge 权限。reviewer 的 scope 由 subagent hook 写入工具调用上下文，不能只依赖 `ToolsConfig.restrict_to_workspace`（会话默认 `full` 时 reviewer 仍须只读目标仓库）。
- `restrict_to_workspace` 是应用层防护，不替代操作系统或容器隔离；执行隔离复用 `agent/tools/sandbox.py`。

## 网络

- Agent 工具发起 HTTP/SSE 请求时使用 `validate_url_target`；重定向使用 `validate_resolved_url` 再校验。
- 默认阻止 loopback、private、link-local、CGNAT 和云 metadata 地址；私有端点通过 `tools.ssrf_whitelist` 显式配置。
- DNS 解析是阻塞调用，须经 `asyncio.to_thread` 移出事件循环。
- DNS pin 改的是进程全局 `socket.getaddrinfo`，互斥必须用线程级锁并按嵌套计数，只有最外层恢复原解析器；每事件循环各持一把 `asyncio.Lock`（弱引用键），不可用单个模块级 `asyncio.Lock` —— 它会绑定到第一个 await 它的 loop，之后换 loop 直接抛 `bound to a different event loop`。回归见 `tests/security/test_network_guards.py`。
- 环境代理（`HTTP_PROXY`/`HTTPS_PROXY`）与 SSRF 白名单必须一致：被 `tools.ssrf_whitelist` 放行的 loopback 目标要同时豁免代理，否则白名单只对「探测/首个请求」生效，后续请求仍被送进代理。症状是本地 MCP 服务器的 SSE 消息端点被代理回 `404`，只有 `/sse` 流连得上。`httpx_env_proxy_mounts` 与 `env_proxy_applies_to_url` 必须用同一套豁免规则（httpx mount 只按 host 匹配，不接受 CIDR）。

## Review 输入边界

- review 唯一输入是本地 diff：admission 只接受 `auto`/`local` target type，`github` 等值按 `invalid_target_type` 拒绝；GitHub URL 不再特判，退化为普通本地路径校验。action 只保留 `diff`，显式 `repo` 按 `invalid_action` 拒绝；无 Git 仓库 / diff 读取失败 / 空 diff 分别映射为 `not_a_git_repo` / `diff_unavailable` / `empty_diff`，在准入阶段失败且不启动任何模型角色。
- **`local_review`（仓库级 reader/evidence wrapper）已整体删除**：工具、`ReviewToolBase`、`LocalRepoReader`、`skills/rag` 及其专属测试全部移除，`ConversationLoop._CONVERSATION_DENIED_TOOLS` 随之取消。Review 流程的模型角色因此天然不含它：Planner/coordinator 只注册 `submit_review_plan`，四类 reviewer 只有 `grep`/`list_dir`/`read_file`/`review_submit`，Judge 只注册 verdict 工具；授权证据由 ReviewLoop 内部依赖 `ReviewEvidenceService` 直接注入 frozen task，不依赖工具 wrapper 注册。residual 扫描须确认 review coordinator/reviewer prompt 与 task 不再建议模型调用任何仓库 reader 工具。
- `ReviewAction.REPO` 仅作为被拒 action 的规范名保留（错误信息与历史 metadata 识别），任何入口都不再产生它；本节点不恢复 repo review 入口。
- 不提供 GitHub source、`github_review` 工具、远程 snapshot/cache、GitHub metadata/evidence 或远程 diff 校验入口；`ReviewPlan` 无 `target_repo`/`pr_number`/`target_ref`/`target_subpath` 字段，`ReviewMetaKey` 无 `TARGET_REF`/`GITHUB_PREFETCH_READY`/`GITHUB_PR_HEAD_REF`。
- 通用联网能力不受影响并须保留：模型 provider HTTP/OAuth（含 `github-copilot`）、`web_search`、`web_fetch`、stdio/SSE/Streamable HTTP MCP，以及 SSRF/重定向/私网/代理校验；`_GITHUB_TOKEN` 日志脱敏、`.github/workflows` 忽略路径、`HTTP-Referer` 常量与 OAuth provider 同属非 review 能力，不随本次收敛删除。
- reviewer 的重复读取抑制是上下文效率控制，不改变只读权限；reviewer 仍可在 `restricted` scope 内用定向 `read_file`/`grep` 读取 diff 外文件作上下文，但不得据此扩大 accepted finding 的 changed-file 边界。
- **审查内容冻结在准入快照**：prefetch 用准入时采集的净 diff（snapshot 的 `net_diff`），不重读活工作树；changed-file 边界也取自 snapshot（未修改文件的 finding 判 `uncertain`）。因此准入后对工作树的任意修改/提交/回滚都不能改变被审内容或放宽边界。净 diff 统一为 `HEAD -> worktree`，不暴露 staged 中间态。
- 回归见 `tests/agent/tools/test_stage4_boundaries.py`、`tests/agent/tools/test_reviewer_read_dedup.py`、`tests/agent/test_review_loop.py::test_changed_file_boundary_comes_from_the_admitted_snapshot`、`tests/agent/rag/test_rag_review.py::test_local_changed_patches_uses_net_head_diff_not_staged_concat`。

## MCP

- MCP 的 HTTP/SSE 请求逐次校验，包括重定向目标和 SSE 后续请求；`PinnedDNSAsyncTransport` 在校验与连接之间固定 DNS 结果，防止校验与使用分属不同解析。私有端点同样需要 `tools.ssrf_whitelist`，本地 HTTP MCP 服务器必须显式放行。
- `mcpServers.headers` 常含凭据。日志与错误只输出脱敏 URL（`_redact_url` 去掉凭据、query 与 path），不记录完整 URL、header 值和工具返回内容；工具内容按 `RequestContext.log_content` 控制。
- 配置了 OAuth（`auth: oauth`）时明确报错并拒绝连接，不静默忽略后按无鉴权连接。
- MCP 外部服务不受本地文件工具的 workspace scope 或 shell sandbox 约束，访问范围须在服务启动参数及其自身权限中限制。
- 沿用上游自动重试：瞬时故障会重试一次，**可能重复执行写入操作**。不提供恰好执行一次的保证，也不回滚已发生的操作。
- 连接在进程内共享，因此服务端会话状态也共享；`cwd` 取自 MCP 配置，不随 Conversation 的目标仓库切换。

## 持久化与上下文

- 仓库内容、报告、工具结果和历史均可能被重放；按来源处理为不可信内容，限制规模并过滤秘密与内部标记。
- 保留原子持久化和并发写边界；状态、历史、报告各有明确用途，展示历史不等于恢复执行。
- 日志、transcript 和 trace 使用 `sanitize_persisted_log_text` 并设单条/总量上限。删除 session 时清理其关联数据，先验证归属。
- 当前 sidecar 位于 `utils/webui_transcript.py`、`utils/subagent_trace.py`；是否迁移或替换按目标计划与消费者核对，不以旧存储形式限制新设计。
