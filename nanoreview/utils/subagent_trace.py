"""Durable, session-scoped execution traces for review subagents.

Sidecar filenames are derived from ``sha256(session_key)`` to eliminate
cross-session collisions caused by character-sanitisation in
``safe_filename``.  Writes are batched through a background thread to avoid
per-event ``fsync`` overhead on the Agent / WebSocket event loop.

Only card-recovery-essential events are persisted:

* ``started`` — id, label, timestamp
* ``reasoning_delta`` — sanitised reasoning text (length-limited)
* ``tool`` — tool name and final status
* ``finished`` — terminal status

Task descriptions, content deltas, and final results are intentionally
omitted — they are already persisted by the main session / report pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.config.paths import get_webui_dir
from nanoreview.utils.log_sanitization import sanitize_persisted_log_text

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

_MAX_REASONING_CHARS = 4_000  # per subagent
_MAX_SIDECAR_BYTES = 256 * 1024  # 256 KiB per session sidecar
_FLUSH_INTERVAL = 0.25  # seconds
_BATCH_SIZE = 32
_FLUSH_TIMEOUT = 5.0  # seconds
_QUEUE_MAX = 512  # max items in queue; excess events are dropped

# ---------------------------------------------------------------------------
def sanitize_trace_text(text: str) -> str:
    """Sanitize text for trace persistence.

    Cleans DSML / model control markers and masks common credentials
    (Bearer tokens, OpenAI / GitHub / Slack tokens, private-key blocks,
    URL passwords).
    """
    return sanitize_persisted_log_text(text)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def subagent_trace_path(session_key: str) -> Path:
    """Return the sidecar trace path using ``sha256(session_key)``.

    Using a hash eliminates cross-session filename collisions that arose
    when ``safe_filename`` replaced characters like ``:`` and ``/`` with
    underscores — two different session keys could collapse to the same stem.
    """
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
    return get_webui_dir() / f"{digest}.subagents.jsonl"


# ---------------------------------------------------------------------------
# Batch writer (thread-based, non-blocking for the event loop)
# ---------------------------------------------------------------------------


class _TraceWriter:
    """Thread-based batch writer for one session's subagent trace sidecar.

    Events are sanitised and length-checked in :meth:`append` (called from
    the asyncio event loop thread), then queued for the background writer
    thread which batches writes (max 32 records or 250 ms) with a single
    ``fsync`` per batch.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=_QUEUE_MAX
        )
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop_requested = False
        self._flush_event = threading.Event()
        self._lock = threading.Lock()
        # Per-subagent reasoning char counter
        self._reasoning_chars: dict[str, int] = defaultdict(int)
        # Total bytes written so far (approximate, UTF-8 encoded size)
        self._total_bytes = 0
        # Subagent IDs whose reasoning has hit the char limit
        self._text_truncated: set[str] = set()
        # Whether the sidecar has hit the byte limit
        self._sidecar_full = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def append(self, event: dict[str, Any]) -> None:
        """Queue a trace event after sanitisation and limit checks.

        Non-blocking in the common case.  If the queue is saturated the
        call blocks briefly (50 ms) to let the writer thread drain, which
        preserves write ordering.  If the queue remains full the event is
        dropped with a warning.
        """
        if not self._running:
            return
        sanitized = self._sanitize_event(event)
        if sanitized is None:
            return
        try:
            self._queue.put_nowait(sanitized)
        except queue.Full:
            # Queue saturated: block briefly so the writer thread can drain
            # and preserve ordering.  All writes go through the writer
            # thread — never write directly from the caller.
            try:
                self._queue.put(sanitized, timeout=0.05)
            except queue.Full:
                logger.warning("Subagent trace queue full, dropping event")

    def flush(self, timeout: float = _FLUSH_TIMEOUT) -> bool:
        """Wait for all queued events to be written to disk."""
        if not self._running or self._thread is None:
            return True
        self._flush_event.clear()
        try:
            self._queue.put_nowait({"_flush": True})
        except queue.Full:
            # Writer is busy; block briefly to enqueue the flush marker.
            try:
                self._queue.put({"_flush": True}, timeout=0.1)
            except queue.Full:
                return False
        return self._flush_event.wait(timeout=timeout)

    def close(self, timeout: float = _FLUSH_TIMEOUT) -> None:
        """Stop the writer after flushing pending events.

        Sets the stop flag, then enqueues a sentinel (``None``) to wake
        the writer immediately.  Even if the sentinel is dropped (queue
        full for ``timeout`` seconds), the writer's finite poll interval
        in :meth:`_run` guarantees it observes ``_stop_requested`` and
        exits after flushing its buffer.
        """
        if not self._running:
            return
        self._running = False
        self._stop_requested = True
        # Enqueue the sentinel, blocking briefly if the queue is full so
        # the writer observes it and exits cleanly.  If the sentinel is
        # dropped the writer still exits via its finite poll interval.
        try:
            self._queue.put(None, timeout=timeout)  # sentinel
        except queue.Full:
            logger.debug(
                "Subagent trace close: sentinel dropped (queue full); "
                "writer will exit via poll timeout"
            )
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Subagent trace writer did not stop within timeout")

    # -- internal: sanitisation & limits -----------------------------------

    def _sanitize_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Apply text sanitisation and per-session / per-subagent limits.

        Returns ``None`` if the event should be dropped (unknown type,
        limits exceeded for text events).
        """
        event_type = event.get("event")
        subagent_id = str(event.get("subagent_id", ""))

        with self._lock:
            # Sidecar byte limit: drop events that grow the file without
            # being essential for card recovery.  ``reasoning_delta`` and
            # ``tool`` events are the primary growth drivers; ``started``
            # and ``finished`` are small and essential for card state.
            if self._sidecar_full and event_type in ("reasoning_delta", "tool"):
                return None

            if event_type == "reasoning_delta":
                if subagent_id in self._text_truncated:
                    return None
                text = event.get("text", "")
                if not isinstance(text, str):
                    return None
                remaining = _MAX_REASONING_CHARS - self._reasoning_chars[subagent_id]
                if remaining <= 0:
                    self._text_truncated.add(subagent_id)
                    logger.info(
                        "Subagent {} reasoning trace truncated at {} chars",
                        subagent_id,
                        _MAX_REASONING_CHARS,
                    )
                    return None
                if len(text) > remaining:
                    text = text[:remaining]
                    self._text_truncated.add(subagent_id)
                    logger.info(
                        "Subagent {} reasoning trace truncated at {} chars",
                        subagent_id,
                        _MAX_REASONING_CHARS,
                    )
                self._reasoning_chars[subagent_id] += len(text)
                return {
                    "event": "reasoning_delta",
                    "subagent_id": subagent_id,
                    "text": sanitize_trace_text(text),
                }

            if event_type == "started":
                return {
                    "event": "started",
                    "subagent_id": subagent_id,
                    "label": str(event.get("label") or subagent_id),
                }

            if event_type == "finished":
                return {
                    "event": "finished",
                    "subagent_id": subagent_id,
                    "status": str(event.get("status", "error")),
                }

            if event_type == "tool":
                return {
                    "event": "tool",
                    "subagent_id": subagent_id,
                    "name": str(event.get("name", "")),
                    "status": str(event.get("status", "")),
                }

            # Drop unknown event types (reasoning_end, content_delta, etc.)
            return None

    # -- internal: writer thread -------------------------------------------

    def _run(self) -> None:
        buffer: list[str] = []
        while True:
            try:
                # Always use a finite timeout so that _stop_requested is
                # observed even when the buffer is empty.  Without this,
                # a lost close sentinel (queue full during close()) would
                # cause the writer to block forever on queue.get(None).
                event = self._queue.get(timeout=_FLUSH_INTERVAL)
            except queue.Empty:
                # Flush interval elapsed or stop requested.
                if buffer:
                    self._write_batch(buffer)
                    buffer.clear()
                if self._stop_requested:
                    return
                continue

            if event is None:
                # Close sentinel — flush remaining and exit
                if buffer:
                    self._write_batch(buffer)
                return

            if event.get("_flush"):
                if buffer:
                    self._write_batch(buffer)
                    buffer.clear()
                self._flush_event.set()
                continue

            raw = self._serialize(event)
            buffer.append(raw)

            with self._lock:
                self._total_bytes += len(raw.encode("utf-8")) + 1  # +1 newline
                if self._total_bytes >= _MAX_SIDECAR_BYTES:
                    self._sidecar_full = True

            if len(buffer) >= _BATCH_SIZE:
                self._write_batch(buffer)
                buffer.clear()

    def _serialize(self, event: dict[str, Any]) -> str:
        payload = {"createdAt": int(time.time() * 1000), **event}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _write_batch(self, lines: list[str]) -> None:
        """Write pre-serialised JSONL lines to the sidecar file."""
        if not lines:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            logger.warning(
                "Failed to write subagent trace batch {}: {}", self._path, exc
            )


def _merge_reasoning_deltas(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive ``reasoning_delta`` events for the same subagent.

    This is a **pure utility function** — it is not wired into the writer's
    queue-full path.  When the trace queue is saturated, events are dropped
    (not merged).  This helper is kept for potential future use (e.g. a
    compaction pass on read-back) and as a unit-test target.
    """
    merged: list[dict[str, Any]] = []
    for event in events:
        if (
            event.get("event") == "reasoning_delta"
            and merged
            and merged[-1].get("event") == "reasoning_delta"
            and merged[-1].get("subagent_id") == event.get("subagent_id")
        ):
            merged[-1]["text"] += event.get("text", "")
        else:
            merged.append(dict(event))
    return merged


# ---------------------------------------------------------------------------
# Writer registry
# ---------------------------------------------------------------------------

_WRITERS: dict[str, _TraceWriter] = {}
_WRITERS_LOCK = threading.Lock()


def _get_writer(session_key: str) -> _TraceWriter:
    """Get or create a batch writer for the given session."""
    with _WRITERS_LOCK:
        writer = _WRITERS.get(session_key)
        if writer is None or not writer._running:
            path = subagent_trace_path(session_key)
            writer = _TraceWriter(path)
            writer.start()
            _WRITERS[session_key] = writer
        return writer


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_subagent_trace(session_key: str, event: dict[str, Any]) -> None:
    """Append a trace event via the batched writer (non-blocking)."""
    writer = _get_writer(session_key)
    writer.append(event)


def flush_subagent_trace(session_key: str) -> bool:
    """Flush pending trace events for a session to disk."""
    with _WRITERS_LOCK:
        writer = _WRITERS.get(session_key)
    if writer is None:
        return True
    return writer.flush()


def close_subagent_trace_writer(session_key: str) -> None:
    """Stop and flush the writer for a session."""
    with _WRITERS_LOCK:
        writer = _WRITERS.pop(session_key, None)
    if writer is not None:
        writer.close()


def delete_subagent_trace(session_key: str) -> bool:
    """Stop the writer and delete a session's trace sidecar."""
    close_subagent_trace_writer(session_key)
    path = subagent_trace_path(session_key)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Failed to delete subagent trace {}: {}", path, exc)
        return False


def read_subagent_cards(session_key: str) -> list[dict[str, Any]]:
    """Rebuild WebUI card state from a session's subagent trace sidecar."""
    # Ensure all pending events are on disk before reading.
    flush_subagent_trace(session_key)
    path = subagent_trace_path(session_key)
    if not path.is_file():
        return []
    cards: dict[str, dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                task_id = record.get("subagent_id")
                if not isinstance(task_id, str) or not task_id:
                    continue
                if record.get("event") == "started":
                    cards.setdefault(
                        task_id,
                        {
                            "id": task_id,
                            "label": str(record.get("label") or task_id),
                            "status": "running",
                            "thinking": "",
                            "thinkingStreaming": False,
                            "startedAt": int(record.get("createdAt") or 0),
                        },
                    )
                    continue
                card = cards.get(task_id)
                if card is None:
                    continue
                if record.get("event") == "reasoning_delta":
                    text = record.get("text")
                    if isinstance(text, str):
                        card["thinking"] += text
                elif record.get("event") == "finished":
                    status = record.get("status")
                    card["status"] = "completed" if status == "ok" else "error"
    except OSError as exc:
        logger.warning("Failed to read subagent trace {}: {}", path, exc)
        return []
    return list(cards.values())
