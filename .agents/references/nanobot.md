# nanobot

本机位置：C:\Users\Administrator\Desktop\nanobot
github地址： https://github.com/HKUDS/nanobot

## nanobot 的对话、上下文与 Runner（已核查）

- Root: `C:\Users\Administrator\Desktop\nanobot`
- Commit: `432421bceba6e50ec435924c7b21ef341ed69e55`（最新本地 HEAD，提交日期 `2026-09-26`）
- Version: `0.3.5`（`pyproject.toml`）
- Scope: 对话编排调用点、历史回放与保存、提示上下文、Runner 输入及上下文治理接口；对比 `4de728a5`。
- Entry points: `nanobot/agent/loop.py:1237`、`nanobot/agent/context.py:73/276`、`nanobot/agent/runner.py:89/339`、`nanobot/agent/context_governance.py:124/259`、`nanobot/session/manager.py:172/240/1659`。
- Behavior: AgentLoop 通过结构化 turn 输入调用共享 Runner，注入历史摘要、Provider compaction、消息注入、checkpoint 和持续目标回调。`build_transcript()` 保留当前 turn 边界，`build_messages()` 兼容合并相邻同角色消息。Session 按摘要 checkpoint 与 `last_archived` 回放历史；SessionManager 管理缓存、保留和存储，Provider state 独立持久化，runtime checkpoint 另有保存入口。
- Contracts: `TranscriptInput` 分别承载 history、当前消息、media、session summary 和 runtime context。Runner 的 `initial_messages` 与 `transcript_input` 互斥，后者要求 `transcript_builder`，两种入口均要求 `consolidate_history`；Runner 管理 Provider conversation state。`ContextCompactionState` 区分已接受上下文和未发送增量，摘要后经 builder 重建；`ContextGovernor` 管理模型请求上下文并保留持久化历史。`SessionPolicy.persist` 控制是否保存。
- Current mapping: 本条仅记录参考事实与接口差异，不定义 NanoReview 目标或迁移方案。
- Differences: NanoReview 使用 `frozen_messages`/`working_messages` 与独立 run-level compression，不能直接替换为上述 Runner/治理接口。nanobot 对话编排仍耦合 automation、持续目标、跨渠道交付与运行时控制；上下文构造仍依赖 memory、skills、apps 和工具扩展，不能整段作为最小 ConversationLoop 提取。历史回放不等于恢复或重新执行中断任务。
- Risks/tests: 原核查仅阅读相关源码及 turn 边界、Runner 输入、Provider state 持久化的部分测试，未运行测试或服务；本次仅整理格式。相关测试为 `tests/agent/test_context_builder.py`、`test_runner_core.py`、`test_runner_governance.py`、`test_session_atomic.py`、`test_loop_session_policy.py`。未核对远端最新 HEAD；原核查时本地改动仅在 `webui/README.md`，不涉及核查范围。
- Last checked: 2026-10-04

## 历史参考（4de728a5）

- Root: `C:\Users\Administrator\Desktop\nanobot`
- Commit: `4de728a5`
- Version: `0.3.0`
- Scope: 对话编排、历史持久化、提示上下文与 Runner 接口。
- Entry points: `nanobot/agent/loop.py`、`nanobot/session/manager.py`、`nanobot/agent/context.py`、`nanobot/agent/runner.py:AgentRunSpec`。
- Behavior: Loop 加载 session/history、构建 turn、执行与保存；SessionManager 管理历史回放、持久化、保留与 Provider conversation state；ContextBuilder 组装提示词、历史与运行时上下文。
- Contracts: Loop 依赖 automation、持续目标、跨渠道路由和运行时控制；ContextBuilder 依赖 memory、skills、apps 和工具扩展；Runner 使用 `initial_messages` 与 Provider conversation state。
- Current mapping: 保留旧版职责与依赖笔记，复用范围由 NanoReview 目标计划决定。
- Differences: 旧版不是独立的最小对话模块，Runner 接口与 NanoReview 的 frozen/working 分区不同；历史回放与中断任务恢复应分开理解。这些边界仍有参考价值，旧 Runner 输入描述不作为新版完整接口说明。
- Risks/tests: 原核查仅阅读源码，未运行参考项目测试；提取逻辑仍需核对直接依赖与测试。
- Last checked: 2026-10-01
