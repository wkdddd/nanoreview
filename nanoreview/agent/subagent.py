"""Profile-driven manager for concurrent subagent execution."""

import asyncio
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanoreview.agent.hooks.subagent import SubagentHook, SubagentStatus
from nanoreview.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanoreview.agent.subagent_profiles import (
    GENERIC_SUBAGENT_PROFILE,
    SubagentCompletion,
    SubagentExecutionLimits,
    SubagentExecutionProfile,
)
from nanoreview.agent.tools.context import ToolContext
from nanoreview.agent.tools.file_state import FileStates
from nanoreview.agent.tools.loader import ToolLoader
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.config.schema import AgentDefaults, ToolsConfig
from nanoreview.providers.base import LLMProvider
from nanoreview.utils.prompt_templates import render_template
from nanoreview.utils.subagent_trace import append_subagent_trace, flush_subagent_trace

_TASK_RUNNING = "running"
_TASK_COMPLETED = "completed"
_TASK_FAILED = "failed"


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
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        reasoning_effort: str | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None]
        | None = None,
        execution_profiles: dict[str, SubagentExecutionProfile] | None = None,
    ):
        defaults = AgentDefaults()
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.tools_config = tools_config or ToolsConfig()
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
        self._execution_profiles = {
            GENERIC_SUBAGENT_PROFILE.id: GENERIC_SUBAGENT_PROFILE,
            **(execution_profiles or {}),
        }
        # Validate profiles before any task can be dispatched.  A typo in a
        # scope should fail configuration at startup rather than silently
        # producing an agent with no capabilities.
        for profile in self._execution_profiles.values():
            self._validate_profile_scope(profile)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        self._session_results: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._session_task_state: dict[str, dict[str, str]] = {}

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
            file_state_store=FileStates(),
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
        profile: SubagentExecutionProfile | None = None,
        target_type: str = "",
    ) -> ToolRegistry:
        """Build an isolated tool registry authorized by the profile scope."""
        profile = profile or GENERIC_SUBAGENT_PROFILE
        target_type = str(target_type or "").strip().lower()
        denied_names: set[str] = set()
        # Review profiles declare both transport tools, but only the active
        # target transport is exposed to the model at runtime.
        if target_type == "github":
            denied_names.add("local_review")
        elif target_type == "local":
            denied_names.add("github_review")
        registry = ToolRegistry()
        loader = ToolLoader()
        loader.load(
            self._build_tool_context(workspace=workspace, tools_config=tools_config),
            registry,
            scope=profile.scope,
            denied_names=denied_names,
        )
        loaded_names = frozenset(registry.tool_names)
        required_tools = frozenset(getattr(profile, "required_tools", frozenset()))
        # Reviewer profiles share a base tool contract but must also expose
        # the evidence tool for the active transport target.
        if profile.scope.startswith("reviewer."):
            if target_type == "local":
                required_tools |= frozenset({"local_review"})
            elif target_type == "github":
                required_tools |= frozenset({"github_review"})
        missing_tools = required_tools - loaded_names
        if missing_tools:
            missing = ", ".join(sorted(missing_tools))
            raise ValueError(
                "Subagent profile {!r} (scope={!r}) is missing required tools: {} "
                "for target_type={!r}".format(
                    profile.id,
                    profile.scope,
                    missing,
                    target_type or "unknown",
                )
            )
        logger.info(
            "subagent.tools.loaded profile_id={} scope={} target_type={} tools={}",
            profile.id,
            profile.scope,
            target_type or "unknown",
            sorted(loaded_names),
        )
        return registry

    def register_execution_profile(self, profile: SubagentExecutionProfile) -> None:
        self._validate_profile_scope(profile)
        self._execution_profiles[profile.id] = profile

    @staticmethod
    def _validate_profile_scope(profile: SubagentExecutionProfile) -> None:
        """Reject profiles whose scope is not declared by any tool class."""
        loader = ToolLoader()
        validate_scope = getattr(loader, "validate_scope", None)
        if callable(validate_scope):
            validate_scope(profile.scope)
            return

        # Keep this fallback for loaders that do not yet expose the explicit
        # validation helper (for example, lightweight test doubles).
        declared_scopes: set[str] = set()
        for tool_cls in loader.discover():
            declared_scopes.update(getattr(tool_cls, "_scopes", {"core"}))
        discover_plugins = getattr(loader, "_discover_plugins", None)
        if callable(discover_plugins):
            for tool_cls in discover_plugins().values():
                declared_scopes.update(getattr(tool_cls, "_scopes", {"core"}))
        if profile.scope not in declared_scopes:
            raise ValueError(
                f"Unknown subagent scope {profile.scope!r} for profile {profile.id!r}"
            )

    def resolve_profile(self, metadata: dict[str, Any]) -> SubagentExecutionProfile:
        profile_id = str(metadata.get("profile_id") or "generic").strip()
        profile = self._execution_profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"Unknown subagent execution profile: {profile_id}")
        return profile

    def build_tool_context(
        self, workspace: Path, tools_config: ToolsConfig | None = None
    ) -> ToolContext:
        return self._build_tool_context(workspace, tools_config)

    def build_tools(
        self, profile: SubagentExecutionProfile, workspace: Path, *, target_type: str = ""
    ) -> ToolRegistry:
        return self._build_tools(workspace=workspace, profile=profile, target_type=target_type)

    @staticmethod
    def build_system_prompt(
        profile: SubagentExecutionProfile,
        metadata: dict[str, Any],
        workspace: Path,
    ) -> str:
        if profile.prompt_builder is not None:
            return profile.prompt_builder(metadata, workspace)
        return (
            "You are a focused subagent. Complete the assigned task using only the "
            "available tools and return a concise result.\n\n"
            f"Workspace: {workspace}"
        )

    @staticmethod
    def terminal_tools(profile: SubagentExecutionProfile) -> frozenset[str]:
        return profile.terminal_tools

    @staticmethod
    def soft_tool_error_tools(profile: SubagentExecutionProfile) -> frozenset[str]:
        return profile.soft_tool_error_tools

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
        execution_limits: SubagentExecutionLimits | None = None,
    ) -> str:
        """Start a dedicated subagent for same-turn result integration."""
        if session_key:
            state = self._session_task_state.setdefault(session_key, {})
            current = state.get(label)
            if current == _TASK_RUNNING:
                return (
                    "Error: Cannot spawn subagent: task label "
                    f"'{label}' is already running for this session."
                )
            if current == _TASK_COMPLETED:
                return (
                    "Error: Cannot spawn subagent: task label "
                    f"'{label}' has already completed for this session."
                )
            state[label] = _TASK_RUNNING
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
                task_id,
                task,
                label,
                origin,
                status,
                origin_message_id,
                origin_metadata,
                execution_limits,
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
                and self._task_state(session_key, label) == _TASK_RUNNING
            ):
                self._set_task_state(session_key, label, _TASK_FAILED)

        bg_task.add_done_callback(_cleanup)
        logger.info("Spawned subagent [{}]: {}", task_id, label)
        return (
            f"Subagent [{label}] started (id: {task_id}). "
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
        execution_limits: SubagentExecutionLimits | None = None,
    ) -> None:
        """Execute one profile-configured subagent and announce its result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        lifecycle_started_at = time.monotonic()
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
        result: AgentRunResult | None = None
        try:
            metadata = dict(origin_metadata or {})
            profile = self.resolve_profile(metadata)
            sub_workspace = (
                profile.workspace_resolver(metadata, self.workspace)
                if profile.workspace_resolver is not None
                else self.workspace
            )
            target_type = str(metadata.get("review_target_type") or "").strip().lower()
            tools = self.build_tools(profile, sub_workspace, target_type=target_type)
            system_prompt = self.build_system_prompt(profile, metadata, sub_workspace)

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
            effective_iterations = (
                execution_limits.max_iterations
                if execution_limits and execution_limits.max_iterations is not None
                else self.max_iterations
            )
            effective_max_tokens = execution_limits.max_tokens if execution_limits else None
            timeout_seconds = execution_limits.timeout_seconds if execution_limits else None
            timeout_scope = (
                asyncio.timeout(
                    max(0.0, timeout_seconds - (time.monotonic() - lifecycle_started_at))
                )
                if timeout_seconds is not None and timeout_seconds > 0
                else nullcontext()
            )
            async with timeout_scope:
                result = await self.runner.run(
                    AgentRunSpec(
                        initial_messages=messages,
                        tools=tools,
                        model=self.model,
                        max_iterations=effective_iterations,
                        max_tokens=effective_max_tokens,
                        max_tool_result_chars=self.max_tool_result_chars,
                        reasoning_effort=self.reasoning_effort,
                        hook=hook,
                        max_iterations_message=profile.max_iterations_message,
                        error_message=None,
                        fail_on_tool_error=True,
                        soft_tool_error_tools=self.soft_tool_error_tools(profile),
                        terminal_tools=self.terminal_tools(profile),
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
                        usage=result.usage,
                        execution_limits=execution_limits,
                    )
                    return
                if result.stop_reason == "error":
                    status.phase = "error"
                    await self._announce_result(
                        task_id,
                        label,
                        task,
                        result.error or "Error: subagent execution failed.",
                        origin,
                        "error",
                        origin_message_id,
                        usage=result.usage,
                        execution_limits=execution_limits,
                    )
                    return

                completion = await self.handle_completed_result(
                    profile=profile,
                    result=result,
                    target_type=target_type,
                )
                final_result = completion.content
                status.stop_reason = completion.stop_reason or status.stop_reason

                logger.info("Subagent [{}] completed status={}", task_id, completion.status)
                # A reviewer that submitted empty findings without evidence is
                # an execution failure; missing terminal submission is still a
                # completed lifecycle with an error result for compatibility.
                status.phase = (
                    "error"
                    if completion.status == "error"
                    and completion.content.startswith("Error: Review incomplete")
                    else "done"
                )
                lifecycle_status = "completed" if completion.status == "ok" else "error"
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    final_result,
                    origin,
                    completion.status,
                    origin_message_id,
                    usage=result.usage,
                    execution_limits=execution_limits,
                )

        except TimeoutError:
            status.phase = "error"
            status.stop_reason = "timeout"
            timeout_seconds = execution_limits.timeout_seconds if execution_limits else None
            message = f"Error: subagent execution timed out after {timeout_seconds or 0:g}s"
            logger.warning("Subagent [{}] timed out after {}s", task_id, timeout_seconds)
            await self._announce_result(
                task_id,
                label,
                task,
                message,
                origin,
                "error",
                origin_message_id,
                usage=result.usage if result is not None else None,
                execution_limits=execution_limits,
            )

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            logger.exception("Subagent [{}] failed", task_id)
            await self._announce_result(
                task_id,
                label,
                task,
                f"Error: {e}",
                origin,
                "error",
                origin_message_id,
                usage=result.usage if result is not None else None,
                execution_limits=execution_limits,
            )
        finally:
            await self._publish_subagent_lifecycle(
                origin.get("channel", "cli"),
                origin.get("chat_id", "direct"),
                task_id,
                label,
                lifecycle_status,
            )

    async def handle_completed_result(
        self,
        *,
        profile: SubagentExecutionProfile,
        result: AgentRunResult,
        target_type: str,
    ) -> SubagentCompletion:
        """Normalize the final result of the single completed AgentRun.

        Terminal-tool retries already happened inside ``AgentRunner``; no
        compensation run is started here. The profile's result handler only
        parses the final structured outcome of the finished run.
        """
        if profile.result_handler is None:
            return SubagentCompletion(
                result.final_content or result.error or "",
                status="ok" if result.stop_reason != "error" else "error",
                stop_reason=result.stop_reason,
            )
        return await profile.result_handler(result=result, target_type=target_type)

    @staticmethod
    def _extract_review_submit_result(
        messages: list[dict[str, Any]],
        tool_events: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Extract a successfully processed structured review submission."""
        candidates: list[Any] = []
        for event in reversed(tool_events or []):
            if event.get("name") == "review_submit" and event.get("status") == "ok":
                candidates.append(event.get("raw_result"))
        for message in reversed(messages):
            if message.get("role") == "tool" and message.get("name") == "review_submit":
                candidates.append(message.get("content"))
        import json

        for raw in candidates:
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if payload.get("submitted") is True and isinstance(payload.get("findings"), list):
                return json.dumps(payload, ensure_ascii=False)
        return None

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
        usage: dict[str, int] | None = None,
        execution_limits: SubagentExecutionLimits | None = None,
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
        if usage:
            metadata["subagent_usage"] = dict(usage)
        if execution_limits is not None:
            if execution_limits.input_tokens is not None:
                metadata["subagent_input_tokens"] = execution_limits.input_tokens
            if execution_limits.quota_tokens is not None:
                metadata["subagent_quota_tokens"] = execution_limits.quota_tokens
            if execution_limits.max_iterations is not None:
                metadata["subagent_max_rounds"] = execution_limits.max_iterations
            if execution_limits.max_tokens is not None:
                metadata["subagent_max_tokens"] = execution_limits.max_tokens
            if execution_limits.timeout_seconds is not None:
                metadata["subagent_timeout_seconds"] = execution_limits.timeout_seconds
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
        self._set_task_state(
            override,
            label,
            _TASK_COMPLETED if status == "ok" else _TASK_FAILED,
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

    def _task_state(self, session_key: str, label: str) -> str | None:
        return self._session_task_state.get(session_key, {}).get(label)

    def _dimension_state(self, session_key: str, label: str) -> str | None:
        """Compatibility alias for the session task lifecycle lookup."""
        return self._task_state(session_key, label)

    def _set_task_state(self, session_key: str, label: str, state: str) -> None:
        self._session_task_state.setdefault(session_key, {})[label] = state

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
