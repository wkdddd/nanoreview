"""Session coordinator: the process-level agent entry point.

``SessionCoordinator`` is both the process runtime and the decision point
between the two agent phases a NanoReview session can be in:

* **review** — one review run owns the session; ordinary messages and
  non-control commands are refused without leaving history behind.
* **conversation** — the review reached a terminal status, its resources were
  cleaned up and its result was persisted; ordinary messages are accepted and
  the first one carries the review handoff.

It owns the process skeleton — MessageBus receive/send, per-session serial
locks and the bounded pending queue, command dispatch,
cancellation scheduling, the review/conversation route and gates, the handoff
value object with its single session write, and final result publication — and
delegates the actual turn algorithms:

* ``ReviewLoop`` owns a complete review turn: it builds the review context
  (``ContextBuilder`` + ``COMMON_RULES``), persists the user message and the
  report artifact, drives the lifecycle and returns the structured result.
* ``ConversationLoop`` owns a complete conversation turn: session/history,
  the per-turn context and ``ToolRegistry``, the single ``AgentRunner`` run,
  history persistence and reply assembly. It consumes the handoff by calling
  back into :meth:`SessionCoordinator.consume_handoff`, so the coordinator
  stays the only writer of the injected handoff.

The coordinator itself calls no model and executes no tool: a review turn is
handed over whole and only its produced report is published (including the
report chunk stream); a conversation turn's history is built and saved by the
conversation loop, never here — apart from the one handoff injection, which the
coordinator writes before that turn reads history.

Handoff states
--------------
``complete``  — report artifact on disk, run completed without gaps.
``partial``   — report artifact on disk, but the run reported gaps.
``failed``    — no usable artifact. The conversation still opens, but the
                injected system context states the failure, what partial
                results exist and which coverage gaps remain.

No handoff is ever retried, auto-repaired or re-run: a failed handoff stays
failed for the life of the session, and the persisted artifact (when there is
one) remains readable.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
import uuid
from contextlib import nullcontext, suppress
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanoreview.agent import model_presets as preset_helpers
from nanoreview.agent.context import ContextBuilder
from nanoreview.agent.conversation_loop import (
    MAX_PENDING_CONVERSATION_MESSAGES,
    ConversationLoop,
)
from nanoreview.agent.event_sink import build_bus_event_sink, event_text, metadata_for_event
from nanoreview.agent.hooks.lifecycle import AgentHook
from nanoreview.agent.memory import Consolidator
from nanoreview.agent.review_loop import (
    ReviewLoop,
    ReviewTurnRequest,
    persist_review_subagent_result,
)
from nanoreview.agent.review_state import (
    ReviewArtifactError,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
)
from nanoreview.agent.runner import AgentRunner
from nanoreview.agent.subagent import SubagentManager
from nanoreview.agent.tools.file_state import FileStateStore
from nanoreview.agent.tools.mcp import MCPProvider
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.command import (
    CommandContext,
    CommandRouter,
    register_builtin_commands,
)
from nanoreview.config.schema import AgentDefaults, ModelPresetConfig
from nanoreview.events import (
    EventSink,
    StreamDeltaEvent,
    StreamEndEvent,
)
from nanoreview.providers.base import LLMProvider
from nanoreview.providers.factory import ProviderSnapshot
from nanoreview.review.admission import (
    ReviewAdmission,
    ReviewAdmissionCode,
    ReviewAdmissionError,
    ReviewAdmissionRequest,
    ReviewAdmissionService,
)
from nanoreview.review.output.judge import ReviewJudge, ReviewJudgeConfig
from nanoreview.review.profiles import reviewer_execution_profiles
from nanoreview.review.result import (
    ReviewHandoffState,
    ReviewResult,
    render_handoff_block,
    render_handoff_directive,
    render_review_context_index,
    result_from_session_metadata,
)
from nanoreview.review.types import ReviewMetaKey
from nanoreview.session.manager import Session, SessionManager
from nanoreview.utils.helpers import estimate_prompt_tokens
from nanoreview.utils.log_style import log_event
from nanoreview.utils.webui_titles import maybe_generate_webui_title_after_turn
from nanoreview.utils.webui_turn_helpers import publish_turn_run_status

if TYPE_CHECKING:
    from nanoreview.config.schema import ChannelsConfig, ToolsConfig

UNIFIED_SESSION_KEY = "unified:default"

#: Commands that stay usable while a review run owns the session.
REVIEW_ALLOWED_COMMANDS = frozenset({"/status", "/stop"})

#: Index / handoff markers stored on the persisted session messages.
REVIEW_CONTEXT_EVENT = "review_context"
REVIEW_HANDOFF_EVENT = "review_handoff"

#: How much of the context window is held back from the handoff check for the
#: system prompt, runtime block and the model's own output.
_HANDOFF_PROMPT_RESERVE_TOKENS = 1024

#: Reason recorded when a run lost its executor before producing a result.
INTERRUPTED_RUN_REASON = (
    "the review was interrupted before it produced a result and cannot be resumed"
)

#: Cancellation message stamped on the tasks ``/stop`` cancels, so a cancelled
#: entry point can tell a deliberate stop from a timeout, a client disconnect
#: or a caller-initiated cancel (those keep their own cancellation semantics and
#: must not be reported as a stop).
STOP_CANCEL_REASON = "nanoreview.stop"

#: Reply body for a direct request whose turn was cancelled by ``/stop``.
STOPPED_REPLY_CONTENT = "Stopped."

#: Metadata flag set on the stopped reply so transports can treat it as a
#: deliberate terminal outcome (no empty-reply retry, not a model failure).
STOP_REASON_META_KEY = "stop_reason"
STOPPED_STOP_REASON = "stopped"

#: Placeholder for "key absent" when snapshotting session metadata before a
#: repair write, so a failed save can restore the exact previous state.
_ABSENT = object()


def _is_review_turn(metadata: dict[str, Any] | None) -> bool:
    meta = metadata or {}
    return bool(meta.get(ReviewMetaKey.TARGET) or meta.get("review_target"))


def _is_internal_event(msg: InboundMessage) -> bool:
    """Whether *msg* is a legacy system/subagent event.

    These bypass review session gating, but they no longer drive a model turn:
    they are dropped ``(with a warning)`` before dispatch instead of running the
    conversation agent, and they never consume the pending review handoff.
    """
    meta = msg.metadata if isinstance(msg.metadata, dict) else {}
    return (
        msg.channel == "system"
        or msg.sender_id == "subagent"
        or meta.get("injected_event") in ("subagent_result", "subagent_barrier")
    )


def _is_stop_cancellation(exc: BaseException) -> bool:
    """Whether *exc* is the ``CancelledError`` raised by a ``/stop``.

    ``/stop`` cancels its targets with :data:`STOP_CANCEL_REASON`; a timeout,
    a disconnected client or a caller-initiated cancel uses a bare
    ``task.cancel()`` and carries no reason, so only a deliberate stop matches.
    """
    return bool(exc.args) and exc.args[0] == STOP_CANCEL_REASON


def _stream_chunks(text: str, *, chunk_size: int = 2048) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


class SessionRoute(StrEnum):
    """Which agent phase owns the session right now."""

    REVIEW = "review"
    CONVERSATION = "conversation"


@dataclasses.dataclass(frozen=True, slots=True)
class ReviewHandoff:
    """The review result about to be handed to the first conversation turn.

    The handoff is the boundary between ``ReviewLoop`` (which owns the report
    artifact and the run state) and ``ConversationLoop`` (which owns the
    conversation history). It is deliberately a plain, immutable value: the
    coordinator *prepares* it read-only inside the session lock, the
    conversation loop *calls back* to persist it through
    :meth:`SessionCoordinator.consume_handoff`, and neither side may rewrite the
    authoritative report.
    """

    result: ReviewResult
    report_markdown: str | None
    fits: bool

    @property
    def block(self) -> str:
        """Full injected context: framing, gaps, and the complete report."""
        return render_handoff_block(self.result, self.report_markdown)

    @property
    def directive(self) -> str:
        """Short system-level directive naming provenance and handoff state."""
        return render_handoff_directive(self.result)


class SessionCoordinator:
    """Route one session between review and conversation, and run its turns."""

    @property
    def current_iteration(self) -> int:
        return self.conversation_loop.current_iteration

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

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
        channels_config: "ChannelsConfig | None" = None,
        timezone: str | None = None,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: "ToolsConfig | None" = None,
        review_config: Any | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
        default_reasoning_effort: str | None = None,
        reserved_output_tokens: int | None = None,
    ):
        from nanoreview.config.schema import ToolsConfig, _resolve_tool_config_refs

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
        self.review_config = review_config
        self.exec_config = _tc.exec
        self._default_reasoning_effort = default_reasoning_effort
        self._reserved_output_tokens = int(
            reserved_output_tokens
            if reserved_output_tokens is not None
            else getattr(getattr(provider, "generation", None), "max_tokens", 4096)
            or 4096
        )

        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._total_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(
            workspace, timezone=timezone, disabled_skills=disabled_skills
        )
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        # MCP belongs to the Conversation Agent only, so it owns a separate
        # registry and connection set. Registering these wrappers into
        # ``self.tools`` would expose them to planner/reviewer/Judge.
        self.mcp = MCPProvider.from_config(_tc)
        # One file-read/write tracker per logical session. Each turn binds its
        # own registry to the session's state via a contextvar.
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
            context_window_tokens=self.context_window_tokens,
            context_block_limit=self.context_block_limit,
        )
        self._unified_session = unified_session
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._closed = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        self._pending_queues: dict[str, asyncio.Queue] = {}

        # Review side: ReviewLoop owns the one-shot run state, its lifecycle,
        # its persistence, and the review turn's own context.
        self.review_loop = ReviewLoop(
            workspace=workspace,
            sessions=self.sessions,
            runner=self.runner,
            subagents=self.subagents,
            model=self.model,
            max_tool_result_chars=self.max_tool_result_chars,
            context_builder=self.context,
            max_messages=self._max_messages,
            max_concurrent_subagents=int(
                getattr(self.review_config, "max_concurrent_subagents", 4) or 4
            ),
            context_window_tokens=self.context_window_tokens,
            judge_factory=self._build_review_judge,
            evidence_provider_getter=self._review_evidence_provider,
        )
        #: Authoritative in-process review runs, owned by ``ReviewLoop``.
        self._review_runs: dict[str, ReviewRunState] = self.review_loop.runs
        #: Authoritative admission/domain service for every review entry point.
        self._admission_service: ReviewAdmissionService | None = None

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

        # Conversation side: ConversationLoop owns one complete conversation
        # turn; the coordinator owns the bus, queues, locks and cancellation.
        self.conversation_loop = ConversationLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            sessions=self.sessions,
            context=self.context,
            runner=self.runner,
            consolidator=self.consolidator,
            file_state_store=self._file_state_store,
            tools_config=_tc,
            model=self.model,
            max_iterations=self.max_iterations,
            max_tool_result_chars=self.max_tool_result_chars,
            context_window_tokens=self.context_window_tokens,
            context_block_limit=self.context_block_limit,
            provider_retry_mode=self.provider_retry_mode,
            tool_hint_max_length=self.tool_hint_max_length,
            max_messages=self._max_messages,
            review_config=self.review_config,
            provider_snapshot_loader=self._provider_snapshot_loader,
            background_scheduler=self._schedule_background,
            hooks=self._extra_hooks,
            hooks_getter=lambda: self._extra_hooks,
            usage_recorder=self._record_result_usage,
            handoff_consumer=self.consume_handoff,
            mcp_provider=self.mcp,
        )

        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> "SessionCoordinator":
        """Create a coordinator from config with the common parameter set."""
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

    # -- provider / model ---------------------------------------------------

    def _resolve_subagent_reasoning_effort(self) -> str | None:
        subagent_effort = getattr(self.review_config, "subagent_reasoning_effort", None)
        if subagent_effort is not None:
            return subagent_effort
        return self._default_reasoning_effort

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
        self.conversation_loop.set_runtime_model(
            provider, model, context_window_tokens
        )
        self.review_loop.set_runtime_model(provider, model, context_window_tokens)
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

    # -- tools / judge ------------------------------------------------------

    def _register_default_tools(self) -> None:
        """Register the default tool set (core scope, review tools included).

        This registry is the source of the review evidence provider; the
        conversation loop registers its own core-only registry per turn.
        """
        from nanoreview.agent.tools.context import ToolContext
        from nanoreview.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            workspace=str(self.workspace),
            provider=self.provider,
            model=self.model,
            review_config=self.review_config,
            bus=self.bus,
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
        """Build the judge on the coordinator/plan runner and model."""
        judge_settings = getattr(self.review_config, "judge", None)
        if judge_settings is not None and not getattr(judge_settings, "enabled", True):
            return None
        config = ReviewJudgeConfig(
            enabled=bool(getattr(judge_settings, "enabled", True)),
            timeout_seconds=int(getattr(judge_settings, "timeout_seconds", 60)),
            max_tokens=int(getattr(judge_settings, "max_tokens", 2048)),
            context_window_tokens=int(self.context_window_tokens or 0) or None,
        )
        return ReviewJudge(
            runner=self.runner,
            model=self.model,
            config=config,
            common_rules_workspace=self.workspace,
        )

    def _review_evidence_provider(self) -> Any | None:
        """Return the review tool's shared evidence service, if registered."""
        tool = self.tools.get("local_review")
        if tool is None:
            return None
        return getattr(tool, "evidence_provider", None)

    # -- bus helpers --------------------------------------------------------

    async def _build_bus_event_sink(
        self,
        msg: InboundMessage,
        *,
        stream_kind: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> EventSink:
        """Build the turn's ``EventSink`` bound to this message's bus route.

        Owns the metadata mapping the WebSocket/CLI channels already read, so
        hooks only publish typed events.
        """
        return build_bus_event_sink(
            self.bus.publish_outbound,
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata,
            stream_kind=stream_kind,
            extra=extra,
        )

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
        """Fully stop one session: drop its queued messages, then cancel its work.

        Order matters. The pending queue is removed (and discarded, never
        re-published) *before* the tasks are cancelled, so a cancelled
        ``_dispatch`` finds no queue of its own left to re-publish in its
        ``finally`` — a stop must not resurrect the messages it just dropped.
        Both the already-running turn and the requests still parked on the
        session lock are cancelled. Only *key*'s own queue and tasks are
        touched, so other sessions keep running.
        """
        self._pending_queues.pop(key, None)
        tasks = self._active_tasks.pop(key, [])
        cancelled = 0
        for task in tasks:
            if task.done():
                continue
            # Stamp the reason so the cancelled entry point can tell a stop
            # from a timeout/disconnect/caller cancel.
            task.cancel(STOP_CANCEL_REASON)
            cancelled += 1
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    def _direct_session_key(self, session_key: str) -> str:
        """Resolve the key a direct call runs under.

        A direct caller's ``session_key`` is authoritative and is always
        honoured, ``unified_session`` included: a direct entry point owns its
        own session key and must not be silently folded into the shared
        "unified" session, otherwise admitted CLI/API sessions collide and lose
        their history isolation. Callers that genuinely want the unified
        session pass :data:`UNIFIED_SESSION_KEY` explicitly.
        """
        return session_key

    def _drop_internal_event(self, msg: InboundMessage, *, entry: str) -> bool:
        """Drop a leftover internal event before dispatch; report whether it was.

        Every entry point (bus loop, direct call, ``_execute_turn``) funnels its
        leftover system/subagent events through here so the recognition rule
        lives in exactly one place. Dropping happens *before* command dispatch,
        gating and pending-queue insertion, so a dropped event cannot run a
        command, start a model or tool, write history, consume a review handoff
        or occupy the pending queue. Only the event's identity is logged — never
        its body.
        """
        if not _is_internal_event(msg):
            return False
        logger.warning(
            "Dropping internal event: entry={} channel={} sender={} session={} "
            "injected_event={}",
            entry,
            msg.channel,
            msg.sender_id,
            msg.session_key,
            (msg.metadata or {}).get("injected_event") or "none",
        )
        return True

    # -- admission ----------------------------------------------------------

    def admissions(self) -> ReviewAdmissionService:
        """Shared admission/domain service used by every review entry point."""
        if self._admission_service is None:
            self._admission_service = ReviewAdmissionService(
                sessions=self.sessions,
                workspace=self.workspace,
            )
        return self._admission_service

    def admit(self, request: ReviewAdmissionRequest) -> ReviewAdmission:
        """Validate, snapshot, and register one review run before delivery."""
        live = self.review_loop.get(request.session_key or "")
        if live is not None:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.DUPLICATE_REVIEW,
                (
                    f"Session '{live.session_key}' already has review run "
                    f"'{live.run_id}' in progress."
                ),
            )
        admission = self.admissions().admit(request)
        self.review_loop.register(admission)
        return admission

    def review_admissions(self) -> ReviewAdmissionService:
        """Shared admission/domain service used by every review entry point."""
        return self.admissions()

    def admit_review(self, request: ReviewAdmissionRequest) -> ReviewAdmission:
        """Single admission boundary transports call before publishing a task."""
        session_key = (request.session_key or "").strip()
        if not session_key:
            request = dataclasses.replace(
                request, session_key=f"review:{uuid.uuid4().hex[:12]}"
            )
        return self.admit(request)

    def reset_review_run(self, session_key: str) -> None:
        """Drop the one-shot review gate for a session (used by /new)."""
        self.review_loop.reset(session_key)

    # -- routing ------------------------------------------------------------

    def route(self, session: Session) -> SessionRoute:
        """Decide which phase owns *session*."""
        live = self.review_loop.get(session.key)
        if live is not None:
            if live.status is ReviewRunStatus.RUNNING:
                return SessionRoute.REVIEW
            return SessionRoute.CONVERSATION
        self.result(session)
        return SessionRoute.CONVERSATION

    def result(self, session: Session) -> ReviewResult | None:
        """Structured review result for *session*, or ``None`` if it has none."""
        if self.review_loop.get(session.key) is not None:
            return self.review_loop.result(session.key)
        result = result_from_session_metadata(session.key, session.metadata)
        if result is None:
            return None
        if result.status is ReviewRunStatus.RUNNING:
            return self._normalize_interrupted_run(session, result)
        return result

    def _normalize_interrupted_run(
        self, session: Session, result: ReviewResult
    ) -> ReviewResult:
        """Mark an orphaned ``running`` run as a failed handoff (one write)."""
        keys = (
            ReviewMetaKey.STATUS,
            ReviewMetaKey.PHASE,
            ReviewMetaKey.SUMMARY,
            ReviewMetaKey.ERROR,
        )
        previous = {key: session.metadata.get(key, _ABSENT) for key in keys}
        session.metadata[ReviewMetaKey.STATUS] = ReviewRunStatus.ERROR.value
        session.metadata[ReviewMetaKey.PHASE] = ReviewPhase.DONE.value
        session.metadata[ReviewMetaKey.SUMMARY] = INTERRUPTED_RUN_REASON
        session.metadata[ReviewMetaKey.ERROR] = INTERRUPTED_RUN_REASON
        try:
            self.sessions.save(session)
        except Exception as exc:
            for key, value in previous.items():
                if value is _ABSENT:
                    session.metadata.pop(key, None)
                else:
                    session.metadata[key] = value
            logger.warning(
                "review.route.interrupted_persist_failed session={} run_id={} reason={}",
                session.key,
                result.run_id,
                exc,
            )
            return result
        logger.info(
            "review.route.interrupted session={} run_id={} reason=no_live_executor",
            session.key,
            result.run_id,
        )
        repaired = result_from_session_metadata(session.key, session.metadata)
        if repaired is None:
            return result
        return dataclasses.replace(
            repaired, error=INTERRUPTED_RUN_REASON, summary=INTERRUPTED_RUN_REASON
        )

    # -- gating -------------------------------------------------------------

    def gate_message(
        self,
        msg: InboundMessage,
        live_run: ReviewRunState | None,
        raw: str,
    ) -> OutboundMessage | None:
        """Refuse an ordinary message that reaches a running review."""
        if _is_internal_event(msg) or raw.startswith("/"):
            return None
        if live_run is None or live_run.status is not ReviewRunStatus.RUNNING:
            return None
        admitted_run_id = msg.metadata.get("_review_admitted")
        if (
            isinstance(admitted_run_id, str)
            and admitted_run_id
            and live_run.run_id == admitted_run_id
        ):
            return None
        logger.info(
            "review.gate.rejected session={} run_id={} source={}",
            live_run.session_key,
            live_run.run_id,
            msg.metadata.get("injected_event") or "user_message",
        )
        return self._gate_response(
            msg,
            run_id=live_run.run_id,
            status=live_run.status,
            code="review_gated",
            content=(
                "Review is already running. "
                "Use /status to check progress or /stop to cancel."
            ),
        )

    def _review_gate_blocks(
        self,
        msg: InboundMessage,
        review_state: ReviewRunState | None,
        raw: str,
    ) -> bool:
        """Whether an inbound message must be rejected by the review gate."""
        return self.gate_message(msg, review_state, raw) is not None

    def gate_command(
        self, session: Session, msg: InboundMessage, raw: str
    ) -> OutboundMessage | None:
        """Refuse a slash command inside a review session."""
        if _is_internal_event(msg):
            return None
        command = raw.strip().split(maxsplit=1)[0].lower()
        live = self.review_loop.get(session.key)
        owns_review = live is not None or bool(
            session.metadata.get(ReviewMetaKey.RUN_ID)
        )
        if not owns_review:
            return None
        if command == "/new":
            run_id = (
                live.run_id
                if live is not None
                else str(session.metadata.get(ReviewMetaKey.RUN_ID) or "unknown")
            )
            status = live.status if live is not None else ReviewRunStatus.COMPLETED
            code = "new_in_review_session"
            content = (
                "/new is not available in a review session. "
                "Start a new review session to run another review."
            )
        elif live is not None and live.status is ReviewRunStatus.RUNNING:
            if command in REVIEW_ALLOWED_COMMANDS:
                return None
            run_id, status = live.run_id, live.status
            code = "command_not_allowed_during_review"
            content = (
                f"'{command}' is not available while a review is running. "
                "Use /status to check progress or /stop to cancel."
            )
        else:
            return None
        logger.info(
            "review.gate.command_rejected session={} command={} code={}",
            session.key,
            command,
            code,
        )
        return self._gate_response(
            msg, run_id=run_id, status=status, code=code, content=content
        )

    def _review_command_gate(
        self, session: Session, msg: InboundMessage, raw: str
    ) -> OutboundMessage | None:
        return self.gate_command(session, msg, raw)

    def gate_review_turn(
        self, session_key: str, msg: InboundMessage, live_run: ReviewRunState | None
    ) -> OutboundMessage | None:
        """Refuse a second review turn for a session that already owns one."""
        if _is_internal_event(msg) or msg.content.strip().startswith("/"):
            return None
        if not _is_review_turn(msg.metadata):
            return None
        admitted_run_id = msg.metadata.get("_review_admitted")
        if (
            isinstance(admitted_run_id, str)
            and admitted_run_id
            and live_run is not None
            and live_run.run_id == admitted_run_id
            and live_run.status is ReviewRunStatus.RUNNING
        ):
            return None
        session = self.sessions.get_or_create(session_key)
        persisted_run_id = session.metadata.get(ReviewMetaKey.RUN_ID)
        run_id = (
            live_run.run_id
            if live_run is not None
            else str(persisted_run_id or admitted_run_id or "unknown")
        )
        status = (
            live_run.status if live_run is not None else ReviewRunStatus.COMPLETED
        )
        if live_run is not None or persisted_run_id:
            code = "duplicate_review"
            content = (
                "This review session already owns a review run. "
                "Start a new review session to run another review."
            )
        else:
            code = "review_not_admitted"
            content = (
                "A code review must be submitted through the review entry point "
                "(target, action and focus) so it can be validated and registered."
            )
        logger.info(
            "review.gate.review_turn_rejected session={} code={}", session_key, code
        )
        return self._gate_response(
            msg, run_id=run_id, status=status, code=code, content=content
        )

    def _is_admitted_review_turn(
        self, metadata: dict[str, Any] | None, session_key: str | None
    ) -> bool:
        """Whether this turn is the admitted execution of the live review run."""
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

    @staticmethod
    def _gate_response(
        msg: InboundMessage,
        *,
        run_id: str,
        status: ReviewRunStatus,
        code: str,
        content: str,
    ) -> OutboundMessage:
        """Structured rejection shared by every review gate."""
        metadata = {
            **dict(msg.metadata or {}),
            "render_as": "text",
            "review_gate": {
                "code": code,
                "status": status.value,
                "run_id": run_id,
                "accepted": False,
            },
        }
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            metadata=metadata,
        )

    def report_too_large_response(
        self, msg: InboundMessage, handoff: ReviewHandoff
    ) -> OutboundMessage:
        """Refuse the first conversation turn when the report cannot fit."""
        return self._gate_response(
            msg,
            run_id=handoff.result.run_id,
            status=handoff.result.status,
            code="review_report_too_large",
            content=(
                "The review report for this session does not fit in the model "
                "context window, so it cannot be injected in full. Use a model "
                "with a larger context window to continue this conversation."
            ),
        )

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

    # -- terminal persistence ----------------------------------------------

    def _review_settled(self, session: Session) -> bool:
        """Whether the session's review run reached its final ``DONE`` phase."""
        live = self.review_loop.get(session.key)
        if live is not None:
            return live.phase is ReviewPhase.DONE
        return session.metadata.get(ReviewMetaKey.PHASE) == ReviewPhase.DONE.value

    async def finalize(
        self,
        session_key: str,
        status: ReviewRunStatus,
        *,
        warning: str | None = None,
    ) -> ReviewResult | None:
        """Finalize the review run, then publish its index to the session."""
        result = await self.review_loop.finalize(session_key, status, warning=warning)
        if result is None:
            return None
        # The run's total usage (reviewers + judge + planner) is returned once,
        # on its single terminal result — record it here, never per child.
        self._record_result_usage(result.usage)
        session = self.sessions.get_or_create(session_key)
        self.write_context_index(session)
        return result

    async def _finalize_review_run(
        self,
        session_key: str,
        status: ReviewRunStatus,
        *,
        warning: str | None = None,
    ) -> None:
        await self.finalize(session_key, status, warning=warning)

    def write_context_index(self, session: Session) -> bool:
        """Write the review_context index message for a terminal review."""
        if not self._review_settled(session):
            return False
        result = self.result(session)
        if result is None or not result.is_terminal:
            return False
        if any(
            message.get("injected_event") == REVIEW_CONTEXT_EVENT
            and message.get("review_run_id") == result.run_id
            for message in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            render_review_context_index(result),
            injected_event=REVIEW_CONTEXT_EVENT,
            review_run_id=result.run_id,
            review_report_ref=result.report_ref,
            review_source="review_agent",
        )
        self.sessions.save(session)
        logger.info(
            "review.context.indexed session={} run_id={} handoff={}",
            session.key,
            result.run_id,
            result.handoff.value,
        )
        return True

    # -- handoff ------------------------------------------------------------

    def pending_handoff(self, session: Session) -> ReviewHandoff | None:
        """The handoff the next conversation turn must inject, if any."""
        result = self.result(session)
        if not self._review_settled(session):
            return None
        if result is None or not result.is_terminal:
            return None
        if session.metadata.get(ReviewMetaKey.HANDOFF_RUN_ID) == result.run_id:
            return None
        report = self._read_report(result)
        if report is None and result.handoff is not ReviewHandoffState.FAILED:
            result = dataclasses.replace(
                result,
                handoff=ReviewHandoffState.FAILED,
                error=result.error or "the review report artifact is unavailable",
            )
        return ReviewHandoff(
            result=result,
            report_markdown=report,
            fits=self._handoff_fits(result, report),
        )

    def consume_handoff(self, session: Session, handoff: ReviewHandoff) -> None:
        """Persist the injected handoff so later turns keep the same context.

        The coordinator owns this single replayable write; the conversation loop
        only calls it at the point the prepared handoff must enter history. The
        block goes into the conversation history (an assistant message that names
        ReviewAgent as its source) instead of being re-added to every system
        prompt, so it stays available to later turns and to consolidation while
        the report artifact remains the authoritative copy.
        """
        session.add_message(
            "assistant",
            handoff.block,
            injected_event=REVIEW_HANDOFF_EVENT,
            review_run_id=handoff.result.run_id,
            review_report_ref=handoff.result.report_ref,
            review_source="review_agent",
            review_handoff=handoff.result.handoff.value,
        )
        session.metadata[ReviewMetaKey.HANDOFF_RUN_ID] = handoff.result.run_id
        self.sessions.save(session)
        logger.info(
            "review.handoff.injected session={} run_id={} handoff={} report_chars={}",
            session.key,
            handoff.result.run_id,
            handoff.result.handoff.value,
            len(handoff.report_markdown or ""),
        )

    def _read_report(self, result: ReviewResult) -> str | None:
        """Load the authoritative report markdown for *result*."""
        if not result.report_ref:
            return None
        try:
            artifact = self.review_loop.artifacts.read(
                run_id=result.run_id,
                session_key=result.session_key,
                input_fingerprint=result.input_fingerprint,
            )
        except ReviewArtifactError as exc:
            logger.warning(
                "review.handoff.artifact_unreadable run_id={} reason={}",
                result.run_id,
                exc.reason,
            )
            return None
        markdown = artifact.get("report_markdown")
        return markdown if isinstance(markdown, str) and markdown.strip() else None

    def _prompt_budget(self) -> int:
        if self.context_window_tokens <= 0:
            return 0
        budget = (
            self.context_window_tokens
            - max(1, self._reserved_output_tokens)
            - _HANDOFF_PROMPT_RESERVE_TOKENS
        )
        return budget if budget > 0 else 0

    def _handoff_fits(self, result: ReviewResult, report_markdown: str | None) -> bool:
        """Whether the complete handoff fits the model context window."""
        budget = self._prompt_budget()
        if budget <= 0:
            return True
        tokens = estimate_prompt_tokens(
            [
                {
                    "role": "system",
                    "content": render_handoff_block(result, report_markdown),
                }
            ]
        )
        if tokens <= 0:
            tokens = max(1, len(report_markdown or "") // 4)
        return tokens <= budget

    # -- review turn --------------------------------------------------------

    async def _execute_review_turn(
        self,
        *,
        msg: InboundMessage,
        session: Session | None,
        session_key: str,
        events: EventSink,
        wants_stream: bool,
    ) -> OutboundMessage | None:
        """Hand the admitted review turn to ``ReviewLoop`` and publish its result.

        The coordinator never builds or saves the review context: ``ReviewLoop``
        resolves the plan/evidence/prompt, persists the user message and the
        report artifact, and returns the outcome. Here we only deliver it —
        including the chunked report stream when the transport asks for it.
        """

        async def _persist_automatic_subagent_result(
            subagent_message: InboundMessage,
        ) -> None:
            if session is None:
                return
            if persist_review_subagent_result(session, subagent_message):
                self.sessions.save(session)

        outcome = await self.review_loop.execute(
            ReviewTurnRequest(
                session_key=session_key,
                session=session,
                msg=msg,
                metadata=dict(msg.metadata or {}),
                events=events,
                result_callback=_persist_automatic_subagent_result,
            )
        )
        # The settle-failure wording is owned by ``ReviewLoop``: it appends the
        # bounded reason to the report it produces, so this turn host delivers
        # ``report_markdown`` verbatim and never re-decides what the user is
        # told from review state.
        final_content = outcome.report_markdown
        # The review run's total usage is returned once with its terminal
        # result; record it here so a review turn is counted like any other.
        if outcome.result is not None:
            self._record_result_usage(outcome.result.usage)
        if not final_content:
            return None
        if outcome.produces_report and wants_stream:
            await self._publish_review_report_stream(msg, session_key, final_content)
            return None
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=dict(msg.metadata or {}),
        )

    async def _publish_review_report_stream(
        self, msg: InboundMessage, session_key: str, report_markdown: str
    ) -> None:
        """Stream a produced report to the transport as its own stream kind."""
        stream_id = f"{msg.session_key}:{time.time_ns()}:review_report"
        report_meta = dict(msg.metadata or {})
        report_meta["_stream_delta"] = True
        report_meta["_stream_id"] = stream_id
        report_meta["_stream_kind"] = "review_report"
        end_meta = dict(msg.metadata or {})
        end_meta["_stream_end"] = True
        end_meta["_stream_id"] = stream_id
        end_meta["_stream_kind"] = "review_report"
        logger.info(
            "review.report.stream.start session={} chars={}",
            session_key,
            len(report_markdown),
        )
        for chunk in _stream_chunks(report_markdown):
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=chunk,
                    metadata=report_meta,
                )
            )
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="",
                metadata=end_meta,
            )
        )
        logger.info("review.report.stream.end session={}", session_key)

    async def _settle_review_run_after_stop(self, session_key: str) -> str | None:
        """Settle a review run left ``running`` after ``/stop`` cancelled its turn.

        A settlement that cannot be proven (cleanup or terminal save failed)
        stays unsettled, so the failure is reported instead of the gate
        silently staying closed.
        """
        state = self.review_loop.get(session_key)
        if state is None or state.status is not ReviewRunStatus.RUNNING:
            return None
        try:
            result = await self.finalize(session_key, ReviewRunStatus.STOPPED)
        except Exception as exc:
            logger.warning(
                "review.stop.settle_failed session={} reason={}", session_key, exc
            )
            return (
                f"The review run could not be settled "
                f"({type(exc).__name__}: {exc}); restart nanoreview to recover "
                "the session."
            )
        if result is None:
            return (
                "The review run could not be settled; restart nanoreview to "
                "recover the session."
            )
        return f"Settled review run {result.run_id} as stopped."

    # -- turn execution -----------------------------------------------------

    async def _execute_turn(
        self,
        msg: InboundMessage,
        session_key: str,
        *,
        pending_queue: asyncio.Queue | None,
        events: EventSink,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Route one already-admitted, already-gated turn to the right loop.

        Commands are dispatched here (the coordinator owns the command router);
        an admitted review turn goes to ``ReviewLoop``; a user message is a
        conversation turn.
        """
        self._refresh_provider_snapshot()
        raw = msg.content.strip()
        # Defensive check for callers that reach this internal entry directly
        # (bypassing the bus/direct drop): a leftover internal event must not be
        # routed as a turn, run a command, or consume the review handoff.
        if self._drop_internal_event(msg, entry="execute_turn"):
            return None
        session = self.sessions.get_or_create(session_key)

        # Review-phase gate: while a review run is live, an ordinary message is
        # refused here too, so the direct ``_dispatch`` path is gated exactly
        # like the bus path in ``run``. Commands and internal events stay
        # available and are gated by their own handlers below.
        if not raw.startswith("/") and not _is_internal_event(msg):
            gate_response = self.gate_message(
                msg, self.review_loop.get(session_key), raw
            )
            if gate_response is not None:
                return gate_response

        if raw.startswith("/"):
            command_gate = self.gate_command(session, msg, raw)
            if command_gate is not None:
                return command_gate
            result = await self.commands.dispatch(
                CommandContext(
                    msg=msg, session=session, key=session_key, raw=raw, loop=self
                )
            )
            if result is not None:
                self._persist_command_turn(session, msg, raw, result)
                return result
            # An unrecognised slash command falls through to a normal turn.

        if self._is_admitted_review_turn(msg.metadata, session_key):
            return await self._execute_review_turn(
                msg=msg,
                session=session,
                session_key=session_key,
                events=events,
                wants_stream=bool(msg.metadata.get("_wants_stream")),
            )

        target_root = self._resolve_target_root(session)
        turn_id = f"{session_key}:{time.time_ns()}"

        handoff = self.pending_handoff(session)
        if handoff is not None and not handoff.fits:
            return self.report_too_large_response(msg, handoff)

        return await self.conversation_loop.process_message(
            msg,
            session_key=session_key,
            turn_id=turn_id,
            target_root=target_root,
            handoff=handoff if handoff is not None and handoff.fits else None,
            events=events,
            on_retry_wait=on_retry_wait,
            pending_queue=pending_queue,
        )

    def _resolve_target_root(self, session: Session) -> Path:
        """Resolve the file-path base / default command cwd for a turn.

        A session that carried a review uses the reviewed repository root; every
        other session uses the configured workspace.
        """
        local_root = session.metadata.get(ReviewMetaKey.LOCAL_ROOT)
        if isinstance(local_root, str) and local_root.strip():
            root = Path(local_root)
            if root.is_dir():
                return root
        return self.workspace

    def _persist_command_turn(
        self, session: Session, msg: InboundMessage, raw: str, result: OutboundMessage
    ) -> None:
        """Persist a shortcut command's user/assistant pair for WebUI history."""
        if raw.lower() == "/new":
            return
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        if (isinstance(msg.content, str) and msg.content.strip()) or media_paths:
            extra: dict[str, Any] = {"media": list(media_paths)} if media_paths else {}
            session.add_message(
                "user",
                msg.content if isinstance(msg.content, str) else "",
                _command=True,
                **extra,
            )
        session.add_message("assistant", result.content, _command=True)
        self.sessions.save(session)

    # -- run loop -----------------------------------------------------------

    async def run(self) -> None:
        """Run the coordinator, dispatching messages as tasks for /stop."""
        self._running = True

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            # ``aclose()`` may have run while this turn was waiting on the bus.
            # Admission is closed at that point, so the message must be dropped
            # rather than dispatched into a shutting-down coordinator.
            if self._closed:
                logger.debug(
                    "Dropping inbound message after shutdown: session={}", msg.session_key
                )
                continue

            raw = msg.content.strip()
            # Leftover internal events must not reach command dispatch, gating
            # or the pending queue: they are dropped here, before routing.
            if self._drop_internal_event(msg, entry="bus"):
                continue
            if self.commands.is_priority(raw):
                # Control commands target the same key the turns are
                # registered under, so ``/stop`` cancels *this* session's
                # current and waiting work (unified sessions included).
                await self._dispatch_command_inline(
                    msg,
                    self._effective_session_key(msg),
                    raw,
                    self.commands.dispatch_priority,
                )
                continue
            effective_key = self._effective_session_key(msg)
            review_state = self.review_loop.get(effective_key)
            gate_response = self.gate_message(msg, review_state, raw)
            if gate_response is not None:
                await self._publish_review_gate_response(msg, gate_response)
                continue
            if effective_key in self._pending_queues:
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
        # The task is created by the bus loop and only starts after a yield, so
        # shutdown can win the race. Re-check admission here: a turn that has
        # not started yet must not run after ``aclose()``.
        if self._closed:
            logger.debug("Skipping dispatch after shutdown: session={}", msg.session_key)
            return
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        admission_rejection = self.gate_review_turn(
            session_key, msg, self.review_loop.get(session_key)
        )
        if admission_rejection is not None:
            await self._publish_review_gate_response(msg, admission_rejection)
            return
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        pending = asyncio.Queue(maxsize=MAX_PENDING_CONVERSATION_MESSAGES)
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
                    metadata={**dict(msg.metadata or {}), "_turn_end": True},
                )
            )
            turn_end_sent = True

        try:
            async with lock, gate:
                try:
                    # One sink per turn: progress, stream segments and tool
                    # activity all project onto this message's bus route. The
                    # stream id is resolved per event so each segment gets a
                    # fresh id, exactly like the previous inline callbacks.
                    async def _publish_turn_event(event: Any) -> None:
                        nonlocal stream_segment
                        publish = self.bus.publish_outbound
                        meta = metadata_for_event(
                            msg.metadata,
                            event,
                            stream_kind="review_thinking" if review_stream else None,
                        )
                        if meta is None:
                            return
                        # The end event closes the segment it was opened with, so
                        # it carries the *current* id; only then does the next
                        # segment start. Incrementing first would point the end
                        # at a segment the transport never opened.
                        meta["_stream_id"] = _current_stream_id()
                        await publish(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=event_text(event),
                                metadata=meta,
                            )
                        )
                        if isinstance(event, StreamEndEvent):
                            stream_segment += 1

                    # Without a stream consumer the turn still publishes
                    # progress and tool activity, but skips stream events so
                    # providers do not pay for deltas nobody renders.
                    events = EventSink(
                        publish=_publish_turn_event,
                        accepts_type=lambda event_type: wants_stream
                        or not issubclass(event_type, (StreamDeltaEvent, StreamEndEvent)),
                    )
                    on_retry_wait = await self._build_retry_wait_callback(msg)
                    response = await self._execute_turn(
                        msg,
                        session_key,
                        pending_queue=pending,
                        events=events,
                        on_retry_wait=on_retry_wait,
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

                    session = self.sessions.get_or_create(session_key)
                    self.write_context_index(session)

                    if msg.channel == "websocket":
                        turn_metadata: dict[str, Any] = {
                            **msg.metadata,
                            "_turn_end": True,
                        }
                        latency_ms = (
                            response.metadata.get("latency_ms")
                            if response is not None
                            else None
                        )
                        if latency_ms is not None:
                            turn_metadata["latency_ms"] = int(latency_ms)
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
                        session = self.sessions.get_or_create(session_key)
                        if self.conversation_loop.restore_runtime_checkpoint(session):
                            self.conversation_loop.clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                session_key,
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
            with suppress(asyncio.CancelledError):
                await self._finalize_review_run(session_key, ReviewRunStatus.STOPPED)
            raise
        finally:
            self.review_loop.discard_unstarted(session_key)
            # Only drain the queue this turn owns: ``/stop`` pops it first so a
            # stopped turn re-publishes nothing, and a queue registered by a
            # fresh turn for the same key must not be drained here.
            queue = self._pending_queues.get(session_key)
            if queue is pending:
                self._pending_queues.pop(session_key, None)
            else:
                queue = None
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.bus.publish_inbound(item)
                    leftover += 1
                if leftover:
                    logger.info(
                        "Re-published {} leftover message(s) to bus for session {}",
                        leftover,
                        session_key,
                    )
            await publish_turn_run_status(self.bus, msg, "idle")
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

    async def aclose(self) -> None:
        """Shut the coordinator down: stop admission, then release owned resources.

        Idempotent, and the single shutdown entry for the gateway, CLI and API
        callers — SDK callers must call it explicitly so no MCP subprocess or
        owner task outlives the coordinator. Order matters: admission stops
        first so no new turn can register MCP proxies, then active turn and
        subagent tasks are cancelled and awaited, then background tasks are
        drained, and only then are MCP connections closed.
        """
        if self._closed:
            return
        self._closed = True
        self._running = False
        # ``_closed`` is the real admission switch: the bus loop, the dispatch
        # entry and the MCP proxy registration all check it. It is set before
        # any await so no new turn can be admitted while the tasks below are
        # being cancelled.

        for tasks in list(self._active_tasks.values()):
            for task in tasks:
                task.cancel()
        active = [task for tasks in self._active_tasks.values() for task in tasks]
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active_tasks.clear()

        pending_queues = list(self._pending_queues.values())
        self._pending_queues.clear()
        for queue in pending_queues:
            while not queue.empty():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
        self._session_locks.clear()

        await self.close_background_tasks()
        await self.mcp.aclose()

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._remove_background_task)

    def _remove_background_task(self, task: asyncio.Task) -> None:
        with suppress(ValueError):
            self._background_tasks.remove(task)

    def stop(self) -> None:
        """Stop the coordinator."""
        self._running = False
        log_event(logger, "info", "agent.loop.stopping", status="running")

    def _record_result_usage(self, usage: dict[str, int] | None) -> None:
        """Fold one returned result's usage into ``_last_usage``/``_total_usage``.

        Called exactly once per returned result — for a conversation turn with
        the ``AgentRunResult.usage`` the loop hands back, and for a review run
        with the ``ReviewResult.usage`` of its single terminal result (which
        already totals its reviewers, judge and planner, so it must not be
        summed again per child). Recording happens as the result comes back, so
        a later persistence failure cannot wipe usage that was already produced.
        """
        if not usage:
            return
        self._last_usage = dict(usage)
        self._accumulate_total_usage(usage)

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

    # -- direct entry -------------------------------------------------------

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        events: EventSink | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutboundMessage | None:
        """Process one request directly, serialised on the session lock.

        Used by the API/CLI/SDK entry points. The caller-supplied ``session_key``
        is authoritative: it is stamped as the message override so gating,
        locking, task registration, history and the review-run query all use the
        same key, while ``channel``/``chat_id`` only decide where the reply is
        delivered. The message never joins a running turn's pending queue — it
        waits for the session lock instead — so a concurrent direct request
        always gets its own reply instead of injecting into another turn.

        The request is registered as an active task *before* it waits for the
        lock, so ``/stop`` can cancel it while it is queued and so it counts
        against the concurrency limit. Priority control commands (``/stop`` …)
        are dispatched inline instead: they never wait for the execution lock
        and are not added to the cancellation set (a ``/stop`` must not cancel
        itself).

        A ``/stop``-cancelled request is not an error: it returns an explicit
        "stopped" reply (``metadata["stop_reason"] == "stopped"``) so the
        transport can answer the caller instead of reporting a failure. Any
        other cancellation (timeout, disconnect, caller cancel) keeps its own
        semantics and propagates untouched.

        A request that arrives after ``aclose()`` is refused the same way as an
        inbound bus message: the direct entry points (API/CLI/SDK) share the
        coordinator's admission switch, so ``_closed`` must be checked before a
        turn is started or registered as an active task.
        """
        # ``aclose()`` may have run before this request was issued. Refuse it
        # here, before the message is registered as an active task, so a
        # shutting-down coordinator never services a new direct turn.
        if self._closed:
            logger.debug(
                "Refusing direct request after shutdown: session={}", session_key
            )
            return None
        key = self._direct_session_key(session_key)
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata=dict(metadata or {}),
            session_key_override=key,
        )
        raw = content.strip()

        # A leftover internal event must not run as a turn nor be registered as
        # a cancellable task; report "nothing to answer" to the caller.
        if self._drop_internal_event(msg, entry="direct"):
            return None

        if self.commands.is_priority(raw):
            # Control commands must run even while this session's lock is held.
            return await self.commands.dispatch_priority(
                CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
            )

        # The sink is resolved before the lock so a request cancelled while
        # still queued (``/stop``) can still close its stream.
        if events is None:
            events = await self._build_bus_event_sink(msg)

        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.setdefault(key, []).append(task)
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        try:
            async with lock, gate:
                gate_response = self.gate_message(msg, self.review_loop.get(key), raw)
                if gate_response is not None:
                    return gate_response
                admission_rejection = self.gate_review_turn(
                    key, msg, self.review_loop.get(key)
                )
                if admission_rejection is not None:
                    return admission_rejection
                response = await self._execute_turn(
                    msg,
                    key,
                    pending_queue=None,
                    events=events,
                )
                session = self.sessions.get_or_create(key)
                self.write_context_index(session)
                return response
        except asyncio.CancelledError as exc:
            if not _is_stop_cancellation(exc):
                raise
            # Deliberate stop: absorb the cancellation and answer the caller.
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            return await self._stopped_direct_reply(msg, key, events=events)
        finally:
            if task is not None:
                self._remove_active_task(key, task)

    async def _stopped_direct_reply(
        self,
        msg: InboundMessage,
        session_key: str,
        *,
        events: EventSink,
    ) -> OutboundMessage:
        """Build the explicit reply for a request cancelled by ``/stop``.

        A stopped turn keeps whatever it already did (files, already-streamed
        text) and only its unfinished history is backfilled through the runtime
        checkpoint, exactly like an interrupted bus turn — no tool is re-run.
        A request that never started has no checkpoint, so it writes no history
        at all. Streaming transports get the stop explanation appended once and
        their stream closed; the same text is returned for non-streaming
        callers.
        """
        notes: list[str] = []
        try:
            session = self.sessions.get_or_create(session_key)
            if self.conversation_loop.restore_runtime_checkpoint(session):
                self.conversation_loop.clear_pending_user_turn(session)
                self.sessions.save(session)
        except Exception as exc:
            logger.warning(
                "agent.stop.persist_failed session={} reason={}", session_key, exc
            )
            notes.append(
                f"the interrupted turn could not be persisted "
                f"({type(exc).__name__}: {exc})"
            )

        content = STOPPED_REPLY_CONTENT
        if notes:
            content = f"{content} {'; '.join(notes)}"

        # The same text is returned to every caller as the reply body, so it may
        # only be pushed through the stream channel when a stream consumer is
        # bound. Doing it unconditionally duplicates a non-streaming request's
        # body: once as a delta/end pair nobody renders, once as the reply.
        if events.accepts(StreamDeltaEvent):
            with suppress(Exception):
                await events.emit(StreamDeltaEvent(content=content))
            with suppress(Exception):
                await events.emit(StreamEndEvent(resuming=False))

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            metadata={
                **dict(msg.metadata or {}),
                STOP_REASON_META_KEY: STOPPED_STOP_REASON,
            },
        )


__all__ = [
    "INTERRUPTED_RUN_REASON",
    "MAX_PENDING_CONVERSATION_MESSAGES",
    "REVIEW_ALLOWED_COMMANDS",
    "REVIEW_CONTEXT_EVENT",
    "REVIEW_HANDOFF_EVENT",
    "STOP_CANCEL_REASON",
    "STOPPED_REPLY_CONTENT",
    "STOPPED_STOP_REASON",
    "STOP_REASON_META_KEY",
    "ReviewHandoff",
    "SessionCoordinator",
    "SessionRoute",
    "UNIFIED_SESSION_KEY",
    "_is_review_turn",
]
