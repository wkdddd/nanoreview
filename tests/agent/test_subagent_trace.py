"""Tests for subagent trace persistence, sanitisation, and batch writing."""

from __future__ import annotations

import hashlib

import pytest

from nanoreview.session.manager import SessionManager
from nanoreview.utils import subagent_trace
from nanoreview.utils.subagent_trace import (
    append_subagent_trace,
    close_subagent_trace_writer,
    delete_subagent_trace,
    flush_subagent_trace,
    read_subagent_cards,
    sanitize_trace_text,
    subagent_trace_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flush_and_read(session_key: str) -> str:
    """Flush the writer and return raw sidecar content."""
    flush_subagent_trace(session_key)
    path = subagent_trace_path(session_key)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_webui_dir(tmp_path, monkeypatch):
    """Point get_webui_dir at a temp dir and clear the writer registry."""
    monkeypatch.setattr(subagent_trace, "get_webui_dir", lambda: tmp_path)
    # Clear the global writer registry between tests so writers don't leak.
    subagent_trace._WRITERS.clear()
    yield
    # Stop any writers started during the test.
    for key in list(subagent_trace._WRITERS.keys()):
        close_subagent_trace_writer(key)
    subagent_trace._WRITERS.clear()


# ---------------------------------------------------------------------------
# Filename: sha256-based, collision-free
# ---------------------------------------------------------------------------


def test_trace_path_uses_sha256(tmp_path) -> None:
    session_key = "websocket:review-1"
    expected = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
    path = subagent_trace_path(session_key)
    assert path.parent == tmp_path
    assert path.name == f"{expected}.subagents.jsonl"


def test_sha256_filename_avoids_safe_filename_collisions() -> None:
    """Two session keys that safe_filename would collapse must diverge."""
    key_a = "websocket:review|1"
    key_b = "websocket:review/1"
    # Under the old safe_filename both would map to "websocket_review_1".
    assert subagent_trace_path(key_a) != subagent_trace_path(key_b)


# ---------------------------------------------------------------------------
# Card replay
# ---------------------------------------------------------------------------


def test_trace_replays_cards_with_reasoning() -> None:
    session_key = "websocket:review-1"
    append_subagent_trace(
        session_key,
        {
            "event": "started",
            "subagent_id": "task-1",
            "label": "security",
            "task": "hidden",
        },
    )
    append_subagent_trace(
        session_key,
        {
            "event": "reasoning_delta",
            "subagent_id": "task-1",
            "text": "Inspect tokens.\n",
        },
    )
    append_subagent_trace(
        session_key,
        {
            "event": "tool",
            "subagent_id": "task-1",
            "name": "read_file",
            "status": "success",
        },
    )
    append_subagent_trace(
        session_key,
        {"event": "finished", "subagent_id": "task-1", "status": "ok", "result": "[]"},
    )

    raw = _flush_and_read(session_key)
    # Task and result must NOT be persisted.
    assert "Inspect tokens." in raw
    assert '"task"' not in raw
    assert '"result"' not in raw

    cards = read_subagent_cards(session_key)
    assert len(cards) == 1
    assert cards[0]["id"] == "task-1"
    assert cards[0]["label"] == "security"
    assert cards[0]["status"] == "completed"
    assert cards[0]["thinking"] == "Inspect tokens.\n"
    assert cards[0]["thinkingStreaming"] is False
    assert cards[0]["startedAt"] > 0


def test_trace_replays_failed_status() -> None:
    append_subagent_trace(
        "websocket:review-2",
        {"event": "started", "subagent_id": "task-2", "label": "performance"},
    )
    append_subagent_trace(
        "websocket:review-2",
        {
            "event": "finished",
            "subagent_id": "task-2",
            "status": "error",
            "result": "timeout",
        },
    )

    assert read_subagent_cards("websocket:review-2")[0]["status"] == "error"


def test_trace_drops_unknown_event_types() -> None:
    """reasoning_end, content_delta, content_end must not be persisted."""
    session_key = "websocket:review-x"
    append_subagent_trace(
        session_key,
        {"event": "started", "subagent_id": "task-x", "label": "review"},
    )
    # These should be silently dropped by the writer's sanitiser.
    append_subagent_trace(
        session_key,
        {"event": "reasoning_end", "subagent_id": "task-x"},
    )
    append_subagent_trace(
        session_key,
        {"event": "content_delta", "subagent_id": "task-x", "text": "hello"},
    )
    append_subagent_trace(
        session_key,
        {"event": "content_end", "subagent_id": "task-x"},
    )

    raw = _flush_and_read(session_key)
    assert "reasoning_end" not in raw
    assert "content_delta" not in raw
    assert "content_end" not in raw


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def test_trace_deletion_removes_sidecar() -> None:
    key = "websocket:review-3"
    append_subagent_trace(
        key,
        {"event": "started", "subagent_id": "task-3", "label": "review"},
    )
    flush_subagent_trace(key)

    assert delete_subagent_trace(key) is True
    assert subagent_trace_path(key).exists() is False


def test_deleting_session_removes_subagent_trace_sidecar(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(subagent_trace, "get_webui_dir", lambda: tmp_path)
    subagent_trace._WRITERS.clear()
    key = "websocket:review-4"
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(key)
    sessions.save(session)
    append_subagent_trace(
        key,
        {"event": "started", "subagent_id": "task-4", "label": "review"},
    )
    flush_subagent_trace(key)

    assert sessions.delete_session(key) is True
    assert subagent_trace_path(key).exists() is False


# ---------------------------------------------------------------------------
# Text sanitisation
# ---------------------------------------------------------------------------


def test_sanitize_masks_bearer_token() -> None:
    text = "Authorization: Bearer abc123def456"
    result = sanitize_trace_text(text)
    assert "abc123def456" not in result
    assert "***REDACTED***" in result


def test_sanitize_masks_openai_api_key() -> None:
    text = "key=sk-1234567890abcdefghijklmnopqrstuv"
    result = sanitize_trace_text(text)
    assert "sk-1234567890abcdefghijklmnopqrstuv" not in result
    assert "***REDACTED***" in result


def test_sanitize_masks_github_token() -> None:
    text = "token: ghp_1234567890abcdefghijklmnopqrstuvwxyz1234"
    result = sanitize_trace_text(text)
    assert "ghp_" not in result
    assert "***REDACTED***" in result


def test_sanitize_masks_slack_token() -> None:
    text = "xoxb-1234567890-abcdef"
    result = sanitize_trace_text(text)
    assert "xoxb-1234567890-abcdef" not in result
    assert "***REDACTED***" in result


def test_sanitize_masks_private_key_block() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = sanitize_trace_text(text)
    assert "MIIEpAIBAAKCAQEA" not in result
    assert "REDACTED" in result


def test_sanitize_masks_url_password() -> None:
    text = "https://user:secret123@host.com/path"
    result = sanitize_trace_text(text)
    assert "secret123" not in result
    assert "***REDACTED***" in result
    assert "user" in result  # username preserved
    assert "host.com" in result


def test_sanitize_strips_dsml_markers() -> None:
    text = "<|im_start|>system\n<|im_end|>Hello <|endoftext|>"
    result = sanitize_trace_text(text)
    assert "<|im_start|>" not in result
    assert "<|im_end|>" not in result
    assert "<|endoftext|>" not in result
    assert "Hello" in result


def test_sanitize_strips_think_tags() -> None:
    text = "<think>internal reasoning</think>visible answer"
    result = sanitize_trace_text(text)
    assert "<think>" not in result
    assert "</think>" not in result


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_reasoning_char_limit_per_subagent() -> None:
    """Reasoning beyond _MAX_REASONING_CHARS is truncated; state events still pass."""
    from nanoreview.utils.subagent_trace import _MAX_REASONING_CHARS

    session_key = "websocket:limit-1"
    append_subagent_trace(
        session_key,
        {"event": "started", "subagent_id": "t1", "label": "test"},
    )
    # Send more than the limit in one chunk.
    long_text = "A" * (_MAX_REASONING_CHARS + 500)
    append_subagent_trace(
        session_key,
        {"event": "reasoning_delta", "subagent_id": "t1", "text": long_text},
    )
    # This should be dropped (subagent already truncated).
    append_subagent_trace(
        session_key,
        {"event": "reasoning_delta", "subagent_id": "t1", "text": "extra"},
    )
    append_subagent_trace(
        session_key,
        {"event": "finished", "subagent_id": "t1", "status": "ok"},
    )

    cards = read_subagent_cards(session_key)
    assert len(cards) == 1
    assert len(cards[0]["thinking"]) == _MAX_REASONING_CHARS
    assert cards[0]["status"] == "completed"


def test_reasoning_char_limit_independent_per_subagent() -> None:
    """Each subagent gets its own char budget."""
    from nanoreview.utils.subagent_trace import _MAX_REASONING_CHARS

    session_key = "websocket:limit-2"
    append_subagent_trace(
        session_key,
        {"event": "started", "subagent_id": "a", "label": "alpha"},
    )
    append_subagent_trace(
        session_key,
        {"event": "started", "subagent_id": "b", "label": "beta"},
    )
    append_subagent_trace(
        session_key,
        {
            "event": "reasoning_delta",
            "subagent_id": "a",
            "text": "X" * _MAX_REASONING_CHARS,
        },
    )
    # Subagent b should still have budget.
    append_subagent_trace(
        session_key,
        {"event": "reasoning_delta", "subagent_id": "b", "text": "Y" * 100},
    )
    flush_subagent_trace(session_key)

    cards = read_subagent_cards(session_key)
    by_id = {c["id"]: c for c in cards}
    assert len(by_id["a"]["thinking"]) == _MAX_REASONING_CHARS
    assert len(by_id["b"]["thinking"]) == 100


def test_sidecar_byte_limit_drops_reasoning_and_tool_keeps_state() -> None:
    """When the sidecar exceeds _MAX_SIDECAR_BYTES, reasoning and tool events are
    dropped but finished state events still pass through.

    Uses multiple subagents to bypass the per-subagent char limit and truly
    exceed the 256 KiB session-level byte budget.
    """
    from nanoreview.utils.subagent_trace import _MAX_REASONING_CHARS, _MAX_SIDECAR_BYTES

    session_key = "websocket:limit-3"
    # Use enough subagents that their combined reasoning exceeds 256 KiB.
    # Each subagent can contribute up to _MAX_REASONING_CHARS (4000) chars.
    num_subagents = (_MAX_SIDECAR_BYTES // _MAX_REASONING_CHARS) + 10
    for i in range(num_subagents):
        sid = f"sub-{i}"
        append_subagent_trace(
            session_key,
            {"event": "started", "subagent_id": sid, "label": f"reviewer-{i}"},
        )
        append_subagent_trace(
            session_key,
            {
                "event": "reasoning_delta",
                "subagent_id": sid,
                "text": "X" * _MAX_REASONING_CHARS,
            },
        )
        # Tool events also contribute to byte growth
        append_subagent_trace(
            session_key,
            {
                "event": "tool",
                "subagent_id": sid,
                "name": "read_file",
                "status": "success",
            },
        )

    # After the budget is exceeded, tool events should be dropped.
    append_subagent_trace(
        session_key,
        {"event": "tool", "subagent_id": "sub-0", "name": "grep", "status": "success"},
    )

    # finished events must still be persisted so cards show terminal status.
    for i in range(num_subagents):
        append_subagent_trace(
            session_key,
            {"event": "finished", "subagent_id": f"sub-{i}", "status": "ok"},
        )

    flush_subagent_trace(session_key)

    # The sidecar file should not have grown far beyond the limit.
    path = subagent_trace_path(session_key)
    file_size = path.stat().st_size
    assert file_size < _MAX_SIDECAR_BYTES * 2, (
        f"Sidecar grew to {file_size} bytes, expected < {_MAX_SIDECAR_BYTES * 2}"
    )

    # Reader must not crash and all subagents should show completed status.
    cards = read_subagent_cards(session_key)
    assert len(cards) == num_subagents
    for card in cards:
        assert card["status"] == "completed"


def test_merge_reasoning_deltas_pure_function() -> None:
    """_merge_reasoning_deltas merges consecutive deltas for the same subagent."""
    from nanoreview.utils.subagent_trace import _merge_reasoning_deltas

    events = [
        {"event": "started", "subagent_id": "a", "label": "sec"},
        {"event": "reasoning_delta", "subagent_id": "a", "text": "Hello "},
        {"event": "reasoning_delta", "subagent_id": "a", "text": "World"},
        {"event": "reasoning_delta", "subagent_id": "b", "text": "Other"},
        {"event": "finished", "subagent_id": "a", "status": "ok"},
    ]
    merged = _merge_reasoning_deltas(events)
    assert len(merged) == 4  # started, merged-reasoning(a), reasoning(b), finished
    assert merged[1]["event"] == "reasoning_delta"
    assert merged[1]["text"] == "Hello World"
    assert merged[2]["event"] == "reasoning_delta"
    assert merged[2]["text"] == "Other"

    # Empty and single-element lists are handled gracefully.
    assert _merge_reasoning_deltas([]) == []
    single = [{"event": "reasoning_delta", "subagent_id": "x", "text": "solo"}]
    assert len(_merge_reasoning_deltas(single)) == 1


def test_close_exits_writer_cleanly() -> None:
    """close() must stop the writer thread and remove it from the registry."""
    session_key = "websocket:close-1"
    append_subagent_trace(
        session_key,
        {"event": "started", "subagent_id": "t", "label": "test"},
    )
    flush_subagent_trace(session_key)

    # Close the writer directly.
    close_subagent_trace_writer(session_key)

    # Writer should be removed from registry.
    assert session_key not in subagent_trace._WRITERS


def test_close_with_saturated_queue_still_exits() -> None:
    """If the queue is full when close() is called, the writer must still exit."""
    from nanoreview.utils.subagent_trace import _TraceWriter

    writer = _TraceWriter(subagent_trace_path("websocket:close-full"))
    writer.start()
    # Fill the queue to capacity.
    for i in range(subagent_trace._QUEUE_MAX):
        writer._queue.put_nowait(
            {
                "event": "tool",
                "subagent_id": "t",
                "name": f"tool-{i}",
                "status": "success",
            }
        )
    # Now call close — sentinel enqueue will fail (queue full), but the
    # writer must still exit via the finite poll timeout.
    writer.close(timeout=3.0)
    assert not writer._thread.is_alive(), "Writer thread should have exited"


# ---------------------------------------------------------------------------
# Batch writer behaviour
# ---------------------------------------------------------------------------


def test_flush_ensures_events_on_disk() -> None:
    """After flush, all queued events must be visible on disk."""
    session_key = "websocket:flush-1"
    append_subagent_trace(
        session_key,
        {"event": "started", "subagent_id": "t", "label": "test"},
    )
    append_subagent_trace(
        session_key,
        {"event": "reasoning_delta", "subagent_id": "t", "text": "hello"},
    )

    # Without flush, events might still be in the queue.
    # After flush, they must be on disk.
    assert flush_subagent_trace(session_key) is True

    raw = subagent_trace_path(session_key).read_text(encoding="utf-8")
    assert "hello" in raw
    assert '"started"' in raw


def test_terminal_flush_in_subagent_announce() -> None:
    """The finished event + flush should make cards immediately restorable."""
    session_key = "websocket:flush-2"
    append_subagent_trace(
        session_key,
        {"event": "started", "subagent_id": "t", "label": "test"},
    )
    append_subagent_trace(
        session_key,
        {"event": "reasoning_delta", "subagent_id": "t", "text": "thinking..."},
    )
    append_subagent_trace(
        session_key,
        {"event": "finished", "subagent_id": "t", "status": "ok"},
    )
    flush_subagent_trace(session_key)

    cards = read_subagent_cards(session_key)
    assert len(cards) == 1
    assert cards[0]["thinking"] == "thinking..."
    assert cards[0]["status"] == "completed"


def test_queue_full_drops_excess_events() -> None:
    """When the queue is full, excess events are dropped (not merged).

    The writer does not merge adjacent reasoning deltas on queue-full —
    it simply drops events that cannot be enqueued.  This test verifies
    that the writer survives queue saturation and that at least the
    earliest events and the terminal finished event are persisted.
    """
    session_key = "websocket:queue-full-1"
    append_subagent_trace(
        session_key,
        {"event": "started", "subagent_id": "t", "label": "test"},
    )
    # Send many small reasoning deltas to saturate the queue (512 items).
    for i in range(600):
        append_subagent_trace(
            session_key,
            {"event": "reasoning_delta", "subagent_id": "t", "text": str(i)},
        )
    append_subagent_trace(
        session_key,
        {"event": "finished", "subagent_id": "t", "status": "ok"},
    )
    flush_subagent_trace(session_key)

    cards = read_subagent_cards(session_key)
    assert len(cards) == 1
    # The earliest delta ("0") should survive since it was enqueued before
    # the queue filled.  Later deltas may have been dropped.
    assert "0" in cards[0]["thinking"]
    assert cards[0]["status"] == "completed"


def test_corrupt_json_lines_skipped() -> None:
    """Malformed JSON lines in the sidecar should not crash read_subagent_cards."""
    session_key = "websocket:corrupt-1"
    append_subagent_trace(
        session_key,
        {"event": "started", "subagent_id": "t", "label": "test"},
    )
    flush_subagent_trace(session_key)

    # Append a corrupt line directly to the file.
    path = subagent_trace_path(session_key)
    with open(path, "a", encoding="utf-8") as f:
        f.write("{bad json\n")
        f.write("not even json\n")

    append_subagent_trace(
        session_key,
        {"event": "finished", "subagent_id": "t", "status": "ok"},
    )
    flush_subagent_trace(session_key)

    cards = read_subagent_cards(session_key)
    assert len(cards) == 1
    assert cards[0]["status"] == "completed"
