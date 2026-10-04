"""Session coordinator: the process-level agent entry point.

``SessionCoordinator`` is both the process runtime and the decision point
between the two agent phases a NanoReview session can be in:

* **review** — one review run owns the session; ordinary messages and
  non-control commands are refused without leaving history behind.
* **conversation** — the review reached a terminal status, its resources were
  cleaned up and its result was persisted; ordinary messages are accepted and
  the first one carries the review handoff.

It owns the process skeleton — MessageBus receive/send, per-session serial
locks and the bounded pending queue, command dispatch and permission responses,
cancellation scheduling, the review/conversation route and gates, handoff
preparation, and final result publication — and delegates the actual turn
algorithms:

* ``ReviewLoop`` owns a complete review turn: it builds the review context
  (``ContextBuilder`` + ``COMMON_RULES``), persists the user message and the
  report artifact, drives the lifecycle and returns the structured result.
* ``ConversationLoop`` owns a complete conversation turn: session/history,
  handoff consumption, context and per-turn ``ToolRegistry``, the single
  ``AgentRunner`` run, history persistence and reply assembly.

The coordinator itself calls no model and executes no tool: a review turn is
handed over whole and only its produced report is published (including the
report chunk stream); a conversation turn's history is built and saved by the
conversation loop, never here.

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
from nanoreview.agent.autocompact import AutoCompact
from nanoreview.agent.context import ContextBuilder
from nanoreview.agent.conversation_loop import (
    MAX_PENDING_CONVERSATION_MESSAGES,
    ConversationLoop,
    _is_consumed_subagent_result,
    persist_subagent_followup,
)
from nanoreview.agent.handoff import (
    REVIEW_CONTEXT_EVENT,
    REVIEW_HANDOFF_EVENT,
    ReviewHandoff,
    consume_handoff,
)
from nanoreview.agent.hooks.lifecycle import AgentHook
from nanoreview.agent.memory import Consolidator
from nanoreview.agent.review_loop import ReviewLoop, ReviewTurnRequest
from nanoreview.agent.review_state import (
    ReviewArtifactError,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
)
from nanoreview.agent.runner import AgentRunner
from nanoreview.agent.subagent import SubagentManager
from nanoreview.agent.tools.file_state import FileStateStore
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.command import (
    CommandContext,
    CommandRouter,
    register_builtin_commands,
)
from nanoreview.config.schema import AgentDefaults, ModelPresetConfig
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

#: How much of the context window is held back from the handoff check for the
#: system prompt, runtime block and the model's own output.
_HANDOFF_PROMPT_RESERVE_TOKENS = 1024

#: Reason recorded when a run lost its executor before producing a result.
INTERRUPTED_RUN_REASON = (
    "the review was interrupted before it produced a result and cannot be resumed"
)

#: Placeholder for "key absent" when snapshotting session metadata before a
#: repair write, so a failed save can restore the exact previous state.
_ABSENT = object()


def _is_review_turn(metadata: dict[str, Any] | None) -> bool:
    meta = metadata or {}
    return bool(meta.get(ReviewMetaKey.TARGET) or meta.get("review_target"))


def _is_internal_event(msg: InboundMessage) -> bool:
    """Subagent results and system events bypass review session gating."""
    meta = msg.metadata if isinstance(msg.metadata, dict) else {}
    return (
        msg.channel == "system"
        or msg.sender_id == "subagent"
        or meta.get("injected_event") in ("subagent_result", "subagent_barrier")
    )


def _stream_chunks(text: str, *, chunk_size: int = 2048) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


class SessionRoute(StrEnum):
    """Which agent phase owns the session right now."""

    REVIEW = "review"
    CONVERSATION = "conversation"


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
        session_ttl_minutes: int = 0,
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
        # Permission approval policy currently lives on ToolsConfig.
        self.permissions_config = _tc
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
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        self._permission_futures: dict[str, asyncio.Future[bool]] = {}

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
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
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
            subagents=self.subagents,
            consolidator=self.consolidator,
            auto_compact=self.auto_compact,
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
            permission_requester=self._request_tool_permission,
            hooks=self._extra_hooks,
            hooks_getter=lambda: self._extra_hooks,
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

    # -- provider / model ---------------------------------------------------

    def _resolve_subagent_reasoning_effort(self) -> str | None:
        subagent_effort = getattr(self.review_config, "subagent_reasoning_effort", None)
        if subagent_effort is not None:
            return subagent_effort
        return self._default_reasoning_effort

    def _sync_subagent_runtime_limits(self) -> None:
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
        tool = self.tools.get("local_review") or self.tools.get("github_review")
        if tool is None:
            return None
        return getattr(tool, "evidence_provider", None)

    # -- bus helpers --------------------------------------------------------

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

    async def _request_tool_permission(
        self,
        request_id: str,
        payload: dict[str, Any],
        future: asyncio.Future[bool],
        channel: str,
        chat_id: str,
    ) -> bool:
        """Ask the transport for a per-tool approval and await its response."""
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
        """Cancel and await all active tasks and subagents for *key*."""
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
        """Persist the injected handoff so later turns keep the same context."""
        consume_handoff(session, handoff, self.sessions)

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
        on_progress: Callable[..., Awaitable[None]] | None,
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
            if persist_subagent_followup(session, subagent_message):
                self.sessions.save(session)

        outcome = await self.review_loop.execute(
            ReviewTurnRequest(
                session_key=session_key,
                session=session,
                msg=msg,
                metadata=dict(msg.metadata or {}),
                progress_callback=on_progress,
                result_callback=_persist_automatic_subagent_result,
            )
        )
        final_content = outcome.report_markdown
        if outcome.produces_report and outcome.error:
            final_content = (
                f"{final_content}\n\n> Review settlement failed: {outcome.error}"
            )
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
        """Settle a review run left ``running`` after ``/stop`` cancelled its turn."""
        state = self.review_loop.get(session_key)
        if state is None or state.status is not ReviewRunStatus.RUNNING:
            return None
        result = await self.finalize(session_key, ReviewRunStatus.STOPPED)
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
        consumed_subagent_task_ids: set[str] | None,
        on_progress: Callable[..., Awaitable[None]] | None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Route one already-admitted, already-gated turn to the right loop.

        Commands are dispatched here (the coordinator owns the command router);
        an admitted review turn goes to ``ReviewLoop``; everything else —
        including system/subagent events — is a conversation turn.
        """
        self._refresh_provider_snapshot()
        raw = msg.content.strip()
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
                on_progress=on_progress,
                wants_stream=bool(msg.metadata.get("_wants_stream")),
            )

        target_root = self._resolve_target_root(session)
        turn_id = f"{session_key}:{time.time_ns()}"

        if msg.channel == "system":
            return await self.conversation_loop.process_system_message(
                msg,
                session_key=session_key,
                turn_id=turn_id,
                target_root=target_root,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
            )

        handoff = self.pending_handoff(session)
        if handoff is not None and not handoff.fits:
            return self.report_too_large_response(msg, handoff)

        return await self.conversation_loop.process_message(
            msg,
            session_key=session_key,
            turn_id=turn_id,
            target_root=target_root,
            handoff=handoff if handoff is not None and handoff.fits else None,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            on_retry_wait=on_retry_wait,
            pending_queue=pending_queue,
            consumed_subagent_task_ids=consumed_subagent_task_ids,
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
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
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
                    metadata={**dict(msg.metadata or {}), "_turn_end": True},
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
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
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
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
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
                            stream_segment += 1

                    on_progress = await self._build_bus_progress_callback(msg)
                    on_retry_wait = await self._build_retry_wait_callback(msg)
                    response = await self._execute_turn(
                        msg,
                        session_key,
                        pending_queue=pending,
                        consumed_subagent_task_ids=consumed_subagent_task_ids,
                        on_progress=on_progress,
                        on_stream=on_stream,
                        on_stream_end=on_stream_end,
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
            queue = self._pending_queues.pop(session_key, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if _is_consumed_subagent_result(
                        item, consumed_subagent_task_ids
                    ):
                        continue
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
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly, serialised on the session lock.

        Used by the API/CLI entry points: the message never joins the pending
        queue of a running turn — it waits for the session lock instead — so a
        concurrent direct request cannot inject itself into another turn.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata=dict(metadata or {}),
        )
        key = self._effective_session_key(msg)
        if key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=key)
        raw = content.strip()
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        async with lock:
            gate_response = self.gate_message(msg, self.review_loop.get(key), raw)
            if gate_response is not None:
                return gate_response
            admission_rejection = self.gate_review_turn(
                key, msg, self.review_loop.get(key)
            )
            if admission_rejection is not None:
                return admission_rejection
            if on_progress is None:
                on_progress = await self._build_bus_progress_callback(msg)
            response = await self._execute_turn(
                msg,
                key,
                pending_queue=None,
                consumed_subagent_task_ids=None,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
            )
            session = self.sessions.get_or_create(key)
            self.write_context_index(session)
            return response


__all__ = [
    "INTERRUPTED_RUN_REASON",
    "MAX_PENDING_CONVERSATION_MESSAGES",
    "REVIEW_ALLOWED_COMMANDS",
    "REVIEW_CONTEXT_EVENT",
    "REVIEW_HANDOFF_EVENT",
    "ReviewHandoff",
    "SessionCoordinator",
    "SessionRoute",
    "UNIFIED_SESSION_KEY",
    "_is_review_turn",
]
