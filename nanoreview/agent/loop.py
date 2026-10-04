"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
import uuid
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanoreview.agent import model_presets as preset_helpers
from nanoreview.agent.autocompact import AutoCompact
from nanoreview.agent.context import ContextBuilder
from nanoreview.agent.hooks.lifecycle import AgentHook, CompositeHook
from nanoreview.agent.hooks.progress import AgentProgressHook
from nanoreview.agent.memory import Consolidator
from nanoreview.agent.review_loop import (
    ReviewLoop,
    ReviewTurnRequest,
)
from nanoreview.agent.review_state import (
    ReviewRunState,
    ReviewRunStatus,
)
from nanoreview.agent.runner import (
    _MAX_INJECTIONS_PER_TURN,
    AgentRunner,
    AgentRunResult,
    AgentRunSpec,
)
from nanoreview.agent.subagent import SubagentManager
from nanoreview.agent.tools.file_state import (
    FileStateStore,
    bind_file_states,
    reset_file_states,
)
from nanoreview.agent.tools.message import MessageTool
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.command import CommandContext, CommandRouter, register_builtin_commands
from nanoreview.config.schema import AgentDefaults, ModelPresetConfig
from nanoreview.providers.base import LLMProvider
from nanoreview.providers.factory import ProviderSnapshot
from nanoreview.review import apply_review_metadata_from_message
from nanoreview.review.admission import (
    ReviewAdmission,
    ReviewAdmissionRequest,
    ReviewAdmissionService,
)
from nanoreview.review.output.judge import ReviewJudge, ReviewJudgeConfig
from nanoreview.review.profiles import reviewer_execution_profiles
from nanoreview.agent.coordinator import (
    SessionCoordinator,
    _is_review_turn,
)
from nanoreview.session.manager import Session, SessionManager
from nanoreview.utils.artifacts import generated_image_paths_from_messages
from nanoreview.utils.document import extract_documents
from nanoreview.utils.helpers import image_placeholder_text
from nanoreview.utils.helpers import truncate_text as truncate_text_fn
from nanoreview.utils.log_style import log_event
from nanoreview.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE
from nanoreview.utils.session_attachments import merge_turn_media_into_last_assistant
from nanoreview.utils.webui_titles import (
    mark_webui_session,
    maybe_generate_webui_title_after_turn,
)
from nanoreview.utils.webui_turn_helpers import publish_turn_run_status

if TYPE_CHECKING:
    from nanoreview.config.schema import (
        ChannelsConfig,
        ToolsConfig,
    )


UNIFIED_SESSION_KEY = "unified:default"


##状态机
class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    COMMAND = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()


@dataclass
class StateTraceEntry:
    state: TurnState
    started_at: float
    duration_ms: float
    event: str
    error: str | None = None


@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    state: TurnState
    turn_id: str
    session: Session | None = None

    history: list[dict[str, Any]] = field(default_factory=list)
    frozen_messages: list[dict[str, Any]] = field(default_factory=list)
    working_messages: list[dict[str, Any]] = field(default_factory=list)

    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    had_injections: bool = False
    content_replaced: bool = False

    user_persisted_early: bool = False
    save_skip: int = 0

    outbound: OutboundMessage | None = None
    generated_media: list[str] = field(default_factory=list)

    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None

    #: System-level directive injected on the first conversation turn after a
    #: review, naming the review provenance and its read-only status.
    handoff_directive: str | None = None

    pending_queue: asyncio.Queue | None = None
    consumed_subagent_task_ids: set[str] | None = None
    pending_summary: str | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    turn_latency_ms: int | None = None

    trace: list[StateTraceEntry] = field(default_factory=list)


def _is_consumed_subagent_result(
    msg: InboundMessage,
    consumed_task_ids: set[str] | None,
) -> bool:
    if not consumed_task_ids:
        return False
    meta = msg.metadata if isinstance(msg.metadata, dict) else {}
    task_id = meta.get("subagent_task_id")
    return (
        meta.get("injected_event") == "subagent_result"
        and isinstance(task_id, str)
        and task_id in consumed_task_ids
    )


def _stream_chunks(text: str, *, chunk_size: int = 2048) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"

    # Event-driven state transition table.
    # Handlers return an event string; the driver looks up the next state here.
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        max_concurrent_subagents: int | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        review_config: Any | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
        default_reasoning_effort: str | None = None,
    ):
        from nanoreview.config.schema import (
            ToolsConfig,
            _resolve_tool_config_refs,
        )
        _resolve_tool_config_refs()
        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self._provider_snapshot_loader = provider_snapshot_loader
        self._preset_snapshot_loader = preset_snapshot_loader
        self._runtime_model_publisher = runtime_model_publisher
        self._provider_signature = provider_signature
        self._default_selection_signature = preset_helpers.default_selection_signature(
            provider_signature
        )
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length
            if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        self.tools_config = _tc
        # Permission approval policy currently lives on ToolsConfig.
        self.permissions_config = _tc
        self.review_config = review_config
        self.exec_config = _tc.exec
        self._default_reasoning_effort = default_reasoning_effort

        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._total_usage: dict[str, int] = {}
        self._pending_turn_latency_ms: dict[str, int] = {}
        self._pending_turn_traces: dict[str, list[dict[str, Any]]] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(
            workspace, timezone=timezone, disabled_skills=disabled_skills
        )
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            tools_config=_tc,
            max_tool_result_chars=self.max_tool_result_chars,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            max_concurrent_subagents=(
                max_concurrent_subagents
                if max_concurrent_subagents is not None
                else defaults.max_concurrent_subagents
            ),
            reasoning_effort=self._resolve_subagent_reasoning_effort(),
            llm_wall_timeout_for_session=lambda sk: None,
            execution_profiles=reviewer_execution_profiles(),
            # Subagents share the loop's context window so the runner trims
            # reviewer history the same way as the main agent's conversation.
            context_window_tokens=self.context_window_tokens,
            context_block_limit=self.context_block_limit,
        )
        self._unified_session = unified_session
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # Review side: ReviewLoop owns the one-shot run state, its lifecycle
        # and its persistence; SessionCoordinator owns admission, routing
        # between the review and conversation phases, command gating, and the
        # review -> conversation handoff. Neither calls a model or a tool.
        self.review_loop = ReviewLoop(
            workspace=workspace,
            sessions=self.sessions,
            runner=self.runner,
            subagents=self.subagents,
            model=self.model,
            max_tool_result_chars=self.max_tool_result_chars,
            max_concurrent_subagents=int(
                getattr(self.review_config, "max_concurrent_subagents", 4) or 4
            ),
            context_window_tokens=self.context_window_tokens,
            judge_factory=self._build_review_judge,
            evidence_provider_getter=self._review_evidence_provider,
        )
        #: Authoritative in-process review runs, owned by ``ReviewLoop``.
        self._review_runs: dict[str, ReviewRunState] = self.review_loop.runs
        self.review_coordinator = SessionCoordinator(
            sessions=self.sessions,
            workspace=workspace,
            review_loop=self.review_loop,
            context_window_tokens=self.context_window_tokens,
            reserved_output_tokens=int(
                getattr(getattr(provider, "generation", None), "max_tokens", 4096) or 4096
            ),
        )
        self._permission_futures: dict[str, asyncio.Future[bool]] = {}
        # NANOBOT_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = self._parse_max_concurrent_requests()
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``,
        allowing callers to override or extend the standard config-derived
        parameters (e.g. ``cron_service``, ``session_manager``).
        """
        from nanoreview.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = extra.pop("provider", None) or make_provider(config)
        resolved = config.resolve_preset()
        model = extra.pop("model", None) or resolved.model
        context_window_tokens = (
            extra.pop("context_window_tokens", None) or resolved.context_window_tokens
        )
        provider_snapshot_loader = extra.pop("provider_snapshot_loader", None)
        preset_snapshot_loader = extra.pop(
            "preset_snapshot_loader", None
        ) or preset_helpers.make_preset_snapshot_loader(
            config,
            provider_snapshot_loader,
        )
        return cls(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=model,
            max_iterations=defaults.max_tool_iterations,
            context_window_tokens=context_window_tokens,
            context_block_limit=defaults.context_block_limit,
            max_tool_result_chars=defaults.max_tool_result_chars,
            provider_retry_mode=defaults.provider_retry_mode,
            tool_hint_max_length=defaults.tool_hint_max_length,
            max_concurrent_subagents=defaults.max_concurrent_subagents,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            channels_config=config.channels,
            timezone=defaults.timezone,
            unified_session=defaults.unified_session,
            disabled_skills=defaults.disabled_skills,
            session_ttl_minutes=defaults.session_ttl_minutes,
            consolidation_ratio=defaults.consolidation_ratio,
            max_messages=defaults.max_messages,
            tools_config=config.tools,
            review_config=config.review,
            model_presets=preset_helpers.configured_model_presets(config),
            model_preset=defaults.model_preset,
            provider_snapshot_loader=provider_snapshot_loader,
            preset_snapshot_loader=preset_snapshot_loader,
            default_reasoning_effort=defaults.reasoning_effort,
            **extra,
        )

    def _resolve_subagent_reasoning_effort(self) -> str | None:
        """Resolve subagent reasoning effort with config inheritance.

        Priority:
        1. ``review.subagent_reasoning_effort`` (explicit, including ``"none"``)
        2. ``agents.defaults.reasoning_effort``
        3. ``None`` (provider default behaviour)
        """
        subagent_effort = getattr(self.review_config, "subagent_reasoning_effort", None)
        if subagent_effort is not None:
            return subagent_effort
        return self._default_reasoning_effort

    def _sync_subagent_runtime_limits(self) -> None:
        """Keep subagent runtime limits aligned with mutable loop settings."""
        self.subagents.max_iterations = self.max_iterations
        self.subagents.reasoning_effort = self._resolve_subagent_reasoning_effort()

    def _apply_provider_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        publish_update: bool = True,
        model_preset: str | None = None,
    ) -> None:
        """Swap model/provider for future turns without disturbing an active one."""
        provider = snapshot.provider
        model = snapshot.model
        context_window_tokens = snapshot.context_window_tokens
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.runner.provider = provider
        self.subagents.set_provider(provider, model, context_window_tokens)
        self.consolidator.set_provider(provider, model, context_window_tokens)
        self._provider_signature = snapshot.signature
        if publish_update and self._runtime_model_publisher is not None:
            self._runtime_model_publisher(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        log_event(
            logger,
            "info",
            "agent.model.switched",
            status="success",
            old_model=old_model,
            model=model,
        )

    def _refresh_provider_snapshot(self) -> None:
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("Failed to refresh provider config")
            return
        default_selection = preset_helpers.default_selection_signature(
            snapshot.signature
        )
        if self._active_preset and self._default_selection_signature in (
            None,
            default_selection,
        ):
            self._default_selection_signature = default_selection
            try:
                snapshot = self._build_model_preset_snapshot(self._active_preset)
            except Exception:
                logger.exception("Failed to refresh active model preset")
                return
        else:
            self._active_preset = None
            self._default_selection_signature = default_selection
        if snapshot.signature == self._provider_signature:
            return
        self._default_selection_signature = preset_helpers.default_selection_signature(
            snapshot.signature
        )
        self._apply_provider_snapshot(snapshot)

    @property
    def model_preset(self) -> str | None:
        return self._active_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        self.set_model_preset(name)

    def _build_model_preset_snapshot(self, name: str) -> ProviderSnapshot:
        return preset_helpers.build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=self.provider,
            loader=self._preset_snapshot_loader,
        )

    def set_model_preset(
        self, name: str | None, *, publish_update: bool = True
    ) -> None:
        """Resolve a preset by name and apply all runtime model dependents."""
        name = preset_helpers.normalize_preset_name(name, self.model_presets)
        snapshot = self._build_model_preset_snapshot(name)
        self._apply_provider_snapshot(
            snapshot, publish_update=publish_update, model_preset=name
        )
        self._active_preset = name

    def _register_default_tools(self) -> None:
        """Register the default set of tools via plugin loader."""
        from nanoreview.agent.tools.context import ToolContext
        from nanoreview.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            workspace=str(self.workspace),
            provider=self.provider,
            model=self.model,
            review_config=self.review_config,
            bus=self.bus,
            subagent_manager=self.subagents,
            sessions=self.sessions,
            provider_snapshot_loader=self._provider_snapshot_loader,
            timezone=self.context.timezone or "UTC",
        )
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools)

        log_event(
            logger,
            "info",
            "agent.tools.registered",
            status="success",
            count=len(registered),
            tools=",".join(registered),
        )

    def _build_review_judge(self) -> ReviewJudge | None:
        """Build the judge on the coordinator/plan runner and model.

        The judge never resolves its own model or provider: it reuses
        ``self.runner`` and ``self.model`` so a review batch runs on exactly the
        same execution path as the plan batch. ``review.judge.model_preset`` is
        deliberately ignored here (kept only as a legacy config field), so the
        judge can never silently fork onto a different model than the plan.
        """
        judge_settings = getattr(self.review_config, "judge", None)
        if judge_settings is not None and not getattr(judge_settings, "enabled", True):
            return None
        config = ReviewJudgeConfig(
            enabled=bool(getattr(judge_settings, "enabled", True)),
            timeout_seconds=int(getattr(judge_settings, "timeout_seconds", 60)),
            max_tokens=int(getattr(judge_settings, "max_tokens", 2048)),
            context_window_tokens=int(self.context_window_tokens or 0) or None,
        )
        return ReviewJudge(runner=self.runner, model=self.model, config=config)

    def _review_evidence_provider(self) -> Any | None:
        """Return the review tool's shared evidence service, if registered.

        The provider is what makes evidence prefetch possible, so ``ReviewLoop``
        resolves it lazily: tools are registered after the loop is built.
        """
        tool = self.tools.get("local_review") or self.tools.get("github_review")
        if tool is None:
            return None
        return getattr(tool, "evidence_provider", None)

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        from nanoreview.agent.tools.context import (
            ContextAware,
            RequestContext,
            set_current_request_context,
        )

        if session_key is not None:
            effective_key = session_key
        elif self._unified_session:
            effective_key = UNIFIED_SESSION_KEY
        else:
            effective_key = f"{channel}:{chat_id}"

        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=dict(metadata or {}),
        )

        for name in self.tools.tool_names:
            tool = self.tools.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)
        set_current_request_context(request_ctx)

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """Return the chat id shown in runtime metadata for the model."""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """Build a progress callback that publishes to the message bus."""

        async def _bus_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict[str, Any]] | None = None,
            reasoning: bool = False,
            reasoning_end: bool = False,
        ) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            if reasoning:
                meta["_reasoning_delta"] = True
            if reasoning_end:
                meta["_reasoning_end"] = True
            if tool_events:
                meta["_tool_events"] = tool_events
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _bus_progress

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str], Awaitable[None]]:
        """Build a retry-wait callback that publishes to the message bus."""

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _on_retry_wait

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = {"media": list(media_paths)} if media_paths else {}
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Build the frozen/working partition for the LLM turn.

        Frozen is the system prompt plus the current user turn; working is the
        replayed session history that run-level compression may summarize.
        """
        return self.context.build_partitioned_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata,
        )


    @staticmethod
    def _apply_handoff_directive(
        frozen_messages: list[dict[str, Any]], directive: str | None
    ) -> None:
        """Append the review handoff directive to the frozen system prompt.

        The frozen zone is copied verbatim into every request and is never
        summarized by run-level compression, so the review's provenance and
        its read-only constraint survive even if the replayed history is later
        compacted.
        """
        if not directive or not frozen_messages:
            return
        first = frozen_messages[0]
        if first.get("role") != "system":
            return
        content = first.get("content")
        if isinstance(content, str):
            first["content"] = f"{content}\n\n{directive}" if content else directive
        elif isinstance(content, list):
            content.append({"type": "text", "text": directive})

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t
        sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    # -- Review coordination delegation --------------------------------------
    #
    # The loop owns the turn semantics (bus, queues, cancellation), not the
    # review lifecycle. Registration, state transitions, terminal persistence
    # and the review -> conversation routing all live behind the coordinator
    # and ``ReviewLoop``; the methods below only adapt them to the loop's
    # synchronous gate and its async turn handlers.

    def _review_gate_blocks(
        self,
        msg: InboundMessage,
        review_state: ReviewRunState | None,
        raw: str,
    ) -> bool:
        """Whether an inbound message must be rejected by the review gate."""
        return (
            self.review_coordinator.gate_message(msg, review_state, raw) is not None
        )

    def review_admissions(self) -> ReviewAdmissionService:
        """Shared admission/domain service used by every review entry point."""
        return self.review_coordinator.admissions()

    def admit_review(self, request: ReviewAdmissionRequest) -> ReviewAdmission:
        """Validate, snapshot, and register one review run before delivery.

        This is the single admission boundary transports call *before*
        publishing an execution task. Rejection raises
        ``nanoreview.review.admission.ReviewAdmissionError`` and leaves no
        session, run, snapshot, or history behind; acceptance registers the
        in-process run and persists plan metadata plus the immutable input
        snapshot reference.
        """
        session_key = (request.session_key or "").strip()
        if not session_key:
            request = dataclasses.replace(
                request, session_key=f"review:{uuid.uuid4().hex[:12]}"
            )
        return self.review_coordinator.admit(request)

    async def _publish_review_gate_response(
        self, msg: InboundMessage, response: OutboundMessage
    ) -> None:
        """Publish a gate rejection plus a websocket turn-end marker."""
        await self.bus.publish_outbound(response)
        if msg.channel == "websocket":
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata={**dict(msg.metadata or {}), "_turn_end": True},
                )
            )

    def reset_review_run(self, session_key: str) -> None:
        """Drop the one-shot review gate for a session (used by /new)."""
        self.review_loop.reset(session_key)

    def _review_command_gate(
        self, session: Session, msg: InboundMessage, raw: str
    ) -> OutboundMessage | None:
        """Gate slash commands inside a review session."""
        return self.review_coordinator.gate_command(session, msg, raw)

    def _is_admitted_review_turn(
        self, metadata: dict[str, Any] | None, session_key: str | None
    ) -> bool:
        """Whether this turn is the admitted execution of the live review run.

        Admission is the only accepted review entry point, so a turn enters
        the review pipeline only when it carries the ``_review_admitted``
        marker of a live, still-running run for its session. A session keeps
        its review target metadata after the review ends, so the persisted
        target alone must never re-enter review.
        """
        if not session_key:
            return False
        run_id = (metadata or {}).get("_review_admitted")
        if not isinstance(run_id, str) or not run_id:
            return False
        run = self.review_loop.get(session_key)
        return (
            run is not None
            and run.run_id == run_id
            and run.status is ReviewRunStatus.RUNNING
        )

    def _review_turn_gate(
        self, ctx: TurnContext, raw: str
    ) -> OutboundMessage | None:
        """Apply every review-phase gate that can refuse an ordinary turn.

        Ordered most specific first: a live running review, then a duplicate
        or unadmitted review turn, and finally the first conversation turn's
        handoff size check.
        """
        session = ctx.session
        if session is None:
            return None
        live_run = self.review_loop.get(session.key)
        response = self.review_coordinator.gate_message(ctx.msg, live_run, raw)
        if response is not None:
            return response
        response = self.review_coordinator.gate_review_turn(
            session.key, ctx.msg, live_run
        )
        if response is not None:
            return response
        handoff = self.review_coordinator.pending_handoff(session)
        if handoff is not None and not handoff.fits:
            return self.review_coordinator.report_too_large_response(ctx.msg, handoff)
        return None

    async def _finalize_review_run(
        self,
        session_key: str,
        status: ReviewRunStatus,
        *,
        warning: str | None = None,
    ) -> None:
        """Move a running review run to a terminal status and persist metadata."""
        await self.review_coordinator.finalize(session_key, status, warning=warning)

    async def _settle_review_run_after_stop(self, session_key: str) -> str | None:
        """Settle a review run left ``running`` after ``/stop`` cancelled its turn.

        ``/stop`` cancels active turn tasks, but a run whose turn already
        returned normally while its cleanup or terminal save failed has no live
        task to cancel, so the session gate would otherwise stay closed
        forever. When the session still owns a running review run, retry the
        terminal transition as ``stopped`` and return a short report of the
        outcome; return ``None`` when there is nothing to settle.
        """
        state = self.review_loop.get(session_key)
        if state is None or state.status is not ReviewRunStatus.RUNNING:
            return None
        result = await self.review_coordinator.finalize(
            session_key, ReviewRunStatus.STOPPED
        )
        if result is None:
            return (
                "The review run could not be settled; restart nanoreview to "
                "recover the session."
            )
        return f"Settled review run {result.run_id} as stopped."

    async def _execute_review_turn(
        self,
        *,
        session: Session | None,
        session_key: str,
        channel: str,
        chat_id: str,
        message_id: str | None,
        metadata: dict[str, Any] | None,
        messages: list[dict[str, Any]],
        on_progress: Callable[..., Awaitable[None]] | None,
    ) -> AgentRunResult:
        """Delegate one admitted review turn to ``ReviewLoop``.

        The loop keeps the turn semantics (bus, queues, cancellation, history
        persistence); ``ReviewLoop`` owns the review lifecycle. The callback
        below is the loop's session-persistence responsibility, so reviewer
        results stay durable in the session while the run executes.
        """

        async def _persist_automatic_subagent_result(
            subagent_message: InboundMessage,
        ) -> None:
            if session is None:
                return
            if self._persist_subagent_followup(session, subagent_message):
                self.sessions.save(session)

        outcome = await self.review_loop.execute(
            ReviewTurnRequest(
                session_key=session_key,
                session=session,
                messages=list(messages),
                metadata=dict(metadata or {}),
                channel=channel,
                chat_id=chat_id,
                message_id=message_id,
                progress_callback=on_progress,
                result_callback=_persist_automatic_subagent_result,
            )
        )
        final_content = outcome.report_markdown
        # A report was produced but the run could not settle (cleanup or
        # terminal save failed): append the bounded reason so the user learns
        # the session is still gated, instead of only seeing the report.
        if outcome.produces_report and outcome.error:
            final_content = (
                f"{final_content}\n\n> Review settlement failed: {outcome.error}"
            )
        return AgentRunResult(
            final_content=final_content,
            messages=list(messages),
            stop_reason=outcome.stop_reason,
            error=outcome.error,
            content_replaced=outcome.produces_report,
        )

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the context window."""
        if self.context_window_tokens <= 0:
            return 0
        max_output = getattr(
            getattr(self.provider, "generation", None), "max_tokens", 4096
        )
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        frozen_messages: list[dict],
        working_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        consumed_subagent_task_ids: set[str] | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        Returns (final_content, tools_used, messages, stop_reason, had_injections, content_replaced).
        """
        self._sync_subagent_runtime_limits()

        active_session_key = session.key if session else session_key
        buffered_pending: list[InboundMessage] = []
        injected_subagent_task_ids: set[str] = set()

        def _running_subagents() -> int:
            if not active_session_key:
                return 0
            getter = getattr(self.subagents, "get_running_count_by_session", None)
            if not callable(getter):
                return 0
            return int(getter(active_session_key))

        def _is_subagent_result(msg: InboundMessage) -> bool:
            return (
                msg.sender_id == "subagent"
                or (msg.metadata or {}).get("injected_event") == "subagent_result"
            )

        def _drain_subagent_results(limit: int) -> list[InboundMessage]:
            if not active_session_key:
                return []
            drain = getattr(self.subagents, "drain_session_results", None)
            if not callable(drain):
                return []
            return list(drain(active_session_key, limit=limit))

        async def _wait_subagent_result() -> InboundMessage | None:
            if not active_session_key:
                return None
            wait = getattr(self.subagents, "wait_for_session_result", None)
            if not callable(wait):
                return None
            return await wait(active_session_key, timeout=0.1)

        # Only the turn admitted for the live review run enters the review
        # pipeline. A session keeps its review_target metadata after the
        # review ends (WebUI/session detail read it), so the persisted target
        # alone must never be enough to re-enter review: a conversation turn
        # would otherwise silently re-run the review it is supposed to discuss.
        review_turn = self._is_admitted_review_turn(metadata, active_session_key)
        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=self.tool_hint_max_length,
            set_tool_context=self._set_tool_context,
            on_iteration=lambda iteration: setattr(
                self, "_current_iteration", iteration
            ),
            suppress_content_progress=review_turn,
        )
        hooks: list[AgentHook] = [loop_hook]
        hooks.extend(self._extra_hooks)
        hook: AgentHook = CompositeHook(hooks) if len(hooks) > 1 else loop_hook

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        async def _drain_pending(
            *, limit: int = _MAX_INJECTIONS_PER_TURN
        ) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            Subagent results are a hard dependency: while same-session
            subagents are running, wait for each completed result and inject it
            immediately so the turn can integrate it. Non-subagent messages are
            buffered until subagents have finished.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                user_content = pending_msg.content
                message: dict[str, Any] = {"role": "user", "content": user_content}
                if pending_msg.metadata:
                    message["_metadata"] = dict(pending_msg.metadata)
                return message

            items: list[dict[str, Any]] = []

            def _accept_pending(pending_msg: InboundMessage) -> bool:
                if _is_subagent_result(pending_msg):
                    task_id = (pending_msg.metadata or {}).get("subagent_task_id")
                    if isinstance(task_id, str) and task_id:
                        if task_id in injected_subagent_task_ids:
                            return False
                        injected_subagent_task_ids.add(task_id)
                        if consumed_subagent_task_ids is not None:
                            consumed_subagent_task_ids.add(task_id)
                    items.append(_to_user_message(pending_msg))
                    return True
                buffered_pending.append(pending_msg)
                return False

            while buffered_pending and len(items) < limit and _running_subagents() == 0:
                items.append(_to_user_message(buffered_pending.pop(0)))

            for result_msg in _drain_subagent_results(limit - len(items)):
                _accept_pending(result_msg)

            while len(items) < limit:
                try:
                    pending_msg = pending_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                _accept_pending(pending_msg)

            while len(items) < limit and _running_subagents() > 0 and not items:
                pending_msg = await _wait_subagent_result()
                if pending_msg is None and not callable(
                    getattr(self.subagents, "wait_for_session_result", None)
                ):
                    try:
                        pending_msg = await asyncio.wait_for(
                            pending_queue.get(), timeout=0.1
                        )
                    except asyncio.TimeoutError:
                        pending_msg = None
                if pending_msg is None:
                    continue
                _accept_pending(pending_msg)

            while buffered_pending and len(items) < limit and _running_subagents() == 0:
                items.append(_to_user_message(buffered_pending.pop(0)))

            return items

        file_state_token = bind_file_states(
            self._file_state_store.for_session(active_session_key)
        )
        try:
            # An admitted review turn is handed to ``ReviewLoop`` whole: it
            # resolves the plan, evidence and prompt, drives reviewers and the
            # Judge, persists the report artifact and settles the terminal
            # state. The loop only delivers the produced report.
            if review_turn:
                result = await self._execute_review_turn(
                    session=session,
                    session_key=active_session_key or "",
                    channel=channel,
                    chat_id=chat_id,
                    message_id=message_id,
                    metadata=metadata,
                    messages=[*frozen_messages, *working_messages],
                    on_progress=on_progress,
                )
            else:
                from nanoreview.agent.tools.permissions import resolve_policy

                permission_policy = resolve_policy(
                    getattr(self, "permissions_config", None),
                    session.metadata if session is not None else {},
                )

                async def _permission_request_cb(
                    request_id: str,
                    payload: dict[str, Any],
                    future: asyncio.Future[bool],
                ) -> bool:
                    self._permission_futures[request_id] = future
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=channel,
                            chat_id=chat_id,
                            content="",
                            metadata={"_permission_request": payload},
                        )
                    )
                    try:
                        return await asyncio.wait_for(future, timeout=300)
                    except asyncio.TimeoutError:
                        return False
                    finally:
                        self._permission_futures.pop(request_id, None)

                result = await self.runner.run(
                    AgentRunSpec(
                        frozen_messages=frozen_messages,
                        working_messages=working_messages,
                        tools=self.tools,

                        model=self.model,
                        max_iterations=self.max_iterations,
                        max_tool_result_chars=self.max_tool_result_chars,
                        hook=hook,
                        error_message="Sorry, I encountered an error calling the AI model.",
                        concurrent_tools=True,
                        workspace=self.workspace,
                        session_key=session.key if session else None,
                        context_window_tokens=self.context_window_tokens,
                        context_block_limit=self.context_block_limit,
                        provider_retry_mode=self.provider_retry_mode,
                        progress_callback=on_progress,
                        stream_progress_deltas=on_stream is not None,
                        retry_wait_callback=on_retry_wait,
                        checkpoint_callback=_checkpoint,
                        injection_callback=_drain_pending,
                        llm_timeout_s=None,
                        permission_policy=permission_policy,
                        permission_request_callback=_permission_request_cb,
                    )
                )
        finally:
            reset_file_states(file_state_token)
        self._last_usage = result.usage
        self._accumulate_total_usage(result.usage)
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            # Push final content through stream so the WebSocket UI does not
            # leave the final response empty.
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return (
            result.final_content,
            result.tools_used,
            result.messages,
            result.stop_reason,
            result.had_injections,
            result.content_replaced,
        )

    def _accumulate_total_usage(self, usage: dict[str, int]) -> None:
        usage_total: int | None = None
        fallback_total = 0
        for key, value in usage.items():
            try:
                amount = int(value or 0)
            except (TypeError, ValueError):
                continue
            if key == "total_tokens":
                usage_total = amount
            elif key.endswith("_tokens"):
                fallback_total += amount
            self._total_usage[key] = self._total_usage.get(key, 0) + amount
        if usage_total is None:
            self._total_usage["total_tokens"] = (
                self._total_usage.get("total_tokens", 0) + fallback_total
            )

    @staticmethod
    def _parse_max_concurrent_requests() -> int:
        raw = os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3")
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid NANOBOT_MAX_CONCURRENT_REQUESTS={!r}; using default 3",
                raw,
            )
            return 3

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""

        self._running = True

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            if msg.metadata.get("_permission_response"):
                resp = msg.metadata["_permission_response"]
                req_id = resp.get("request_id")
                approved = resp.get("approved", False)
                fut = self._permission_futures.pop(req_id, None)
                if fut and not fut.done():
                    fut.set_result(bool(approved))
                continue
            if msg.metadata.get("_permission_disconnect"):
                for req_id, fut in list(self._permission_futures.items()):
                    if not fut.done():
                        fut.set_result(False)
                self._permission_futures.clear()
                continue
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg,
                    msg.session_key,
                    raw,
                    self.commands.dispatch_priority,
                )
                continue
            effective_key = self._effective_session_key(msg)
            # Review phase gate: only a *running* review refuses ordinary
            # messages. Once the run is terminal its resources are cleaned up
            # and its result is persisted, so the session has moved on to
            # conversation. Commands (/status, /stop, …) and internal
            # subagent/system events stay available throughout.
            review_state = self.review_loop.get(effective_key)
            gate_response = self.review_coordinator.gate_message(
                msg, review_state, raw
            )
            if gate_response is not None:
                await self._publish_review_gate_response(msg, gate_response)
                continue
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg,
                        effective_key,
                        raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    log_event(
                        logger,
                        "info",
                        "agent.pending_queue.routed",
                        status="success",
                        session=effective_key,
                        source=msg.metadata.get("injected_event") or "user_message",
                        sender=msg.sender_id,
                        content_chars=len(msg.content or ""),
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._remove_active_task(k, t)
            )

    def _remove_active_task(self, key: str, task: asyncio.Task) -> None:
        tasks = self._active_tasks.get(key)
        if not tasks:
            return
        with suppress(ValueError):
            tasks.remove(task)
        if not tasks:
            self._active_tasks.pop(key, None)
            lock = self._session_locks.get(key)
            if lock is not None:
                self._cleanup_session_lock(key, lock)

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        # Admission already registered the run before delivery, so the loop
        # never registers review state itself. A review-shaped turn that did
        # not come from an admitted run is refused instead of silently
        # creating a second run: a session executes at most one review.
        admission_rejection = self.review_coordinator.gate_review_turn(
            session_key, msg, self.review_loop.get(session_key)
        )
        if admission_rejection is not None:
            await self._publish_review_gate_response(msg, admission_rejection)
            return
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        pending = asyncio.Queue(maxsize=20)
        consumed_subagent_task_ids: set[str] = set()
        self._pending_queues[session_key] = pending
        turn_end_sent = False
        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
        stream_segment = 0
        review_stream = _is_review_turn(msg.metadata)
        wants_stream = bool(msg.metadata.get("_wants_stream"))

        def _current_stream_id() -> str:
            return f"{stream_base_id}:{stream_segment}"

        async def publish_forced_turn_end() -> None:
            nonlocal turn_end_sent, stream_segment
            if msg.channel != "websocket" or turn_end_sent:
                return
            if wants_stream and review_stream and stream_segment == 0:
                meta = dict(msg.metadata or {})
                meta["_stream_end"] = True
                meta["_resuming"] = False
                meta["_stream_id"] = _current_stream_id()
                meta["_stream_kind"] = "review_thinking"
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="",
                        metadata=meta,
                    )
                )
                stream_segment += 1
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata={
                        **dict(msg.metadata or {}),
                        "_turn_end": True,
                    },
                )
            )
            turn_end_sent = True

        try:
            async with lock, gate:
                try:
                    on_stream = on_stream_end = None
                    if wants_stream:

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True  # 标记这是流增量
                            meta["_stream_id"] = _current_stream_id()  # 该段的唯一 ID
                            if review_stream:
                                meta["_stream_kind"] = "review_thinking"
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content=delta,
                                    metadata=meta,
                                )
                            )

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True  # 标记段结束
                            meta["_resuming"] = resuming  # 告诉前端是否继续等待
                            meta["_stream_id"] = _current_stream_id()
                            if review_stream:
                                meta["_stream_kind"] = "review_thinking"
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content="",
                                    metadata=meta,
                                )
                            )
                            stream_segment += 1  # 准备下一段

                    response = await self._process_message(
                        msg,
                        on_stream=on_stream,
                        on_stream_end=on_stream_end,
                        pending_queue=pending,
                        consumed_subagent_task_ids=consumed_subagent_task_ids,
                    )

                    if response is not None:
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata=msg.metadata or {},
                            )
                        )

                    if msg.channel == "websocket":
                        turn_lat = self._pending_turn_latency_ms.pop(session_key, None)
                        turn_trace = self._pending_turn_traces.pop(session_key, None)
                        turn_metadata: dict[str, Any] = {
                            **msg.metadata,
                            "_turn_end": True,
                        }  # 关键标记
                        if turn_lat is not None:
                            turn_metadata["latency_ms"] = int(
                                turn_lat
                            )  # 这一轮用了多长时间
                        if turn_trace:
                            turn_metadata["turn_trace"] = turn_trace
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata=turn_metadata,
                            )
                        )
                        turn_end_sent = True
                        if msg.metadata.get("webui") is True:

                            async def _generate_title_and_notify() -> None:
                                generated = await maybe_generate_webui_title_after_turn(
                                    channel=msg.channel,
                                    metadata=msg.metadata,
                                    sessions=self.sessions,
                                    session_key=session_key,
                                    provider=self.provider,
                                    model=self.model,
                                )
                                if generated:
                                    await self.bus.publish_outbound(
                                        OutboundMessage(
                                            channel=msg.channel,
                                            chat_id=msg.chat_id,
                                            content="",
                                            metadata={
                                                **msg.metadata,
                                                "_session_updated": True,
                                            },
                                        )
                                    )

                            self._schedule_background(_generate_title_and_notify())

                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # The terminal transition (``stopped``) is retried by the
                    # outer cancellation handler, which also covers a cancel
                    # that landed while this turn was still queued on the
                    # session lock or the concurrency gate.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    if msg.channel == "websocket":
                        await publish_forced_turn_end()
                    raise
                except Exception:
                    logger.exception(
                        "Error processing message for session {}", session_key
                    )
                    await self._finalize_review_run(
                        session_key,
                        ReviewRunStatus.ERROR,
                        warning="Review turn failed with an unexpected error.",
                    )
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="Sorry, I encountered an error.",
                        )
                    )
                    if msg.channel == "websocket":
                        await publish_forced_turn_end()

        except asyncio.CancelledError:
            # A cancel can land while this turn is still queued on the session
            # lock or the cross-session concurrency gate, before the inner try
            # above is entered. Settle the admitted run as ``stopped`` here so
            # the finally block does not discard a run whose admission metadata
            # was already persisted as ``running``. A run that stays ``running``
            # is normalized on restart, never published as ``DONE``.
            with suppress(asyncio.CancelledError):
                await self._finalize_review_run(
                    session_key, ReviewRunStatus.STOPPED
                )
            raise
        finally:
            # A review run that never entered the review pipeline (turn failed
            # or was rerouted before planning) releases the gate instead of
            # closing the session permanently.
            self.review_loop.discard_unstarted(session_key)
            queue = self._pending_queues.pop(session_key, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()  # 非阻塞方式取
                    except asyncio.QueueEmpty:
                        break  # 队列空了
                    if _is_consumed_subagent_result(item, consumed_subagent_task_ids):
                        continue
                    # 重新发回总线，让 run() 循环再次处理
                    await self.bus.publish_inbound(item)
                    leftover += 1
                if leftover:
                    logger.info(
                        "Re-published {} leftover message(s) to bus for session {}",
                        leftover,
                        session_key,
                    )

            await publish_turn_run_status(self.bus, msg, "idle")
            # 清除本轮的延迟记录
            self._pending_turn_latency_ms.pop(session_key, None)
            self._pending_turn_traces.pop(session_key, None)
            self._cleanup_session_lock(session_key, lock)

    def _cleanup_session_lock(self, session_key: str, lock: asyncio.Lock) -> None:
        if lock.locked():
            return
        if self._pending_queues.get(session_key) is not None:
            return
        if self._active_tasks.get(session_key):
            return
        if self._session_locks.get(session_key) is lock:
            self._session_locks.pop(session_key, None)

    async def close_background_tasks(self) -> None:
        """Drain pending background tasks."""
        if self._background_tasks:
            tasks = list(self._background_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in tasks:
                self._remove_background_task(task)

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._remove_background_task)

    def _remove_background_task(self, task: asyncio.Task) -> None:
        with suppress(ValueError):
            self._background_tasks.remove(task)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        log_event(logger, "info", "agent.loop.stopping", status="running")

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. subagent announce)."""
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        )
        log_event(
            logger,
            "info",
            "agent.system_message.processing",
            status="running",
            sender=msg.sender_id,
        )
        key = msg.session_key_override or f"{channel}:{chat_id}"
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)
        if pending:
            log_event(
                logger,
                "info",
                "agent.memory_compact.triggered",
                status="start",
                session=key,
            )

        await self.consolidator.maybe_consolidate_by_tokens(
            session,
            replay_max_messages=self._max_messages,
        )
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            self.sessions.save(session)
        self._set_tool_context(
            channel,
            chat_id,
            msg.metadata.get("message_id"),
            msg.metadata,
            session_key=key,
        )
        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        history = session.get_history(**_hist_kwargs)
        current_role = "assistant" if is_subagent else "user"

        frozen_messages, working_messages = self.context.build_partitioned_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_summary=pending,
            session_metadata=session.metadata,
        )
        t_wall = time.time()
        final_content, _, all_msgs, stop_reason, _, _ = await self._run_agent_loop(
            frozen_messages,
            working_messages,
            session=session,

            channel=channel,
            chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
        )
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        self._save_turn(session, all_msgs, 1 + len(history), turn_latency_ms=latency_ms)
        if channel == "websocket":
            self._pending_turn_latency_ms[key] = latency_ms
        session.enforce_file_cap()
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                session,
                replay_max_messages=self._max_messages,
            )
        )
        content = final_content or "Background task completed."
        outbound_metadata: dict[str, Any] = {}
        if origin_message_id := msg.metadata.get("origin_message_id"):
            outbound_metadata["origin_message_id"] = origin_message_id
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata=outbound_metadata,
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        consumed_subagent_task_ids: set[str] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response.

        这个方法是一个「状态机引擎」，由以下状态组成：
        1. RESTORE：恢复上次中断的检查点
        2. COMPACT：根据需要进行会话池压缩
        3. COMMAND：判断是否是命令（如 /stop）
        4. BUILD：构建 LLM 的初始提示词
        5. RUN：实际调用 LLM 并执行工具
        6. SAVE：保存轮下消息到 session
        7. RESPOND：滄下最终的回复
        8. DONE：结束

        状态每次转换是由「事件」驱动的，不是固定顺序。
        例如快捷命令可以跳过 BUILD/RUN/SAVE，直接转到 DONE。
        """
        self._refresh_provider_snapshot()

        if msg.channel == "system":
            return await self._process_system_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
            )

        key = session_key or msg.session_key
        # 创建本轮转的上下文对象，保存所有中间信息
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,  # 从 RESTORE 状态开始
            turn_id=f"{key}:{time.time_ns()}",  # 本轮的唯一 ID
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            consumed_subagent_task_ids=consumed_subagent_task_ids,
        )

        # 状态机主循环
        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise RuntimeError(f"Missing state handler for {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)  # 状态处理器返回事件字符串
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event="",
                        error="exception",
                    )
                )
                self._remember_turn_trace(ctx.session_key, ctx.trace)
                raise

            duration = (time.perf_counter() - t0) * 1000
            # 记录每个状态的执行时间，用于性能分析
            ctx.trace.append(
                StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event=event,
                )
            )
            logger.debug(
                "[turn {}] State {} took {:.1f}ms -> event {}",
                ctx.turn_id,
                ctx.state.name,
                duration,
                event,
            )

            # 查转移表，决定下一个状态
            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] No transition from {ctx.state} "
                    f"on event {event!r}"
                )
            ctx.state = next_state

        logger.info(
            "[turn {}] Turn completed after {} states: {}",
            ctx.turn_id,
            len(ctx.trace),
            ", ".join(
                f"{entry.state.name}={entry.duration_ms:.1f}ms" for entry in ctx.trace
            ),
        )
        self._remember_turn_trace(ctx.session_key, ctx.trace)
        # Publish the review_context index once the turn that produced the
        # report has fully finished, so any later conversation turn (or a
        # restart) reads a settled result. The run's terminal metadata is
        # written by ``ReviewLoop`` itself, not here.
        if ctx.session is not None:
            self.review_coordinator.write_context_index(ctx.session)
        return ctx.outbound

    @staticmethod
    def _serialize_turn_trace(entries: list[StateTraceEntry]) -> list[dict[str, Any]]:
        trace: list[dict[str, Any]] = []
        for entry in entries:
            item: dict[str, Any] = {
                "state": entry.state.name,
                "event": entry.event,
                "duration_ms": max(0, int(round(entry.duration_ms))),
            }
            if entry.error:
                item["error"] = entry.error
            trace.append(item)
        return trace

    def _remember_turn_trace(
        self, session_key: str, entries: list[StateTraceEntry]
    ) -> None:
        trace = self._serialize_turn_trace(entries)
        if trace:
            self._pending_turn_traces[session_key] = trace

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        generated_media: list[str],
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (
            (mt := self.tools.get("message"))
            and isinstance(mt, MessageTool)
            and mt._sent_in_turn
        ):
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = (
            final_content[:120] + "..." if len(final_content) > 120 else final_content
        )
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        # Error-like stop reasons must not be marked as already streamed: the
        # final error message was never streamed to the channel, so it has to
        # be delivered by the outbound message itself. This includes the
        # run-level compression stops, which end the run before the business
        # request that would have streamed any content.
        unstreamed_stop_reasons = {
            "error",
            "tool_error",
            "compression_failed",
            "compression_limit",
        }
        if on_stream is not None and stop_reason not in unstreamed_stop_reasons:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            media=generated_media,
            metadata=meta,
        )

    async def _state_restore(self, ctx: TurnContext) -> TurnState:
        """Restore checkpoint / pending user turn; extract documents.

        RESTORE 是第一个状态，职责是恢复程序的故障。

        场景 1：若上次轮转在工具执行中遇到崩溃，
                  检查点被保存到 session metadata 中。
                  此时宁安抽取已执行的工具结果和 assistant 消息。

        场景 2：若用户消息已经丢进 session，但 assistant 消息没有答复（不常见），
                  里面済一个错误提示。
        """
        msg = ctx.msg

        if msg.media:
            new_content, image_only = extract_documents(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(
            "Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview
        )

        # 确保 session 存在
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        mark_webui_session(ctx.session, msg.metadata)
        if apply_review_metadata_from_message(ctx.session, msg.metadata):
            self.sessions.save(ctx.session)

        # 尝试恢复检查点
        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
        # 尝试恢复待处理的用户轮次
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)

        return "ok"  # 整个恢复步骤完成，下一个状态是 COMPACT

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.session, pending = self.auto_compact.prepare_session(
            ctx.session, ctx.session_key
        )
        ctx.pending_summary = pending
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        raw = ctx.msg.content.strip()
        # Review phase gates. A live running review refuses ordinary messages
        # and any review-shaped turn outright. A terminal review lets the
        # session through to conversation, but the first turn must be able to
        # carry the complete report: an oversized report rejects the turn with
        # an explicit reason instead of being silently summarized. Rejections
        # happen before BUILD/SAVE, so nothing is written to history.
        if ctx.session is not None and not raw.startswith("/"):
            gate_response = self._review_turn_gate(ctx, raw)
            if gate_response is not None:
                ctx.outbound = gate_response
                return "shortcut"
        if ctx.session is not None and raw.startswith("/"):
            command_gate = self._review_command_gate(ctx.session, ctx.msg, raw)
            if command_gate is not None:
                ctx.outbound = command_gate
                return "shortcut"
        cmd_ctx = CommandContext(
            msg=ctx.msg, session=ctx.session, key=ctx.session_key, raw=raw, loop=self
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message("assistant", result.content, _command=True)
                self.sessions.save(ctx.session)
                self._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    async def _state_build(self, ctx: TurnContext) -> str:
        """Build the prompt, history, tool context, and progress callbacks."""
        await self.consolidator.maybe_consolidate_by_tokens(
            ctx.session,
            replay_max_messages=self._max_messages,
        )
        self._set_tool_context(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        # Review handoff: the first conversation turn after a review injects
        # the complete report (or the explicit failure context) before history
        # is read, so the report is available to this turn and to every later
        # one. An oversized report never reaches here — the turn gate already
        # refused it rather than injecting a partial report.
        handoff = self.review_coordinator.pending_handoff(ctx.session)
        if handoff is not None and handoff.fits:
            self.review_coordinator.consume_handoff(ctx.session, handoff)
            ctx.handoff_directive = handoff.directive

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)

        # Filter stale subagent results from prior reviews — the LLM would
        # otherwise try to continue old review work instead of starting fresh.
        ctx.history = [
            m
            for m in ctx.history
            if m.get("_metadata", {}).get("injected_event") != "subagent_result"
        ]

        ctx.frozen_messages, ctx.working_messages = self._build_initial_messages(
            ctx.msg, ctx.session, ctx.history, ctx.pending_summary
        )
        self._apply_handoff_directive(ctx.frozen_messages, ctx.handoff_directive)
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )

        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)

        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        """Run the model/tool loop and collect the final turn state."""
        await publish_turn_run_status(self.bus, ctx.msg, "running")
        result = await self._run_agent_loop(
            ctx.frozen_messages,
            ctx.working_messages,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            on_retry_wait=ctx.on_retry_wait,
            session=ctx.session,
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            metadata=ctx.msg.metadata,
            session_key=ctx.session_key,
            pending_queue=ctx.pending_queue,
            consumed_subagent_task_ids=ctx.consumed_subagent_task_ids,
        )
        (
            final_content,
            tools_used,
            all_msgs,
            stop_reason,
            had_injections,
            content_replaced,
        ) = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        ctx.content_replaced = content_replaced
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        """Persist the turn, media metadata, latency, and runtime cleanup."""
        if ctx.final_content is None or not ctx.final_content.strip():
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        ctx.save_skip = 1 + len(ctx.history) + (1 if ctx.user_persisted_early else 0)
        skip_msgs = ctx.all_messages[ctx.save_skip :]
        ctx.generated_media = generated_image_paths_from_messages(skip_msgs)
        mt = self.tools.get("message")
        extra = getattr(mt, "turn_delivered_media_paths", lambda: [])() if mt else []
        merge_turn_media_into_last_assistant(
            ctx.all_messages, ctx.generated_media, extra
        )

        ctx.turn_latency_ms = max(
            0, int((time.time() - ctx.turn_wall_started_at) * 1000)
        )
        self._save_turn(
            ctx.session,
            ctx.all_messages,
            ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.msg.channel == "websocket":
            self._pending_turn_latency_ms[ctx.session_key] = ctx.turn_latency_ms
        ctx.session.enforce_file_cap()
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        self.sessions.save(ctx.session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                ctx.session,
                replay_max_messages=self._max_messages,
            )
        )
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content,
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.generated_media,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.outbound and ctx.content_replaced:
            ctx.outbound.metadata.pop("_streamed", None)
            if ctx.msg.metadata.get("_wants_stream") and _is_review_turn(
                ctx.msg.metadata
            ):
                stream_id = f"{ctx.msg.session_key}:{ctx.turn_id}:review_report"
                report_meta = dict(ctx.msg.metadata or {})
                report_meta["_stream_delta"] = True
                report_meta["_stream_id"] = stream_id
                report_meta["_stream_kind"] = "review_report"
                end_meta = dict(ctx.msg.metadata or {})
                end_meta["_stream_end"] = True
                end_meta["_stream_id"] = stream_id
                end_meta["_stream_kind"] = "review_report"
                logger.info(
                    "review.report.stream.start session={} chars={}",
                    ctx.session_key,
                    len(ctx.final_content or ""),
                )
                for chunk in _stream_chunks(ctx.final_content or ""):
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=ctx.msg.channel,
                            chat_id=ctx.msg.chat_id,
                            content=chunk,
                            metadata=report_meta,
                        )
                    )
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=ctx.msg.channel,
                        chat_id=ctx.msg.chat_id,
                        content="",
                        metadata=end_meta,
                    )
                )
                logger.info("review.report.stream.end session={}", ctx.session_key)
                ctx.outbound = None
        return "ok"

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                if isinstance(block, bytes):
                    text = "[binary content omit]"
                else:
                    text = str(block)
                if should_truncate_text or isinstance(block, bytes):
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({"type": "text", "text": text})
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            entry.pop("_metadata", None)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if (
                    isinstance(content, str)
                    and len(content) > self.max_tool_result_chars
                ):
                    entry["content"] = truncate_text_fn(
                        content, self.max_tool_result_chars
                    )
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(
                        content, should_truncate_text=True
                    )
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if (
                    isinstance(content, str)
                    and ContextBuilder._RUNTIME_CONTEXT_TAG in content
                ):
                    # Strip the runtime-context block appended at the end.
                    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    before = content[:tag_pos].rstrip("\n ")
                    if before:
                        entry["content"] = before
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(
                        content, drop_runtime=True
                    )
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = (
            msg.metadata.get("subagent_task_id")
            if isinstance(msg.metadata, dict)
            else None
        )
        if task_id and any(
            m.get("injected_event") == "subagent_result"
            and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        structured = {
            key: metadata[key]
            for key in (
                "subagent_label",
                "subagent_status",
                "subagent_result",
            )
            if key in metadata
        }
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
            **structured,
        )
        return True

    def _set_runtime_checkpoint(
        self, session: Session, payload: dict[str, Any]
    ) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        tc = message.get("tool_calls")
        if isinstance(tc, list):
            tc = tuple(
                (c.get("id"), c.get("type"), (c.get("function") or {}).get("name"))
                for c in tc
                if isinstance(c, dict)
            )
        content = message.get("content")
        if isinstance(content, list):
            content = tuple(str(b) for b in content)
        return (
            message.get("role"),
            content,
            message.get("tool_call_id"),
            message.get("name"),
            tc,
            message.get("reasoning_content"),
            tuple(message.get("thinking_blocks") or ()),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left)
                == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata=dict(metadata or {}),
        )
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )
