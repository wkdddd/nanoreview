# Agent Guidelines

本文件是本仓库中 AI 编码代理的首要工作约束。

`.agents/` 包含按主题拆分的补充说明：开始工作时先阅读本文件；任务涉及架构、安全或运行时行为时，再阅读对应主题文件。根文件只保留入口级约束和仓库定位，专题文件负责展开具体规则；两者不得冲突。

## 项目定位

NanoReview 是基于 nanobot 演进的个人多智能体代码审查系统，主体为 Python，配套 React/TypeScript WebUI。涉及消息链路、模块归属和跨边界改动时，阅读 `.agents/architecture.md`；涉及扩展点、抽象与最小改动时，阅读 `.agents/design.md`。

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

## 实现约定

- Python 使用 `pathlib.Path`，异步路径使用 `async`/`await`，不得在事件循环中执行长时间阻塞操作。
- 在实现新模块或功能时，项目的日志打印需要完整（错误信息和关键节点的成功info）,但不要泛滥，方便调试
- 调整代码时不需要兼容旧配置或旧参数，除非用户明确要求。

## 协作要求

- 整体地审视代码，不要忽视前后端细节、相关文档、配置等问题
- 设计不合理时说明具体位置、影响和可执行建议。
- 变更代码时，要说明具体修改位置和修改思路
- 介绍或分析代码时要结合具体位置，不能泛泛而谈
- 信息不足且会影响正确性时先提问；能从本地代码确认的内容不得猜测。
- 交付时说明改动、验证结果，以及未执行的验证和原因。
- 所有输出、文件写入、命令执行和字符串内容必须使用 UTF-8，不能使用 GBK/GB2312。

## 项目具体说明

- Architecture constraints：`.agents/architecture.md`
- Security boundaries：`.agents/security.md`
- Common gotchas：`.agents/gotchas.md`
- Design principle:`.agents/design.md`

任务涉及对应说明时，先阅读相关文件再执行。若对应约束文件与 `AGENTS.md` 冲突，以`AGENTS.md` 为准，并在同一次改动中消除冲突。

