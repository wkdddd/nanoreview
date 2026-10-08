"""Tests for the planner evidence input (direct inline vs. paged index)."""

from __future__ import annotations

from nanoreview.review.planning.manifest import (
    MANIFEST_TOKEN_COUNTER,
    MANIFEST_VERSION,
    PLANNER_MANIFEST_BUDGET_TOKENS,
    build_evidence_manifest,
    reference_priority,
    render_manifest,
)
from nanoreview.review.types import EvidenceReference, ReviewEvidenceBundle


def _reference(index: int, **overrides: object) -> EvidenceReference:
    payload: dict[str, object] = {
        "id": f"ev-{index:03d}",
        "path": f"src/mod{index}.py",
        "start_line": 1,
        "end_line": 10,
        "kind": "function",
        "token_count": 20,
        "excerpt": f"def f{index}():\n    return {index}",
        "preview": f"def f{index}():\n    return {index}",
        "preview_coverage": "chunk lines 1-10; preview covers 1-2",
    }
    payload.update(overrides)
    return EvidenceReference(**payload)  # type: ignore[arg-type]


def test_manifest_inlines_content_in_direct_mode() -> None:
    bundle = ReviewEvidenceBundle(
        references=(
            _reference(1, matched=("token",)),
            _reference(2, tags=("related", "import"), parent_id="ev-001"),
        )
    )

    manifest = build_evidence_manifest(bundle)

    assert manifest.version == MANIFEST_VERSION
    assert manifest.token_counter == MANIFEST_TOKEN_COUNTER
    assert manifest.input_mode == "direct"
    first = manifest.entries[0]
    assert (first.id, first.path, first.kind, first.role) == (
        "ev-001",
        "src/mod1.py",
        "function",
        "main",
    )
    assert first.matched == ("token",)
    # Direct mode inlines the reviewable content itself, not a sampled preview.
    assert first.content == "def f1():\n    return 1"
    # A related reference is marked related but keeps its stable id.
    related = next(entry for entry in manifest.entries if entry.id == "ev-002")
    assert related.role == "related"
    text = render_manifest(manifest)
    assert "matched: token" in text
    assert "def f1():" in text
    assert "risk_hints" not in text


def test_manifest_renders_index_only_in_paged_mode() -> None:
    bundle = ReviewEvidenceBundle(
        references=(_reference(1),),
        input_mode="paged",
    )

    manifest = build_evidence_manifest(bundle)

    assert manifest.input_mode == "paged"
    assert manifest.entries[0].content == ""
    text = render_manifest(manifest)
    # The index names the unit but ships no content; the planner must read it.
    assert "src/mod1.py" in text
    assert "call read_review_diff with this id" in text
    assert "def f1():" not in text


def test_manifest_defaults_match_the_review_window_budget() -> None:
    manifest = build_evidence_manifest(ReviewEvidenceBundle())

    assert manifest.budget_tokens == PLANNER_MANIFEST_BUDGET_TOKENS == 80_000
    assert manifest.input_mode == "direct"


def test_manifest_keeps_frozen_order_and_omits_the_tail() -> None:
    # Ten references, each roughly 40+ tokens rendered; a tight budget forces
    # tail omissions.
    references = tuple(_reference(index) for index in range(1, 11))
    bundle = ReviewEvidenceBundle(references=references)

    manifest = build_evidence_manifest(bundle, budget_tokens=120)

    assert manifest.used_tokens <= manifest.budget_tokens
    assert manifest.omitted_count >= 1
    assert manifest.retained_count + manifest.omitted_count == len(references)
    assert {entry.reason for entry in manifest.omitted} == {"budget_exhausted"}
    # Retained entries are exactly the frozen-order prefix.
    ordered = sorted(references, key=reference_priority)
    assert [entry.id for entry in manifest.entries] == [
        reference.id for reference in ordered[: manifest.retained_count]
    ]
    assert manifest.authorized_ids() == tuple(
        reference.id for reference in ordered[: manifest.retained_count]
    )


def test_manifest_never_exceeds_budget_for_a_single_huge_reference() -> None:
    bundle = ReviewEvidenceBundle(
        references=(_reference(1, excerpt="x" * 100_000, token_count=90_000),)
    )

    manifest = build_evidence_manifest(bundle, budget_tokens=2_000)

    assert manifest.retained_count == 1
    assert manifest.used_tokens <= manifest.budget_tokens
    assert "trimmed to planner budget" in manifest.entries[0].content


def test_manifest_stats_report_retained_omitted_and_skipped() -> None:
    bundle = ReviewEvidenceBundle(
        references=(_reference(1), _reference(2)),
        skipped=(),
    )

    manifest = build_evidence_manifest(bundle)
    stats = manifest.stats()

    assert stats["version"] == MANIFEST_VERSION
    assert stats["token_counter"] == MANIFEST_TOKEN_COUNTER
    assert stats["input_mode"] == "direct"
    assert stats["budget_tokens"] == 80_000
    assert stats["retained"] == 2
    assert stats["omitted"] == 0
    assert stats["total_references"] == 2
    assert manifest.authorized_ids() == ("ev-001", "ev-002")


def test_manifest_reports_omitted_entries_in_rendered_text() -> None:
    references = tuple(
        _reference(index, token_count=100) for index in range(1, 21)
    )
    bundle = ReviewEvidenceBundle(references=references)

    manifest = build_evidence_manifest(bundle, budget_tokens=150)
    text = render_manifest(manifest)

    assert "## Manifest Omitted (budget)" in text
    assert str(manifest.omitted_count) in text
