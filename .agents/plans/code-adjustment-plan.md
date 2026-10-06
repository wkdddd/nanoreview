# 当前代码调整计划

更新时间：2026-10-06

## 当前节点：roadmap 第 4 阶段工具权限与远程 review 边界收敛

状态：待实施。本轮只调整计划文档，生产代码与 WebUI 不在本轮修改范围内。

本计划是第 4 阶段的实施基线。下列目标、字段、优先级、默认值、删除范围和验收条件均已确定，实施时不再引入新的产品决策或兼容行为。

## 已确认目标

- 权限只处理工具调用层，复用 nanobot 的 `restricted` / `full` workspace access 模型。
- Conversation Agent 在每个 turn 开始解析 workspace scope；在 scope 允许的范围内读取、写入文件、执行命令和运行测试，不再逐工具等待确认。
- ReviewLoop、planner、reviewer、Judge 固定使用 `restricted` scope。它们对目标仓库内容只读；已有的 report、session、artifact 内部持久化写入继续保留，并且不通过通用文件或 shell 工具扩大目标仓库权限。
- 删除逐工具 approval、全局和 session approval 开关、permission request/response、permission future 与 approval transcript。Judge 业务结果中的 `needs_confirmation` 保留，它不是工具授权状态。
- 已弃用的配置字段、CLI/API 参数、事件和入口直接删除，不做兼容转换或旧字段回填。
- 删除 GitHub 作为 review 输入及其专属能力：GitHub review/source/target/normalizer/admission 分支、GitHub metadata/evidence、远程 snapshot/cache 和 GitHub review 配置。保留全部通用联网能力：模型 provider 的 HTTP/OAuth 调用、`web_search`、`web_fetch`、stdio/SSE/Streamable HTTP MCP，以及 SSRF、重定向、私网地址和代理校验。
- 第 4–7 阶段只调整后端；WebUI approval 入口和展示残留统一在第 8 阶段清理。第 4–7 阶段后端不再生成或接受 approval 协议事件。


## 固定的 workspace access 契约

### Scope 数据结构

每个 Agent turn 使用一个不可变的 scope 值对象：

```text
workspace_scope = {
    project_path: <绝对且已存在的目录>,
    access_mode: "restricted" | "full",
}
```

- metadata 键名固定为 `workspace_scope`。
- `project_path` 必须是绝对路径并且在解析时已经存在且为目录。
- `access_mode` 的规范值为 `restricted` 和 `full`；兼容 nanobot 的输入别名 `restrict` 与 `full-access`，解析后统一为规范值。
- message metadata 优先于 session metadata，session metadata 优先于全局配置默认值。
- metadata payload、mode、path 任一非法时，整份 scope 回退到全局配置计算出的默认 scope；不得部分采用非法 payload 中的字段，也不得等待用户确认。
- `ToolsConfig.restrict_to_workspace=False` 是当前配置默认值，因此默认 `access_mode` 固定为 `full`；显式设置为 `True` 时默认 `access_mode` 为 `restricted`。
- scope 在 turn 开始解析并绑定到本轮 `ToolContext` / `RequestContext`；同一 turn 的内部迭代使用同一快照，不能被工具调用改变。

### Root 来源

- Conversation Agent：先读取 session metadata 的 `ReviewMetaKey.LOCAL_ROOT`（持久化键 `review_local_root`）；该值无效时使用 `agents.defaults.workspace`。应用 workspace 仅用于 session、report、artifact 等持久化，不作为目标仓库 root 的替代值。
- ReviewLoop、planner、reviewer、Judge：优先读取 review metadata 的 `repository_root` 或 `review_local_root`；该值无效时使用已通过本地准入的目标仓库 root。
- Review Agent 的 restricted scope 只保护目标仓库工具访问；写入 report、session、artifact 的现有领域持久化路径继续有效，不能借此写入目标仓库任意文件。

### 两种模式的边界

- `restricted`：filesystem、shell、message 等通用工具的路径和 cwd 必须位于 `project_path` 内；越界读、写和命令执行都拒绝。
- `full`：允许工具访问 `project_path` 外的路径和 cwd，但仍执行工具自身的路径校验、shell deny/allow pattern、内部/私有 URL guard 和操作系统 sandbox；`full` 不绕过这些限制，也不恢复 approval。
- Review Agent 的工具注册表继续与 Conversation Agent 分离。共享工具实现不改变上述角色边界。

## 实施步骤

### 1. 建立影响面清单（只读）

先核对下列生产入口、消费者、事件和测试，形成实施前清单；此步骤不修改代码：

- 权限链路：`nanoreview/config/schema.py`、`nanoreview/agent/tools/permissions.py`、`nanoreview/agent/tools/filesystem.py`、`nanoreview/agent/tools/shell.py`、`nanoreview/agent/tools/message.py`、`nanoreview/agent/runner.py`、`nanoreview/agent/conversation_loop.py`、`nanoreview/agent/coordinator.py`、`nanoreview/agent/subagent.py`、`nanoreview/channels/websocket.py`。
- 本地权限 root：`nanoreview/agent/context.py`、`nanoreview/agent/tools/*`、`nanoreview/review/types.py`、`nanoreview/review/admission.py`、`nanoreview/agent/review_loop.py`、`nanoreview/review/planning/planner.py`、`nanoreview/review/profiles.py`。
- GitHub/远程 review：`nanoreview/agent/tools/github_review.py`、`nanoreview/agent/tools/review_base.py`、`nanoreview/review/source/github.py`、`nanoreview/review/source/utils.py`、`nanoreview/review/input/targets.py`、`nanoreview/review/input/normalizers.py`、`nanoreview/review/admission.py`、`nanoreview/review/planning/prompt.py`、`nanoreview/review/planning/evidence.py`、`nanoreview/review/planning/preprocessor.py`、`nanoreview/review/profiles.py`、`nanoreview/rag/review_service.py`。
- OAuth/provider（保留并验证）：`nanoreview/providers/openai_codex_provider.py`、`nanoreview/providers/factory.py`、`nanoreview/providers/registry.py`、`nanoreview/cli/commands.py` 及 provider 配置 schema。
- 网络与 MCP（保留并验证）：`nanoreview/security/network.py`、`nanoreview/config/loader.py`、`nanoreview/agent/tools/mcp.py`、MCP 配置 schema、MCP 装配和 CLI/API schema；同时核对 `web_search`/`web_fetch` 的实际注册、提示和测试入口。
- 测试：`tests/agent/tools/test_permissions.py`、`tests/agent/tools/test_repo_review_github.py`、`tests/agent/test_mcp_integration.py`、`tests/agent/tools/test_mcp_smoke.py`、`tests/agent/tools/test_mcp_tool.py`、`tests/config/test_mcp_config.py`、`tests/agent/test_loop_modes.py`、`tests/agent/test_review_gate.py`、`tests/security/test_network_guards.py` 以及 OAuth、WebSocket/API 相关测试。

### 2. 实现 workspace access

- 在安全/工具上下文层实现上述 `workspace_scope` 解析、优先级、别名、默认值和非法输入回退。
- 在 Conversation turn 开始绑定 scope；filesystem、shell、message 和测试执行工具读取当前 scope。
- ReviewLoop、planner、reviewer、Judge 创建 scope 时直接固定为目标仓库 root + `restricted`，不得接受 conversation metadata 对其放宽。
- 保留现有路径 guard、命令 guard、内部 URL guard 和 OS sandbox；只替换原先以 `restrict_to_workspace` 为中心的统一判定入口。
- 删除或改写仅依赖旧布尔值的调用方，避免同一 turn 同时存在旧布尔值和新 scope 两套权限事实。

### 3. 移除 approval 执行链路

- 从 `AgentRunSpec`、Runner、ConversationLoop、Coordinator 和 subagent 装配中删除 permission policy、requester、callback、future、响应等待和 approval transcript。
- 删除 `nanoreview/agent/tools/permissions.py` 的运行时调用方以及工具上的 `requires_approval()`；清理 `ToolsConfig.approval_enabled`、session approval 状态和相关 CLI/API 参数。
- 删除 WebSocket 后端的 permission request/response、session approval 读写和 approval transcript 生产。第 4–7 阶段不改 `review-webui/`；前端旧入口在此期间没有有效后端协议，统一到第 8 阶段删除。
- 保留 pairing、普通业务确认、控制命令和 Judge 的 `needs_confirmation`；这些语义不转换为工具 approval。

### 4. 删除 GitHub 远程 review 接入，保留联网能力

- 删除 `github_review` 工具及其注册、提示、技能说明、denied list、trace/evidence 记录和测试。
- 删除 GitHub 专属 source、target 类型、normalizer 分支、admission 分支、metadata/evidence、远程 snapshot/cache 和 GitHub 配置字段。保留本地 admission 的仓库存在性、路径校验、归一化、快照、session 注册和错误处理流程；本地 target 仍是唯一审查输入。
- 保留 `openai_codex_provider`、token/login/logout 入口、provider 注册、OAuth 依赖及所有已支持 provider 的 HTTP API 调用；仅删除实际绑定 GitHub 远程 review 的 OAuth/config 字段（如代码核对确认存在）。
- 保留 `web_search`、`web_fetch` 的工具实现、注册、运行时提示、技能说明、compactable/hint 注册和测试；它们可读取普通网页和代码托管文档，但不得把 URL 转换为 review target 或恢复远程仓库 review。
- `MCPServerConfig` 与 MCP provider 继续支持 `type="stdio"`、`type="sse"`、`type="streamableHttp"`，以及现有 `url`、`headers`、`auth` 等网络鉴权字段和实现；继续应用 SSRF、重定向、私网地址和代理校验。无 MCP 配置时保持关闭。
- 删除 `ToolsConfig.github_repo` 等 GitHub 专属配置字段；保留 `ToolsConfig.ssrf_whitelist` 及其 loader 接线。保留 `nanoreview/security/network.py` 中所有联网场景使用的 URL guard；普通模型 provider 的 HTTP 连接不经过 Agent 网络工具注册表。
- 同步收敛工具 loader、registry、CLI/API schema、服务装配、日志事件、skills 和生产文档；不保留失效兼容门面。

### 5. 同步测试、约束和文档

- 删除或改写仅涉及 GitHub 远程 review 的旧测试，不保留已删除入口的兼容断言；保留并增强 OAuth/provider、`web_search`/`web_fetch`、stdio/SSE/Streamable HTTP MCP 及 network guard 测试。
- 新增或保留 workspace access 契约测试：默认 full、显式 restricted、message/session/config 优先级、别名、非法 payload 回退、restricted 越界拒绝、full 受既有 guard 约束、Review 固定 restricted、内部 report/session/artifact 写入继续有效。
- 新增 approval 删除测试：工具执行不创建或等待 future，不发送 permission request/response；Judge 仍返回业务 `needs_confirmation`。
- 新增本地-only review 与联网能力边界测试；验证 GitHub target/source/tool/cache/metadata/evidence 无有效入口，同时 OAuth/provider、web 工具及三种 MCP transport 继续可用并受 network guard 约束。
- 更新 `.agents/constraints/security.md`、`.agents/constraints/architecture.md`、`.agents/mcp-usage.md`、`nanoreview/skills/github/SKILL.md`、`nanoreview/skills/repo-reader/SKILL.md`、`nanoreview/skills/rag/SKILL.md` 及相关生产文档，明确普通联网资料可访问但不能成为远程 review target；本计划记录实施结果和验收结果。

## 受影响文件清单

### 生产代码

```text
nanoreview/config/schema.py
nanoreview/config/loader.py
nanoreview/agent/context.py
nanoreview/agent/runner.py
nanoreview/agent/conversation_loop.py
nanoreview/agent/coordinator.py
nanoreview/agent/subagent.py
nanoreview/agent/review_loop.py
nanoreview/agent/tools/permissions.py
nanoreview/agent/tools/filesystem.py
nanoreview/agent/tools/shell.py
nanoreview/agent/tools/message.py
nanoreview/agent/tools/mcp.py
nanoreview/agent/tools/github_review.py
nanoreview/agent/tools/review_base.py
nanoreview/review/types.py
nanoreview/review/admission.py
nanoreview/review/source/github.py
nanoreview/review/source/utils.py
nanoreview/review/input/targets.py
nanoreview/review/input/normalizers.py
nanoreview/review/planning/prompt.py
nanoreview/review/planning/evidence.py
nanoreview/review/planning/preprocessor.py
nanoreview/review/profiles.py
nanoreview/rag/review_service.py
nanoreview/providers/openai_codex_provider.py   # 保留 provider/OAuth，仅核对 GitHub review 绑定
nanoreview/providers/factory.py                 # 保留并验证
nanoreview/providers/registry.py                # 保留并验证
nanoreview/channels/websocket.py
nanoreview/cli/commands.py
nanoreview/security/network.py                  # 保留并验证所有 network guard
```

### 测试与文档

```text
tests/agent/tools/test_permissions.py
tests/agent/tools/test_repo_review_github.py
tests/agent/test_mcp_integration.py
tests/agent/tools/test_mcp_smoke.py
tests/agent/tools/test_mcp_tool.py
tests/config/test_mcp_config.py
tests/agent/test_loop_modes.py
tests/agent/test_review_gate.py
tests/security/test_network_guards.py
tests/agent/test_coordinator.py
tests/agent/test_conversation_loop.py
tests/agent/test_review_loop.py
tests/review/test_admission.py
tests/review/test_policy.py
.agents/constraints/security.md
.agents/constraints/architecture.md
.agents/mcp-usage.md
nanoreview/skills/github/SKILL.md
nanoreview/skills/repo-reader/SKILL.md
nanoreview/skills/rag/SKILL.md
```

### WebUI（第 8 阶段，仅记录，不在本阶段修改）

```text
review-webui/
```

以上清单是本阶段的固定范围；实施时不得新增范围外文件。若核对后某个列出的文件没有对应入口，只能从清单中删除该文件并在实施记录中说明原因，不得扩展到未列出的功能。

## 验收矩阵

| 场景 | 固定输入 | 必须结果 |
|---|---|---|
| 默认 Conversation scope | `restrict_to_workspace=False`，无 message/session override | `access_mode=full`，root 为有效 `review_local_root`，无效时为 `agents.defaults.workspace` |
| 显式 restricted | message metadata 提供合法 `workspace_scope` | 只能访问 `project_path` 内路径；越界读、写、cwd 和测试命令拒绝 |
| 显式 full | message/session metadata 提供 `full` 或别名 `full-access` | 可访问 workspace 外路径，但仍受路径 guard、shell guard、内部 URL guard 和 OS sandbox 约束 |
| 非法 scope | 缺字段、非法 mode、相对路径、非目录或不存在路径 | 整体回退到配置默认 scope，不等待确认、不部分采用 payload |
| Review Agent | `repository_root`/`review_local_root` 有效或回退到本地 target root | 目标仓库工具访问固定 restricted；目标仓库写入/修改命令拒绝；report/session/artifact 内部持久化成功 |
| Approval | 任意 Conversation 工具调用 | 不创建、不等待 approval future，不产生 permission request/response；工具直接按 scope 执行 |
| Judge 业务确认 | Judge 返回 `needs_confirmation` | 字段和值语义保留，不触发工具 approval |
| Review 输入 | 本地仓库 target | admission、归一化、快照、注册和 review 流程成功 |
| 远程输入 | GitHub URL、GitHub target 或远程 review 参数 | 作为 review 输入被拒绝；无 GitHub source/tool/cache/metadata/evidence 有效入口；普通网页或代码托管文档 URL 可由 `web_fetch` 读取，但不转为 review target |
| MCP | `type="stdio"`、`type="sse"`、`type="streamableHttp"` 配置 | 三种 transport 均按既有字段连接；`url`、`headers`、`auth` 继续解析并受 SSRF、重定向、私网和代理 guard 约束 |
| 模型调用 | 任一已支持 provider（含 OAuth） | provider 所需 HTTP API 调用继续工作，不注册为 Agent 网络工具 |
| 通用联网工具 | `web_search`、`web_fetch` | 工具入口、提示和测试继续有效；可访问普通网页资料，不创建远程 review target |
| WebUI 范围 | 第 4–7 阶段 | `review-webui/` 不修改；后端不生成或接受 approval 协议事件；第 8 阶段再删除前端残留并做闭环验收 |

## 验证命令

实施完成后按以下顺序执行：

```powershell
git diff --check
pytest <受影响测试文件>
pytest
ruff check nanoreview/
rg -n "approval_enabled|permission_request|permission_response|requires_approval|github_review|github_repo|review_github|target_type.*github|remote.*review" nanoreview tests
```

残留检查必须人工区分允许项与删除项：`needs_confirmation`、pairing approval、`contains_internal_url`、OAuth/provider login/logout、`web_search`/`web_fetch`、HTTP MCP transport、network guard 和 provider HTTP client 均应保留；仅 GitHub source/tool/target 分支、远程 review 参数、远程 snapshot/cache、GitHub metadata/evidence 及 approval request/response/future 不得保留有效生产入口。

## 明确不做

- 不新增逐工具确认、风险分级授权、approval 兼容层或新的用户确认流程。
- 不把普通模型 provider 的 HTTP API 当作 Agent 可调用的通用网络工具，也不删除模型调用所需的 HTTP/OAuth 能力。
- 不删除 `web_search`、`web_fetch` 或任何已支持的 MCP transport；继续保留 `nanoreview/security/network.py`、SSRF whitelist、重定向、私网和代理 guard。
- 不把普通网页或代码托管文档访问扩展为远程仓库 review；GitHub URL 只可作为普通网页资料输入（如适用），不能成为 review target。
- 不修改 `review-webui/`，不在第 4–7 阶段修复前端 approval 残留。
- 不修改审查策略、上下文压缩、报告交接契约、finding 语义或 `/stop` 回滚行为。
- 不自动创建 worktree，不恢复远程 review，不保留已删除入口的兼容门面。

## 当前决策状态

当前无待确认产品决策。第 4 阶段可以按本计划直接实施；实施过程中仅记录代码事实、测试结果和超出本计划范围的阻塞，不自行改变产品范围。
