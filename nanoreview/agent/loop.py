"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
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
from nanoreview.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunResult, AgentRunner, AgentRunSpec
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
from nanoreview.agent.orchestration import (
    ReviewExecutionContext,
    ReviewOrchestrator,
    ReviewPlanningError,
)
from nanoreview.review.planning.planner import prepare_code_review_context
from nanoreview.review.output.judge import ReviewJudge, ReviewJudgeConfig
from nanoreview.review.types import ReviewMetaKey
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
    initial_messages: list[dict[str, Any]] = field(default_factory=list)

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

    pending_queue: asyncio.Queue | None = None
    consumed_subagent_task_ids: set[str] | None = None
    pending_summary: str | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    turn_latency_ms: int | None = None

    trace: list[StateTraceEntry] = field(default_factory=list)


def _is_review_turn(metadata: dict[str, Any] | None) -> bool:
    meta = metadata or {}
    return bool(meta.get(ReviewMetaKey.TARGET) or meta.get("review_target"))


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
        embedding_config: Any | None = None,
        rerank_config: Any | None = None,
        qdrant_config: Any | None = None,
        rag_config: Any | None = None,
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
        from nanoreview.rag.config import RAGConfig

        _resolve_tool_config_refs()
        _tc = tools_config or ToolsConfig()
        _rag_config = rag_config if rag_config is not None else RAGConfig()
        _embedding_config = (
            embedding_config if embedding_config is not None else _rag_config.embedding
        )
        _rerank_config = (
            rerank_config if rerank_config is not None else _rag_config.rerank
        )
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
        self.embedding_config = _embedding_config
        self.rerank_config = _rerank_config
        self.qdrant_config = (
            qdrant_config if qdrant_config is not None else _rag_config.qdrant
        )
        self.rag_config = _rag_config
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
            embedding_config=self.embedding_config,
            rerank_config=self.rerank_config,
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
            rag_config=config.rag,
            review_config=config.review,
            embedding_config=config.rag.embedding,
            rerank_config=config.rag.rerank,
            qdrant_config=config.rag.qdrant,
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
        self.subagents.set_provider(provider, model)
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
            rag_config=self.rag_config,
            review_config=self.review_config,
            embedding_config=self.embedding_config,
            rerank_config=self.rerank_config,
            qdrant_config=self.qdrant_config,
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
        judge_settings = getattr(self.review_config, "judge", None)
        if judge_settings is not None and not getattr(judge_settings, "enabled", True):
            return None
        provider = self.provider
        model = self.model
        preset_name = (
            getattr(judge_settings, "model_preset", None)
            if judge_settings is not None
            else None
        )
        if preset_name:
            try:
                snapshot = self._build_model_preset_snapshot(preset_name)
                provider = snapshot.provider
                model = snapshot.model
                logger.info(
                    "review.judge.preset.loaded preset={} model={}", preset_name, model
                )
            except Exception as exc:
                logger.warning(
                    "review.judge.preset_unavailable preset={} reason={}",
                    preset_name,
                    exc,
                )
        config = ReviewJudgeConfig(
            enabled=bool(getattr(judge_settings, "enabled", True)),
            max_candidates=int(getattr(judge_settings, "max_candidates", 40)),
            timeout_seconds=int(getattr(judge_settings, "timeout_seconds", 60)),
            max_tokens=int(getattr(judge_settings, "max_tokens", 2048)),
        )
        return ReviewJudge(provider=provider, model=model, config=config)

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
    ) -> list[dict[str, Any]]:
        """Build the initial message list for the LLM turn."""
        messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata,
        )
        return messages

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
        initial_messages: list[dict],
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
        saw_running_subagents = False
        subagent_barrier_sent = False

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

        is_review_session = session is not None and bool(
            session.metadata.get(ReviewMetaKey.TARGET)
        )
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
            suppress_content_progress=is_review_session,
        )
        review_hook: AgentHook | None = None
        if is_review_session:
            on_stream = None
            on_stream_end = None
        hooks: list[AgentHook] = [loop_hook]
        if review_hook is not None:
            hooks.append(review_hook)
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

            Subagent results are a hard dependency for review turns. While
            same-session subagents are running, wait for each completed result
            and inject it immediately so validation can run incrementally.
            Non-subagent messages are buffered until subagents have finished.
            """
            if pending_queue is None:
                return []
            nonlocal saw_running_subagents, subagent_barrier_sent

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                user_content = pending_msg.content
                message: dict[str, Any] = {"role": "user", "content": user_content}
                if pending_msg.metadata:
                    message["_metadata"] = dict(pending_msg.metadata)
                return message

            items: list[dict[str, Any]] = []
            running_at_start = _running_subagents() > 0
            saw_running_subagents = saw_running_subagents or running_at_start

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

            def _subagent_barrier_message() -> dict[str, Any]:
                return {
                    "role": "user",
                    "content": (
                        "[System] All same-session review subagents have completed. "
                        "Continue integrating the injected subagent results and finalize "
                        "only if validation is complete. Do not spawn another subagent "
                        "for a review dimension that has already returned a result."
                    ),
                    "_metadata": {"injected_event": "subagent_barrier"},
                }

            def _append_subagent_barrier_if_done() -> None:
                nonlocal subagent_barrier_sent
                if (
                    saw_running_subagents
                    and not subagent_barrier_sent
                    and _running_subagents() == 0
                    and is_review_session
                    and len(items) < limit
                ):
                    subagent_barrier_sent = True
                    items.append(_subagent_barrier_message())

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

            if (
                not items
                and saw_running_subagents
                and not subagent_barrier_sent
                and _running_subagents() == 0
                and is_review_session
            ):
                subagent_barrier_sent = True
                items.append(_subagent_barrier_message())
            else:
                _append_subagent_barrier_if_done()

            return items

        file_state_token = bind_file_states(
            self._file_state_store.for_session(active_session_key)
        )
        try:
            from nanoreview.agent.tools.permissions import resolve_policy

            _session_meta = session.metadata if session is not None else {}
            permission_policy = resolve_policy(
                getattr(self, "permissions_config", None),
                _session_meta,
            )

            review_meta = dict(_session_meta)
            review_meta.setdefault(
                ReviewMetaKey.MAX_SUBAGENTS,
                getattr(self.review_config, "max_concurrent_subagents", 4),
            )
            review_meta[ReviewMetaKey.DIFF_CONTEXT_WINDOW_TOKENS] = self.context_window_tokens
            review_tool = self.tools.get("local_review") or self.tools.get(
                "github_review"
            )
            if review_tool is not None:
                if evidence_provider := getattr(review_tool, "evidence_provider", None):
                    review_meta[ReviewMetaKey.EVIDENCE_PROVIDER] = evidence_provider
            review_preparation = await prepare_code_review_context(
                initial_messages,
                review_meta,
                progress_callback=on_progress,
            )
            specialist_prompt = review_preparation.prompt
            review_meta_keys_to_sync = (
                ReviewMetaKey.ALLOWED_DIMENSIONS,
                ReviewMetaKey.GITHUB_PREFETCH_READY,
                ReviewMetaKey.DIFF_CONTEXT_WINDOW_TOKENS,
                ReviewMetaKey.GITHUB_PR_HEAD_REF,
                ReviewMetaKey.LOCAL_ROOT,
                ReviewMetaKey.LOCAL_TARGET,
                ReviewMetaKey.LOCAL_SCOPE_KIND,
            )
            for key in review_meta_keys_to_sync:
                if key in review_meta:
                    _session_meta[key] = review_meta[key]
                elif key in (
                    ReviewMetaKey.LOCAL_ROOT,
                    ReviewMetaKey.LOCAL_TARGET,
                    ReviewMetaKey.LOCAL_SCOPE_KIND,
                ):
                    _session_meta.pop(key, None)
            if ReviewMetaKey.ALLOWED_DIMENSIONS in review_meta:
                if review_hook is not None and hasattr(
                    review_hook, "set_allowed_dimensions"
                ):
                    review_hook.set_allowed_dimensions(
                        review_meta[ReviewMetaKey.ALLOWED_DIMENSIONS]
                    )
            validation_workspace = str(
                review_meta.get(ReviewMetaKey.LOCAL_ROOT) or self.workspace
            )
            changed_files: list[str] = []
            local_target = review_meta.get(ReviewMetaKey.LOCAL_TARGET)
            remote_diff = None
            if review_preparation.plan is not None:
                target_type_value = (
                    str(review_meta.get(ReviewMetaKey.TARGET_TYPE) or "")
                    .strip()
                    .lower()
                )
                evidence_provider = review_meta.get(ReviewMetaKey.EVIDENCE_PROVIDER)
                if target_type_value == "github" and evidence_provider is not None:
                    diff_evidence = getattr(evidence_provider, "last_diff_evidence", None)
                    if diff_evidence is not None:
                        remote_diff = diff_evidence
                        changed_files = list(getattr(diff_evidence, "changed_files", []))
                        review_meta[ReviewMetaKey.GITHUB_PR_HEAD_REF] = getattr(
                            diff_evidence, "head_sha", ""
                        )
                    cache_root = getattr(evidence_provider, "last_cache_root", None)
                    if cache_root is not None:
                        validation_workspace = str(cache_root)
                        changed_files = list(
                            getattr(evidence_provider, "last_changed_files", [])
                        )
                        local_target = None
            if review_meta:
                updated_tool_meta = {
                    **dict(metadata or {}),
                    **{
                        key: value
                        for key, value in review_meta.items()
                        if key != ReviewMetaKey.EVIDENCE_PROVIDER
                    },
                }
                loop_hook.update_metadata(updated_tool_meta)
                self._set_tool_context(
                    channel,
                    chat_id,
                    message_id,
                    updated_tool_meta,
                    session_key=session_key,
                )
            if specialist_prompt:
                initial_messages.insert(
                    0, {"role": "system", "content": specialist_prompt}
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

            async def _persist_automatic_subagent_result(
                subagent_message: InboundMessage,
            ) -> None:
                if session is None:
                    return
                if self._persist_subagent_followup(session, subagent_message):
                    self.sessions.save(session)

            if review_preparation.plan is not None and review_preparation.evidence is not None:
                orchestrator = ReviewOrchestrator(
                    runner=self.runner,
                    subagents=self.subagents,
                    model=self.model,
                    workspace=self.workspace,
                    max_tool_result_chars=self.max_tool_result_chars,
                    judge=self._build_review_judge(),
                )
                try:
                    final_content = await orchestrator.execute(
                        coordinator_messages=initial_messages,
                        plan=review_preparation.plan,
                        evidence=review_preparation.evidence,
                        context=ReviewExecutionContext(
                            channel=channel,
                            chat_id=chat_id,
                            session_key=active_session_key,
                            message_id=message_id,
                            metadata=updated_tool_meta if review_meta else dict(metadata or {}),
                            concurrency=review_preparation.plan.max_subagents,
                            result_callback=_persist_automatic_subagent_result,
                        ),
                        validation_workspace=validation_workspace,
                        changed_files=changed_files,
                        local_target=local_target if isinstance(local_target, str) else None,
                        remote_diff=remote_diff,
                    )
                    result = AgentRunResult(
                        final_content=final_content,
                        messages=list(initial_messages),
                    )
                except ReviewPlanningError as exc:
                    logger.warning("review.orchestration.failed reason={}", exc)
                    result = AgentRunResult(
                        final_content=f"## Code Review Report\n\n### Error\n\n{exc}",
                        messages=list(initial_messages),
                        stop_reason="error",
                        error=str(exc),
                    )
            else:
                result = await self.runner.run(
                    AgentRunSpec(
                    initial_messages=initial_messages,
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
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
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
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="Sorry, I encountered an error.",
                        )
                    )
                    if msg.channel == "websocket":
                        await publish_forced_turn_end()

        finally:
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

    async def close_mcp(self) -> None:
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

        messages = self.context.build_messages(
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
            messages,
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
        3. COMMAND：常疗判断是否是命令（如 /stop）
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
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
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

        ctx.initial_messages = self._build_initial_messages(
            ctx.msg, ctx.session, ctx.history, ctx.pending_summary
        )
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
            ctx.initial_messages,
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
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            media=media or [],
        )
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )
