# Gotchas

- 禁止仓库级 `ruff format`；仅在必要时格式化本次修改文件。
- Windows 路径与 shell 语法不能按 Bash 假定；路径使用 `Path`，多语言命令启用 UTF-8。
- `config/loader.py` 的 `${VAR}` 经 `resolve_config_env_vars` 解析，不支持 shell 默认值语法；缺少变量应明确失败。
- 模板、工具说明、skills 和重放历史均影响模型行为；不要把内部标记、工具调用回显或无关路径教给模型。
- 读取对话历史与恢复中断任务是不同能力；勿因禁止 review resume 而删除历史回放。
- 未合并 worktree 的提交与未提交修改分开核查；其测试和进展记录不可作为主工作区已实现的证据。
- 黑板写入只在 coordinator 侧发生：`ReviewLoop` 拿到的 `ReviewBoardWriter` 没有可达的可变 board 引用，新增 review 状态字段要加 store 的 mutator + writer 方法，不要在 loop 里直改对象。
- 黑板锁只在记结果的瞬间持有，**不得**跨模型或工具调用；把 `async with store._lock_for()` 包住一次 agent run 会造成整个 review 串行化。
- `reviewer_settled` 的 `completed` 是终态：重复 settle 不改状态但仍累加 usage。需要改状态时只能改“首次 settle 前”的逻辑。
- review 角色现在没有提交工具：新增/修改 prompt 时不要写 `review_submit`、`submit_review_decision`、`finish_review_triage`，模型会去找不存在的工具。
- 结构化输出被拒**不能**当成功：`structured_output is None` 时必须走失败分支，不能把 `final_content` 里的散文当成 findings。
- `OutputContract.parse` 必须是纯函数；写成有状态校验器会让“上一次被拒”在重试时变成通过。
- `tests/` 下没有 `__init__.py`，`--import-mode=importlib` 不把测试目录塞进 `sys.path`：不要写跨目录的共享测试 helper 模块，helper 直接定义在各测试文件里。
