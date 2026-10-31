# Debug

## 日志类型与路径

以下为主工作区当前路径，迁移后按代码核对。运行时根目录为 `Path.home()/.nanoreview`，Windows 对应 `%USERPROFILE%/.nanoreview`。日志与展示历史用途不同：

- `logs/<workspace_id>/<run_id>.jsonl`：后端 Loguru 日志，查启动、流程、Provider 错误及异常。workspace id 是解析后路径 SHA-256 前 32 位，run id 是 UTC 启动时间加随机后缀。
- `webui/websocket_<chat_id>.jsonl`：WebUI transcript，查入站/出站事件和报告交付；可能包含用户与模型正文，不等于执行恢复日志。
- `webui/<sha256(session_key)[:32]>.subagents.jsonl`：子代理卡片 trace，查 `started/tool/finished`。推理文本脱敏且限长，单 agent 约 4,000 字符、单文件约 256 KiB。
- `webui/<safe_session_key>.json`：旧快照，仅历史问题排查时读取。

推荐关联方式：先从 WebUI transcript 的 `chat_id`/文件名取得会话 ID，再在 `logs` 中搜索 `websocket:<chat_id>`、错误文本或 `trace_id`；最后用同一 `session_key` 的 SHA-256 前 32 位定位 `.subagents.jsonl`。`logs` 里的 `timestamp` 是 ISO UTC，WebUI 的 `createdAt` 是 Unix 毫秒，比较时间时先统一时区/单位。JSONL 按行解析，单行损坏时不要把整个文件当作无效。

## 上下文与长期记忆

- 上下文治理按 run 打一条 trace：`context.compact.start` / `context.compact.done` / `context.compact.failed` / `context.compact.degraded` 共用同一 `trace={}` 与 `scope=`（session key 或角色），`context.unsent_read.replaced` 记录被替换的 `tool_call_id`，`context.limit` 是 run 以 `context_limit` 结束的唯一入口。先按 `trace=` 串起来，再看 `accepted=`/`measured=`/`budget=` 三个数。
- `source=` 说明本次估算依据（provider counter / tiktoken / chars），`fallback_chars_per_token=4` 出现时估算只是量级参考，接近预算的判定不要当精确值。
- reviewer/Judge 的生效参数不写在日志里，而是 run snapshot 的 `models` 与 `budget` 段（`review-snapshots/<run_id>.json`）：排查「为什么这批没过」先读它，不要猜配置。
- 长期记忆：`dream.start` / `dream.done`（`applied=` / `advanced_to=`）/ `dream.failed`（`stop_reason=` 与原因）/ `dream.skip`（`reason=running|no_entries`）/ `dream.cancelled` 覆盖一次 Dream 的全过程；`memory.journal.appended` 是唯一的写入点。`dream.state.read_failed`/`corrupt` 表示 `memory/dream-state.json` 损坏，此时 cursor 归零会让历史条目重放（不会丢数据，但可能重复）。
- 排查「记忆没更新」的顺序：`memory/history.jsonl` 是否新增 → `dream-state.json` 的 `last_cursor` 是否落后 → 最近一条 `dream.*` 日志的 `status`。`applied=false` 且 cursor 前进属于正常结果（模型判定无需改动）。
