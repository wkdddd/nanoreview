# 当前代码调整计划

更新时间：2026-10-22

## 既有节点待验收与后续事项

- diff-only 预处理清理（2026-10-22 追加：移除 `classify_path` 语言分类与 `unsupported_code_type` 跳过，任何路径的 diff 都按文本证据处理）、reviewer 三态/general、Planner diff 分诊均已实施，待验收。
- 原 Planner 节点已整体移除（2026-10-28），其 golden diff replay 缺口随节点一并作废。
- WebUI 的 reviewer mode 三态适配留待已确认的前端节点。

## 已实施节点：移除 Planner，改为手动选择 Reviewer（2026-10-28）

- Planner 的 LLM 分诊整体移除：用户在准入时用 `focus` 直接选 reviewer（缺省/`null`/空列表 → 只跑 `general`），五个 reviewer 可任意组合、含全选。`auto/special/general` 三态路由语义删除，显式 `auto` 视为非法 reviewer 名称。
- 执行链路改为 `冻结 diff → 预处理 → 程序生成 assignments → Reviewer 并发审查 → 校验 → Judge → 报告`：每个选中的 reviewer 接收全部接纳证据（`evidence.references`），不经过 Planner manifest 二次筛选；assignment 数量上限（4）已移除，并发上限（默认 4）保留，全选五项时排队执行。
- 删除仅服务 Planner 的模块：`review/planning/manifest.py`、`review/planning/triage.py`、`agent/tools/review_plan.py` 及对应测试。`plan` 进度阶段保留，含义改为程序生成 assignments；前端消费的阶段名、状态、报告引用与汇总 usage 不变。
- 新 snapshot 与报告不再写 Planner 专属数据，改记预处理接纳、任务分配与预算跳过；报告去掉 `Mode:` 行与 `Triage Outcome` 段，只列所选 Reviewer。
- **前端暂不修改**：现有 Auto 将启动 general，Custom 继续支持多选；Auto 文案暂时不准确，记录为后续前端事项。
- 验收口径：新审查不再产生 Planner 模型调用；默认 general、五项自由组合生效；现有证据准备与审查能力保留，前端现有提交流程可继续使用。
- 遗留：当前确认节点（run 黑板与结构化输出收敛）里对 Planner 角色的描述需按「无 Planner」重写，尚未实施。

## 当前确认节点：run 黑板与结构化输出收敛

状态：已验收（2026-10-25）。生产代码、测试迁移与约束文档均已完成，经四轮 subagent 复审收敛。全量 `pytest` 为 `25 failed / 1042 passed`，25 项全部是本机禁止派生子进程导致的 git 子进程环境失败（admission 10、rag 3、channels 1、review_gate 10、api 1），属既有基线；`ruff check nanoreview/` 45 项、`tests/` 0 项（均优于或等于基线）；`git diff --check` 通过。复审修复项：`coordinator.result()` 的 running 守卫、`/new` 的持久标识清理、stop/error 出口的 summary、结构化 run 的终局答案轮（含提示双写与被拒即 `invalid_output`）、`drop_unstarted` 的 run 归属与持久标识清理、`process_direct` 各返回路径的 run 释放、judge 空 verdicts 的降级语义与提示一致、reviewer findings 一次报全字段问题。既有节点的待验收与后续事项另列，不自动纳入本节点范围。

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
pytest tests/review/test_reviewer_profiles.py tests/review/test_judge.py tests/review/test_finalizer.py tests/review/test_validator.py tests/review/test_report.py tests/review/test_quality.py
pytest tests/agent/test_review_loop.py tests/agent/test_review_state.py tests/agent/test_loop_modes.py tests/agent/test_coordinator.py tests/agent/test_conversation_loop.py tests/agent/test_review_gate.py
pytest tests/review/test_admission.py tests/review/test_result.py tests/api/test_review_admission.py tests/channels/test_review_submission.py tests/webui/test_review_report_api.py tests/cli/test_review_chat.py
ruff check nanoreview/
pytest
git diff --check
```

另补黑板所有权/角色权限及结构化输出契约测试；遵循本文件既有“实施 → 自查 → codex 循环复审”流程。历史验证记录不作为本节点验证结果。
