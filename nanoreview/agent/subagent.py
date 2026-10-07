"""Profile-driven manager for concurrent review subagent execution.

The manager is dispatched by ``ReviewLoop`` only: it runs one registered
execution profile per task and hands the result back through an in-process
per-session queue. It never publishes a subagent result to the message bus —
review results are not conversation inbound events.
"""

import asyncio
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanoreview.agent.event_sink import event_text, metadata_for_event
from nanoreview.agent.hooks.subagent import SubagentHook, SubagentStatus
from nanoreview.agent.hooks.turn_hooks import AgentTurnHookSpec, build_agent_turn_hook
from nanoreview.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanoreview.agent.subagent_profiles import (
    SubagentCompletion,
    SubagentExecutionLimits,
    SubagentExecutionProfile,
)
from nanoreview.agent.tools.context import ToolContext
from nanoreview.agent.tools.file_state import FileStates
from nanoreview.agent.tools.loader import ToolLoader
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.agent.tools.workspace_scope import (
    WorkspaceScope,
    resolve_workspace_scope,
    review_workspace_scope,
)
from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.config.schema import AgentDefaults, ToolsConfig
from nanoreview.events import EventSink, ProgressEvent
from nanoreview.providers.base import LLMProvider
from nanoreview.utils.prompt_templates import render_template
from nanoreview.utils.subagent_trace import append_subagent_trace, flush_subagent_trace

_TASK_RUNNING = "running"
_TASK_COMPLETED = "completed"


def subagent_workspace_scope(
    profile: SubagentExecutionProfile,
    workspace: Path,
    restrict_to_workspace: bool,
) -> WorkspaceScope:
    """Resolve the workspace scope one subagent run is bound to.

    Review profiles are pinned to ``restricted`` against their target
    repository root, so a reviewer can neither read nor execute outside it
    regardless of the conversation-side configuration. Any non-review profile
    falls back to the config-derived default.
    """
    if profile.scope.startswith("reviewer."):
        return review_workspace_scope(workspace)
    return resolve_workspace_scope(
        default_project_path=workspace,
        restrict_to_workspace=restrict_to_workspace,
    )
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
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
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
        # Context window wiring mirrors the main agent: the runner trims history
        # to this window, so subagents get the same protection as the main
        # agent. None keeps the runner's no-trim behaviour for callers that
        # manage context themselves.
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.runner = AgentRunner(provider)
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        self._execution_profiles = dict(execution_profiles or {})
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
        )

    def _build_tool_context(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
        *,
        review_dedup: bool = False,
    ) -> ToolContext:
        root = self.workspace if workspace is None else workspace
        cfg = (
            tools_config if tools_config is not None else self._subagent_tools_config()
        )
        return ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            file_state_store=FileStates(review_dedup=review_dedup),
        )

    def _build_tools(
        self,
        profile: SubagentExecutionProfile,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
    ) -> ToolRegistry:
        """Build an isolated tool registry authorized by the profile scope."""
        denied_names: set[str] = set()
        registry = ToolRegistry()
        loader = ToolLoader()
        # Reviewer runs get a duplicate-read ledger scoped to this single run.
        # Each subagent builds a fresh FileStates, so the ledger never leaks
        # across reviewers or review runs.
        review_dedup = profile.scope.startswith("reviewer.")
        loader.load(
            self._build_tool_context(
                workspace=workspace,
                tools_config=tools_config,
                review_dedup=review_dedup,
            ),
            registry,
            scope=profile.scope,
            denied_names=denied_names,
        )
        loaded_names = frozenset(registry.tool_names)
        required_tools = frozenset(getattr(profile, "required_tools", frozenset()))
        missing_tools = required_tools - loaded_names
        if missing_tools:
            missing = ", ".join(sorted(missing_tools))
            raise ValueError(
                "Subagent profile {!r} (scope={!r}) is missing required tools: {}".format(
                    profile.id,
                    profile.scope,
                    missing,
                )
            )
        logger.info(
            "subagent.tools.loaded profile_id={} scope={} tools={}",
            profile.id,
            profile.scope,
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
        """Resolve the explicitly declared profile a task must run under.

        There is no default profile: a task without a ``profile_id`` (or with an
        unregistered one) is a programming error, not a generic run.
        """
        profile_id = str(metadata.get("profile_id") or "").strip()
        if not profile_id:
            raise ValueError("Subagent execution requires an explicit profile_id.")
        profile = self._execution_profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"Unknown subagent execution profile: {profile_id}")
        return profile

    def build_tool_context(
        self, workspace: Path, tools_config: ToolsConfig | None = None
    ) -> ToolContext:
        return self._build_tool_context(workspace, tools_config)

    def build_tools(
        self, profile: SubagentExecutionProfile, workspace: Path
    ) -> ToolRegistry:
        return self._build_tools(profile, workspace=workspace)

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

    @staticmethod
    def _dedup_stats(tools: ToolRegistry | None) -> dict[str, int]:
        """Read the reviewer-run duplicate-read/search counters, if any.

        All reviewer tools share one ``FileStates`` (and thus one ledger), so
        the first ledger found carries the whole run's totals.
        """
        if tools is None:
            return {}
        for name in tools.tool_names:
            tool = tools.get(name)
            ledger = getattr(getattr(tool, "_file_states", None), "review_ledger", None)
            if ledger is not None:
                return {
                    "duplicate_reads": ledger.duplicate_reads,
                    "duplicate_searches": ledger.duplicate_searches,
                }
        return {}

    def _apply_dedup_stats(
        self, status: SubagentStatus, tools: ToolRegistry | None
    ) -> None:
        """Copy the reviewer-run duplicate counters onto the run status."""
        stats = self._dedup_stats(tools)
        status.duplicate_reads = stats.get("duplicate_reads", 0)
        status.duplicate_searches = stats.get("duplicate_searches", 0)

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int | None = None,
    ) -> None:
        """Swap provider/model for future subagents.

        ``context_window_tokens`` mirrors ``Consolidator.set_provider`` so a
        runtime model switch also updates the window used for history
        trimming; without it new subagents would keep the old window value.
        """
        self.provider = provider
        self.model = model
        self.runner.provider = provider
        if context_window_tokens is not None:
            self.context_window_tokens = context_window_tokens

    async def spawn(
        self,
        task: str,
        label: str,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        origin_metadata: dict[str, Any] | None = None,
        execution_limits: SubagentExecutionLimits | None = None,
    ) -> str:
        """Start a dedicated review subagent whose result returns to the caller.

        The result is delivered to the owning session's in-process result queue
        (drained by ``ReviewLoop``); it is never published as a conversation
        inbound message.
        """
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
        return f"Subagent [{label}] started (id: {task_id})."

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
        tools: ToolRegistry | None = None
        try:
            metadata = dict(origin_metadata or {})
            profile = self.resolve_profile(metadata)
            sub_workspace = (
                profile.workspace_resolver(metadata, self.workspace)
                if profile.workspace_resolver is not None
                else self.workspace
            )
            tools = self.build_tools(profile, sub_workspace)
            system_prompt = self.build_system_prompt(profile, metadata, sub_workspace)
            workspace_scope = subagent_workspace_scope(
                profile, sub_workspace, self.restrict_to_workspace
            )

            stream_id = f"subagent:{task_id}"
            origin_channel = origin.get("channel", "cli")
            origin_chat_id = origin.get("chat_id", "direct")

            # --- Typed event sink: the hook publishes progress / stream events
            # and this projection gives them the subagent discriminator metadata
            # the WebSocket channel already expects.
            async def _publish_subagent_event(event: Any) -> None:
                meta = metadata_for_event(
                    metadata,
                    event,
                    stream_kind="subagent_content",
                    extra={
                        "_stream_id": stream_id,
                        "_subagent_id": task_id,
                        "_subagent_label": label,
                    },
                )
                if meta is None:
                    return
                if isinstance(event, ProgressEvent) and event.reasoning_delta:
                    append_subagent_trace(
                        session_key,
                        {
                            "event": "reasoning_delta",
                            "subagent_id": task_id,
                            "text": event.reasoning or "",
                        },
                    )
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=origin_channel,
                        chat_id=origin_chat_id,
                        content=event_text(event),
                        metadata=meta,
                    )
                )

            events = EventSink(publish=_publish_subagent_event)

            # --- Lifecycle: notify frontend that this subagent is starting ---
            await self._publish_subagent_lifecycle(
                origin_channel, origin_chat_id, task_id, label, "running"
            )

            # The subagent is the only progress surface for this run, so the
            # turn's own progress hook stays unbound (``NO_EVENTS``) and the
            # subagent hook carries delivery. Going through the builder keeps
            # turn-local state per run instead of per process.
            hook = build_agent_turn_hook(
                AgentTurnHookSpec(
                    turn_hooks=[
                        SubagentHook(
                            task_id,
                            status,
                            tools=tools,
                            origin_channel=origin_channel,
                            origin_chat_id=origin_chat_id,
                            session_key=origin.get("session_key"),
                            origin_message_id=origin_message_id,
                            metadata=metadata,
                            events=events,
                            streaming=True,
                            workspace_scope=workspace_scope,
                            on_tool_events=lambda events: self._record_tool_events(
                                session_key, task_id, events
                            ),
                        )
                    ],
                )
            )
            # Reviewer system prompt + task/evidence envelope are a frozen
            # envelope; the reviewer run starts with no inherited history.
            frozen_messages: list[dict[str, Any]] = [
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
            effective_context_window = (
                execution_limits.context_window_tokens
                if execution_limits and execution_limits.context_window_tokens is not None
                else self.context_window_tokens
            )
            # One info record of the actually effective limits replaces the
            # per-result metadata observability (quota/input estimates were
            # removed with the budget admission gate).
            logger.info(
                "subagent.limits task_id={} label={} max_iterations={} max_tokens={} "
                "timeout_seconds={} context_window_tokens={} context_block_limit={}",
                task_id,
                label,
                effective_iterations,
                effective_max_tokens,
                timeout_seconds,
                effective_context_window,
                self.context_block_limit,
            )
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
                        frozen_messages=frozen_messages,
                        working_messages=[],
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
                        preserve_tool_result_tools=profile.preserve_tool_result_tools,
                        checkpoint_callback=_on_checkpoint,
                        session_key=sess_key,
                        llm_timeout_s=llm_timeout,
                        # Route the subagent through the same context-window
                        # trimming as the main agent (runner._snip_history);
                        # without this value the runner skips trimming and
                        # long reviewer runs grow unbounded.
                        context_window_tokens=effective_context_window,
                        context_block_limit=self.context_block_limit,
                    )
                )
                status.stop_reason = result.stop_reason
                self._apply_dedup_stats(status, tools)
                if status.duplicate_reads or status.duplicate_searches:
                    logger.info(
                        "subagent.dedup task_id={} label={} duplicate_reads={} duplicate_searches={}",
                        task_id,
                        label,
                        status.duplicate_reads,
                        status.duplicate_searches,
                    )

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
                if result.stop_reason in ("error", "compression_failed", "compression_limit"):
                    # Run-level compression stopped the run: it is an execution
                    # failure, not a completion. The reviewer is marked error so
                    # the finalizer treats its dimension as incomplete.
                    status.phase = "error"
                    reason = result.error or (
                        f"Error: reviewer run stopped with {result.stop_reason}."
                    )
                    await self._announce_result(
                        task_id,
                        label,
                        task,
                        reason,
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
                    metadata=metadata,
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
            self._apply_dedup_stats(status, tools)
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
            self._apply_dedup_stats(status, tools)
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
        metadata: dict[str, Any] | None = None,
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
        return await profile.result_handler(result=result, metadata=metadata)

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
        """Hand the finished subagent result to the owning session's queue."""
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

        # Deliver the result to the owning session's in-process queue only: the
        # session key is the review run's key, so ``ReviewLoop`` drains exactly
        # the results it dispatched. Nothing is published to the message bus.
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
        status_obj = self._task_statuses.get(task_id)
        if status_obj is not None and (
            status_obj.duplicate_reads or status_obj.duplicate_searches
        ):
            metadata["subagent_duplicate_reads"] = status_obj.duplicate_reads
            metadata["subagent_duplicate_searches"] = status_obj.duplicate_searches
        if execution_limits is not None:
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
