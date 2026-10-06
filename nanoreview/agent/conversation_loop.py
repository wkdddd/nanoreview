"""ConversationLoop: one complete conversation turn for one session.

``ConversationLoop`` owns the whole ordinary (non-review) conversation turn:
it loads the session, consumes the first review handoff through the
coordinator, builds the frozen/working context, creates a core-only
``ToolRegistry`` for the turn, runs one ``AgentRunner`` run (the model/tool loop
lives in the runner), persists the turn's history and assembles the outbound
reply.

It never decides *whether* the conversation is open: the ``SessionCoordinator``
routes, gates, and prepares the read-only handoff inside the session lock, then
calls :meth:`ConversationLoop.process_message`. The handoff is written by the
coordinator through the injected ``handoff_consumer``; the loop only chooses
*when* in its turn sequence that write happens. The loop also never reads the
review report artifact or rewrites the review run state.

Turn shape (linear helper calls, no state machine, no transition table):

    load session -> restore checkpoint -> consume handoff
      -> build context/tools -> run -> persist history -> assemble reply
      -> cleanup
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanoreview.agent.context import ContextBuilder
from nanoreview.agent.hooks.file_edit import create_file_edit_activity_hook
from nanoreview.agent.hooks.lifecycle import AgentHook
from nanoreview.agent.hooks.turn_hooks import AgentTurnHookSpec, build_agent_turn_hook
from nanoreview.agent.memory import Consolidator
from nanoreview.agent.runner import (
    _MAX_INJECTIONS_PER_TURN,
    AgentRunner,
    AgentRunResult,
    AgentRunSpec,
)
from nanoreview.agent.tools.context import (
    ContextAware,
    RequestContext,
    set_current_request_context,
)
from nanoreview.agent.tools.file_state import (
    FileStateStore,
    bind_file_states,
    reset_file_states,
)
from nanoreview.agent.tools.loader import ToolLoader
from nanoreview.agent.tools.message import MessageTool
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.events import NO_EVENTS, EventSink, StreamDeltaEvent, StreamEndEvent
from nanoreview.review import apply_review_metadata_from_message
from nanoreview.utils.artifacts import generated_image_paths_from_messages
from nanoreview.utils.document import extract_documents
from nanoreview.utils.helpers import image_placeholder_text
from nanoreview.utils.helpers import truncate_text as truncate_text_fn
from nanoreview.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE
from nanoreview.utils.session_attachments import merge_turn_media_into_last_assistant
from nanoreview.utils.webui_titles import mark_webui_session
from nanoreview.utils.webui_turn_helpers import publish_turn_run_status

if TYPE_CHECKING:
    from nanoreview.agent.coordinator import ReviewHandoff
    from nanoreview.agent.tools.mcp import MCPProvider
    from nanoreview.config.schema import ToolsConfig
    from nanoreview.providers.base import LLMProvider
    from nanoreview.session.manager import Session, SessionManager

#: Maximum number of messages a session may queue while a turn is running.
MAX_PENDING_CONVERSATION_MESSAGES = 20

#: Tools that stay review-only: the conversation agent never sees them.
_CONVERSATION_DENIED_TOOLS = frozenset({"local_review", "github_review"})

_RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
_PENDING_USER_TURN_KEY = "pending_user_turn"


@dataclass
class _TurnContext:
    """Mutable state for exactly one turn; never shared or persisted."""

    msg: InboundMessage
    session_key: str
    turn_id: str
    target_root: Path
    session: "Session | None" = None

    #: Outbound routing, derived from the message's channel/chat id.
    outbound_channel: str | None = None
    outbound_chat_id: str | None = None

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

    tools: ToolRegistry | None = None
    handoff_directive: str | None = None

    pending_queue: asyncio.Queue | None = None

    #: Typed event delivery for this turn. Transports build the sink; the
    #: turn's hook chain publishes onto it and never sees raw callbacks.
    events: EventSink = NO_EVENTS
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    turn_latency_ms: int | None = None


class ConversationLoop:
    """Execute the conversation phase's turns; one ``AgentRunner`` run each."""

    def __init__(
        self,
        *,
        handoff_consumer: Callable[["Session", "ReviewHandoff"], None],
        bus: MessageBus,
        provider: "LLMProvider",
        workspace: Path,
        sessions: "SessionManager",
        context: ContextBuilder,
        runner: AgentRunner,
        consolidator: Consolidator,
        file_state_store: FileStateStore,
        tools_config: "ToolsConfig",
        model: str,
        max_iterations: int,
        max_tool_result_chars: int,
        context_window_tokens: int,
        context_block_limit: int | None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int = 0,
        max_messages: int = 120,
        review_config: Any = None,
        provider_snapshot_loader: Callable[..., Any] | None = None,
        background_scheduler: Callable[[Awaitable[Any]], None] | None = None,
        permission_requester: (
            Callable[
                [str, dict[str, Any], "asyncio.Future[bool]", str, str],
                Awaitable[bool],
            ]
            | None
        ) = None,
        hooks: list[AgentHook] | None = None,
        hooks_getter: Callable[[], list[AgentHook]] | None = None,
        usage_recorder: Callable[[dict[str, int]], None] | None = None,
        mcp_provider: "MCPProvider | None" = None,
    ) -> None:
        self._bus = bus
        self._provider = provider
        self._workspace = Path(workspace)
        self._sessions = sessions
        self._context = context
        self._runner = runner
        self._consolidator = consolidator
        self._file_state_store = file_state_store
        self._tools_config = tools_config
        self._model = model
        self._max_iterations = max_iterations
        self._max_tool_result_chars = max_tool_result_chars
        self._context_window_tokens = context_window_tokens
        self._context_block_limit = context_block_limit
        self._provider_retry_mode = provider_retry_mode
        self._tool_hint_max_length = tool_hint_max_length
        self._max_messages = max_messages if max_messages > 0 else 120
        self._review_config = review_config
        self._provider_snapshot_loader = provider_snapshot_loader
        self._background_scheduler = background_scheduler
        self._permission_requester = permission_requester
        self._extra_hooks: list[AgentHook] = hooks if hooks is not None else []
        # Dynamic accessor so the owner (coordinator/SDK) can swap its hook
        # list between turns; falls back to the static list when absent.
        self._hooks_getter = hooks_getter
        #: In-memory hand-off of each returned run's usage to the owner
        #: (the coordinator aggregates last/total usage). Reported right after
        #: the runner returns, so it is independent of whether a reply body was
        #: delivered and survives a later persistence failure.
        self._usage_recorder = usage_recorder
        #: Writes the prepared review handoff into the session. Injected by the
        #: coordinator, which owns that single replayable write; the loop only
        #: calls it between history consolidation and the history read.
        self._handoff_consumer = handoff_consumer
        # MCP is Conversation-only. The provider owns the connections and the
        # live wrappers; each turn registers proxies that resolve the current
        # wrapper at call time, so a reconnect does not require rebuilding an
        # in-flight turn's registry.
        self._mcp_provider = mcp_provider
        # One loader instance keeps the (package-scan) discovery cache warm;
        # each turn still registers a fresh registry from it.
        self._tool_loader = ToolLoader()
        self._current_iteration = 0

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    def set_runtime_model(
        self,
        provider: "LLMProvider",
        model: str,
        context_window_tokens: int | None,
    ) -> None:
        """Swap the provider/model for future turns without touching an active one.

        Called by the coordinator when a model preset is applied. The runner and
        subagents are swapped by the coordinator through their own setters; the
        loop only needs the values its per-turn context and budget depend on.
        """
        self._provider = provider
        self._model = model
        if context_window_tokens is not None:
            self._context_window_tokens = context_window_tokens

    # -- entry points -------------------------------------------------------

    async def process_message(
        self,
        msg: InboundMessage,
        *,
        session_key: str,
        turn_id: str,
        target_root: Path,
        handoff: "ReviewHandoff | None" = None,
        events: EventSink = NO_EVENTS,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Run one conversation turn and return its reply (or ``None``).

        ``None`` means the turn produced nothing to deliver as a reply body
        (e.g. the ``message`` tool already sent it); it never means failure.
        """
        ctx = _TurnContext(
            msg=msg,
            session_key=session_key,
            turn_id=turn_id,
            target_root=Path(target_root),
            handoff_directive=handoff.directive if handoff is not None else None,
            outbound_channel=msg.channel,
            outbound_chat_id=msg.chat_id,
            events=events,
            on_retry_wait=on_retry_wait,
            pending_queue=pending_queue,
        )
        try:
            await self._load_session(ctx)
            await self._build(ctx, handoff)
            await self._run(ctx)
            self._save(ctx)
            await self._respond(ctx)
            return ctx.outbound
        finally:
            self._cleanup(ctx)

    # -- pipeline helpers ---------------------------------------------------

    async def _load_session(self, ctx: _TurnContext) -> None:
        """Load the session and restore any interrupted turn's placeholder."""
        msg = ctx.msg
        if msg.media:
            new_content, image_only = extract_documents(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(
            "Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview
        )

        session = self._sessions.get_or_create(ctx.session_key)
        mark_webui_session(session, msg.metadata)
        if apply_review_metadata_from_message(session, msg.metadata):
            self._sessions.save(session)
        if self._restore_runtime_checkpoint(session):
            self._sessions.save(session)
        if self._restore_pending_user_turn(session):
            self._sessions.save(session)
        ctx.session = session

    async def _build(
        self, ctx: _TurnContext, handoff: "ReviewHandoff | None"
    ) -> None:
        """Consume the handoff, build the context and create the tool registry."""
        await self._consolidator.maybe_consolidate_by_tokens(
            ctx.session,
            replay_max_messages=self._max_messages,
        )

        # The handoff was prepared (and size-checked) by the coordinator, which
        # also owns the single replayable write. The loop calls it after history
        # consolidation and before reading history, so the report is available to
        # this turn and every later one; a failed write propagates and the
        # runner never starts.
        if handoff is not None:
            self._handoff_consumer(ctx.session, handoff)

        ctx.history = self._session_history(ctx.session)
        ctx.tools = await self._build_turn_tools(ctx)
        self._set_tool_context(ctx)
        if (message_tool := ctx.tools.get("message")) and isinstance(
            message_tool, MessageTool
        ):
            message_tool.start_turn()

        ctx.frozen_messages, ctx.working_messages = self._build_partitioned(ctx)
        self._apply_handoff_directive(ctx.frozen_messages, ctx.handoff_directive)
        ctx.user_persisted_early = self._persist_user_message_early(ctx)

    async def _run(self, ctx: _TurnContext) -> None:
        """Execute one ``AgentRunner`` run and collect its result."""
        await publish_turn_run_status(self._bus, ctx.msg, "running")
        result = await self._run_runner(ctx)
        ctx.final_content = result.final_content
        ctx.tools_used = list(result.tools_used)
        ctx.all_messages = list(result.messages)
        ctx.stop_reason = result.stop_reason
        ctx.had_injections = result.had_injections
        ctx.content_replaced = result.content_replaced
        # Report the returned run's usage before anything else can fail: the
        # owner must count it even when the ``message`` tool already sent the
        # reply (``None`` outbound) and even if persisting this turn fails.
        if self._usage_recorder is not None:
            self._usage_recorder(dict(result.usage or {}))
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self._max_iterations)
            # The wrap-up body is delivered as a normal reply too (see
            # ``_respond``), so it may only be pushed through the stream channel
            # when a stream consumer is actually bound. Publishing it regardless
            # duplicates a non-streaming turn's body: once as a delta/end pair
            # nobody renders, and once as the plain reply.
            #
            # Delivery failures must propagate (``publish``, not ``emit``):
            # ``_respond`` marks the reply ``_streamed=True`` on the strength of
            # this push having happened, and ``ChannelManager`` then skips
            # ``channel.send`` for a ``_streamed`` message. A swallowed failure
            # would therefore mark a reply as already-rendered that never was,
            # and the wrap-up text would reach nobody at all.
            if ctx.events.accepts(StreamDeltaEvent):
                await ctx.events.publish(
                    StreamDeltaEvent(content=result.final_content or "")
                )
                await ctx.events.publish(StreamEndEvent(resuming=False))
        elif result.stop_reason == "error":
            logger.error(
                "LLM returned error: {}", (result.final_content or "")[:200]
            )

    def _save(self, ctx: _TurnContext) -> None:
        """Persist the turn's incremental history and clean up turn state."""
        if ctx.final_content is None or not ctx.final_content.strip():
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        ctx.save_skip = 1 + len(ctx.history) + (1 if ctx.user_persisted_early else 0)
        skip_msgs = ctx.all_messages[ctx.save_skip :]
        ctx.generated_media = generated_image_paths_from_messages(skip_msgs)
        message_tool = ctx.tools.get("message") if ctx.tools else None
        extra = (
            getattr(message_tool, "turn_delivered_media_paths", lambda: [])()
            if message_tool
            else []
        )
        merge_turn_media_into_last_assistant(
            ctx.all_messages, ctx.generated_media, extra
        )
        ctx.turn_latency_ms = max(
            0, int((time.time() - ctx.turn_wall_started_at) * 1000)
        )
        self._write_turn(
            ctx.session,
            ctx.all_messages,
            ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        ctx.session.enforce_file_cap()
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        self._sessions.save(ctx.session)

    async def _respond(self, ctx: _TurnContext) -> None:
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content or "",
            ctx.tools,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.generated_media,
            ctx.events.accepts(StreamDeltaEvent),
            channel=ctx.outbound_channel or ctx.msg.channel,
            chat_id=ctx.outbound_chat_id or ctx.msg.chat_id,
            turn_latency_ms=ctx.turn_latency_ms,
        )

    def _cleanup(self, ctx: _TurnContext) -> None:
        """Per-turn teardown; the coordinator owns queues/locks/queued messages."""
        if ctx.session is not None and self._background_scheduler is not None:
            self._background_scheduler(
                self._consolidator.maybe_consolidate_by_tokens(
                    ctx.session,
                    replay_max_messages=self._max_messages,
                )
            )

    # -- runner -------------------------------------------------------------

    async def _run_runner(self, ctx: _TurnContext) -> AgentRunResult:
        from nanoreview.agent.tools.permissions import resolve_policy

        permission_policy = resolve_policy(
            self._tools_config,
            ctx.session.metadata if ctx.session is not None else {},
        )

        async def _permission_request_cb(
            request_id: str,
            payload: dict[str, Any],
            future: asyncio.Future[bool],
        ) -> bool:
            if self._permission_requester is None:
                return False
            return await self._permission_requester(
                request_id,
                payload,
                future,
                ctx.outbound_channel or ctx.msg.channel,
                ctx.outbound_chat_id or ctx.msg.chat_id,
            )

        hook = build_agent_turn_hook(
            AgentTurnHookSpec(
                events=ctx.events,
                streaming=ctx.events.accepts(StreamDeltaEvent),
                channel=ctx.outbound_channel or ctx.msg.channel,
                chat_id=ctx.outbound_chat_id or ctx.msg.chat_id,
                message_id=ctx.msg.metadata.get("message_id"),
                metadata=ctx.msg.metadata,
                session_key=ctx.session_key,
                workspace=ctx.target_root,
                tool_hint_max_length=self._tool_hint_max_length,
                set_tool_context=lambda *a, **k: self._set_registry_context(
                    ctx.tools, *a, **k
                ),
                on_iteration=lambda iteration: setattr(
                    self, "_current_iteration", iteration
                ),
                # File-edit activity is a Conversation Agent concern: review,
                # planner and Judge paths never assemble this factory.
                registered_hook_factories=[create_file_edit_activity_hook],
                registered_hooks=(
                    list(self._hooks_getter())
                    if self._hooks_getter is not None
                    else self._extra_hooks
                ),
            )
        )

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if ctx.session is not None:
                self._set_runtime_checkpoint(ctx.session, payload)

        drain = self._make_pending_drain(ctx)

        file_state_token = bind_file_states(
            self._file_state_store.for_session(ctx.session_key)
        )
        try:
            return await self._runner.run(
                AgentRunSpec(
                    frozen_messages=ctx.frozen_messages,
                    working_messages=ctx.working_messages,
                    tools=ctx.tools,
                    model=self._model,
                    max_iterations=self._max_iterations,
                    max_tool_result_chars=self._max_tool_result_chars,
                    hook=hook,
                    error_message="Sorry, I encountered an error calling the AI model.",
                    concurrent_tools=True,
                    workspace=ctx.target_root,
                    session_key=ctx.session_key,
                    context_window_tokens=self._context_window_tokens,
                    context_block_limit=self._context_block_limit,
                    provider_retry_mode=self._provider_retry_mode,
                    retry_wait_callback=ctx.on_retry_wait,
                    checkpoint_callback=_checkpoint,
                    injection_callback=drain,
                    llm_timeout_s=None,
                    permission_policy=permission_policy,
                    permission_request_callback=_permission_request_cb,
                )
            )
        finally:
            reset_file_states(file_state_token)

    # -- pending injection --------------------------------------------------

    def _make_pending_drain(
        self, ctx: _TurnContext
    ) -> Callable[..., Awaitable[list[dict[str, Any]]]]:
        """Build the mid-turn injector for ordinary user messages.

        The drain only consumes queued *user* messages: the coordinator routes
        them here while this turn holds the session lock. Review subagent
        results never enter this queue (they are drained by ``ReviewLoop``), so
        no subagent waiting, deduping or filtering happens here.
        """
        pending_queue = ctx.pending_queue

        async def _drain_pending(
            *, limit: int = _MAX_INJECTIONS_PER_TURN
        ) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            if pending_queue is None:
                return items

            while len(items) < limit:
                try:
                    pending_msg = pending_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                message: dict[str, Any] = {
                    "role": "user",
                    "content": pending_msg.content,
                }
                if pending_msg.metadata:
                    message["_metadata"] = dict(pending_msg.metadata)
                items.append(message)

            return items

        return _drain_pending

    # -- context / tools ----------------------------------------------------

    def _session_history(self, session: "Session") -> list[dict[str, Any]]:
        history = session.get_history(
            max_messages=self._max_messages,
            max_tokens=self._replay_token_budget(),
            include_timestamps=True,
        )
        # Filter stale subagent results from prior reviews — the LLM would
        # otherwise try to continue old review work instead of starting fresh.
        return [
            m
            for m in history
            if m.get("_metadata", {}).get("injected_event") != "subagent_result"
        ]

    def _build_partitioned(
        self, ctx: _TurnContext
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return self._context.build_partitioned_messages(
            history=ctx.history,
            current_message=ctx.msg.content,
            media=ctx.msg.media if ctx.msg.media else None,
            channel=ctx.outbound_channel or ctx.msg.channel,
            chat_id=self._runtime_chat_id(ctx.msg),
            current_role="user",
            sender_id=ctx.msg.sender_id,
            session_metadata=ctx.session.metadata,
        )

    async def _build_turn_tools(self, ctx: _TurnContext) -> ToolRegistry:
        from nanoreview.agent.tools.context import ToolContext

        registry = ToolRegistry()
        tool_ctx = ToolContext(
            config=self._tools_config,
            workspace=str(ctx.target_root),
            provider=self._provider,
            model=self._model,
            review_config=self._review_config,
            bus=self._bus,
            sessions=self._sessions,
            file_state_store=self._file_state_store,
            provider_snapshot_loader=self._provider_snapshot_loader,
            timezone=self._context.timezone or "UTC",
        )
        registered = self._tool_loader.load(
            tool_ctx,
            registry,
            scope="core",
            denied_names=_CONVERSATION_DENIED_TOOLS,
        )
        mcp_count = await self._register_mcp_proxies(ctx, registry)
        logger.debug(
            "conversation.tools.registered session={} count={} tools={} mcp={}",
            ctx.session_key,
            len(registered),
            ",".join(registered),
            mcp_count,
        )
        return registry

    async def _register_mcp_proxies(
        self, ctx: _TurnContext, registry: ToolRegistry
    ) -> int:
        """Connect configured MCP servers, then register per-turn proxies.

        Connection preparation runs first so a turn only exposes capabilities
        that are actually reachable. Servers that are configured but not yet
        connected are retried on the next turn; a failure here never fails the
        conversation turn itself. An external cancellation propagates so
        ``/stop`` stays responsive.
        """
        provider = self._mcp_provider
        if provider is None or not provider.configured_server_names:
            return 0
        await provider.connect()
        count = provider.build_turn_proxies(registry)
        logger.debug(
            "conversation.mcp.registered session={} proxies={} connected={} status={}",
            ctx.session_key,
            count,
            sorted(provider.connected_server_names),
            provider.runtime_status(),
        )
        return count

    @staticmethod
    def _apply_handoff_directive(
        frozen_messages: list[dict[str, Any]], directive: str | None
    ) -> None:
        """Append the review handoff directive to the frozen system prompt.

        The frozen zone is copied verbatim into every request and is never
        summarized by run-level compression, so the review's provenance and its
        read-only constraint survive even if the replayed history is compacted.
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

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    def _set_tool_context(self, ctx: _TurnContext) -> None:
        self._set_registry_context(
            ctx.tools,
            ctx.outbound_channel or ctx.msg.channel,
            ctx.outbound_chat_id or ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )

    def _set_registry_context(
        self,
        registry: ToolRegistry | None,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        if registry is None:
            return
        effective_key = session_key or f"{channel}:{chat_id}"
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=dict(metadata or {}),
        )
        for name in registry.tool_names:
            tool = registry.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)
        set_current_request_context(request_ctx)

    # -- persistence helpers ------------------------------------------------

    def _persist_user_message_early(self, ctx: _TurnContext) -> bool:
        media_paths = [p for p in (ctx.msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(ctx.msg.content, str) and ctx.msg.content.strip()
        if not (has_text or media_paths):
            return False
        extra: dict[str, Any] = {"media": list(media_paths)} if media_paths else {}
        text = ctx.msg.content if isinstance(ctx.msg.content, str) else ""
        ctx.session.add_message("user", text, **extra)
        self._mark_pending_user_turn(ctx.session)
        self._sessions.save(ctx.session)
        return True

    def _write_turn(
        self,
        session: "Session",
        messages: list[dict[str, Any]],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into the session, truncating large results."""
        from datetime import datetime

        last_assistant_idx: int | None = None
        for message in messages[skip:]:
            entry = dict(message)
            entry.pop("_metadata", None)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison context
            if role == "tool":
                if (
                    isinstance(content, str)
                    and len(content) > self._max_tool_result_chars
                ):
                    entry["content"] = truncate_text_fn(
                        content, self._max_tool_result_chars
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

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                if isinstance(block, bytes):
                    text = "[binary content omit]"
                else:
                    text = str(block)
                if should_truncate_text or isinstance(block, bytes):
                    text = truncate_text_fn(text, self._max_tool_result_chars)
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
                if should_truncate_text and len(text) > self._max_tool_result_chars:
                    text = truncate_text_fn(text, self._max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        registry: ToolRegistry | None,
        stop_reason: str,
        had_injections: bool,
        generated_media: list[str],
        streamed: bool,
        *,
        channel: str,
        chat_id: str,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results.

        A ``None`` return means the ``message`` tool already delivered the
        reply (and no injected content needs a body); it never means failure.
        """
        message_tool = registry.get("message") if registry else None
        if (
            isinstance(message_tool, MessageTool)
            and message_tool._sent_in_turn
            and (not had_injections or stop_reason == "empty_final_response")
        ):
            return None

        preview = (
            final_content[:120] + "..." if len(final_content) > 120 else final_content
        )
        logger.info("Response to {}:{}: {}", channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        unstreamed_stop_reasons = {
            "error",
            "tool_error",
            "compression_failed",
            "compression_limit",
        }
        if streamed and stop_reason not in unstreamed_stop_reasons:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=final_content,
            media=generated_media,
            metadata=meta,
        )

    # -- runtime checkpoint -------------------------------------------------

    def _set_runtime_checkpoint(
        self, session: "Session", payload: dict[str, Any]
    ) -> None:
        session.metadata[_RUNTIME_CHECKPOINT_KEY] = payload
        self._sessions.save(session)

    def _mark_pending_user_turn(self, session: "Session") -> None:
        session.metadata[_PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: "Session") -> None:
        session.metadata.pop(_PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: "Session") -> None:
        session.metadata.pop(_RUNTIME_CHECKPOINT_KEY, None)

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

    def _restore_runtime_checkpoint(self, session: "Session") -> bool:
        """Materialize an unfinished turn into history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(_RUNTIME_CHECKPOINT_KEY)
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

    def _restore_pending_user_turn(self, session: "Session") -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(_PENDING_USER_TURN_KEY):
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

    # -- public checkpoint API (used by the coordinator) --------------------

    def restore_runtime_checkpoint(self, session: "Session") -> bool:
        """Recover a turn interrupted mid-run; returns True when it restored."""
        return self._restore_runtime_checkpoint(session)

    def clear_pending_user_turn(self, session: "Session") -> None:
        """Drop the pending-user-turn marker after recovery has consumed it."""
        self._clear_pending_user_turn(session)

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the window."""
        if self._context_window_tokens <= 0:
            return 0
        max_output = getattr(
            getattr(self._provider, "generation", None), "max_tokens", 4096
        )
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self._context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self._context_window_tokens // 2)


__all__ = [
    "MAX_PENDING_CONVERSATION_MESSAGES",
    "ConversationLoop",
]
