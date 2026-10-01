# nanobot

- Root: `C:\Users\Administrator\Desktop\nanobot`
- Commit: `4de728a5`
- Last checked: `2026-10-01`
- Scope: 对话编排、历史持久化、提示上下文与 Runner 接口；仅源码核查，未运行参考项目测试。

## 入口与差异

- `nanobot/agent/loop.py`：加载 session/history、构建 turn、执行与保存，亦依赖 automation、持续目标、跨渠道路由和运行时控制；不是独立的最小对话模块。
- `nanobot/session/manager.py`：历史回放、持久化、保留策略和 Provider conversation state。
- `nanobot/agent/context.py`：提示词、历史与运行时上下文组装，依赖 memory、skills、apps 和工具扩展。
- `nanobot/agent/runner.py:AgentRunSpec` 使用 `initial_messages` 与 Provider conversation state；NanoReview 使用 `frozen_messages`/`working_messages` 和 run-level compression，接口不能直接替换。

对话历史回放与中断任务恢复是不同能力。提取上述逻辑时应核对直接依赖和相应测试；NanoReview 的复用范围由目标计划决定。
