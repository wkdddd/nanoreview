# Debug

## 日志类型与路径

以下为主工作区当前路径，迁移后按代码核对。运行时根目录为 `Path.home()/.nanoreview`，Windows 对应 `%USERPROFILE%/.nanoreview`。日志与展示历史用途不同：

- `logs/<workspace_id>/<run_id>.jsonl`：后端 Loguru 日志，查启动、流程、Provider 错误及异常。workspace id 是解析后路径 SHA-256 前 32 位，run id 是 UTC 启动时间加随机后缀。
- `webui/websocket_<chat_id>.jsonl`：WebUI transcript，查入站/出站事件和报告交付；可能包含用户与模型正文，不等于执行恢复日志。
- `webui/<sha256(session_key)[:32]>.subagents.jsonl`：子代理卡片 trace，查 `started/tool/finished`。推理文本脱敏且限长，单 agent 约 4,000 字符、单文件约 256 KiB。
- `webui/<safe_session_key>.json`：旧快照，仅历史问题排查时读取。

推荐关联方式：先从 WebUI transcript 的 `chat_id`/文件名取得会话 ID，再在 `logs` 中搜索 `websocket:<chat_id>`、错误文本或 `trace_id`；最后用同一 `session_key` 的 SHA-256 前 32 位定位 `.subagents.jsonl`。`logs` 里的 `timestamp` 是 ISO UTC，WebUI 的 `createdAt` 是 Unix 毫秒，比较时间时先统一时区/单位。JSONL 按行解析，单行损坏时不要把整个文件当作无效。
