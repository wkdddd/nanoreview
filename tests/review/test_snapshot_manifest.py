"""Snapshot + manifest persistence for one review run.

The snapshot records *what was reviewed* and *what the planner saw* — ids,
ranges, budget and coverage — without duplicating reviewed source; the report
artifact references the same boundary and per-reviewer cost summary.
"""

from __future__ import annotations

from nanoreview.review.input.snapshot import (
    REVIEW_SNAPSHOTS_DIR_NAME,
    ReviewSnapshotStore,
    build_snapshot,
)
from nanoreview.review.planning.manifest import build_evidence_manifest
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
    }
    payload.update(overrides)
    return EvidenceReference(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# snapshot augment
# ---------------------------------------------------------------------------


def test_augment_merges_sections_and_preserves_the_snapshot(tmp_path) -> None:
    store = ReviewSnapshotStore(tmp_path)
    ref = store.write(
        build_snapshot(
            run_id="run-abc",
            session_key="cli:1",
            action="diff",
            target_type="local",
            target="/repo",
            input_fingerprint="fp",
            changed_files=["src/a.py"],
        )
    )

    augmented = store.augment("run-abc", sections={"planner_manifest": {"retained": 1}})

    assert augmented == ref
    payload = (tmp_path / REVIEW_SNAPSHOTS_DIR_NAME / "run-abc.json").read_text("utf-8")
    assert '"planner_manifest"' in payload
    # The admission-time fields survive the merge.
    assert '"input_fingerprint": "fp"' in payload
    assert '"changed_files"' in payload


def test_augment_returns_none_when_the_snapshot_is_missing(tmp_path) -> None:
    store = ReviewSnapshotStore(tmp_path)

    assert store.augment("run-missing", sections={"budget": {}}) is None


# ---------------------------------------------------------------------------
# manifest snapshot payload
# ---------------------------------------------------------------------------


def test_manifest_snapshot_payload_records_layout_not_source() -> None:
    bundle = ReviewEvidenceBundle(
        references=(_reference(1, matched=("token",)),)
    )
    manifest = build_evidence_manifest(bundle)

    payload = manifest.snapshot_payload()

    assert payload["version"] == manifest.version
    assert payload["token_counter"] == manifest.token_counter
    assert payload["budget_tokens"] == manifest.budget_tokens
    assert payload["input_mode"] == "direct"
    assert payload["retained"] == 1
    entry = payload["entries"][0]
    assert entry["id"] == "ev-001"
    assert entry["matched"] == ["token"]
    assert "risk_hints" not in entry
    # The inlined content is summarized by length, never copied verbatim.
    assert entry["content_chars"] == len(manifest.entries[0].content)
    assert "content" not in entry
    assert "preview" not in entry


def test_manifest_snapshot_payload_records_omitted_references() -> None:
    references = tuple(_reference(i) for i in range(1, 40))
    manifest = build_evidence_manifest(
        ReviewEvidenceBundle(references=references), budget_tokens=60
    )

    payload = manifest.snapshot_payload()

    assert len(payload["omitted_entries"]) >= 1
    assert payload["omitted"] == len(manifest.omitted)
    assert set(payload["omitted_entries"][0]) == {"id", "path", "reason", "token_count"}
