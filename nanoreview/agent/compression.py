"""Run-level context compression primitives for :class:`AgentRunner`.

This module owns everything that is *not* the runner's request loop:

- the compression thresholds derived from the resolved context window;
- the canonical JSON protocol used to validate a model-produced summary;
- the three-zone context model (frozen + optional synthetic summary + active);
- interactive-unit grouping so a tool round is never split;
- the run-local :class:`RunCompressionState`.

Nothing here touches a provider, a session, or a persisted message list: the
runner owns those. All state is created per ``AgentRunner.run()`` call and never
shared on the runner instance, so concurrent reviewer/judge runs cannot cancel
or overwrite each other's compression jobs.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

#: Compress asynchronously once the rebuilt request reaches this ratio of the
#: resolved context window.
SOFT_LIMIT_RATIO = 0.60
#: Compress synchronously (and stop the run if it still does not fit) at this
#: ratio.
SYNC_LIMIT_RATIO = 0.80

#: Tags wrapping the synthetic summary message content.
COMPRESSED_CONTEXT_OPEN = "<compressed_context>"
COMPRESSED_CONTEXT_CLOSE = "</compressed_context>"
#: Fixed preamble telling the model the block is history, not new evidence.
COMPRESSED_CONTEXT_PREAMBLE = (
    "This is a summary of prior history, not new source evidence."
)
#: Synthetic summaries are injected as a user message so every provider accepts
#: them at the same position in the alternation.
COMPRESSED_ROLE = "user"
#: Metadata key marking a synthetic summary so it can be replaced, never
#: accumulated.
COMPRESSED_METADATA_KEY = "_compressed_context"

#: Canonical (top-level) keys kept from a validated summary. Extra fields the
#: model invents are dropped before serialization.
_CANONICAL_KEYS = (
    "task_context",
    "confirmed_conclusions",
    "evidence",
    "findings",
    "pending_tasks",
    "constraints_and_availability",
)


class CompressionError(RuntimeError):
    """A compression request or its output failed validation.

    Raised by :func:`parse_and_validate` and by the runner's compression
    requests; the runner converts it into a retry, then into the
    ``compression_failed`` stop reason. When the Provider did return a
    response whose usage was already billed (error finish reason or empty
    content), ``visible_usage`` carries that usage so the runner can still
    account for it; it stays ``None`` for timeouts and raised exceptions,
    where no usage was exposed.
    """

    def __init__(self, message: str, *, visible_usage: dict[str, int] | None = None):
        super().__init__(message)
        self.visible_usage = visible_usage


#: JSON Schema for the summary object. Mirrors ``memory_compression.md`` and is
#: validated with the project's shared ``Schema.validate_json_schema_value``.
COMPRESSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": list(_CANONICAL_KEYS),
    "properties": {
        "task_context": {
            "type": "object",
            "required": ["task", "objective", "focus"],
            "properties": {
                "task": {"type": "string"},
                "objective": {"type": "string"},
                "focus": {"type": "string"},
            },
        },
        "confirmed_conclusions": {"type": "array", "items": {"type": "string"}},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["evidence_id", "path", "line_range", "summary"],
                "properties": {
                    "evidence_id": {"type": "string"},
                    "path": {"type": "string"},
                    "line_range": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "status",
                    "conclusion",
                    "evidence_ids",
                    "counterevidence",
                    "uncertainty",
                ],
                "properties": {
                    "status": {"type": "string"},
                    "conclusion": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "counterevidence": {"type": "array", "items": {"type": "string"}},
                    "uncertainty": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "pending_tasks": {"type": "array", "items": {"type": "string"}},
        "constraints_and_availability": {
            "type": "object",
            "required": ["constraints", "evidence_availability"],
            "properties": {
                "constraints": {"type": "array", "items": {"type": "string"}},
                "evidence_availability": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


def _limit(context_window_tokens: int | None, ratio: float) -> int | None:
    """Return ``floor(window * ratio)`` or ``None`` when the window is unusable."""
    if not isinstance(context_window_tokens, int) or context_window_tokens <= 0:
        return None
    return int(context_window_tokens * ratio)


def soft_limit(context_window_tokens: int | None) -> int | None:
    """Async-compression threshold, or ``None`` when the window is unusable."""
    return _limit(context_window_tokens, SOFT_LIMIT_RATIO)


def sync_limit(context_window_tokens: int | None) -> int | None:
    """Sync-compression threshold, or ``None`` when the window is unusable."""
    return _limit(context_window_tokens, SYNC_LIMIT_RATIO)


def is_compression_summary(message: dict[str, Any]) -> bool:
    """Whether *message* is a synthetic summary produced by compression."""
    meta = message.get("_metadata")
    if isinstance(meta, dict) and meta.get(COMPRESSED_METADATA_KEY):
        return True
    content = message.get("content")
    return (
        isinstance(content, str)
        and content.lstrip().startswith(COMPRESSED_CONTEXT_OPEN)
    )


def build_summary_message(canonical: str) -> dict[str, Any]:
    """Wrap canonical JSON in the synthetic user message the model sees."""
    content = (
        f"{COMPRESSED_CONTEXT_OPEN}\n"
        f"{COMPRESSED_CONTEXT_PREAMBLE}\n"
        f"{canonical}\n"
        f"{COMPRESSED_CONTEXT_CLOSE}"
    )
    return {
        "role": COMPRESSED_ROLE,
        "content": content,
        "_metadata": {COMPRESSED_METADATA_KEY: True},
    }


def _has_content(payload: dict[str, Any]) -> bool:
    """Whether any canonical field carries non-empty content."""

    def _nonempty(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(_nonempty(item) for item in value)
        if isinstance(value, dict):
            return any(_nonempty(item) for item in value.values())
        return value is not None

    return any(_nonempty(payload.get(key)) for key in _CANONICAL_KEYS)


def parse_and_validate(raw_content: str) -> dict[str, Any]:
    """Parse *raw_content* as the summary JSON and validate it.

    Raises :class:`CompressionError` on illegal JSON, missing fields, wrong
    types, or a completely empty summary. Extra fields are dropped; only
    canonical fields are returned.
    """
    from nanoreview.agent.tools.base import Schema

    text = (raw_content or "").strip()
    if not text:
        raise CompressionError("compression returned empty content")
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise CompressionError(f"compression returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CompressionError(
            f"compression summary must be a JSON object, got {type(payload).__name__}"
        )
    errors = Schema.validate_json_schema_value(payload, COMPRESSION_SCHEMA, "")
    if errors:
        raise CompressionError("compression summary invalid: " + "; ".join(errors[:5]))
    canonical = {key: payload[key] for key in _CANONICAL_KEYS}
    if not _has_content(canonical):
        raise CompressionError("compression summary is empty")
    return canonical


def canonical_json(payload: dict[str, Any]) -> str:
    """Serialize a validated summary deterministically for the model context."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def has_usable_content(payload: dict[str, Any]) -> bool:
    """Whether a summary object carries anything worth keeping.

    Public counterpart of the internal emptiness check, used by tests and by
    callers that validate a summary without re-parsing it.
    """
    return _has_content(payload)


def _message_id(message: dict[str, Any], index: int) -> str:
    """Best-effort stable identifier for a message in the serialized transcript."""
    meta = message.get("_metadata")
    if isinstance(meta, dict):
        for key in ("message_id", "id", "tool_call_id"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("tool_call_id",):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    calls = _tool_call_ids(message)
    if calls:
        return calls[0]
    return f"m{index}"


def serialize_transcript(messages: list[dict[str, Any]]) -> str:
    """Serialize messages to a delimited text blob for the compression request.

    The blob is inserted into a single user message so the original working
    messages are never replayed as a compression-session role history.
    """
    blocks: list[str] = []
    for index, message in enumerate(messages):
        role = str(message.get("role") or "unknown")
        mid = _message_id(message, index)
        content = message.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        parts = [
            f"<message index=\"{index}\" id=\"{mid}\" role=\"{role}\">",
            content,
        ]
        calls = message.get("tool_calls")
        if calls:
            parts.append("tool_calls: " + json.dumps(calls, ensure_ascii=False))
        if message.get("tool_call_id"):
            parts.append(f"tool_call_id: {message['tool_call_id']}")
        if message.get("name"):
            parts.append(f"name: {message['name']}")
        parts.append("</message>")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def _tool_call_ids(message: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            call_id = call.get("id")
            if call_id:
                ids.append(str(call_id))
    return ids


def split_units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group messages into indivisible interactive units.

    Deterministic rules (shared by compression partitioning and the runner's
    hard trim, so the two never disagree about where a replayed round ends):

    - a ``user``/injection message opens a unit and is grouped with the one
      assistant response that directly follows it;
    - an assistant tool-call message and all of its matching tool results are
      one inseparable unit; when it is the direct response to a user message,
      that user message joins the unit;
    - every later assistant round that starts without a new user prompt (the
      normal tool-driven shape of coordinator/reviewer/judge runs that begin
      with an empty working zone) is its own unit, so older rounds can enter
      the compress zone while the newest stays active;
    - a plain assistant response may also form its own unit;
    - a trailing unanswered user message, or an open round still missing tool
      results, is kept whole as the latest (active) unit.

    A unit boundary is therefore: every ``user`` message, and every
    ``assistant`` message that does not directly follow a ``user`` message.
    """
    units: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_role: str | None = None
    for message in messages:
        role = message.get("role")
        if role == "user" or (role == "assistant" and previous_role != "user"):
            if current:
                units.append(current)
            current = [message]
        else:
            current.append(message)
        previous_role = role
    if current:
        units.append(current)
    return units


@dataclass(slots=True)
class PartitionedContext:
    """Result of partitioning working messages into compress/active zones."""

    #: Older whole-unit prefix handed to the summarizer (may be empty).
    compress_prefix: list[dict[str, Any]] = field(default_factory=list)
    #: Newest complete units kept verbatim for the model.
    active_prefix: list[dict[str, Any]] = field(default_factory=list)
    #: Whether ``compress_prefix`` is a legal, non-empty unit prefix.
    compress: bool = False


def partition_working(
    working: list[dict[str, Any]],
    *,
    keep_budget_tokens: int,
    estimate: Callable[[dict[str, Any]], int],
) -> PartitionedContext:
    """Split *working* into a compress prefix and an active verbatim suffix.

    Whole interactive units are taken newest-first until the accumulated
    original-text tokens would exceed ``keep_budget_tokens``. The newest unit is
    always kept whole even when it alone exceeds the budget. The compress zone is
    therefore always a complete unit prefix that ends exactly where the active
    zone begins, so rebuilding ``[summary, *active_prefix]`` can never drop or
    duplicate a message.
    """
    units = split_units(working)
    if not units:
        return PartitionedContext()
    kept_units: list[list[dict[str, Any]]] = []
    kept_tokens = 0
    for unit in reversed(units):
        unit_tokens = sum(estimate(message) for message in unit)
        if kept_units and kept_tokens + unit_tokens > keep_budget_tokens:
            break
        kept_units.append(unit)
        kept_tokens += unit_tokens
    kept_units.reverse()
    compress_units = units[: len(units) - len(kept_units)]
    compress_prefix = [message for unit in compress_units for message in unit]
    active_prefix = [message for unit in kept_units for message in unit]
    return PartitionedContext(
        compress_prefix=compress_prefix,
        active_prefix=active_prefix,
        # A compress zone needs at least one complete unit before the active zone.
        compress=bool(compress_prefix),
    )


@dataclass(slots=True)
class AsyncSnapshot:
    """Captured prefixes for an in-flight async compression.

    The job is only applied when the frozen prefix and the working prefix it
    snapshotted are still exact prefixes of the current state. The rebuild after
    a successful job is ``summary + snapshot.active_prefix + suffix``: the active
    zone of the snapshot is replayed verbatim (it was never summarized), and the
    suffix is everything appended to ``state.working`` after the snapshot.
    """

    frozen_prefix: list[dict[str, Any]]
    #: Full working prefix at snapshot time (compress zone + active zone).
    working_prefix: list[dict[str, Any]]
    #: Region actually handed to the summarizer.
    compress_prefix: list[dict[str, Any]] = field(default_factory=list)
    #: Active units the summary must be prepended to when the job is applied.
    active_prefix: list[dict[str, Any]] = field(default_factory=list)
    #: ``working_revision`` captured when the snapshot was taken. After the
    #: job fails, a retry is only permitted once the revision moved on (a
    #: complete round or an injection was appended after the snapshot).
    working_revision: int = 0
    #: Full-request token count observed when the job started (logging only).
    before_tokens: int = 0

    @property
    def frozen_len(self) -> int:
        return len(self.frozen_prefix)

    @property
    def working_len(self) -> int:
        return len(self.working_prefix)


@dataclass(slots=True)
class RunCompressionState:
    """Per-``run()`` compression state. Never stored on the runner instance."""

    frozen: list[dict[str, Any]]
    working: list[dict[str, Any]]
    context_window_tokens: int | None
    #: Tokens actually consumed by compression requests, merged into run usage.
    usage: dict[str, int] = field(default_factory=dict)
    #: In-flight async compression task, if any.
    pending: Any | None = None
    #: Snapshot governing an in-flight async task.
    snapshot: AsyncSnapshot | None = None
    #: Whether the most recent async attempt failed.
    async_failed: bool = False
    #: ``working_revision`` at the moment the failed attempt started. A retry
    #: is allowed only after the revision moves on, so a failure with no new
    #: messages never loops, while messages appended while the job was still
    #: in flight (earlier than the failure result was collected) do count.
    async_failed_revision: int | None = None
    #: Terminal stop reason set by compression (or ``None``).
    stopped_reason: str | None = None
    #: Bounded failure reason accompanying ``stopped_reason``.
    stopped_error: str | None = None
    #: Guard so run cleanup runs exactly once.
    closed: bool = False
    #: Guard so ``state.usage`` is merged into run usage exactly once, even
    #: though the run settles accounting both before the run-level result hook
    #: and again in ``finally`` as an exit-path safety net.
    usage_banked: bool = False
    #: Monotonic counter bumped on every append or rewrite of the working
    #: zone (assistant/tool/injection appends and summary rebuilds). It is the
    #: single retry signal: same revision means nothing new to compress.
    working_revision: int = 0
    #: Private per-run trace id included in every compression log line, so
    #: concurrent reviewer/judge runs can be told apart in the log stream.
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    #: Set when the runner has stopped for compression; the dispatch loop then
    #: breaks instead of issuing the next business request.
    stopped: bool = False

    @property
    def soft_limit(self) -> int | None:
        return soft_limit(self.context_window_tokens)

    @property
    def sync_limit(self) -> int | None:
        return sync_limit(self.context_window_tokens)

    def snapshot_context(self) -> Any | None:
        """Context tuple for logging the current snapshot, if any."""
        if self.snapshot is None:
            return None
        return (
            self.snapshot.frozen_len,
            self.snapshot.working_len,
            len(self.snapshot.compress_prefix),
            len(self.snapshot.active_prefix),
        )
