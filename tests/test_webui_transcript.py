from __future__ import annotations

from nanobot.utils.webui_transcript import (
    append_transcript_object,
    read_transcript_lines,
    replay_transcript_to_subagent_cards,
    replay_transcript_to_ui_messages,
)


def test_review_report_ignores_thinking_and_untyped_content() -> None:
    lines = [
        {"event": "delta", "kind": "review_thinking", "text": "Inspect files.\n"},
        {"event": "stream_end", "kind": "review_thinking"},
        {"event": "delta", "text": "intermediate content that is not user-facing"},
        {"event": "delta", "kind": "review_report", "text": "## Report\n"},
        {"event": "stream_end", "kind": "review_report"},
        {"event": "turn_end"},
    ]

    messages = replay_transcript_to_ui_messages(lines)

    assert len(messages) == 1
    assert messages[0]["content"] == "## Report\n"
    assert "reasoning" not in messages[0]


def test_review_thinking_is_not_restored_without_report() -> None:
    lines = [
        {"event": "delta", "kind": "review_thinking", "text": "Inspect files.\n"},
        {"event": "stream_end", "kind": "review_thinking"},
        {"event": "turn_end"},
    ]

    messages = replay_transcript_to_ui_messages(lines)

    assert messages == []


def test_subagent_content_is_replayed_in_its_card_not_main_messages() -> None:
    lines = [
        {
            "event": "subagent_status",
            "subagent_id": "security",
            "label": "Security",
            "status": "running",
        },
        {
            "event": "delta",
            "kind": "subagent_content",
            "subagent_id": "security",
            "text": "Inspect auth flow.\n",
        },
        {
            "event": "delta",
            "kind": "subagent_content",
            "subagent_id": "security",
            "text": "No issue found.\n",
        },
        {
            "event": "stream_end",
            "kind": "subagent_content",
            "subagent_id": "security",
        },
        {
            "event": "subagent_status",
            "subagent_id": "security",
            "label": "Security",
            "status": "completed",
        },
    ]

    assert replay_transcript_to_ui_messages(lines) == []
    cards = replay_transcript_to_subagent_cards(lines, [])

    assert len(cards) == 1
    assert cards[0]["id"] == "security"
    assert cards[0]["label"] == "Security"
    assert cards[0]["status"] == "completed"
    assert cards[0]["output"] == "Inspect auth flow.\nNo issue found.\n"
    assert cards[0]["outputStreaming"] is False


def test_transcript_persistence_sanitizes_nested_text_and_enforces_total_limit(
    monkeypatch, tmp_path
) -> None:
    import nanobot.utils.webui_transcript as transcript

    monkeypatch.setattr(transcript, "get_webui_dir", lambda: tmp_path)
    monkeypatch.setattr(transcript, "_MAX_TRANSCRIPT_FILE_BYTES", 180)

    append_transcript_object(
        "websocket:chat",
        {
            "event": "message",
            "text": "Authorization: Bearer secret-token <think>hidden</think>",
            "nested": ["sk-abcdefghijklmnopqrstuvwxyz123456"],
        },
    )
    append_transcript_object("websocket:chat", {"event": "message", "text": "x" * 100})

    lines = read_transcript_lines("websocket:chat")

    assert len(lines) == 1
    assert "secret-token" not in lines[0]["text"]
    assert "<think>" not in lines[0]["text"]
    assert lines[0]["nested"] == ["***REDACTED***"]
