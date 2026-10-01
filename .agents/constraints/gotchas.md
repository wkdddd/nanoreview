# Gotchas

- 禁止仓库级 `ruff format`；仅在必要时格式化本次修改文件。
- Windows 路径与 shell 语法不能按 Bash 假定；路径使用 `Path`，多语言命令启用 UTF-8。
- `config/loader.py` 的 `${VAR}` 经 `resolve_config_env_vars` 解析，不支持 shell 默认值语法；缺少变量应明确失败。
- 模板、工具说明、skills 和重放历史均影响模型行为；不要把内部标记、工具调用回显或无关路径教给模型。
- 读取对话历史与恢复中断任务是不同能力；勿因禁止 review resume 而删除历史回放。
- 未合并 worktree 的提交与未提交修改分开核查；其测试和进展记录不可作为主工作区已实现的证据。
