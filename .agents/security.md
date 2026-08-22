# Security Boundaries

## Filesystem and Workspace

Tool paths must be resolved through the established filesystem/path utilities and checked against the active workspace when restriction is enabled. Any additional root must have a capability-specific grant: use read-only roots for read operations, writable roots only for intentional writes, and exact-file allowlists for narrowly scoped writes.

`restrict_to_workspace` in the execution tool is an application-level guard. It does not replace OS-level or container-level isolation. Extend execution sandboxing through `nanoreview/agent/tools/sandbox.py`; do not create ad hoc command wrappers.

## Network and SSRF

Every outbound HTTP request created by an agent tool must use `nanoreview.security.network.validate_url_target`. Revalidate redirect destinations with `validate_resolved_url`. The default policy blocks loopback, private, link-local, CGNAT and cloud metadata ranges.

HTTP/SSE MCP URLs belong to the same rule. Private endpoints require an explicit `tools.ssrf_whitelist` entry; stdio MCP servers are not HTTP requests. Do not introduce direct HTTP calls that bypass these checks.

## Persistent Prompt Data

Memory, session history, template inputs and tool results can be replayed into future model calls. Bound their size and remove secrets, raw fallback dumps, local media paths, timestamps and internal tool-call markers unless they are essential user context. Do not weaken atomic session persistence in `nanoreview/session/manager.py`.

WebUI transcripts and subagent traces under `nanoreview.utils.webui_transcript` and `nanoreview.utils.subagent_trace` are session-scoped persistence sidecars. Sanitize every persisted text value with `sanitize_persisted_log_text`, enforce per-record and total-file limits, and retain only data needed for recovery. WebSocket session deletion must remove the WebUI transcript through `delete_webui_thread`; `SessionManager.delete_session` removes the associated subagent trace.
