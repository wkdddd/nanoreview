# Agent.md

本文件为在本仓库工作的 AI Agent 提供约定。所有输出、文件写入、命令执行和字符串内容必须统一使用 UTF-8 编码，不能使用 GBK/GB2312。

## 项目概览

nanobot 是一个轻量级个人code-review Agent，主体为 Python 项目，并包含 React/TypeScript WebUI。核心流程是：渠道接收消息，主Agent 构建上下文并调用 LLM Provider，spawn所需的用于codereview的subagent，然后把响应发回对应渠道。

## 常用命令

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest
pytest tests/test_xxx.py -v

# 代码检查
ruff check nanobot/

# 只格式化自己改过的文件
ruff format <changed-files>

# WebUI 开发与构建
cd review-webui && bun run dev
cd review-webui && bun run build

# 启动 gateway
nanobot gateway
```

在 Windows/PowerShell 中执行命令时，先设置 UTF-8：

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```

## 目录与职责

- `nanobot/agent/`：Agent 核心循环、Runner、Subagent、Hooks 和 Review 流程。
- `nanobot/channels/`：外部平台渠道接入。
- `nanobot/providers/`：LLM Provider 实现、注册和工厂。
- `nanobot/agent/tools/`：Agent 可调用工具。
- `nanobot/session/`：会话、上下文压缩、目标状态。
- `nanobot/config/`：Pydantic 配置模型与加载逻辑。
- `nanobot/templates/`：运行时 Prompt 模板，改动影响 Agent 行为。
- `nanobot/skills/`：内置技能定义。
- `review-webui/`：Vite + React + Tailwind WebUI。
- `tests/`：测试代码。

## 开发原则

- 优先阅读现有代码和约定，再修改。
- 保持核心小而清晰，新能力优先放在 `channels/`、`tools/`、`skills/`、Provider 或 MCP 扩展中。
- 对 `nanobot/agent/loop.py`、`nanobot/agent/runner.py` 的改动要谨慎、聚焦，并说明原因。
- 不做无关重构，不批量格式化整个仓库。
- Prompt 模板按代码对待：改动要窄，必要时加回归测试。
- Python 代码使用 `pathlib.Path` 处理路径，保持 Windows 兼容。
- 异步路径优先使用 async/await，不在事件循环中做长时间阻塞操作。

## 测试与质量

- Python 目标版本为 3.11+。
- Ruff 行宽为 100，规则见 `pyproject.toml`。
- pytest 使用 `asyncio_mode = "auto"`。
- 测试应尽量贴近被改动模块；共享行为或用户可见行为变更要补充覆盖。
- 修改前后都注意已有工作区变更，不要回滚他人或用户的改动。

## 安全边界

- 文件系统访问应遵守工作区边界；工具相关路径解析使用既有安全函数。
- 工具中的外部 HTTP 请求应走既有网络校验逻辑，避免直接访问私有地址、云元数据地址等危险目标。
- Session/memory 写入涉及持久上下文，写入前要清理不必要或敏感的元数据。
- `agent/memory.py` 使用临时文件、fsync、rename 的原子写入方式，不要简化为普通写入。

## 协作要求

- 给出必要注释，保证代码可读性
- 遇到设计不合理处，给出具体位置、影响和建议。
- 信息不足且会影响正确性时，先提问；能从代码中确认的内容不要猜。
- 回答与项目相关问题时要结合实际代码回答。
- 调整代码时不需要兼容旧配置/旧参数。
- 变更配置、文档、前端或后端时，检查相关联的调用链和用户可见行为。
- 日志应包含关键节点和错误上下文，但避免噪音。

