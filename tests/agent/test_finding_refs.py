"""Tests for transient finding-reference collection."""

from __future__ import annotations

from nanoreview.agent.finding_refs import (
    collect_finding_references,
    extract_finding_ids,
    report_finding_ids,
)
from nanoreview.review.result import REVIEW_HANDOFF_HEADER


class TestExtractFindingIds:
    def test_extracts_in_order_and_dedupes(self) -> None:
        assert extract_finding_ids("F002 then F001 then F002") == ["F002", "F001"]

    def test_accepts_lowercase_and_normalizes(self) -> None:
        assert extract_finding_ids("look at f003") == ["F003"]

    def test_ignores_ids_embedded_in_identifiers(self) -> None:
        # ``ref_F001`` has no word boundary before ``F``: not a finding ID.
        assert extract_finding_ids("ref_F001 and F0002") == ["F0002"]

    def test_ignores_two_digit_tokens(self) -> None:
        assert extract_finding_ids("F01 and F0") == []

    def test_empty_text(self) -> None:
        assert extract_finding_ids("") == []


class TestReportFindingIds:
    def test_reads_ids_from_the_latest_handoff_block(self) -> None:
        history = [
            {"content": f"{REVIEW_HANDOFF_HEADER}\nrun_id=a\nF001 F002"},
            {"content": "assistant follow-up"},
            {"content": f"{REVIEW_HANDOFF_HEADER}\nrun_id=b\nF007"},
        ]

        # A re-review replaces the valid set instead of merging with the old one.
        assert report_finding_ids(history) == frozenset({"F007"})

    def test_no_handoff_block_yields_no_ids(self) -> None:
        assert report_finding_ids([{"content": "just chatting about F001"}]) == frozenset()

    def test_ignores_non_string_content(self) -> None:
        assert report_finding_ids([{"content": [{"type": "text", "text": "F001"}]}]) == frozenset()


class TestCollectFindingReferences:
    def test_keeps_only_ids_present_in_the_report(self) -> None:
        refs = collect_finding_references(
            ["fix F002 and F009", "and F001", "F002 again"],
            {"F001", "F002"},
        )

        assert refs == ["F002", "F001"]

    def test_unknown_ids_are_dropped(self) -> None:
        assert collect_finding_references(["F999"], {"F001"}) == []

    def test_no_valid_ids_short_circuits(self) -> None:
        assert collect_finding_references(["F001"], set()) == []

    def test_ignores_non_string_entries(self) -> None:
        assert collect_finding_references([None, "F001"], {"F001"}) == ["F001"]
