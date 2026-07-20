# Architecture Reference

## Message and Review Flow

`nanobot/channels/` publishes inbound messages to `nanobot/bus/`. `AgentLoop` restores session state, builds context and orchestrates the task. `AgentRunner` owns the LLM/tool execution loop. Results are emitted through the bus to the source channel.

Code review is a specialization of that flow. `nanobot/review/` normalizes review targets, builds a plan, dispatches specialized subagents, validates structured findings, and renders the final report. `nanobot/agent/subagent.py` owns subagent lifecycle; user-facing review state must remain coherent with `review-webui/`.

## Ownership Boundaries

- `agent/loop.py`: turn orchestration, session/context handoff and generic runtime events.
- `agent/runner.py`: LLM conversation and tool execution only; do not add product-specific review or WebUI behavior here.
- `channels/`: transport-specific parsing, delivery, retries and platform UI behavior.
- `providers/`: provider-specific request/response adaptation and model resolution.
- `agent/tools/`: explicit model-callable capability contracts and permission checks.
- `session/`: durable session state, replay, compaction and goal lifecycle.
- `templates/` and `skills/`: model behavior contracts; treat as runtime code.
- `review-webui/`: presentation and interaction; keep API/WebSocket wire details out of core orchestration.
- `tests/`:test the code,it should mirror the nanobot/ package structure.

When a change crosses an ownership boundary, identify the owner and update the closest contract, consumer and test rather than adding a shortcut in a central module.
