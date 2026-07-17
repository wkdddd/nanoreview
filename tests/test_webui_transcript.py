from __future__ import annotations

from nanobot.utils.webui_transcript import replay_transcript_to_ui_messages


def test_review_report_absorbs_thinking_and_ignores_untyped_content() -> None:
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
    assert messages[0]["reasoning"] == "Inspect files.\n"


def test_review_thinking_is_preserved_when_report_is_missing() -> None:
    lines = [
        {"event": "delta", "kind": "review_thinking", "text": "Inspect files.\n"},
        {"event": "stream_end", "kind": "review_thinking"},
        {"event": "turn_end"},
    ]

    messages = replay_transcript_to_ui_messages(lines)

    assert len(messages) == 1
    assert messages[0]["content"] == ""
    assert messages[0]["reasoning"] == "Inspect files.\n"
