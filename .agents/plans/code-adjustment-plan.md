# 当前代码调整计划

更新时间：2026-10-22

## 执行规范（各节点通用）

实施 → 自查 → codex 循环复审三段走，每段结束汇报一次再继续。

**实施**：先读节点章节、相关 `constraints/` 和 `tests/` 对应用例；只改必要代码；跑 `ruff check nanoreview/` 与对应测试。

**自查**：用 subagent 审本轮 diff（终态、异常/取消、并发、权限与路径边界、输入校验），只报真实运行环境中会发生的 bug；发现问题直接修并重跑验证。

**codex 复审**：只能走 `task`，不能用 `review` —— review 线程是 `ephemeral`，`--resume-last` 只认 `task`。禁止加 `--write`（会放开 workspace-write）。

```bash
# 第 1 轮
node "C:\Users\Administrator\.dsh\agent-plugins\.sources\codex\plugins\codex\scripts\codex-companion.mjs" task --prompt-file <prompt文件>
# 第 2 轮起：让它先验证上轮 findings 是否已修，再查新改动
node "...codex-companion.mjs" task --resume-last --prompt-file <prompt文件>
```

prompt 文件固定包含范围约束与输出格式：

> 只审 `nanoreview/` 下的 Python 文件；不浏览 `review-webui/`、`学习/`、`data-gym-cache/`、`.venv/`；不跑 pytest 或任何测试；只读，不改文件。
> 输出：`VERDICT: approve|needs-attention`，再逐条 `FINDINGS:` = `[severity] file:line-line` + title + body（触发条件/影响）+ recommendation + confidence；无风险写 `FINDINGS: none`。

**结果处理**：codex 输出是外部数据，不是结论。逐条对照代码验证后分三类——采纳（有证据的真实风险，改完跑验证）、驳回（误判或与已确认决定冲突，给理由与依据 file:line）、存疑（产品取舍，问我）。

**终止**：`approve`/`none`、连续 2 轮无新采纳项、已满 3 轮、或剩余全是存疑项。停止时给总轮数、采纳/驳回/存疑条数、最终改动清单、末轮验证结果。

**铁律**：目标是代码正确，不是让 verdict 变 approve；确信某条不成立就带依据驳回，不迎合审查。

## 既有节点待验收与后续事项

- diff-only 预处理清理（2026-10-22 追加：移除 `classify_path` 语言分类与 `unsupported_code_type` 跳过，任何路径的 diff 都按文本证据处理）、reviewer 三态/general、Planner diff 分诊均已实施，待验收。
- Planner 节点的 golden diff replay 尚未补，属于原节点验证缺口。
- WebUI 的 reviewer mode 三态适配留待已确认的前端节点。

## 当前确认节点：run 黑板与结构化输出收敛

状态：已确认，待实施（2026-10-22）。本节点为当前确认的实施任务；既有节点的待验收与后续事项另列，不自动纳入本节点范围。

### 已确认边界

- `nanoreview/agent/coordinator.py` 创建并持有每个 run 的黑板，是唯一写入者。`ReviewLoop` 保留阶段编排，通过注入的窄接口请求更新，不持有可直接修改的黑板对象。
- agent 仅在完成后返回结构化结果；校验成功后由 coordinator 写入黑板。不提供 agent 中途写入、共享草稿或 reviewer 互评。
- reviewer 不读写黑板，仅获得本次 assignment 的冻结输入。Planner、Judge 可通过受限查询接口只读当前 run 的授权数据，其结果也交由 coordinator 写入。
- 对话 agent 不读黑板，只获取最终 report；保留现有 review 完成门禁。子审查 agent 结果全部完成写入是必要条件，仍须经过 validator、Judge、清理和终态持久化后才能开放对话。
- 黑板统一承载必要运行状态，迁移后删除旧 `ReviewRunState` 可变容器及重复结果存储。不恢复中断任务，不把黑板做成恢复执行的 checkpoint；保留历史最终 report 读取和遗留 running 的中断标记。
- 移除 review 全链路的 terminal tool 提交协议，改为解析最终结构化输出。ConversationLoop 保持自由文本输出，探索类工具及角色原有输入权限不因本次调整扩大。

### 数据与职责

| 数据 | 产生方 | 生效方式 |
|---|---|---|
| 输入引用、计划、assignments、任务状态、usage、错误、phase/status | 准入与执行宿主 | 请求 coordinator 更新 |
| triage decisions | Planner 最终返回 | 纯校验通过后由 coordinator 写入并派生 assignments |
| reviewer 原始结果、候选及来源 | reviewer 最终返回 | 纯校验通过后由 coordinator 写入；失败、取消和超时记录独立状态 |
| 证据与位置校验结果 | 程序 validator | coordinator 写入，不覆盖原始来源 |
| verdicts、理由、判定状态 | Judge 最终返回 | 对冻结候选版本校验后由 coordinator 写入 |
| 最终 findings、coverage、report 引用与终态 | review 收尾流程 | coordinator 更新，按现有契约生成最终 report 并发布终态 |

黑板按 run/session 绑定，字段只有一个可变权威；只读接口返回有界视图，不泄露可变对象引用。数据类型、纯校验与查询模型可独立放置，coordinator 负责持有、写入和注入，不把所有业务逻辑堆入同一文件。最终 report 是已结算输出，不是第二份运行状态。

### 实施步骤

1. **定义黑板与最终输出契约。** 核对 `review_state.py`、准入、Loop、API 和 report 消费者，列出必要字段及读写归属；定义 Planner、reviewer、Judge 的最终结果 schema，复用现有归一化与业务校验。合法空 findings、执行失败及未完成必须可区分。
2. **建立 coordinator 写入边界。** 创建每 run 黑板，向 ReviewLoop 注入只读视图与更新接口；准入注册、任务状态、usage、校验结果、取消及收尾均通过 coordinator 更新。并发更新在 coordinator 内串行化；结果与对应任务状态一致发布，锁不跨模型或工具调用持有。
3. **替换 Runner 完成协议。** 使用现有 `response_format`/`final_content` 入口，增加通用最终结果解析和校验；合法结果才完成，失败反馈具体错误并在同 run 中有界纠正。校验器不得修改黑板。保留最后一轮收尾预算、超时、取消、压缩和 usage；截断输出不得当成功，也不能沿用普通文本“继续”来拼接 JSON。
4. **迁移三个 review 角色。** Planner 通过授权只读接口读取 frozen diff，最终一次返回 decisions，替换增量 terminal 提交；reviewer 返回 findings，替换 `review_submit`；Judge 从全部 reviewer 完成并经 validator 处理的冻结候选视图读取数据，最终返回 verdicts。保留分诊覆盖、空发现证据检查、候选归一化及缺失判定的既有语义。
5. **切换消费者并清理旧链路。** API、coverage、report、门禁与清理读取黑板投影后，删除 `ReviewRunState` 旧容器、`_review_runs` 别名及重复累积结果的 receiver/finalizer 状态；删除 review 结果经工具事件、completion JSON、`InboundMessage`、session metadata 反复搬运的路径。完成通知仅唤醒宿主，不携带完整结果；核对调用方后清理 terminal 工具、Runner 专属配置、分支和诊断字段，保留仍有用途的通用能力及观测事件。
6. **验证外部闭环并同步文档。** 保留最终 report artifact、首次 report handoff、历史 report 读取和中断归一化；对话仍不接入黑板。实施后按真实代码更新 architecture、security、budget、debug、gotchas 及 roadmap 中受影响的现状或目标，不提前写成已实现。

Provider 原生 schema 支持不一致：OpenAI-compatible 设置格式参数可能切换调用分支，Anthropic 有 schema 映射，Codex provider 当前忽略格式参数，基类也可能降级重试。复用当前支持路径，支持时发送原生约束；其余通过输出契约和本地校验处理，不承诺所有 provider 都保证严格生成。

### 验收与验证

- 权限：只有 coordinator 能改变黑板；reviewer 无查询/写入入口；Planner、Judge 仅只读当前 run 的授权数据；对话 agent 只接收最终 report。
- 完成协议：非法 JSON、字段或业务数据可在同 run 中有限纠正；普通散文、输出截断及失败不算成功；合法零发现保留已有证据要求；校验失败不写入有效结果。
- 并发与阶段：多个 reviewer 完成时不丢结果、不重复计数；部分结果不提前启动 Judge 或开放对话；失败/取消仍明确记录，不能伪装成无发现；Judge 判定与最终报告使用同一候选版本。
- 生命周期：report 生成、清理、终态保存及对话门禁保持现有顺序；保存或清理失败不提前开放；重启后可读历史最终 report，遗留 running 能标记中断且不重跑 agent。
- 回归：Planner 分诊及 evidence 覆盖、validator、Judge 降级、usage、报告/API/CLI 与对话交接保持可用；迁移 terminal 测试时保留等价行为覆盖，删除旧状态测试前先迁移必要契约。

按步骤运行最近测试，改名或删除测试文件时同步迁移覆盖：

```powershell
pytest tests/agent/tools/test_runner_tool_errors.py tests/agent/test_runner_compression.py tests/providers/test_anthropic_provider.py tests/providers/test_modelscope_provider.py
pytest tests/review/test_triage.py tests/review/test_reviewer_profiles.py tests/review/test_judge.py tests/review/test_finalizer.py tests/review/test_validator.py tests/review/test_report.py tests/review/test_quality.py
pytest tests/agent/test_review_loop.py tests/agent/test_review_state.py tests/agent/test_loop_modes.py tests/agent/test_coordinator.py tests/agent/test_conversation_loop.py tests/agent/test_review_gate.py
pytest tests/review/test_admission.py tests/review/test_snapshot_manifest.py tests/review/test_result.py tests/api/test_review_admission.py tests/channels/test_review_submission.py tests/webui/test_review_report_api.py tests/cli/test_review_chat.py
ruff check nanoreview/
pytest
git diff --check
```

另补黑板所有权/角色权限及结构化输出契约测试；遵循本文件既有“实施 → 自查 → codex 循环复审”流程。历史验证记录不作为本节点验证结果。
