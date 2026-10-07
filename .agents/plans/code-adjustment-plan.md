# 当前代码调整计划

更新时间：2026-10-07

## 当前节点：ReviewLoop 预算与 diff-only 审查

状态：**已实施，待用户验收（2026-10-07）**。全量 `pytest tests/` = 991 passed / 0 failed；`ruff check nanoreview/` 45 项（基线 48，净减少 3）、`tests/` 4 项（与基线持平）；`git diff --check` 干净。

实施结果（步骤 1–9 全部落地）：

- 步骤 1 diff-only 准入：`normalize_review_action` 默认 `diff`、显式 `repo` 报 `invalid_action`；新增 `diff_unavailable`；CLI `--action` 默认 `diff`；snapshot 不再记录 `repo_content`/`scope_files`。
- 步骤 2 diff evidence：单文件 `8_000` token 阈值、截断标记、`evidence_token_budget` 默认 `150_000`。
- 步骤 3 Planner manifest：新增 `review/planning/manifest.py`（`evidence-manifest/1`，`80_000` 预算），`EvidenceReference.matched`；planner/review_loop 接线改造。
- 步骤 4–6 已在本节点前完成（200k/8192/30/180s、diff-only 工具权限、GitPython fallback 与 dispatch 契约）。
- 步骤 7 重复工作与质量评测：reviewer run 内 `FileStates.review_ledger` 抑制重复 `read_file`/`grep`（`force` 不可绕过、按 run 隔离）；`ReviewerRunState` 记录 evidence 分配与重复读取计数；新增 `review/quality.py` golden diff replay（finding 匹配 / P·R·F1 / incomplete rate / coverage / usage / tool calls / 耗时）。
- 步骤 8 持久化与报告：`ReviewSnapshotStore.augment` 回写 Planner manifest + 预算契约；report artifact 新增 `snapshot_ref` 与 `coverage`（changed/skipped files、per-reviewer 状态/usage/重复读取、incomplete dimensions）；新增 `review_budget_contract()`。
- 步骤 9 验证：residual 扫描确认 Planner/reviewer/Judge 的模型工具集与 review prompt **均不含仓库级 reader 工具**；architecture/budget/security/roadmap 已同步。此后又追加一轮收尾（保留提交轮 + `local_review`/RAG skill 清理），见文末「2026-10-07 追加」。

待用户确认的遗留项：

- `EvidenceBudget.task_cap_tokens` 仍保留为配置字段（`tests/review/test_preprocessor.py` 仍断言其存在）。计划允许清理，但需先确认无生产消费者；本节点未删除。
- （已解决）`agent/conversation_loop.py::_CONVERSATION_DENIED_TOOLS = {"local_review"}`（`728e47f9` 引入）曾使 Conversation Agent 也看不到 `local_review`。2026-10-07 追加清理直接删除了 `local_review` 工具本身与该 deny 列表，矛盾消失。
- WebUI 展示适配（omitted/skipped/coverage）留待前端节点。

本计划覆盖此前的第 4 阶段计划；实施前只允许按源码核对修正文件清单，不改变已确认目标和预算。

## 前置状态

- 工具 workspace scope 与远程 review 边界第 4 阶段已完成，具体历史实施流水不在本计划重复保留。
- `nanoreview review` 已支持审查后在同一 CLI session 继续对话；`pytest tests/cli` 已验证该入口。
- 本计划只调整后端与相关测试/约束/roadmap，不修改 `review-webui/`。

## 已确认目标

- ReviewLoop 的 Planner、reviewer、Judge 固定使用 `200_000` tokens 上下文窗口；通用 Conversation Agent 保持独立配置。
- reviewer 每次模型输出最多 `8_192` tokens，最多 `30` 次模型请求（最后一次用于 `review_submit`，计入 30 次），单 reviewer 超时 `180s`。不新增累计 token、金额或日/月配额。
- Review 流程中的模型工具集（Planner/coordinator 与四类 reviewer subagent）均不暴露 `local_review`。ReviewLoop 通过内部依赖直接使用 `nanoreview/review/planning/evidence.py` 中的 `ReviewEvidenceService` 完成 prefetch 和 evidence dispatch；工具 wrapper 的注册状态不影响该服务。
- review 产品入口只保留本地 diff review，移除 repo review 的有效入口、配置和兼容分支。
- diff 包含 staged、unstaged 和 untracked 变更。无 Git 仓库、无法读取 diff 或 diff 为空时，准入失败并返回明确原因，不启动 Planner/reviewer/Judge。
- Planner/evidence 的授权范围只来自 diff。reviewer 可以用定向 `read_file`/`grep` 读取 diff 外文件作为上下文；Review 流程中任何模型都不能调用 `local_review`。非 review 场景中既有的工具调用不在本节点清理范围内。
- accepted finding 必须位于 changed file；行号允许在同一文件的邻近未修改上下文，但需说明与变更的关联。
- diff evidence 以 changed hunk 为主，按现有优先级排序；必要时补充有限 related context。
- 单个 changed file 小于 `8_000` tokens 时保留为一个完整 evidence unit；达到或超过阈值时沿用现有语义/diff hunk 切分规则。按单文件判定，不因整个目标进入 chunked 模式而拆分所有小文件。
- 主 evidence 总预算为 `150_000` tokens；related evidence 保持总预算的四分之一，默认约 `37_500` tokens，维持单层补充规则。
- Planner 只使用一个结构化 evidence manifest。manifest references 和 skipped/omitted 说明预算固定为上下文窗口的 40%，即 `80_000` tokens。优先保留高优先级 previews；超限时省略低优先级 references 并记录覆盖缺口。
- manifest 是从同一份结构化 `EvidenceReference` 数据生成的 Planner 唯一输入，不再并行维护 summary manifest 和 coordinator reference manifest。
- 最终经过预算筛选的 Planner manifest 持久化到 review snapshot，并由 report/ReviewRunState 引用其版本、覆盖和省略统计。
- 可借鉴 Kodus 的优先级排序、adaptive fit、有界工具输出和 overflow recovery；保留 NanoReview 的 evidence ID、Planner 路由和多维 reviewer，不移植按文件分配 agent 的模式。

## 预算契约

```text
context_window_tokens = 200_000
reserved_overhead_tokens = 9_000
safety_margin = 10% of context_window_tokens
usable_context_tokens = 171_000
evidence_token_budget = 150_000
related_evidence_budget = evidence_token_budget / 4 = 37_500
planner_manifest_budget = context_window_tokens * 40% = 80_000
reviewer_max_output_tokens = 8_192
reviewer_model_request_limit = 30
reviewer_timeout_seconds = 180
```

上述数值是 ReviewLoop 的固定运行预算，不表示所有模型都能使用 200k；本项目本节点按已确认决定统一设置，不做 provider 动态识别。Planner 的 80k 约束只针对 manifest references 与 coverage 说明；Planner system/task/tool 定义和输出仍计入完整 200k 请求窗口。

`subagent_evidence_budget_chars` 若保留，必须说明其实际用途是派生 chunk cap，而不是 reviewer 请求的独立总预算。无生产消费者的 `EvidenceBudget.task_cap_tokens` 可以清理，但需核对并同步测试、构造与序列化消费者；不得误删仍影响 `chunk_cap_tokens` 的配置语义。

Planner 和 reviewer 收到的输入不同：Planner 看到 manifest 元数据与 previews；reviewer 看到被分配的 evidence excerpts 及关联 related evidence。相同窗口不代表 reviewer 输入一定不超限。实施时必须核对并保护 reviewer frozen task 的请求窗口；不得静默丢弃授权 evidence，也不得假定 working-history compression 能裁剪 frozen task。若容量不足，失败或省略行为必须明确记录到 coverage 和 run 状态。

## 实施步骤

### 1. 收敛 diff-only 准入

- 梳理 `ReviewAdmissionService`、CLI/API、tool、配置和 prompt 的 action 消费者，删除 repo review 的可达入口及仅服务于该入口的分支。
- 保留 staged、unstaged、untracked 收集；明确新增、删除、重命名、不可读和被过滤文件的输入/coverage 语义。
- 将非 Git 仓库、diff 读取异常、空 diff 统一映射为准入错误；失败不得创建 running review 或调用 agent。
- snapshot 记录 action、HEAD SHA（可得时）、changed files、skipped files、scope 和 input fingerprint。

### 2. 调整 diff evidence 预处理

- 让本地 diff evidence 成为 Planner 唯一授权证据来源；diff 外文件只能由 reviewer 定向读取作上下文。
- 以单文件 token 数应用 `8_000` 阈值：低于阈值保留一个完整 unit；达到阈值后使用现有解析、hunk、递归拆分和合并策略。
- 设置主 evidence `150_000` tokens 与 related 四分之一比例；按现有优先级选择主 evidence，related 只作一层补充。
- 核查文件读取字符上限，确保截断文件被明确标记，不能将截断内容描述成完整文件 evidence。
- 检查 direct/chunked/oversized 模式、changed-line 定位和 skipped reason，保证大仓库不会因新增单文件规则丢失覆盖信息。

### 3. 统一并预算 Planner manifest

- 以 `EvidenceReference` 为唯一结构化来源，统一 id、path/range、kind、role、token count、matched/risk hints、preview coverage、preview 和 skipped/omitted 记录。
- 删除 Planner 对旧 summary/context 文本的依赖，所有展示由同一 manifest renderer 生成；Planner 的 assignment 只能引用 manifest 中可用的 evidence IDs。
- 对 Planner 实际收到的 references 与 coverage 执行 `80_000` token 预算。按优先级保留必要元数据和 previews；预算不足时省略低优先级 references，并记录文件、数量、原因和预算统计。
- 预算计算需包含 token counter 的来源/估算方式及 manifest 版本；manifest 本身不得超过预算，Planner 全请求不得超过 200k 窗口。
- reviewer assignment 使用同一 evidence ID 集合，并按现有 parent 关系附加 related evidence。

### 4. 接线 ReviewLoop 预算与范围

- Planner、reviewer、Judge 的 `AgentRunSpec` 显式绑定 `context_window_tokens=200_000`；不得改变 Conversation Agent 的独立上下文配置。
- reviewer 固定 `max_tokens=8_192`、最多 30 次模型请求、timeout 180 秒；最后一次请求用于最终结构化提交并计入 30 次。一次模型请求可返回多个并行工具调用，模型请求数不等于工具调用数。
- 维持 Runner frozen/working 分区语义。manifest 和 reviewer task 是 frozen 输入，不得被工作历史压缩静默改写。
- 从 ReviewLoop 的 Planner/coordinator 及四类 reviewer subagent 模型工具注册表中移除 `local_review`；保留各角色所需的其他工具及受限 scope。将 `ReviewEvidenceService` 从 `ReviewToolBase`/工具注册表取 provider 的接线改为 ReviewLoop 的内部依赖注入，确保 prefetch、Planner manifest 和 reviewer frozen task 仍使用同一授权 evidence。
- 将 changed-file 集合贯穿 reviewer、Judge、validator、finalizer；非 changed-file finding 不得进入 accepted report。

### 5. Reviewer 收尾与错误语义

- 在 30 轮耗尽前保留最后一轮用于 `review_submit`；成功提交结构化 findings 或空 findings 后正常完成。
- 最终提交仍失败、reviewer 超时或执行异常时，将该维度标为 incomplete/error；不得把未提交结果解释为空 findings 或完整审查。
- CLI 对 reviewer/证据不完整或 review 失败返回非零；完整完成且无 findings 返回 0。`--fail-on` 继续只按 finding 严重级别判定。

### 6. 修复证据与工具接口缺陷

- GitPython 不可用时，CLI diff fallback 将 Git worktree-relative 路径转换为目录 target-relative 路径；子目录 target 的 staged、unstaged 和 untracked 变更应进入 evidence。
- 修正非 review 场景仍保留的 `local_review` wrapper 在非 reader action 调用 `ReviewEvidenceService.dispatch()` 时传入不支持的 `tree_pattern` 参数；保留其既有非 review reader/evidence 行为，不恢复 repo review 入口。
- 为目录 target + GitPython fallback、仍可达的非 review `local_review` evidence dispatch 和 CLI 不完整/完整空 findings 退出码补回归测试。

### 7. 重复工作诊断与质量评测

- 按 run/reviewer 记录 evidence 分配、重复文件读取、工具调用数、token usage、耗时和 coverage；用于定位重复探索，不新增累计 token 硬上限。
- 在 reviewer run 内按解析后的绝对路径和规范化行范围识别 `read_file` 重复读取；文件内容未变且同一范围已成功返回时，阻止再次返回全文，向模型返回简短提示，要求复用已有上下文或请求不同范围。
- 对 `grep` 等可定位范围的读取工具使用相同原则：仅抑制相同查询/相同范围且内容未变的重复结果；允许读取不同范围和文件变化后的内容，不按“同一文件”粗略禁止全部后续读取。Reviewer 不暴露 `local_review`，因此不为其设计 reviewer 内去重路径。
- Reviewer 的重复读取拦截不可由 `force` 参数绕过；其他 Agent 的既有行为不因此改变。去重状态限定在单个 reviewer run，不跨 reviewer 或 review run 共享。
- 建立固定 golden diff case 与 deterministic tool replay，输出 finding 匹配明细、precision/recall/F1、incomplete rate、token usage、工具调用数和耗时。
- A/B 比较必须固定 case、Planner/reviewer 配置、模型与 reasoning effort；工具回放覆盖不足的 case 标为不可判定，不计作模型漏报。

### 8. 持久化与报告覆盖

- review snapshot 持久化最终给 Planner 的结构化 manifest、budget/renderer 版本、保留和省略 evidence、changed-file 边界及统计。
- report/ReviewRunState 持有 snapshot/manifest 引用，并呈现 omitted/skipped、reviewer timeout/overflow 和未覆盖原因；不复制完整源码或完整会话。
- 日志关联 `run_id`/`trace_id`，记录窗口、预算、保留/省略数量、重复读取、usage 和停止原因，不记录完整源码、密钥或完整会话。
- 同步更新架构、预算、安全约束和 roadmap 中与本节点冲突的阶段边界；WebUI 展示适配留待后续阶段。

### 9. 测试与验证

- 准入测试覆盖 staged、unstaged、untracked、空 diff、非 Git、读取失败、受限 scope、重复路径及新增/删除文件。
- reviewer 测试覆盖最后一轮结构化提交、提交失败/超时的 incomplete 状态，以及失败不能被规范化为空 findings。
- reviewer 空 findings 校验覆盖 frozen task 已带 evidence excerpts 的情况；无需 reviewer 调用 `local_review` 才能证明已读取证据。无分配 evidence 且没有成功 `read_file`/`grep` 时仍标记 incomplete。
- evidence/tool/CLI 回归测试覆盖子目录 target 的 GitPython fallback、非 review `local_review` dispatch 参数契约、ReviewLoop 全部模型角色均不注册 `local_review`、内部 evidence service 无工具 wrapper 时仍可 prefetch、incomplete 非零退出码和完整空 findings 零退出码。
- Preprocessor 测试覆盖 `<8k` 整文件、`>=8k` 切分、主/related 预算、优先级淘汰、截断标记和大仓库 coverage。
- Manifest 测试覆盖单一路径、80k token 上限、优先级保留、低优先级省略、稳定 ID、assignment 校验、统计和持久化回放。
- ReviewLoop 测试覆盖 200k 传递、reviewer 8192/30 次模型请求/180 秒、Conversation 隔离、diff-only 工具权限、changed-file finding 边界及 frozen task 超窗可见失败。
- 去重测试覆盖路径别名与等价范围归一化、相同范围/未变化内容返回提示、`force` 不绕过 reviewer 去重、不同范围和文件变化允许读取、不同 reviewer 状态隔离，以及 reviewer 可用的 `read_file`/`grep` 路径。
- Golden replay 验证固定 case 的 finding 匹配、coverage、incomplete rate、usage、工具调用和耗时；质量不能只以 wiring smoke 或 LLM judge 单独判定。
- 清理整个 ReviewLoop 的模型可调用 `local_review`：从 Planner/coordinator 与 reviewer 的工具注册结果中排除该工具，移除 reviewer 的必需工具约束，并清除 review coordinator、reviewer task/system prompt 中要求或建议模型调用它的内容。保持非 review 场景既有行为。
- 将 `ReviewEvidenceService` 保留在 `nanoreview/review/planning/evidence.py`，并从工具 wrapper/registry 解耦实例创建和注入；核对其 preprocessor 配置、ReviewLoop 生命周期及 evidence bundle 输出，不迁移、不删除该服务。
- 核对工具错误软处理和空 findings evidence 校验。Planner 分配并注入 reviewer frozen task 的 evidence excerpts 应计为已提供证据；reviewer 也可通过 `read_file`/`grep` 补充上下文。没有分配 evidence 且没有成功读取证据时，空 findings 仍应标为 incomplete。
- 运行最贴近测试、完整 pytest、`ruff check nanoreview/`、`git diff --check`；残留扫描确认 ReviewLoop 的模型工具、prompt 和 task 不再暴露 `local_review`，同时非 review 工具调用和内部 evidence service 行为仍正确。

## 主要影响面

实施前按实际调用关系确认并收敛下列模块，不做无关重构：

```text
nanoreview/config/schema.py
nanoreview/config/loader.py
nanoreview/agent/review_loop.py
nanoreview/agent/runner.py
nanoreview/agent/subagent.py
nanoreview/agent/tools/review_base.py
nanoreview/review/types.py
nanoreview/review/admission.py
nanoreview/review/input/snapshot.py
nanoreview/review/planning/evidence.py
nanoreview/review/planning/preprocessor.py
nanoreview/review/planning/prefetch.py
nanoreview/review/planning/planner.py
nanoreview/review/planning/prompt.py
nanoreview/review/profiles.py
 nanoreview/agent/tools/local_review.py
nanoreview/review/output/judge.py
nanoreview/review/output/validator.py
nanoreview/review/output/finalizer.py
nanoreview/cli/commands.py
nanoreview/api/server.py
```

主要测试位于 `tests/review/`、`tests/agent/`、`tests/cli/` 和 `tests/api/`。至少核对 admission、preprocessor、prefetch、prompt、review loop、runner terminal/compression、CLI/API schema 与 report serialization 的对应测试。

## 验收标准

| 场景 | 必须结果 |
|---|---|
| ReviewLoop context | Planner/reviewer/Judge 均为 200k；Conversation Agent 独立配置不变 |
| Reviewer limits | max output 8192、最多 30 次模型请求且最后一次用于提交、180 秒超时；工具调用数单独观测 |
| Duplicate reads | reviewer 对相同规范化文件范围和未变化内容不重复接收全文；返回复用提示，不阻止不同范围或更新后的内容 |
| Reviewer completion | 所有成功维度有结构化提交；耗尽/超时/异常显式 incomplete，不伪装为空 findings |
| CLI result | incomplete/failed 返回非零；完整且无 findings 返回 0；`--fail-on` 只按严重级别判定 |
| Diff input | staged/unstaged/untracked 纳入；无 Git、读取失败、空 diff 在准入阶段明确失败 |
| Directory diff fallback | GitPython 不可用时，子目录 target 的 diff evidence 路径正确且变更可审查 |
| Review model tool surface | Planner/coordinator 与四类 reviewer 的模型工具集均不含仓库级 reader 工具；各角色其他工具仍符合各自 profile |
| Internal evidence service | `ReviewEvidenceService` 无需任何工具 wrapper 即可生成 diff prefetch；Planner manifest 和 reviewer task 收到同一授权 evidence |
| Repository reader tool | `local_review`、`ReviewToolBase`、`LocalRepoReader` 与 `skills/rag` 已删除；Conversation/Review 均无仓库级 reader 工具，且不恢复 repo review 入口 |
| Reserved submission turn | reviewer 的 30 次请求中最后一次强制为 `review_submit`：终局迭代只暴露 terminal tool 并强制调用，探索耗尽预算也不会被判未提交 |
| Diff-only boundary | Planner 只收到 diff evidence；reviewer 可定向读取上下文但不能 broad repo review |
| Finding boundary | accepted finding 文件必须 changed；同一文件邻近未修改行允许并需关联变更 |
| Evidence granularity | 单文件 `<8k` 一个完整 unit；`>=8k` 沿用现有切分策略 |
| Evidence budgets | 主预算 150k；related 为其四分之一；省略和跳过原因可见 |
| Planner manifest | 唯一结构化输入路径；实际 manifest 不超过 80k token budget；超限优先保留高优先级并记录遗漏 |
| Persistence | snapshot 有最终 manifest 与预算版本；report/RunState 可读其引用和 coverage |
| Review efficiency | run/reviewer 可观测重复读取、tool calls、usage、耗时与 coverage；固定 golden replay 可比较质量和成本 |
| Overflow | frozen 输入超窗时不静默截断；明确恢复结果或 failure/coverage gap 被持久化 |
| Network capabilities | 不因 diff-only 删除 provider HTTP/OAuth、`web_search`、`web_fetch` 或 MCP 网络能力 |
| WebUI boundary | 本节点不修改 `review-webui/`；后端不恢复 approval 协议 |

## 本节点不做

- 不支持 repo-wide review、远端仓库或远端 PR review。
- 不新增累计 token/金额/周期配额，不动态识别模型窗口；本节点固定 ReviewLoop 200k。
- 不将 reviewer 改成按文件分配，不整体移植 Kodus 实现。
- 不允许 Conversation Agent 自主发起 review，不修改 WebUI。
- 不让 reviewer 的上下文读取扩大 finding 审查范围；accepted findings 仍受 changed-file 边界约束。
- 不自动恢复中断的 review/tool run，不引入第二套 runner、压缩器或 review 状态机。

## 2026-10-07 追加：保留提交轮 + `local_review`/RAG skill 清理

用户指令：修复 P1「30 次模型请求没有真正保留最后一次 `review_submit` 机会」，并清理 tool `local_review` 与 skill `RAG skill`。

### P1 保留提交轮

- 原实现把 reviewer 的 `max_iterations` 直接设为 `REVIEWER_MODEL_REQUEST_LIMIT`（30），Runner 跑满 30 次后进入 `max_iterations` 出口。前 30 次若全是 `read_file`/`grep` 探索，模型不会被强制提交，reviewer 被判未提交；文案里的「最后一次请求用于 review_submit」并未实现。
- 修复：新增 `AgentRunSpec.reserve_terminal_iteration`（默认 `True`）。`AgentRunner` 在 `iteration == max_iterations - 1` 时只向模型暴露 terminal tool，且当 terminal tool 唯一时通过 `tool_choice` 强制调用（复用 Planner/Judge 既有的强制模式），仍在同一 30 次预算内。
- 影响面：reviewer 获得保证的提交轮；Planner（`submit_review_plan`）与 Judge（`submit_verdicts`）本就每轮强制，行为不变；其他 terminal-tool run 同样受用。
- 回归：`tests/agent/tools/test_runner_tool_errors.py` 新增「终局轮只暴露 terminal tool 且强制调用」与「关闭后仍以 `max_iterations` 收场」两例。

### `local_review` 工具与 RAG skill 清理

- 删除：`nanoreview/agent/tools/local_review.py`、`nanoreview/agent/tools/review_base.py`、`nanoreview/review/source/local.py`、`nanoreview/skills/rag/`、`tests/agent/tools/test_local_review_tool.py`。
- 引用同步：`tools/__init__.py` 的 `__all__`、`tools/loader.py` 的 `_SKIP_MODULES`、`review/__init__.py` 的 `LocalRepoReader` 导出、`conversation_loop.py` 的 `_CONVERSATION_DENIED_TOOLS` 与 deny 参数。
- 该工具此前已不可达（不在任何 review scope，且被 conversation deny），删除属纯死代码清理，行为零变化；非 review 的仓库理解改用 `read_file`/`grep`/`list_dir` 与 `repo-reader` skill。
- 保留：`nanoreview/rag/` 包与其测试（自带测试、无生产调用方，未在本次点名范围）；`.nanobot/` 下的 RAG 运行产物未动。

### 验证

- 全量 `pytest tests/` = **991 passed / 0 failed**（删 2 个 `local_review` 用例、增 2 个保留提交轮用例，净持平）。
- `ruff check nanoreview/` 45 项、`tests/` 4 项（均与追加前基线持平，无新增）；`git diff --check` 干净。
- 已知 Windows 环境 flaky 未复现：`sessions/*.jsonl.tmp` 的 `WinError 5` rename 竞争（单独跑必过，全量偶发 1 条）。

## 2026-10-07 追加：4 个审查正确性缺陷修复

用户排查出 4 个缺陷（3×P1、1×P2），全部修复。

### P1-1 changed-file 边界未生效

- 根因：`ReviewLoop._resolve_execution_inputs` 恒返回 `changed_files=[]`，`validator` 的 `self._changed_files` 为空集，`if self._changed_files and ...` 短路跳过 diff 边界校验；目录审查时未修改文件中的 finding 会被 accepted，report coverage 也恒为空。
- 修复：`_prepare_review` 读回本 run 的 snapshot（`ReviewSnapshotStore.read`），把 snapshot 的 `changed_files` 送入 `_ReviewInputs.changed_files` → `ReviewFinalizer` → `validator` 边界，并进入 `ReviewRunState.changed_files` 供 report coverage。

### P1-2 注入的 evidence 未被识别为已读取

- 根因：`_dispatch_and_collect` 派发 reviewer 时未在 `origin_metadata` 放 `assigned_evidence`，而 `profiles._handle_reviewer_result` 只凭该标记或成功的 `read_file`/`grep` 判断证据存在。reviewer 仅依据 frozen evidence 提交 `findings: []` 时被错误标记 incomplete（reviewer 已无仓库 reader 工具，无法自证）。
- 修复：派发时写入 `"assigned_evidence": len(references)`。

### P1-3 审查未使用 admission snapshot

- 根因：admission 把 `net_diff` 写入 snapshot，但 prefetch 经 `evidence.local_changed_context` 重新读取当前工作树；准入后修改/提交/回滚会改变实际审查内容。
- 修复：新增 `ReviewSnapshotStore.read`；`_prepare_review` 把 snapshot 的净 diff 以私有键 `ReviewMetaKey.FROZEN_DIFF`（`{"patches","skipped"}`）交给 prefetch，`evidence.dispatch/local_changed_context` 收到 `frozen_diff` 时直接使用、不再读工作树。快照缺失降级为读活工作树并记 warning，不失败 run。该键不进 session metadata、不进 reviewer metadata。

### P2 staged 与 unstaged diff 拼接

- 根因：`evidence.local_changed_patches` 对 tracked 文件把 `HEAD -> index` 与 `index -> worktree` 两个 patch 拼成一条，同时暴露 staged 中间态与最终工作树态；admission 用的是净 diff。
- 修复：统一为 `git diff --no-ext-diff --unified=3 HEAD -- path`（`HEAD -> worktree`），与 `collect_net_diff` 一致。`local_changed_summary.touched_lines` 的 union 语义属 RAG 评分用途，保留不动。

### 验证

- 全量 `pytest tests/` = **996 passed / 1 failed**；唯一失败 `test_same_session_direct_requests_serialize_and_each_get_replies` 单独跑必过（既有 Windows 并发/rename flaky，非回归）。
- `ruff check nanoreview/` 45 项、`tests/` 4 项（与基线持平，无新增）；`git diff --check` 干净。
- 新增回归：`test_prefetch_forwards_the_frozen_diff_to_the_evidence_service`、`test_prefetch_omits_frozen_diff_when_absent`、`test_local_diff_prefers_the_frozen_snapshot_over_the_worktree`、`test_local_changed_patches_uses_net_head_diff_not_staged_concat`、`test_changed_file_boundary_comes_from_the_admitted_snapshot`、`test_review_reviews_the_frozen_snapshot_diff_not_the_worktree`（另在既有 dispatch 用例补 `assigned_evidence` 断言）。
- 残留（未扩大范围）：reviewer 提交后 `validator` 仍按**活工作树**校验 evidence/line；若准入后该文件又变动，evidence 可能对不上。本次未改，属候选跟进项。
