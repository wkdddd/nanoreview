# Architecture Reference

## Message and Review Flow

`nanoreview/channels/` publishes inbound messages to `nanoreview/bus/`. `AgentLoop` restores session state, builds context and orchestrates the task. `AgentRunner` owns the LLM/tool execution loop. Results are emitted through the bus to the source channel.

Code review is a specialization of that flow. `nanoreview/review/` normalizes review targets, builds a plan, dispatches specialized subagents, validates structured findings, and renders the final report. `agent/loop.py` owns the ReviewAgent supervisor lifecycle: review run state (`ReviewRunState`), phase transitions, cancellation, same-process recovery, and the final `completed`/`error`/`stopped` status persisted into session metadata. `nanoreview/agent/orchestration.py` is the legacy implementation kept as a compatibility shell during migration; new review orchestration must not grow there. `nanoreview/agent/subagent.py` owns subagent lifecycle; user-facing review state must remain coherent with `review-webui/`.

One session executes at most one review run. The final report is persisted as an independent artifact under `review-artifacts/` in the workspace and referenced from session metadata via `review_report_ref`; review sessions reject ordinary follow-up messages once the run has finished.

## Ownership Boundaries

- `agent/loop.py`: turn orchestration, session/context handoff, generic runtime events, and the ReviewAgent supervisor lifecycle (run state, phase, cancellation, final status, message gatekeeping for review sessions).
- `agent/runner.py`: LLM conversation and tool execution only.
- `channels/`: transport-specific parsing, delivery, retries and platform UI behavior.
- `providers/`: provider-specific request/response adaptation and model resolution.
- `agent/tools/`: explicit model-callable capability contracts and permission checks.
- `agent/review_state.py`: in-process `ReviewRunState`, input fingerprint, and report artifact persistence for one review run.
- `agent/orchestration.py`: legacy review execution path (migration pending); retained as a compatibility shell, do not extend.
- `review/planning/`, `review/input/`, `review/output/` and `review/source/`: review target normalization, evidence and policy, report validation, and source acquisition.
- `session/`: durable session state, replay, compaction and goal lifecycle.
- `templates/` and `skills/`: model behavior contracts; treat as runtime code.
- `review-webui/`: presentation and interaction; keep API/WebSocket wire details out of core orchestration. The WebUI/API consumes only `review_phase`, `review_run_id`, `review_report_ref` review metadata and the report API — it never participates in review orchestration.
- `tests/`:test the code,it should mirror the nanoreview/ package structure.

When a change crosses an ownership boundary, identify the owner and update the closest contract, consumer and test rather than adding a shortcut in a central module.
