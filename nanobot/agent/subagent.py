"""Subagent manager for concurrent review task execution."""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanobot.agent.hooks.subagent import SubagentHook, SubagentStatus
from nanobot.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults, ToolsConfig
from nanobot.providers.base import LLMProvider
from nanobot.review.types import ReviewMetaKey
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.subagent_trace import append_subagent_trace, flush_subagent_trace

_DIMENSION_RUNNING = "running"
_DIMENSION_COMPLETED = "completed"
_DIMENSION_FAILED = "failed"
_SUBAGENT_SOFT_TOOL_ERROR_TOOLS = frozenset(
    {
        "github_review",
        "grep",
        "list_dir",
        "local_review",
        "read_file",
    }
)


class SubagentManager:
    """Manages concurrent subagent execution and result injection."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        embedding_config: Any | None = None,
        rerank_config: Any | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        reasoning_effort: str | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None]
        | None = None,
    ):
        defaults = AgentDefaults()
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.tools_config = tools_config or ToolsConfig()
        self.embedding_config = embedding_config
        self.rerank_config = rerank_config
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = (
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else defaults.max_concurrent_subagents
        )
        self.reasoning_effort = reasoning_effort
        self.runner = AgentRunner(provider)
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        self._session_results: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._session_dimension_state: dict[str, dict[str, str]] = {}

    def _subagent_tools_config(self) -> ToolsConfig:
        """Build a ToolsConfig scoped for subagent use."""
        return ToolsConfig(
            exec=self.tools_config.exec,
            restrict_to_workspace=self.restrict_to_workspace,
            github_repo=self.tools_config.github_repo,
        )

    def _build_tool_context(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
    ) -> ToolContext:
        root = self.workspace if workspace is None else workspace
        cfg = (
            tools_config if tools_config is not None else self._subagent_tools_config()
        )
        return ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            embedding_config=self.embedding_config,
            rerank_config=self.rerank_config,
            file_state_store=FileStates(),
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
    ) -> ToolRegistry:
        """Build an isolated subagent tool registry via ToolLoader."""
        registry = ToolRegistry()
        ToolLoader().load(
            self._build_tool_context(workspace=workspace, tools_config=tools_config),
            registry,
            scope="subagent",
        )
        return registry

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    async def spawn(
        self,
        task: str,
        label: str,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        origin_metadata: dict[str, Any] | None = None,
        deliver_to_bus: bool = True,
    ) -> str:
        """Start a dedicated review subagent for same-turn result integration."""
        if session_key:
            state = self._session_dimension_state.setdefault(session_key, {})
            current = state.get(label)
            if current == _DIMENSION_RUNNING:
                return (
                    "Error: Cannot spawn review subagent: dimension "
                    f"'{label}' is already running for this review."
                )
            if current == _DIMENSION_COMPLETED:
                return (
                    "Error: Cannot spawn review subagent: dimension "
                    f"'{label}' has already completed for this review."
                )
            state[label] = _DIMENSION_RUNNING
        task_id = str(uuid.uuid4())[:8]
        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
            "session_key": session_key,
            "deliver_to_bus": deliver_to_bus,
        }
        status = SubagentStatus(
            task_id=task_id,
            label=label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id, task, label, origin, status, origin_message_id, origin_metadata
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]
            if (
                session_key
                and self._dimension_state(session_key, label) == _DIMENSION_RUNNING
            ):
                self._set_dimension_state(session_key, label, _DIMENSION_FAILED)

        bg_task.add_done_callback(_cleanup)
        logger.info("Spawned review subagent [{}]: {}", task_id, label)
        return (
            f"Review subagent [{label}] started (id: {task_id}). "
            "The coordinator will wait for and integrate its result before finalizing."
        )

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        origin_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Execute a dedicated review subagent and announce structured findings."""
        logger.info("Review subagent [{}] starting task: {}", task_id, label)
        session_key = (
            origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        )
        append_subagent_trace(
            session_key,
            {"event": "started", "subagent_id": task_id, "label": label},
        )

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        lifecycle_status = "error"
        try:
            metadata = dict(origin_metadata or {})
            target_type = (
                str(metadata.get(ReviewMetaKey.TARGET_TYPE) or "").strip().lower()
            )

            # Align subagent tool workspace with the review local root so that
            # read_file("types.py") resolves relative to the review target
            # directory rather than the project root.
            sub_workspace: Path | None = None
            local_root = metadata.get(ReviewMetaKey.LOCAL_ROOT)
            if target_type == "local" and local_root:
                try:
                    root_path = Path(str(local_root)).expanduser().resolve()
                    if root_path.is_dir():
                        sub_workspace = root_path
                except (OSError, ValueError):
                    sub_workspace = None

            tools = self._build_tools(workspace=sub_workspace)
            system_prompt = self._build_subagent_prompt(workspace=sub_workspace)

            stream_id = f"subagent:{task_id}"
            origin_channel = origin.get("channel", "cli")
            origin_chat_id = origin.get("chat_id", "direct")

            # --- Streaming callbacks: publish reasoning / content deltas
            # to the bus so the WebSocket channel can forward them to the
            # frontend with subagent discriminator metadata. ---
            async def _on_progress(
                content: str,
                *,
                reasoning: bool = False,
                reasoning_end: bool = False,
                tool_hint: bool = False,
                tool_events: list[dict[str, Any]] | None = None,
            ) -> None:
                meta = dict(metadata)
                meta["_progress"] = True
                meta["_tool_hint"] = tool_hint
                meta["_stream_id"] = stream_id
                meta["_subagent_id"] = task_id
                meta["_subagent_label"] = label
                if reasoning:
                    meta["_reasoning_delta"] = True
                    append_subagent_trace(
                        session_key,
                        {
                            "event": "reasoning_delta",
                            "subagent_id": task_id,
                            "text": content,
                        },
                    )
                if reasoning_end:
                    meta["_reasoning_end"] = True
                if tool_events:
                    meta["_tool_events"] = tool_events
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=origin_channel,
                        chat_id=origin_chat_id,
                        content=content,
                        metadata=meta,
                    )
                )

            async def _on_stream(delta: str) -> None:
                meta = dict(metadata)
                meta["_stream_delta"] = True
                meta["_stream_id"] = stream_id
                meta["_stream_kind"] = "subagent_content"
                meta["_subagent_id"] = task_id
                meta["_subagent_label"] = label
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=origin_channel,
                        chat_id=origin_chat_id,
                        content=delta,
                        metadata=meta,
                    )
                )

            async def _on_stream_end(*, resuming: bool = False) -> None:
                meta = dict(metadata)
                meta["_stream_end"] = True
                meta["_stream_id"] = stream_id
                meta["_stream_kind"] = "subagent_content"
                meta["_subagent_id"] = task_id
                meta["_subagent_label"] = label
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=origin_channel,
                        chat_id=origin_chat_id,
                        content="",
                        metadata=meta,
                    )
                )

            # --- Lifecycle: notify frontend that this subagent is starting ---
            await self._publish_subagent_lifecycle(
                origin_channel, origin_chat_id, task_id, label, "running"
            )

            hook = SubagentHook(
                task_id,
                status,
                tools=tools,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                session_key=origin.get("session_key"),
                origin_message_id=origin_message_id,
                metadata=metadata,
                on_progress=_on_progress,
                on_stream_cb=_on_stream,
                on_stream_end_cb=_on_stream_end,
                on_tool_events=lambda events: self._record_tool_events(
                    session_key, task_id, events
                ),
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]
            sess_key = origin.get("session_key")
            llm_timeout = (
                self._llm_wall_timeout_for_session(sess_key)
                if self._llm_wall_timeout_for_session
                else None
            )
            result = await self.runner.run(
                AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=self.model,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    reasoning_effort=self.reasoning_effort,
                    hook=hook,
                    max_iterations_message=(
                        "Review task completed but no structured findings were submitted."
                    ),
                    error_message=None,
                    fail_on_tool_error=True,
                    soft_tool_error_tools=_SUBAGENT_SOFT_TOOL_ERROR_TOOLS,
                    terminal_tools=frozenset({"review_submit"}),
                    checkpoint_callback=_on_checkpoint,
                    session_key=sess_key,
                    llm_timeout_s=llm_timeout,
                )
            )
            status.stop_reason = result.stop_reason

            if result.stop_reason == "tool_error":
                status.phase = "error"
                status.tool_events = list(result.tool_events)
                final_result = self._format_partial_progress(result)
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    final_result,
                    origin,
                    "error",
                    origin_message_id,
                )
                return
            if result.stop_reason == "error":
                status.phase = "error"
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    result.error or "Error: review subagent execution failed.",
                    origin,
                    "error",
                    origin_message_id,
                )
                return

            final_result = self._extract_review_submit_result(
                result.messages, result.tool_events
            )

            # When no structured findings were submitted, force a review_submit
            # call. This covers completed, max_iterations, and
            # empty_final_response stop reasons — only tool_error and error
            # take the failure paths above.
            if final_result is None:
                status.phase = "retrying_review_submit"
                final_result, retry_stop_reason = await self._force_review_submit(
                    result, tools, hook, sess_key, llm_timeout
                )
                status.stop_reason = retry_stop_reason

            if final_result is None:
                # Retry still produced no structured findings. Announce as a
                # failure so the finalizer reports an accurate incomplete reason
                # instead of masking the problem as "completed successfully".
                final_result = (
                    "No structured findings submitted: the review subagent did "
                    "not produce a review_submit result."
                )
                logger.warning(
                    "Review subagent [{}] ended without structured findings", task_id
                )
                status.phase = "done"
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    final_result,
                    origin,
                    "error",
                    origin_message_id,
                )
                return

            # Guard against evidence-less empty findings: a subagent that
            # submits ``findings: []`` without having successfully read any
            # target evidence is treated as incomplete rather than clean.
            if self._is_empty_findings(final_result):
                if not self._has_successful_evidence_read(
                    result.tool_events, target_type
                ):
                    final_result = (
                        "Error: Review incomplete — no target evidence was "
                        "successfully read before submitting empty findings. "
                        "The reviewer must read the target files before "
                        "reporting no issues."
                    )
                    logger.warning(
                        "Review subagent [{}] submitted empty findings without evidence",
                        task_id,
                    )
                    status.phase = "error"
                    await self._announce_result(
                        task_id,
                        label,
                        task,
                        final_result,
                        origin,
                        "error",
                        origin_message_id,
                    )
                    return

            logger.info("Review subagent [{}] completed successfully", task_id)
            status.phase = "done"
            lifecycle_status = "completed"
            await self._announce_result(
                task_id, label, task, final_result, origin, "ok", origin_message_id
            )

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            logger.exception("Review subagent [{}] failed", task_id)
            await self._announce_result(
                task_id, label, task, f"Error: {e}", origin, "error", origin_message_id
            )
        finally:
            await self._publish_subagent_lifecycle(
                origin.get("channel", "cli"),
                origin.get("chat_id", "direct"),
                task_id,
                label,
                lifecycle_status,
            )

    async def _force_review_submit(
        self,
        result: AgentRunResult,
        tools: ToolRegistry,
        hook: SubagentHook,
        sess_key: str | None,
        llm_timeout: float | None,
    ) -> tuple[str | None, str]:
        """Force a ``review_submit`` call when no structured findings were extracted.

        Returns ``(extracted_result, retry_stop_reason)``. ``extracted_result``
        is the canonical ``review_submit`` JSON string when the retry succeeds,
        or ``None`` when the retry still produces no structured findings.
        """
        retry_messages = list(result.messages) + [
            {
                "role": "user",
                "content": (
                    "You did not call review_submit. You MUST call it now. "
                    "Use JSON-compatible structured tool arguments. "
                    "Use findings:[] if no issues were found."
                ),
            }
        ]
        retry = await self.runner.run(
            AgentRunSpec(
                initial_messages=retry_messages,
                tools=tools,
                model=self.model,
                max_iterations=2,
                max_tool_result_chars=self.max_tool_result_chars,
                reasoning_effort=self.reasoning_effort,
                hook=hook,
                tool_choice={
                    "type": "function",
                    "function": {"name": "review_submit"},
                },
                response_format={"type": "json_object"},
                error_message=None,
                fail_on_tool_error=False,
                terminal_tools=frozenset({"review_submit"}),
                session_key=sess_key,
                llm_timeout_s=llm_timeout,
            )
        )
        extracted = self._extract_review_submit_result(
            retry.messages, retry.tool_events
        )
        return extracted, retry.stop_reason

    @staticmethod
    def _is_empty_findings(result_json: str) -> bool:
        """Check whether a review_submit result has zero findings."""
        try:
            data = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(data, dict)
            and data.get("submitted") is True
            and isinstance(data.get("findings"), list)
            and len(data["findings"]) == 0
        )

    @staticmethod
    def _has_successful_evidence_read(
        tool_events: list[dict[str, Any]],
        target_type: str,
    ) -> bool:
        """Check whether the subagent successfully read target evidence.

        For local targets, a successful ``read_file`` or ``local_review``
        counts.  For GitHub targets, a successful ``github_review`` counts.
        """
        if target_type == "github":
            evidence_tools = {"github_review"}
        else:
            evidence_tools = {"read_file", "local_review"}
        for event in tool_events:
            if event.get("name") in evidence_tools and event.get("status") == "ok":
                return True
        return False

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        session_key = (
            origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        )
        append_subagent_trace(
            session_key,
            {"event": "finished", "subagent_id": task_id, "status": status},
        )
        # Flush on terminal state so the sidecar is consistent before the
        # main agent reads back cards or the session is reloaded.
        flush_subagent_trace(session_key)
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = (
            origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        )
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
            "subagent_label": label,
            "subagent_status": status,
            "subagent_result": result,
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        if origin.get("deliver_to_bus", True):
            await self.bus.publish_inbound(msg)
        self._publish_session_result(override, msg)
        self._set_dimension_state(
            override,
            label,
            _DIMENSION_COMPLETED if status == "ok" else _DIMENSION_FAILED,
        )
        logger.debug(
            "Subagent [{}] announced result to {}:{}",
            task_id,
            origin["channel"],
            origin["chat_id"],
        )

    async def _record_tool_events(
        self,
        session_key: str,
        task_id: str,
        tool_events: list[dict[str, str]],
    ) -> None:
        """Persist only tool names and their terminal status for auditability."""
        for event in tool_events:
            name = event.get("name")
            status = event.get("status")
            if not isinstance(name, str) or not isinstance(status, str):
                continue
            append_subagent_trace(
                session_key,
                {
                    "event": "tool",
                    "subagent_id": task_id,
                    "name": name,
                    "status": "success" if status == "ok" else "error",
                },
            )

    async def _publish_subagent_lifecycle(
        self,
        channel: str,
        chat_id: str,
        task_id: str,
        label: str,
        status: str,
    ) -> None:
        """Publish a subagent lifecycle event (start/end) to the bus.

        ``status`` is ``"running"`` when the subagent starts and
        ``"completed"`` or ``"error"`` when it finishes.  The WebSocket
        channel translates this into a ``subagent_status`` wire event so
        the frontend can create / update per-subagent cards.
        """
        meta: dict[str, Any] = {
            "_subagent_id": task_id,
            "_subagent_label": label,
            "_subagent_status": status,
        }
        if status == "running":
            meta["_subagent_start"] = True
        else:
            meta["_subagent_end"] = True
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content="",
                metadata=meta,
            )
        )

    def _dimension_state(self, session_key: str, label: str) -> str | None:
        return self._session_dimension_state.get(session_key, {}).get(label)

    def _set_dimension_state(self, session_key: str, label: str, state: str) -> None:
        self._session_dimension_state.setdefault(session_key, {})[label] = state

    def _publish_session_result(self, session_key: str, msg: InboundMessage) -> None:
        self._session_results.setdefault(session_key, asyncio.Queue()).put_nowait(msg)

    async def wait_for_session_result(
        self,
        session_key: str,
        *,
        timeout: float = 0.1,
    ) -> InboundMessage | None:
        """Wait briefly for the next completed subagent result for a session."""
        queue = self._session_results.setdefault(session_key, asyncio.Queue())
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=timeout)
            self._cleanup_session_result_queue(session_key)
            return msg
        except asyncio.TimeoutError:
            self._cleanup_session_result_queue(session_key)
            return None

    def drain_session_results(
        self, session_key: str, *, limit: int
    ) -> list[InboundMessage]:
        """Return already completed subagent results for a session."""
        queue = self._session_results.setdefault(session_key, asyncio.Queue())
        items: list[InboundMessage] = []
        while len(items) < limit:
            try:
                items.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        self._cleanup_session_result_queue(session_key)
        return items

    def _cleanup_session_result_queue(self, session_key: str) -> None:
        queue = self._session_results.get(session_key)
        if (
            queue is not None
            and queue.empty()
            and session_key not in self._session_tasks
        ):
            self._session_results.pop(session_key, None)

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] == "ok"]
        failure = next(
            (e for e in reversed(result.tool_events) if e["status"] == "error"), None
        )
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    def _build_subagent_prompt(self, workspace: Path | None = None) -> str:
        """Build the system prompt for dedicated review subagents."""
        from nanobot.agent.context import ContextBuilder

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        return render_template(
            "agent/review_subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(workspace or self.workspace),
            skills_summary="",  # 保留skillsummary接口
        )

    @staticmethod
    def _extract_review_submit_result(
        messages: list[dict[str, Any]],
        tool_events: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Extract the last canonical review_submit tool result."""
        for event in reversed(tool_events or []):
            if (
                event.get("name") == "review_submit"
                and event.get("status") == "ok"
                and isinstance(event.get("raw_result"), str)
            ):
                result = SubagentManager._canonical_review_submit_json(
                    event["raw_result"]
                )
                if result is not None:
                    return result

        for message in reversed(messages):
            if message.get("role") != "tool" or message.get("name") != "review_submit":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            result = SubagentManager._canonical_review_submit_json(content)
            if result is not None:
                return result
        return None

    @staticmethod
    def _canonical_review_submit_json(content: str) -> str | None:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None
        if (
            not isinstance(data, dict)
            or data.get("submitted") is not True
            or not isinstance(data.get("findings"), list)
            or not isinstance(data.get("errors"), list)
        ):
            return None
        return json.dumps(data, ensure_ascii=False)

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [
            self._running_tasks[tid]
            for tid in self._session_tasks.get(session_key, [])
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1
            for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )
