# Debug Constraints

## 日志类型与路径

运行时数据默认位于 `C:\Users\Administrator\.nanoreview`（代码中使用 `Path.home()/.nanoreview`；换机器时用 `%USERPROFILE%/.nanoreview` 表示）。两个日志目录用途不同，不要混用：

- `C:\Users\Administrator\.nanoreview\logs\<workspace_id>\<run_id>.jsonl`：网关/后端进程日志。`workspace_id` 是解析后 workspace 路径 SHA-256 的前 32 位，`run_id` 是 UTC 启动时间加随机后缀；每个网关生命周期通常对应一个文件。每行是运行日志对象：`timestamp`（UTC）、`level`、`channel`、`message`、`exception`。包含启动、WebSocket 监听、状态机耗时、工具/审查流程、Provider 错误和异常堆栈；排查“服务是否启动、后端为何失败”时先看这里。
- `C:\Users\Administrator\.nanoreview\webui\websocket_<chat_id>.jsonl`（也可能是其他安全化 session key）：WebUI 主会话 transcript，不是 Loguru 运行日志。每行是可回放的前端事件，常见 `event` 有 `user`、`delta`、`message`、`reasoning_delta`、`stream_end`、`turn_end`、`permission_request/response`；`message` 可能包含 `kind=progress/tool_hint/review_report`、`tool_events` 以及 `review_*` 元数据。用于恢复聊天、确认前端收到的事件和审查参数；文件可能包含用户输入、审查内容和模型输出。
- `C:\Users\Administrator\.nanoreview\webui\<sha256(session_key)[:32]>.subagents.jsonl`：Subagent trace sidecar，与同一 session 的 transcript 配套。只持久化卡片恢复所需的 `started`、`reasoning_delta`、`tool`、`finished` 事件；推理文本会脱敏并限长（单 subagent 约 4,000 字符，单文件约 256 KiB），不包含完整任务描述/最终结果。排查“子代理卡片不显示、状态未结束”时看这里。
- `C:\Users\Administrator\.nanoreview\webui\<safe_session_key>.json`：旧版 WebUI 快照格式；新代码主要使用上述 JSONL transcript，只有遇到历史会话兼容问题时才检查此类文件。

推荐关联方式：先从 WebUI transcript 的 `chat_id`/文件名取得会话 ID，再在 `logs` 中搜索 `websocket:<chat_id>`、错误文本或 `trace_id`；最后用同一 `session_key` 的 SHA-256 前 32 位定位 `.subagents.jsonl`。`logs` 里的 `timestamp` 是 ISO UTC，WebUI 的 `createdAt` 是 Unix 毫秒，比较时间时先统一时区/单位。JSONL 按行解析，单行损坏时不要把整个文件当作无效。
