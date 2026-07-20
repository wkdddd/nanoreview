# Common Gotchas

## Formatting and Repository History

Never run `ruff format` across the repository. Restrict any formatting to files changed for the current task and only when formatting is required. Avoid unrelated churn that obscures blame and review history.

## Windows Compatibility

Windows is supported. Use `pathlib.Path`, never assume `/` path separators, and set PowerShell output encoding to UTF-8 for commands that handle multilingual text. The current execution tool launches commands through the platform shell; do not assume Bash or PowerShell syntax is universally available.

## Configuration Environment Variables

`nanobot/config/loader.py` resolves `${VAR}` references through `resolve_config_env_vars`. This is not shell default-value syntax. A missing variable raises `ValueError`; retain that explicit failure instead of silently substituting a value.

## Prompt and Context Surfaces

Templates in `nanobot/templates/`, tool descriptions, skills and replayed session history affect LLM behavior as directly as Python code. Keep changes narrow and test them where possible. Do not teach the model to reproduce internal markers, raw paths or tool-call transcripts.

Anything stored in session metadata or memory can persist. Sanitize and limit it before it becomes an example that future model turns imitate.
