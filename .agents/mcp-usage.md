# MCP 使用示例

MCP 实现自本机 nanobot `432421bc` 提取适配，仅 Conversation Agent 使用。审查侧（planner / reviewer / Judge）不可见。

配置修改后重启生效。配置结构见 `nanoreview/config/schema.py` 的 `MCPServerConfig`，字段使用 camelCase（`toolTimeout`、`enabledTools`），也接受 snake_case。

## 三种 transport

stdio —— 本地进程：

```json
{
  "tools": {
    "mcpServers": {
      "filesystem": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "env": { "LOG_LEVEL": "info" },
        "cwd": "C:/work/repo"
      }
    }
  }
}
```

Windows 上 `npx` / `npm` / `pnpm` / `yarn` / `bunx` 以及 `.cmd` / `.bat` 会自动经 `COMSPEC /d /c` 启动，无需自己拼 `cmd /c`。

SSE —— 独立 HTTP 端点：

```json
{
  "tools": {
    "mcpServers": {
      "remote": {
        "type": "sse",
        "url": "https://mcp.example.com/sse",
        "headers": { "Authorization": "Bearer ${MCP_TOKEN}" },
        "toolTimeout": 60
      }
    }
  }
}
```

Streamable HTTP：

```json
{
  "tools": {
    "mcpServers": {
      "remote": {
        "type": "streamableHttp",
        "url": "https://mcp.example.com/mcp",
        "headers": { "Authorization": "Bearer ${MCP_TOKEN}" }
      }
    }
  }
}
```

`${VAR}` 在 `command`、`args`、`url`、`cwd`、`env` 和 `headers` 中解析；缺失变量会明确报错。`env` 与 `headers` 的字典键保持原样，不参与变量名解析。

## headers 鉴权

`headers` 用于静态鉴权。`auth: "oauth"` 本轮未实现，配置后连接会明确报错而不会静默按无鉴权连接。

凭据不要写进配置文件本体 —— 用 `${MCP_TOKEN}` 之类的环境变量引用，日志中的 URL 也会做脱敏（去掉凭据、query 和 path）。

## enabledTools

沿用上游语义：

| 值 | 效果 |
|---|---|
| `["*"]`（默认） | 开放全部能力 |
| `[]` | 禁用全部能力 |
| `["alpha", "mcp_docs_beta"]` | 仅列出的工具；此时不注册 resources/prompts |

## 本地 HTTP MCP 需放行 SSRF

MCP 的 HTTP/SSE 请求逐次做 SSRF 校验（含重定向和 SSE 后续请求），默认阻止 loopback。本地 MCP 服务器必须显式放行：

```json
{
  "tools": {
    "ssrfWhitelist": ["127.0.0.0/8"],
    "mcpServers": {
      "local": { "type": "streamableHttp", "url": "http://127.0.0.1:8931/mcp" }
    }
  }
}
```

## 图片结果

MCP 工具返回的图片内容复用本项目的 artifact 存储：落盘后只把路径交给模型，base64 不进入模型历史。artifact 的 `provider` 记为 `mcp:<服务器名>`。

## 行为边界

- **不触发逐工具 approval 确认**，即使 `approval_enabled=true`。
- 沿用上游自动重试，瞬时故障会重试一次，**可能重复执行写入操作**；不保证恰好执行一次，也不回滚已发生的操作。
- MCP 外部服务不受本地文件工具的 workspace guard 或 shell sandbox 约束，需在服务启动参数及其自身权限中限制访问范围。
- 连接进程内共享，服务端会话状态也共享；`cwd` 取自 MCP 配置，不随 Conversation 的目标仓库切换。
- 未连接的服务器在下一轮对话重试；单个服务连接准备限时 30 秒，失败不影响该轮对话。
