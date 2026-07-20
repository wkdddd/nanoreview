# Agent Guidelines

本文件是本仓库中 AI 编码代理的首要工作约束。所有输出、文件写入、命令执行和字符串内容必须使用 UTF-8，不能使用 GBK/GB2312。

`.agents/` 包含按主题拆分的补充说明：开始工作时先阅读本文件；任务涉及架构、安全或运行时行为时，再阅读对应主题文件。不要让主题文件与本文件重复或产生冲突。

## 项目与数据流

NanoReview 是基于 nanobot 演进的个人多智能体代码审查系统，主体为 Python，配套 React/TypeScript WebUI。核心链路如下：

1. `nanobot/channels/` 接收外部消息并向 `nanobot/bus/` 发布入站事件。
2. `nanobot/agent/loop.py` 构建上下文、恢复会话并协调一次任务。
3. `nanobot/agent/runner.py` 调用 LLM、执行工具调用并生成结果。
4. `nanobot/agent/subagent.py` 按需调度代码审查子代理；`nanobot/review/` 负责审查计划、结果校验和报告。
5. 出站结果通过消息总线返回对应渠道；`review-webui/` 展示用户可见状态和报告。

重要目录：

- `nanobot/agent/`：AgentLoop、AgentRunner、子代理、上下文、记忆与工具。
- `nanobot/review/`：代码审查目标解析、计划、子代理提交和报告生成。
- `nanobot/channels/`：外部平台适配层。
- `nanobot/providers/`：LLM Provider、注册表和工厂。
- `nanobot/session/`：会话、上下文压缩和目标状态。
- `nanobot/config/`：Pydantic 配置模型、配置加载与环境变量解析。
- `nanobot/templates/`、`nanobot/skills/`：影响模型行为的提示词和技能。
- `nanobot/agent/tools/`：文件系统、执行、检索、MCP、审查和子代理工具。
- `nanobot/security/`：网络安全边界。
- `review-webui/`：Vite + React + Tailwind 审查界面。
- `tests/`：镜像 `nanobot/` 结构的 Python 测试。

## 常用命令

在已启用项目 Python 环境的前提下执行：

```bash
# Python 测试与静态检查
pytest
pytest tests/agent/test_codereview.py -v
ruff check nanobot/

# WebUI
cd review-webui && bun run dev
cd review-webui && bun run build
cd review-webui && bun run test

# Gateway
nanobot gateway
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

## 变更原则

- 先阅读相邻实现、测试和调用链，再修改；不猜测可从代码确认的事实。
- 保持核心小而清晰。新能力优先落在 `channels/`、`agent/tools/`、`skills/`、Provider 或 MCP 扩展中，不要内联进核心循环。
- `nanobot/agent/loop.py` 和 `nanobot/agent/runner.py` 是关键路径。改动必须聚焦、最小且说明原因；运行时事件可通用发布，渠道和 WebUI 的协议细节保留在各自适配层。
- 优先使用简单、可读且显式的代码。仅在消除真实复杂度、保护明确边界或匹配既有模式时新增抽象。
- 允许channel和 Provider 存在小范围重复；不要为了消除重复而引入复杂基类或共享框架。
- Bug 修复只改保护该不变量所需的最小表面，并添加最近的回归测试。行为变更、重构和清理不要混在同一改动中。
- 配置必须显式定义在 `nanobot/config/schema.py` 的 Pydantic 模型中；错误应清晰暴露，Provider 解析路径必须可追踪。
- 提示词模板、工具描述、技能和会话回放内容都是运行时行为的一部分。变更应窄，并在可行时补充聚焦测试。
- Python 使用 `pathlib.Path`，异步路径使用 `async`/`await`，不得在事件循环中执行长时间阻塞操作。

## 协作要求

- 设计不合理时说明具体位置、影响和可执行建议。
- 信息不足且会影响正确性时先提问；能从本地代码确认的内容不得猜测。
- 调整代码时不需要兼容旧配置或旧参数，除非用户明确要求。
- 交付时说明改动、验证结果，以及未执行的验证和原因。

## 项目具体说明

- Architecture constraints：`.agent/design.md`
- Security boundaries：`.agent/security.md`
- Common gotchas：`.agent/gotchas.md`
- Design principle:`.agent/design.md`

