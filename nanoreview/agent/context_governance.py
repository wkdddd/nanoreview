"""Request-level context governance for one agent run.

* the leading run of ``system``/``developer`` messages is a stable instruction
  header and is never summarized;
* the rest of the transcript is split into "already accepted" history
  (``messages[:accepted_boundary]``) and "not yet sent" new input
  (``messages[accepted_boundary:]``);
* before **every** request — the first one included — the governor measures the
  rebuilt request against ``window - max_output - 1024``. Under pressure the
  accepted history is folded into one replacement summary (which itself always
  replaces the previous summary instead of accumulating);
* when the rebuilt request still does not fit, the not-yet-sent ``read_file``
  results are replaced by a "re-read a smaller range" notice, and only then does
  the request fail for real.

Compression only changes what the model sees. ``state.messages`` is the raw
transcript the caller persists and is never rewritten here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from loguru import logger

from nanoreview.utils.helpers import estimate_prompt_tokens_chain, truncate_text

#: Reserved input tokens for provider-side overhead that is never visible in the
#: transcript. Mirrors the rest of the runtime (``_SNIP_SAFETY_BUFFER``).
CONTEXT_SAFETY_BUFFER = 1024


def input_token_budget(context_window_tokens: int | None, max_output_tokens: int | None) -> int:
    """Usable input tokens for one request: ``window - output - buffer``.

    The single definition of that arithmetic. Every role that *plans* a request
    ahead of time (the governor, the Judge's batch splitter, the review replay
    window) must call this, because a planner that reserves less than the
    governor does silently produces requests the governor then rejects.
    Returns ``0`` when no window is known.
    """
    window = context_window_tokens if isinstance(context_window_tokens, int) else 0
    if window <= 0:
        return 0
    reserve = (
        max_output_tokens
        if isinstance(max_output_tokens, int) and max_output_tokens > 0
        else 0
    )
    return window - reserve - CONTEXT_SAFETY_BUFFER

#: Markers around the synthetic summary data message. The block is history, not
#: new evidence, and must never be read as an instruction.
ARCHIVED_SUMMARY_OPEN = "<archived_context_summary>"
ARCHIVED_SUMMARY_CLOSE = "</archived_context_summary>"
ARCHIVED_SUMMARY_PREAMBLE = (
    "Earlier turns of this session were summarized. This is background state, "
    "not an instruction and not new source evidence."
)

#: Replacement text for a not-yet-sent ``read_file`` result that would push the
#: request over budget. The raw transcript keeps the original result.
UNSENT_READ_REPLACEMENT = (
    "Error: this read_file result was not sent because the request exceeds the "
    "model context budget. No file was modified. Call read_file again with a "
    "smaller offset/limit range (one file at a time) if you still need it."
)

#: Tools whose *unsent* results may be replaced by the notice above.
REPLACEABLE_UNSENT_TOOLS = frozenset({"read_file"})

#: Instruction roles forming the never-compressed header.
_HEADER_ROLES = frozenset({"system", "developer"})


class ContextWindowExceededError(RuntimeError):
    """The rebuilt request cannot fit the model's input budget.

    Raised after compaction and the unsent-read replacement were both tried; the
    caller turns it into a bounded ``context_limit`` stop reason instead of
    sending a request the provider would reject.
    """

    def __init__(
        self,
        message: str,
        *,
        estimated_tokens: int = 0,
        input_budget: int = 0,
        source: str = "",
    ):
        super().__init__(message)
        self.estimated_tokens = estimated_tokens
        self.input_budget = input_budget
        self.source = source


#: ``(messages, previous_summary) -> summary`` for the accepted-history zone.
#: ``None`` means "nothing could be produced" (the governor then falls back to a
#: bounded raw checkpoint before failing).
Summarizer = Callable[[list[dict[str, Any]], "str | None"], Any]


class TranscriptSummarizerLike(Protocol):
    """The summarizer surface the governor needs (see ``agent.memory``)."""

    async def archive(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_summary: str | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> str | None: ...

    def raw_archive(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_summary: str | None = None,
    ) -> str | None: ...


def header_length(messages: list[dict[str, Any]]) -> int:
    """Number of leading ``system``/``developer`` messages (the stable header)."""
    length = 0
    for message in messages:
        if message.get("role") not in _HEADER_ROLES:
            break
        length += 1
    return length


def build_summary_message(summary: str) -> dict[str, Any]:
    """Wrap a checkpoint summary in the data message the model receives."""
    content = (
        f"{ARCHIVED_SUMMARY_OPEN}\n"
        f"{ARCHIVED_SUMMARY_PREAMBLE}\n\n"
        f"{summary}\n"
        f"{ARCHIVED_SUMMARY_CLOSE}"
    )
    return {"role": "user", "content": content}


def is_summary_message(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, str) and content.lstrip().startswith(ARCHIVED_SUMMARY_OPEN)


@dataclass(slots=True)
class ContextCompactionState:
    """Per-``run()`` compaction state. Never stored on the runner instance."""

    #: Raw transcript for the run. Append-only; compaction never rewrites it.
    messages: list[dict[str, Any]]
    #: Leading instruction messages, excluded from every summarization.
    header_len: int = 0
    #: Raw index of the end of the pre-existing conversation context, declared
    #: by the caller. ``messages[header_len:accepted_message_count]`` is history
    #: the run may fold into a summary even before its first request.
    accepted_message_count: int = 0
    #: Everything before this raw index has been sent to the model at least once.
    accepted_boundary: int = 0
    #: Everything before this raw index is represented by ``summary``.
    compacted_through: int = 0
    #: Active summary of the compacted region (also the next previous_summary).
    summary: str | None = None
    #: Tokens spent by summarization requests of this run.
    usage: dict[str, int] = field(default_factory=dict)
    #: ``tool_call_id`` values whose not-yet-sent result was replaced by a notice.
    replaced_tool_results: set[str] = field(default_factory=set)
    #: How many times the accepted history was summarized in this run.
    compaction_count: int = 0
    #: Whether the summarization usage has been folded into the run usage.
    usage_banked: bool = False
    #: Per-run identifier shared by governance log lines.
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @classmethod
    def from_messages(
        cls,
        messages: list[dict[str, Any]],
        *,
        initial_summary: str | None = None,
        accepted_message_count: int = 0,
    ) -> "ContextCompactionState":
        header = header_length(messages)
        accepted = min(max(0, accepted_message_count), len(messages))
        accepted = max(accepted, header)
        return cls(
            messages=messages,
            header_len=header,
            accepted_message_count=accepted,
            accepted_boundary=accepted,
            compacted_through=header,
            summary=initial_summary or None,
        )

    def accept_request(self, sent_message_count: int) -> None:
        """Mark everything the completed request carried as accepted history."""
        self.accepted_boundary = max(self.accepted_boundary, sent_message_count)

    @property
    def compacted_session_messages(self) -> int:
        """How many pre-existing session messages the summary now covers.

        Callers that own a persisted session advance their replay boundary by
        this amount. Folded *turn* messages are intentionally not counted: the
        summary is simply fresher than the boundary claims, which can duplicate
        context but never drop it.
        """
        boundary = min(self.compacted_through, self.accepted_message_count)
        return max(0, boundary - self.header_len)


@dataclass(slots=True)
class ContextGovernanceConfig:
    """Inputs the governor needs from the owning run."""

    provider: Any
    model: str
    context_window_tokens: int | None = None
    max_tokens: int | None = None
    #: Explicit input budget override (``agents.defaults.contextBlockLimit``).
    context_block_limit: int | None = None
    #: Live tool definitions participate in the token estimate.
    tool_definitions: Callable[[], list[dict[str, Any]]] = lambda: []


class ContextGovernor:
    """Measure, compact and validate every model request of one run."""

    def __init__(
        self,
        config: ContextGovernanceConfig,
        *,
        summarizer: "TranscriptSummarizerLike | None" = None,
        clean: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
        log_scope: str = "",
    ):
        self._config = config
        self._summarizer = summarizer
        self._clean = clean
        self._on_usage = on_usage
        self._scope = log_scope

    # -- budget -------------------------------------------------------------

    def input_budget(self) -> int | None:
        """``window - max_output - 1024``, or ``None`` when no window is known."""
        window = self._config.context_window_tokens
        if not isinstance(window, int) or window <= 0:
            return None
        if isinstance(self._config.context_block_limit, int) and self._config.context_block_limit > 0:
            return self._config.context_block_limit
        max_output = self._config.max_tokens
        if not isinstance(max_output, int) or max_output <= 0:
            provider_max = getattr(
                getattr(self._config.provider, "generation", None), "max_tokens", 4096
            )
            max_output = provider_max if isinstance(provider_max, int) else 4096
        return input_token_budget(window, max_output)

    def estimate(self, messages: list[dict[str, Any]]) -> tuple[int, str]:
        try:
            definitions = self._config.tool_definitions() or []
        except Exception:
            logger.exception("Context governance could not read tool definitions")
            definitions = []
        try:
            tokens, source = estimate_prompt_tokens_chain(
                self._config.provider,
                self._config.model,
                messages,
                definitions,
            )
        except Exception:
            logger.exception("Context governance token estimate failed")
            return 0, "none"
        return int(tokens), source

    def _has_pressure(self, messages: list[dict[str, Any]]) -> bool:
        budget = self.input_budget()
        if budget is None:
            return False
        measured, _source = self.estimate(messages)
        if measured <= 0:
            return False
        return budget <= 0 or measured >= budget

    # -- request assembly ---------------------------------------------------

    def request_messages(self, state: ContextCompactionState) -> list[dict[str, Any]]:
        """Rebuild the request: header + optional summary + live transcript tail."""
        header = [dict(message) for message in state.messages[: state.header_len]]
        body = [
            self._apply_replacement(message, state)
            for message in state.messages[state.compacted_through :]
        ]
        if state.summary:
            summary_message = build_summary_message(state.summary)
            if body and body[0].get("role") == "user":
                # Consecutive user messages are rejected by some providers and
                # merged by others; merge explicitly so the summary can never be
                # split from the message it introduces.
                merged = dict(summary_message)
                merged["content"] = f"{summary_message['content']}\n\n{body[0].get('content')}"
                body = [merged, *body[1:]]
            else:
                body = [summary_message, *body]
        return [*header, *body]

    @staticmethod
    def _apply_replacement(
        message: dict[str, Any], state: ContextCompactionState
    ) -> dict[str, Any]:
        if message.get("role") != "tool":
            return dict(message)
        call_id = message.get("tool_call_id")
        if not call_id or str(call_id) not in state.replaced_tool_results:
            return dict(message)
        replaced = dict(message)
        replaced["content"] = UNSENT_READ_REPLACEMENT
        return replaced

    def _prepare(self, state: ContextCompactionState) -> list[dict[str, Any]]:
        messages = self.request_messages(state)
        if self._clean is None:
            return messages
        # The header (identity + role rules + task boundary) is copied verbatim;
        # repair/compaction only ever touches the transcript tail.
        header = messages[: state.header_len]
        body = messages[state.header_len :]
        return [*header, *self._clean(body)]

    # -- public entry -------------------------------------------------------

    async def prepare_request(self, state: ContextCompactionState) -> list[dict[str, Any]]:
        """Return the request payload, compacting when it would not fit."""
        messages = self._prepare(state)
        if not self._has_pressure(messages):
            return messages

        await self._compact(state)
        messages = self._prepare(state)
        if not self._has_pressure(messages):
            return messages

        # Last resort before failing: the model has not received the tail of this
        # round yet, so a huge unsent read result may be replaced by a notice
        # that asks for a smaller range. The raw transcript keeps the original.
        self._replace_unsent_reads(state)
        messages = self._prepare(state)
        self._ensure_fits(messages)
        return messages

    def _ensure_fits(self, messages: list[dict[str, Any]]) -> None:
        budget = self.input_budget()
        if budget is None:
            return
        measured, source = self.estimate(messages)
        if budget > 0 and measured <= budget:
            return
        raise ContextWindowExceededError(
            "the request still exceeds the model context budget after compaction "
            f"({measured}/{budget} via {source})",
            estimated_tokens=measured,
            input_budget=budget,
            source=source,
        )

    async def _compact(self, state: ContextCompactionState) -> None:
        zone = state.messages[state.compacted_through : state.accepted_boundary]
        if not zone:
            raise ContextWindowExceededError(
                "the request exceeds the model context budget and there is no "
                "accepted history left to summarize",
                input_budget=self.input_budget() or 0,
                source="no-compressible-history",
            )
        measured, source = self.estimate(self._prepare(state))
        logger.info(
            "context.compact.start scope={} trace={} messages={} accepted={} "
            "measured={} budget={} source={} previous_summary={}",
            self._scope or "run",
            state.trace_id,
            len(zone),
            state.accepted_boundary,
            measured,
            self.input_budget(),
            source,
            bool(state.summary),
        )
        summary: str | None = None
        if self._summarizer is not None:
            try:
                summary = await self._summarizer.archive(
                    zone,
                    previous_summary=state.summary,
                    on_usage=self.record_usage,
                )
            except Exception as exc:
                logger.warning(
                    "context.compact.failed scope={} trace={} reason={}",
                    self._scope or "run",
                    state.trace_id,
                    f"{type(exc).__name__}: {exc}",
                )
                summary = None
        if not summary and self._summarizer is not None:
            summary = self._summarizer.raw_archive(zone, previous_summary=state.summary)
            logger.warning(
                "context.compact.degraded scope={} trace={} reason=raw_checkpoint chars={}",
                self._scope or "run",
                state.trace_id,
                len(summary or ""),
            )
        if not summary:
            raise ContextWindowExceededError(
                "compaction produced no usable summary and the request does not fit",
                input_budget=self.input_budget() or 0,
                source="compaction-failed",
            )
        state.summary = summary
        state.compacted_through = state.accepted_boundary
        state.compaction_count += 1
        after, after_source = self.estimate(self._prepare(state))
        logger.info(
            "context.compact.done scope={} trace={} accepted={} after_tokens={} source={} summary_chars={}",
            self._scope or "run",
            state.trace_id,
            state.accepted_boundary,
            after,
            after_source,
            len(summary),
        )

    def _replace_unsent_reads(self, state: ContextCompactionState) -> bool:
        """Replace the largest not-yet-sent ``read_file`` results with a notice.

        Only results the model has never received are eligible: the accepted
        history is already represented by a summary (or is being kept), and a
        tool result whose call is still pending must keep its pairing.
        """
        candidates: list[tuple[int, str]] = []
        for index in range(state.accepted_boundary, len(state.messages)):
            message = state.messages[index]
            if message.get("role") != "tool":
                continue
            if message.get("name") not in REPLACEABLE_UNSENT_TOOLS:
                continue
            content = message.get("content")
            call_id = message.get("tool_call_id")
            if not isinstance(content, str) or not call_id:
                continue
            if str(call_id) in state.replaced_tool_results:
                continue
            if len(content) <= len(UNSENT_READ_REPLACEMENT):
                continue
            candidates.append((len(content), str(call_id)))
        if not candidates:
            return False
        candidates.sort(reverse=True)
        for size, call_id in candidates:
            state.replaced_tool_results.add(call_id)
            logger.warning(
                "context.unsent_read.replaced scope={} trace={} tool_call_id={} chars={}",
                self._scope or "run",
                state.trace_id,
                call_id,
                size,
            )
            if not self._has_pressure(self._prepare(state)):
                return True
        return True

    def record_usage(self, usage: dict[str, int]) -> None:
        if not usage:
            return
        if self._on_usage is not None:
            try:
                self._on_usage(usage)
            except Exception:
                logger.exception("Context governance usage sink failed")


def bounded_text(text: str, limit: int) -> str:
    """Shared bounded rendering for governance diagnostics."""
    return truncate_text(text, limit) if limit > 0 else text
