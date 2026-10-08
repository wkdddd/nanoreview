# 当前代码调整计划

更新时间：2026-10-08

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

## 当前节点：Review reviewer 选择模式与通用 reviewer

状态：**已实施，待验收**。

本节点调整 ReviewLoop 的 reviewer 路由和 profile 注册，不改变已经落地的
diff-only 输入、evidence manifest、预算、Judge、报告持久化和 ReviewLoop 生命周期。

实施记录（2026-10-08）：

- `normalize_requested_dimensions()` 现在返回 `(roles, mode)`，mode 固定为
  `auto|special|general`；旧 `explicit` 值不再产生，旧 `focus` 字符串仍按维度解析。
  `general` 作为显式别名归一化为 general 模式；`general` 与专项混用、空 special、
  未知维度、超 max 分别抛出可区分的 ValueError。
- `ReviewPlan.routing_mode` 改名为 `mode`（`ReviewMode`）；`ReviewPlanReceiver` 以
  `mode` 决定合法提交形状：auto=1–4 专项子集、special=用户所选专项全集、
  general=恰好一个 general。
- 注册 `general` reviewer profile：独立 prompt（`agent/reviewers/general.md`）、
  `reviewer.general` scope、`general` context policy、details schema
  `{concern, symptom, affected_behavior, recommended_fix}` 与报告标签；
  execution scope 改为显式 `_SCOPE_BY_PROFILE_ID` 映射。
- `ALL_REVIEW_ROLES` 含 5 个角色，`SPECIAL_REVIEW_ROLE_NAMES` 固定 4 个专项；
  auto 只从专项集合取角色，general 不会自动进入 auto。
- planner prompt 按 mode 分支：auto 要求“最少充分集合”、未选维度不补派；
  special 要求全覆盖；general 只允许单一 general。删除 “every required dimension”
  与 `risk_hints` 路由框架表述（`risk_hints` 仍作为 manifest 元数据保留到下一节点）。
- 报告 `Selected Reviewers` 标签由 `Routing` 改为 `Mode`；finalizer/report 参数
  `routing_mode` 改名 `mode`。
- 验证：`ruff check nanoreview/` 回到基线 45 项；`tests/review/`、`tests/agent/test_review_loop.py`、
  `test_codereview.py` 全绿（新增 general 模式/profile/receiver 用例）。全量 pytest 仅剩
  既有环境失败（git 子进程 PermissionError，与本次改动无关，改动前后一致）。
- 待适配（后续节点）：`review-webui` 的 `ReviewRoutingMode` 仍是 `auto|explicit`，
  `ReviewConfig.tsx`/`NewReviewForm.tsx`/`types.ts` 未切换到三态；后端已兼容旧 payload。

## 当前实现基线

- auto 输入会把四类专项 reviewer 全部作为 Planner 的可用角色；Planner 当前可以提交 1–4 个 assignment，但 prompt 中同时出现“required dimension”等容易诱导多派 reviewer 的表述。
- `risk_hints` 来自预处理阶段的静态候选线索。它们继续用于 evidence 排序和 Planner 路由参考，但不是漏洞、finding 或必须启动 reviewer 的信号。
- reviewer profile 注册表目前包含 bug、security、performance、maintainability 四类专项 profile。profile 同时提供 Planner 描述、任务 prompt、工具 scope、finding details schema 和报告字段。
- assignment 最大数量保持现有值 **4**，本节点不提高、不降低，也不新增累计 token、金额或周期配额。
- 当前 ReviewLoop 的 Planner、reviewer、Judge 均使用已确认的 200k 上下文和既有 reviewer 预算；Conversation Agent 配置不变。

## 已确认目标

### Reviewer 模式

reviewer 选择模式固定为三种，模式之间互斥：

1. `auto`
   - Planner 只能在四类专项 reviewer 中选择。
   - 选择 1–4 个 reviewer。
   - prompt 要求按照证据和风险相关性选择“最少但足以覆盖本次变更”的集合。
   - 不因为某条 evidence 存在 `risk_hints` 就额外增加 reviewer。
   - `general` 不出现在 auto 的可用角色、manifest 路由说明或 assignment allowlist 中。

2. `special`
   - 用户至少选择一个专项 reviewer。
   - 只能选择四类专项 reviewer，不能混入 `general`。
   - 选择数量受现有 assignment max=4 约束。
   - Planner 仍使用 explicit 路由，必须为用户选择的每个专项 reviewer 生成一个 assignment。

3. `general`
   - 只运行一个通用 reviewer。
   - 不与专项 reviewer 组合。
   - 先沿用 Planner 链路；Planner 的可用角色只包含 `general`，且必须提交恰好一个 `general` assignment。
   - 后续是否让 general 模式绕过 Planner，另立评估任务，不在本节点实现。

### 通用 reviewer

- 新增一个正式的 `general` reviewer profile，复用现有 subagent、Runner、`review_submit`、validator、Judge 和报告链路。
- 通用 reviewer 不作为未注册 profile 的 fallback，不替换四类专项 reviewer。
- 为 `general` 定义独立的 prompt/template、工具 scope、context policy、finding details schema 和报告展示标签；不得复用某一个专项 reviewer 的语义约束冒充通用审查。
- 通用 reviewer 仍只能审查 diff evidence；定向 `read_file`/`grep` 只能补充上下文，accepted finding 继续受 changed-file 边界约束。
- 同一 run 不允许 general 与专项 reviewer 并存，因此本节点不新增跨 general/专项 finding 合并规则；既有同维度去重和跨专项结果处理保持不变。

## 实施步骤

### 1. 统一 reviewer mode 与输入契约

- 梳理 CLI、API、session metadata、Planner preparation 和 WebUI 当前 reviewer/focus 参数的生产者与消费者。
- 建立一个明确的 reviewer mode 语义：`auto`、`special`、`general`。保留必要的旧参数兼容解析，但在内部归一化为唯一模式和值集合。
- `auto` 的 allowed dimensions 固定为四类专项 profile。
- `special` 拒绝空选择、`general` 和未知维度，并沿用 assignment max=4。
- `general` 拒绝专项维度、多个 general assignment 和空选择。
- 错误返回应区分 mode 不合法、专项选择为空、general 与专项混用、超过 assignment max 等情况。

### 2. 调整 Planner prompt 与 manifest 路由语义

- 删除或改写 auto prompt 中没有明确定义的“every required dimension”表述。
- 明确：Planner 只提交实际需要运行的 reviewer；未选择的维度不会被程序补派。
- 明确“最少充分集合”原则，并说明 `risk_hints` 是候选路由线索，不是 reviewer 数量要求。
- 保留 manifest 的 `risk_hints`、`matched`、preview 和 coverage 信息，不删除现有证据排序能力。
- 为 `special` prompt 保留“每个用户选定专项维度都必须有 assignment”的 explicit 语义。
- 为 `general` prompt 提供单一角色约束：只能提交一个 `general` assignment，不能提交专项维度。

### 3. 注册通用 reviewer profile

- 在 profile registry 增加 `general`，并将 execution profile 的 scope 映射改为显式注册，不引入隐式 generic fallback。
- 新增通用 reviewer prompt，要求检查可复现的功能、边界、错误处理、安全、性能和可维护性问题，但只报告有证据和实际影响的问题。
- 为通用 finding 设计严格且足够通用的 details schema，并同步 `review_submit` 的工具说明、validator 校验和报告字段。
- 保持 reviewer 所需工具、terminal tool、结果保留、超时、30 次请求和最后提交轮契约一致。
- 明确 auto 专项角色集合与全部 profile 集合分离，避免注册 `general` 后它自动出现在 auto Planner 中。

### 4. 接线 ReviewLoop、报告和状态

- ReviewLoop 根据 normalized mode 创建 Planner receiver：auto 允许四专项子集，special 要求所选专项全集，general 只允许单一 general。
- dispatch 仍按 assignment 的 profile id 执行，不增加第二套 Runner 或独立状态机。
- report、ReviewRunState、snapshot 和 coverage 能显示 `general`，并继续记录实际选中的 reviewer。
- 不改变 assignment max=4、并发上限、reviewer 预算、Judge 批处理、终态和 handoff 契约。

### 5. 测试与验证

- normalizer/API/CLI 测试覆盖三种模式、非法混用、空 special、未知维度和 assignment max=4。
- Planner prompt 测试覆盖：auto 只出现四类专项、最少充分集合、未选维度不补派、risk_hints 不等于 reviewer 要求；special 的全覆盖约束；general 的单 assignment 约束。
- profile 测试覆盖 general 的 public profile、execution scope、required tools、terminal contract、prompt、details schema 和结果解析。
- ReviewPlanReceiver/ReviewLoop 测试覆盖 auto 单/多专项、special 至少一个专项、general 恰好一个，以及 general 不与专项组合。
- validator/finalizer/report 测试覆盖 general finding 的 details 校验、报告标签、空 findings 和 incomplete 语义。
- 保留并运行现有 diff-only、manifest、预算、Judge、CLI/API 和全量回归测试。
- 完成 `ruff check nanoreview/`、相关 pytest、全量 pytest 和 `git diff --check`；实现后再同步 architecture/roadmap 中受影响的产品边界。

## 验收标准

| 场景 | 必须结果 |
|---|---|
| auto | Planner 只能选择四类专项 reviewer，提交数量为 1–4，未选维度不被补派 |
| auto routing | prompt 要求最少充分集合，`risk_hints` 不会被描述为必须启动 reviewer |
| special | 用户至少选择一个专项 reviewer，只能选择四类专项，assignment max 仍为 4 |
| general | 只运行一个 general reviewer，Planner 只能提交一个 general assignment |
| mode isolation | general 与专项不能出现在同一个 run |
| general profile | general 有独立 prompt、scope、details schema、报告标签和结构化提交契约 |
| existing pipeline | 既有 Runner、Judge、validator、finalizer、snapshot 和 handoff 契约继续有效 |
| budget | 200k context、8192 output、30 requests、180 秒超时及最后提交轮不变 |
| diff boundary | general 和专项 reviewer 都只以 diff 为审查范围，accepted finding 仍须满足 changed-file 边界 |

## 不在本节点范围

- 不修改 assignment 最大数量。
- 不让 auto Planner 选择 general。
- 不支持 general 与专项 reviewer 组合。
- 不绕过 Planner；general 直派和 Kodus finder/verifier 结构另立评估任务。
- 不按文件分配 reviewer，不整体移植 Kodus，不恢复 repo-wide 或远端 review。
- 不修改 Conversation Agent、WebUI 视觉实现或无关的 session 状态机。

## 后续确认节点：Planner 预审查与 diff 分诊

状态：**已实施，待验收**（实施记录见本节末尾）。

本节点建立 Planner 的真实预审查职责：Planner 读取 frozen diff，按 evidence 做轻量风险分诊；程序根据分诊结果聚合并派发专项 reviewer。原有 reviewer mode、general reviewer 和 ReviewLoop 生命周期计划保留，本节点不回滚或替代它们。原计划中与本节点冲突的 `risk_hints` 保留/路由条目仅作为历史基线，实施时以本节点的删除决定为准。

### 已确认目标

- Planner 的唯一审查对象是 admission 时冻结的 diff evidence；不恢复 repo-wide 或远端 review，也不再提供独立的 raw diff 输入路径。
- 单文件 patch 小于 `8_000` tokens 时生成一个完整 diff evidence；达到或超过该阈值时沿用现有 hunk/语义切分生成多个 evidence。该阈值只决定 evidence unit 边界。
- 当全部 evidence 的总 token 数不超过现有 `direct_cap` 时，Planner 首轮注入全部 evidence 内容；超过 `direct_cap` 时首轮只注入 evidence index，Planner 通过受限的 `list_review_diff`、`read_review_diff` 工具分页读取。是否全量注入由总量判定，不由单文件 `8_000` 阈值单独决定。
- 工具只能读取程序预先生成的 diff evidence，不能读取任意路径、创建 evidence 或访问 diff 外的仓库代码。
- Planner 可以多次调用 `submit_review_decision`，每个 decision 可包含多个 evidence；完成后调用 `finish_review_triage`。
- decision 至少包含 `evidence_ids`、`risk_level`、`dimensions`、`focus` 和 `rationale`。`risk_level` 采用 `low`、`medium`、`high`、`critical`；`dimensions` 只能使用已注册的 reviewer 维度。
- 允许低风险 decision 使用空 `dimensions`，表示明确判断无需专项 reviewer；未读取或未提交 decision 的 evidence 记录为 `unexamined`。
- 一个 evidence 可以分配给多个 reviewer；程序按 dimension 聚合 decisions，生成 reviewer assignments，不让 Planner 同时维护第二份 assignments。
- Planner 的 risk decision、focus 和 rationale 只用于 reviewer 分配、任务上下文、运行审计和评测，不进入最终报告，不作为 finding 或 Judge 结论。
- `risk_hints` 从运行模型中完全删除：不再生成、存储、渲染、评分、排序或注入 prompt。`matched` 保留为普通 evidence 元数据，但不参与 evidence 排序或 reviewer 路由。
- 删除 risk hints 后，evidence 保持 frozen diff 的稳定顺序、hunk/unit 边界、稳定 ID、行范围、预算和 skipped 记录；不再按风险或查询命中排序。

### 实施步骤

1. **重塑 Planner 输入与工具**
   - 统一以 evidence 作为 Planner 输入：为 `direct` 模式注入全部 evidence 内容，为超出 `direct_cap` 的模式注册 `list_review_diff` 和 `read_review_diff`。
   - 复用现有单文件 `8_000` token 阈值和 hunk/语义切分规则，不新增另一套 Planner 专用 evidence 切分阈值。
   - 工具返回受 token 和 evidence 数量限制，读取内容必须来自 frozen diff 和授权 evidence ID。
   - Planner 保留唯一终态提交入口 `finish_review_triage`；工具失败、未知 ID 或非法参数在同一 AgentRun 内返回错误并允许重试。

2. **建立 triage decision 契约**
   - 新增结构化 `ReviewTriageDecision` 和 receiver，支持多次 decision 累积、重复 evidence 校验、维度校验和显式低风险 decision。
   - `finish_review_triage` 校验提交状态；不要求 Planner 读取全部 evidence，允许未读取 evidence 留在 `unexamined`。
   - Planner 超时、未完成 finish 或终端重试耗尽时，review 进入 planning error，不由程序根据关键词自动补派 reviewer。

3. **程序聚合 reviewer assignments**
   - 按 decision 的 `dimensions` 聚合 evidence、focus 和 rationale，为每个选中的 dimension 生成一个 assignment。
   - 低风险空维度不启动 reviewer；同一 evidence 可进入多个 reviewer；无 assignment 时明确记录无深入 reviewer/覆盖不足。
   - reviewer 只接收被分配的 diff evidence 和 Planner 分诊上下文，仍沿用现有 Runner、review_submit、validator、Judge 和 finalizer。

4. **删除 risk_hints 运行链路**
   - 删除 `CodeUnit`、`EvidenceReference`、manifest entry、prefetch、prompt、snapshot 和 runtime state 中的 `risk_hints`。
   - 删除静态风险关键词表、检测函数及 `_score_unit` 的风险加分和相关排序逻辑。
   - 读取旧 snapshot 时忽略历史 `risk_hints` 字段；新 snapshot 不再写入该字段。

5. **记录覆盖与审计信息**
   - 保存 decision、assigned、dismissed、unexamined 状态、Planner tool trace 和各 reviewer 的 evidence 覆盖范围。
   - 不把 Planner decision 混入 report findings；coverage/audit 只用于运行状态、排障和评测。

### 验收标准

| 场景 | 必须结果 |
|---|---|
| evidence 切分 | 单文件 patch 小于 `8_000` tokens 保持为一个完整 evidence；达到或超过阈值时按现有 hunk/语义规则切分 |
| direct 输入 | 全部 evidence 总 token 数不超过现有 `direct_cap` 时，Planner 首轮收到全部 evidence 内容 |
| paged 输入 | 全部 evidence 总 token 数超过 `direct_cap` 时，Planner 通过 list/read 工具分批读取 evidence，工具不能越过 frozen diff 边界 |
| decision | 一个 decision 可包含多个 evidence；一个 evidence 可进入多个 reviewer |
| 聚合 | 程序按 dimension 聚合 decision 并生成 assignments，不由 Planner 重复提交 assignments |
| 低风险 | `risk_level=low` 且空 dimensions 的 decision 不启动 reviewer，并记录为 dismissed |
| 未读证据 | 未读取或未提交的 evidence 记录为 unexamined，不自动补派 reviewer |
| 完成协议 | Planner 必须调用 finish；失败、超时或终端重试耗尽进入 planning error |
| hints 清理 | 新运行对象、prompt、snapshot、manifest 和测试中不存在 risk_hints；不再按风险或 query 命中排序 |
| reviewer 边界 | reviewer 只收到其 assignment 的 diff evidence，最终 finding 仍由 reviewer/validator/Judge 产生 |
| 报告边界 | Planner decision、risk_level、rationale 不进入最终 report findings |

### 不在本节点范围

- 不让 Planner 读取 diff 之外的任意仓库代码。
- 不让 Planner 创建、修改或伪造 evidence。
- 不根据静态关键词、risk_level 或未读 evidence 自动补派 reviewer。
- 不把 Planner 的预审查结论直接当作 finding、Judge verdict 或报告内容。
- 不改变四类专项 reviewer 的 profile、现有 reviewer 预算、并发上限和终态生命周期契约。

### 验证要求

- 新增 Planner tool/decision/聚合的单元测试和 ReviewLoop 契约测试。
- 增加 golden diff replay，验证 evidence 覆盖、低风险 dismiss、多 reviewer 聚合和 unexamined 记录。
- 保留并运行现有 diff-only、reviewer profile、validator、Judge、finalizer、snapshot、CLI/API 和全量回归测试。
- 完成 `ruff check nanoreview/`、相关 pytest、全量 pytest 和 `git diff --check`。

### 实施记录（2026-10-09）

本节点把 Planner 从“一次性提交 assignments”改成“对 frozen diff 做真实分诊”。原有
reviewer mode、general reviewer 与 ReviewLoop 生命周期契约全部保留。

- 新增 `review/planning/triage.py`：`TriageReceiver` 是 Planner 分诊的唯一契约。
  `submit` 校验 evidence ID 授权、同一 evidence 只能分诊一次、维度白名单、
  `risk_level ∈ {low,medium,high,critical}`，以及**非 low 必须给维度**
  （用户已确认：拒绝并要求补维度，而不是静默当作 dismissed）。`finish` 是唯一
  终态入口；`special`/`general` 下用户所选维度必须至少被一条 decision 命名，
  否则 `finish` 被拒——**用户选择靠拒绝 finish 强制，而不是给 reviewer 加塞 evidence**。
  assignment 由程序按维度聚合（`assignments()`），覆盖状态由程序派生
  （`assigned`/`dismissed`/`unexamined`），Planner 不再维护第二份 assignment。
- `agent/tools/review_plan.py` 重写：删除 `submit_review_plan`/`ReviewPlanReceiver`，
  改为 `submit_review_decision`、`finish_review_triage`、`list_review_diff`、
  `read_review_diff`。reader 只由 frozen evidence 构造，无文件系统访问。
  reader 的分页预算取 `min(MAX_DIFF_READ_CHARS, run 的 max_tool_result_chars)`，
  避免 `AgentRunner` 在 reader 声称“已读”之后再截断页面（自查发现的真实证据丢失）。
- `ReviewEvidenceBundle.input_mode`（`direct`/`paged`）由预处理 mode 决定；
  manifest 相应分两种形状：direct 内联 unit 全文，paged 只渲染索引并提示调用
  `read_review_diff`。manifest 版本升至 `evidence-manifest/2`，
  `reference_priority` 只按 main/related + 稳定 ID 排序，预算改为按顺序取前缀并
  只裁剪首个超大 unit（不再按风险排序）。
- `risk_hints` 运行链路整体删除：`_RISK_TERMS`/`_RISK_HINT_PATTERNS`/`detect_risk_hints`/
  `CodeUnit.risk_hints`/`_score_unit`/`EvidenceReference.risk_hints`/manifest 字段与渲染/
  prefetch 迁移全部移除；`_apply_main_budget` 与 dense-backfill 改为按 frozen 顺序截尾。
  `matched` 保留为纯展示元数据。preview 采样的 risk-hit 优先级改为 query 命中优先级。
- Planner 取消 `tool_choice` 强制（现在要读/决策/finish，是自由回合），请求上限
  `7 -> 24`；`finish_review_triage` 作为唯一 terminal tool，保留轮仍保证一次提交。
- `ReviewRunState.triage` + coverage/snapshot `planner_triage` 记录分诊审计；
  **decision/risk_level/rationale 不进入 report findings**。规划主动不派 reviewer 时
  （全部 dismissed/unexamined），报告显式输出 Triage Outcome 并在 Checks Performed
  标明“no reviewer was dispatched by planning”而非 `incomplete`；该 run 仍以
  `completed` 结算并产出 report artifact。
- 自查/审查修复：
  - `_EXCERPT_CHAR_LIMIT` `16_000 -> 40_000`（单文件完整 diff unit 最大约 32k 字符，
    原值会让 direct 模式静默截断 unit 尾部）；reader 分页预算对齐运行期
    `max_tool_result_chars`，避免 `AgentRunner` 在 reader 声称“已读”之后再截断页面。
  - **删除 `MAX_DIFF_UNIT_CHARS` 隐藏的 per-unit 12k 上限**：unit 超过页面预算时改为在
    正文内追加显式“不完整”标记（按剩余预算降级为短标记），并为“未展示的 ID”预留固定
    空间，避免尾部注释把整页顶出预算后由 `_bounded` 盲截掉首个 unit 的标记。
  - **`read_review_diff` 不再隐藏丢弃 ID**：schema 声明 `maxItems=8`，此前只返回
    `DEFAULT_DIFF_READ_LIMIT=4` 且无任何标记，Planner 会分诊 4 个自己没读过的 unit；
    现在按请求全量返回，超预算部分显式列出 ID。
  - **Planner 的 prose 回合不再消耗 terminal 提交预算**：新增
    `AgentRunSpec.prose_retry_limit`，为自由回合单独计散文配额；未声明时保持原有共享
    语义（强制工具的 reviewer/Judge 行为不变）。Planner `prose_retry_limit=8`，
    `terminal_retry_limit` 仍为 5。
- 验证：`ruff check nanoreview/` 保持基线 45 项；`tests/` 全量 1053 passed；
  `git diff --check` 干净。新增 `tests/review/test_triage.py`（分诊契约 + 有界读取 +
  “绝不返回无标记的半个 unit”）与 loop 侧 dismissed/unexamined/special-pin 用例。
- 本节点未做：golden diff replay 尚未补（已由 triage 单元测试 + loop 契约测试覆盖
  evidence 覆盖、dismiss、多维度聚合与 unexamined）；`review-webui` 的
  `ReviewRoutingMode` 仍是 `auto|explicit`，留待前端节点。
