# Architecture

## 当前链路

本节基于主工作区 `1675aeb5`；候选分支与目标方案不等于已落地实现。

`channels -> MessageBus -> AgentLoop -> AgentRunner / ReviewOrchestrator -> MessageBus -> channels`

- `agent/loop.py`：消息与 turn 编排、session/context、审查门禁、run 状态、取消和报告交付。
- `agent/orchestration.py`：当前实际执行计划、reviewer 调度/收集与 finalizer/Judge，并非空壳；其迁移设计见计划。
- `agent/runner.py`：单个 agent 的模型/工具循环、运行内压缩、停止原因和 usage。
- `agent/subagent.py`：子代理任务生命周期；`agent/review_state.py`：run 状态、fingerprint 与报告 artifact。
- `review/input/`、`planning/`、`source/`、`output/`：输入、证据、源码、finding 校验、Judge 与报告领域逻辑。
- `session/`：历史持久化与回放；`agent/context.py`、`memory.py`、`autocompact.py`：提示上下文与会话整理。
- `channels/`：协议、交付和重试；`providers/`：模型调用适配；`agent/tools/`：能力与权限。
- `review-webui/`：展示与交互；`templates/`、`skills/`：模型行为契约。

当前审查终态门禁仍拒绝后续普通消息；长期目标见 `.agents/plans/project-roadmap.md`，当前代码调整计划见 `.agents/plans/code-adjustment-plan.md`。

## 模块边界

- 编排拥有生命周期和状态迁移，领域组件拥有业务算法；避免多个对象竞争写入同一状态。
- Runner 不承担 review、session 路由或 WebUI 协议；transport 细节留在 adapter。
- 持久化明确状态与历史各自的权威来源；前端不通过展示文本推断业务终态。
- 跨边界变更同时核对接口、调用方、持久化和最近的测试。具体迁移与产品限制只写入所属计划。
