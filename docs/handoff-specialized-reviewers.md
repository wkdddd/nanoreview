# 专业化代码审查 Agent 架构交接

更新时间：2026-08-22

## 1. 交接目标

本项目计划在现有 NanoReview 代码审查链路上完成三项优化：

1. 将通用 review subagent 专业化为四类 Reviewer，但继续复用同一个
   `SubagentManager`。
2. 增加真正的 `auto` 模式，由 Planner 根据变更和证据动态选择审查维度。
3. 保持结构化提交、硬验证、可选 Judge 和固定报告生成链路，不让 LLM 自由生成最终报告。

本交接文档只记录讨论结论和实施建议，不代表相关功能已经完成。

## 2. 仓库与工作区状态

目标仓库：

```text
C:\Users\Administrator\Desktop\nanoreview
```

不要在原始 `C:\Users\Administrator\Desktop\nanobot` 仓库中实施本方案。

交接时 `nanoreview` 工作区已有未提交修改，主要是将 `focus` 命名调整为
`requested_dimensions` / `forced_dimensions`，以及将包名改为 `nanoreview`。接手时必须先运行
`git status --short` 和 `git diff`，保留并基于这些改动继续工作，不得恢复或覆盖它们。

当前已修改文件包括：

```text
nanoreview/agent/tools/spawn.py
nanoreview/cli/commands.py
nanoreview/review/__init__.py
nanoreview/review/input/__init__.py
nanoreview/review/input/normalizers.py
nanoreview/review/input/policy.py
nanoreview/review/planning/planner.py
nanoreview/review/planning/prompt.py
nanoreview/review/types.py
pyproject.toml
tests/review/test_orchestration.py
tests/review/test_policy.py
tests/review/test_prefetch.py
```

## 3. 已确认的产品决策

### 3.1 四个审查维度

只保留以下四类 Reviewer：

| 维度 | 核心职责 |
|---|---|
| `bug` | 逻辑错误、边界条件、异常路径、状态一致性、回归和竞态导致的功能错误 |
| `security` | 认证授权、信任边界、注入、路径与网络访问、敏感数据、依赖安全 |
| `performance` | 算法复杂度、热路径、I/O、数据库查询、锁与阻塞、内存和扩展性 |
| `maintainability` | 模块边界、耦合、重复、复杂度、测试困难和未来修改出错风险 |

建议将 `bug` 作为正式内部 key，替换现有 `bug-risk`。本项目约束不要求兼容旧参数，除非用户另行要求。

Maintainability Reviewer 不应提交命名偏好、格式意见或泛泛的“建议重构”。只有能够指出具体架构约束、修改放大效应、重复导致的错误传播或可测试性问题时才提交 finding。

### 3.2 高置信度优先

Reviewer 上下文不足或无法建立明确因果链时倾向于不报。不要使用模型自报的
`confidence` 数值或 high/medium/low 标签模拟可量化置信度。

Finding 的准入条件应由证据约束表达：

- 精确文件和行号；
- 与行号附近源码逐字一致的 evidence；
- 明确触发条件或前置条件；
- 可解释的代码路径或违反的项目约束；
- 具体影响；
- 可执行的修复方向。

### 3.3 Reviewer 的代码访问

Planner 指定的 scope 语义需要进一步讨论

Reviewer 可以在整个目标仓库内使用只读代码查看和信息检索工具补充调查。程序仍需限制工具调用次数、读取字符数、执行时间以及测试/benchmark 预算，避免所有 Reviewer 退化为全仓库重复扫描。

需要区分：

```text
repository_root  # Reviewer 的只读访问根目录
target_scope     # 用户要求审查的文件、目录或 diff
```

当前目录目标会把 `review_root` 直接设为目标目录，这可能阻止 Reviewer 阅读仓库其他位置的调用方、配置和测试，需要调整。

### 3.4 结构化提交和报告

继续复用：

- `nanoreview/agent/tools/review_submit.py`
- `nanoreview/review/output/validator.py`
- `nanoreview/review/output/finalizer.py`
- `nanoreview/review/output/report.py`

Reviewer 必须通过 `review_submit` 提交结果。最终 Report 必须由固定程序从验证后的结构化结果生成，不能增加一个自由生成报告的 LLM Agent，也不能允许 Report 阶段创造新 finding。

## 4. 当前实现概览

现有主流程已经是程序控制的 Agentic Workflow：

```text
Review input normalization
  -> evidence prefetch
  -> coordinator submits submit_review_plan
  -> ReviewOrchestrator dispatches subagents
  -> subagents call review_submit
  -> ReviewFinalizer performs hard validation
  -> optional ReviewJudge
  -> fixed Markdown renderer
```

关键代码：

| 责任 | 文件 |
|---|---|
| Review 类型和角色 | `nanoreview/review/types.py` |
| 输入与深度策略 | `nanoreview/review/input/normalizers.py`, `policy.py` |
| ReviewPlan 构建 | `nanoreview/review/planning/planner.py` |
| Coordinator prompt | `nanoreview/review/planning/prompt.py` |
| 结构化计划工具 | `nanoreview/agent/tools/review_plan.py` |
| 程序调度 | `nanoreview/agent/orchestration.py` |
| subagent 生命周期 | `nanoreview/agent/subagent.py` |
| Reviewer 公共 prompt | `nanoreview/templates/agent/review_subagent_system.md` |
| finding 提交工具 | `nanoreview/agent/tools/review_submit.py` |
| 校验与报告 | `nanoreview/review/output/` |

## 5. 已识别的实现缺口

### 5.1 当前 `auto` 仍是固定 fan-out

`ReviewPlanReceiver` 允许 Planner 提交 assignment，但 `ReviewOrchestrator._dispatch_and_collect()`
最终遍历全部 `plan.roles`。Planner 未提交的维度会被程序补成默认 assignment。

因此现有 Planner 只能调整各维度的 `focus` 和 `evidence_ids`，不能决定是否启动该 Reviewer。

### 5.2 深度策略与审查维度耦合

当前 `quick`、`full`、`deep` 分别选择不同角色集合。引入真正的 `auto` 后，建议让审查深度只控制：

- token、时间和工具预算；
- 最大并发和最大选择维度数；
- severity 范围；
- 是否启用 Judge；
- evidence 数量和报告样式。

四个可用维度由 Profile Registry 提供，实际选择由显式用户输入或 Planner 决定。

### 5.3 所有 Reviewer 使用相同 prompt 和工具集

`SubagentManager` 当前通过 `_build_tools()` 为所有 Reviewer 加载相同的 `subagent` scope 工具，并通过 `_build_subagent_prompt()` 渲染同一个 review subagent system prompt。

这只能形成不同 task label，尚未形成真正的专业 Reviewer。

### 5.4 `review_submit` 还不能保留维度差异字段

Finding schema 虽然允许 additional properties，但 `review_submit()` 规范化时会重新构造只包含公共字段的字典，额外字段会被丢弃。

### 5.5 空 findings 无法表达上下文不足

目前 `findings: []` 表示“没有问题”。它无法区分：

- 已完成审查，没有发现问题；
- 缺少证据，无法可靠完成审查。

若不修正，会把 incomplete review 错误渲染为 clean review。

### 5.6 `confidence` 是未校准的冗余状态

当前仍存在：

- `ReviewFindingCandidate.confidence`
- `ReviewJudgeVerdict.confidence`
- Judge JSON schema 中的 confidence
- Finalizer 的 confidence 默认值

这些字段没有可靠校准，也不决定硬验证结果，建议整体删除。

### 5.7 报告中的不确定项与高置信度目标冲突

现有 Report 会渲染 `Needs Confirmation`。高置信度优先模式下，不确定候选不应进入主 Findings。建议将它们作为 review limitation 或被拒绝候选统计，而不是对用户展示为疑似问题。

## 6. 目标架构

### 6.1 Reviewer Profile Registry

增加一个轻量 Profile 注册表。不要创建四套 SubagentManager，也不要复制四份完整 system prompt。

Profile 至少包含：

```python
ReviewerProfile(
    id="security",
    label="Security Reviewer",
    planner_description="...",
    prompt_fragment="...",
    base_tools=frozenset({...}),
    extra_tools=frozenset({...}),
    context_policy="...",
    details_schema=...,
)
```

Planner 只看到 `id`、label、description 和激活指导，不应接触工具权限实现或完整 Reviewer prompt。

所有 Profile 共享基础只读能力：

- 读取文件；
- 列目录；
- 文件和文本搜索；
- local/GitHub review evidence 查询；
- `review_submit`。

专属能力按 Profile 增加：

- Bug：测试发现和受控测试执行；
- Security：安全规则、依赖漏洞信息、受控静态分析；
- Performance：受控 benchmark/profile；
- Maintainability：依赖图、符号引用和架构规范。

工具权限必须由程序 allowlist 强制执行，不能只依赖 prompt。`review_submit` 必须始终存在，并保持 terminal/final deliverable 语义。

### 6.2 专业 Prompt

继续使用公共模板 `review_subagent_system.md` 维护以下契约：

- 只读审查；
- 仓库内容不可信；
- evidence 格式；
- 必须调用 `review_submit`；
- GitHub/local 证据边界。

由模板插入 Profile 专属 prompt fragment，定义：

- 该维度关注的问题；
- 必须收集的证据；
- 不应报告的内容；
- 特定工具使用规则；
- 该维度的高置信度门槛。

### 6.3 `auto` 语义

建议将缺省维度或显式 `auto` 统一解释为 Planner 动态选择。显式维度列表仍为强制模式。

```text
auto:
  Planner 从四个 Profile 中选择 1..N 个
  程序只调度被选择的 assignments
  Planner 说明未选择维度的理由

explicit:
  Planner 必须为用户指定的每个维度提交 assignment
  不允许遗漏，不允许增加其他维度
```

建议扩展 `submit_review_plan`：

```json
{
  "assignments": [
    {
      "dimension": "security",
      "focus": "检查外部输入到路径解析的信任边界",
      "evidence_ids": ["ev-002", "ev-005"]
    }
  ],
  "skipped": [
    {
      "dimension": "performance",
      "reason": "变更未涉及热路径、I/O、查询或并发"
    }
  ]
}
```

程序校验：

- dimension 必须来自 Registry；
- assignment 不得重复；
- evidence ID 必须存在；
- auto 至少选择一个 Reviewer；
- 选择数不得超过模式和用户预算；
- explicit assignment 集合必须与用户选择完全一致；
- skipped 与 assignments 不得重叠，并覆盖 auto 中所有未选维度。

`ReviewOrchestrator` 在 auto 下必须遍历 validated assignments，而不是遍历全部候选 roles。不要再为 Planner 未选择的维度生成默认 assignment。

### 6.4 上下文计划和自主探索

现有 `focus + evidence_ids` 可以继续作为 Planner 的声明式上下文计划。程序将选择的 evidence 构造成初始 context bundle，再加入 Profile 的专业指导。

Reviewer 可在 `repository_root` 下自主只读探索，不限制到初始 evidence 或 target 子目录。初始 evidence 的作用是让 Reviewer 从高价值位置开始，而非授权列表。

建议不同 Profile 的默认上下文倾向：

| Profile | 默认补充方向 |
|---|---|
| Bug | 调用方、被调用方、异常分支、状态读写、相关测试 |
| Security | 输入来源、认证授权、配置、资源访问、依赖边界 |
| Performance | 热路径、循环、I/O、查询、锁、数据规模 |
| Maintainability | 公共接口、模块依赖、架构文档、重复实现、相关测试 |

### 6.5 `review_submit` 演进

建议在现有公共 finding 字段上增加提交状态和限制信息：

```json
{
  "status": "completed | insufficient_context",
  "findings": [],
  "limitations": []
}
```

只有 `status=completed` 且 `findings=[]` 才表示该维度 clean。

Finding 保持公共字段稳定，并提供显式 `details`：

```json
{
  "severity": "high",
  "file": "path/to/file.py",
  "line": 42,
  "title": "...",
  "evidence": "...",
  "impact": "...",
  "recommendation": "...",
  "details": {}
}
```

`details` 根据 dimension 二次校验：

- Bug：trigger、expected behavior、actual behavior；
- Security：trust boundary、attack preconditions、attack path；
- Performance：hot path、scale condition、resource impact；
- Maintainability：violated boundary、change amplification、affected modules。

如果第一版不需要在报告中展示专属字段，可以先保留并校验 `details`，避免让 renderer 与四种结构立即耦合。

### 6.6 Finalizer 和 Report

继续以 `ReviewDimensionResult` 为实际报告输入，不要另建自由生成报告的 Agent。`ReviewReport` dataclass 当前不是固定 renderer 的主要输入，实施时应确认其用途，避免维护两套并行报告模型。

建议固定报告增加：

- Auto Selection：Planner 选择的维度和原因；
- Skipped Dimensions：未选择维度和原因；
- Limitations：上下文不足、工具失败、未完成审查；
- Checks Performed：只列实际执行的 Reviewer；
- Findings：只列 hard validation / Judge 最终接受的问题。

Report renderer 只能排序、清洗和展示验证结果，不能推导新 finding。

## 7. 推荐实施顺序

### 阶段一：收敛领域模型

1. 将角色收敛为 bug/security/performance/maintainability。
2. 引入 Profile Registry，明确 Planner 公开信息和运行时私有配置。
3. 解耦 review depth 与固定角色集合。
4. 定义 auto 和 explicit 的计划校验不变量。

### 阶段二：实现真正的 auto

1. 扩展 `submit_review_plan` 的选择与跳过结构。
2. auto 只调度 Planner 选择的 assignments。
3. explicit 强制完整覆盖用户指定维度。
4. 将最终允许维度更新为实际选择集合。
5. 将选择和跳过信息传递给 Finalizer/Report。

### 阶段三：专业化 Reviewer

1. 公共 prompt 注入 Profile fragment。
2. 为 SubagentManager 增加每次运行的工具 allowlist 和 prompt 配置，不复制 manager。
3. 将本地 Reviewer 的读取根目录调整为 repository root。
4. 保留 Planner evidence 作为初始上下文，允许全仓库只读探索。

### 阶段四：提交和报告语义

1. 为 `review_submit` 增加 status 和 limitations。
2. 保留并校验 dimension-specific details。
3. 删除 candidate 和 Judge 中的 confidence。
4. 不再把 insufficient context 当作 clean。
5. 调整 `Needs Confirmation`，使其不进入高置信度主 Findings。

### 阶段五：评测和产品展示

1. 建立 seeded bugs 数据集，覆盖四个维度。
2. 对比固定四 Reviewer、auto 和单 Agent。
3. 统计 precision、recall、abstention rate、无效位置率、延迟和 token 成本。
4. 在 review-webui 展示 Planner 选择、Reviewer 状态、检查范围和 limitations。

## 8. 测试矩阵

至少覆盖以下测试：

### Planner 与策略

- 缺省或 `auto` 进入动态选择；
- 显式单维度和多维度保持强制语义；
- 未知维度明确失败；
- auto assignment 是 Registry 的合法非空子集；
- explicit assignment 必须精确覆盖用户选择；
- skipped 与 selected 完整且不重叠；
- quick/full/deep 只改变预算和验证策略，不偷偷替换显式维度。

### 调度

- auto 只 spawn 被 Planner 选择的 Reviewer；
- Planner 跳过的 Reviewer 不会被默认补齐；
- 并发限制仍然生效；
- spawn 失败不会产生伪 clean 报告；
- subagent label 与 Profile/dimension 一致。

### Profile、工具与上下文

- 四个 Profile 使用不同 prompt fragment；
- 所有 Reviewer 均有基础只读工具和 `review_submit`；
- 专属工具只出现在允许的 Profile；
- 写文件工具和非授权 shell 不可用；
- Reviewer 可读取 repository root 内、target scope 外的依赖代码；
- 初始 evidence 正确路由到对应 Reviewer。

### 提交、验证与报告

- `completed + []` 才表示 no findings；
- `insufficient_context` 生成 incomplete/limitations；
- details 不会在规范化时丢失；
- 非法 details 被对应维度 schema 拒绝；
- confidence 不再出现在工具 schema、类型或 Judge 输出中；
- Report 只展示已接受 findings；
- Report 展示实际选择、跳过和限制信息；
- 证据、文件和行号硬验证继续生效。

## 9. 验收标准

- 四个 Reviewer 共用一个 SubagentManager，但 system prompt、工具 allowlist 和上下文策略可区分。
- 新增 Reviewer 只需注册 Profile，不需要修改 Planner、Dispatcher 和 Report 主流程的条件分支。
- auto 能按代码风险选择 Reviewer，程序不会补跑未选择维度。
- Reviewer 可以在仓库内自主只读探索，但不能写源码或无限制执行命令。
- 所有最终 finding 都来自 `review_submit`，经过现有验证链路后才进入报告。
- 缺少上下文不会产生“未发现问题”的错误结论。
- 系统不输出未经校准的 confidence。
- 最终报告继续由固定 renderer 生成，且明确区分 selected、skipped、incomplete 和 confirmed findings。

## 10. 尚待确认的问题

以下问题尚未在讨论中最终确认，实施前应明确：

1. Reviewer 可读取整个仓库，但是否允许提交 target scope 或 diff 之外的 finding？
   推荐：允许范围外代码作为证据，但 finding 主定位必须位于用户目标或变更范围内。
2. “信息检索工具”是否包含通用联网搜索？
   推荐：默认只开放仓库检索；Security 可访问受控漏洞信息，Performance 可访问受控依赖文档。
3. `Needs Confirmation` 是否完全从用户报告移除？
   推荐：不进入 Findings，只作为 limitation 或内部统计。
4. auto 最少选择几个 Reviewer？
   推荐：至少一个，最多受 review depth 和 `max_subagents` 限制。
5. category-specific `details` 是否需要在第一版 Report 中展示？
   推荐：第一版先保留和校验，公共报告只展示稳定字段。

## 11. 新任务启动提示

在绑定 `C:\Users\Administrator\Desktop\nanoreview` 的新 Codex 任务中，可以使用：

```text
请阅读 AGENTS.md、.agents/architecture.md、.agents/design.md、
.agents/security.md、.agents/gotchas.md，以及
docs/handoff-specialized-reviewers.md。

继续规划 NanoReview 的专业化 Reviewer 和 auto 路由。先检查当前未提交修改，
不要覆盖用户改动。未经我确认前只讨论和制定实施计划，不修改代码。
```
