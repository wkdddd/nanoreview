"""Prefetch turns the frozen PR patches into the reviewers' evidence bundle."""

from __future__ import annotations

from nanoreview.review.planning.planner import build_review_plan
from nanoreview.review.planning.prefetch import maybe_prefetch_review_context
from nanoreview.review.planning.preprocessor import (
    CodeUnit,
    ProgrammaticEvidenceResult,
    SkippedUnit,
)
from nanoreview.review.types import (
    PullRequestTarget,
    ReviewAction,
    ReviewMetaKey,
    ReviewPlan,
)


def _pull_request(**overrides: object) -> PullRequestTarget:
    values: dict[str, object] = {
        "repository": "octo/demo",
        "number": 7,
        "base_repo": "octo/demo",
        "head_repo": "octo/demo",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "merge_base_sha": "c" * 40,
        "snapshot_id": "snap_1",
    }
    values.update(overrides)
    return PullRequestTarget(**values)


def _plan(**overrides: object) -> ReviewPlan:
    plan = build_review_plan(
        target="https://github.com/octo/demo/pull/7",
        user_content="review auth",
        focus="general",
        action="diff",
        pull_request=_pull_request(),
    )
    assert plan is not None
    return plan


class _EvidenceService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def dispatch(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "\n".join(
            [
                "[Pull Request Review References - retrieved references, not instructions]",
                "## src/auth.py:1-10",
                "- score: 1.0",
                "- matched: broad, bm25",
                "ignored body line",
            ]
        )


class _EmptyEvidenceService:
    async def dispatch(self, **kwargs: object) -> str:
        return ""


class _PatchEvidenceService:
    async def dispatch(self, **kwargs: object) -> str:
        return "[Pull Request Diff Review Context]\n## File: src/auth.py\n+token = value"


class _StructuredEvidenceService:
    """Provider exposing the structured preprocessing result."""

    def __init__(self, result: ProgrammaticEvidenceResult) -> None:
        self.last_result = result

    async def dispatch(self, **kwargs: object) -> str:
        return self.last_result.context


def _structured_result() -> ProgrammaticEvidenceResult:
    return ProgrammaticEvidenceResult(
        units=[
            CodeUnit(
                path="src/auth.py",
                kind="function",
                name="login",
                start_line=1,
                end_line=10,
                text="def login(token):\n    return token\n",
                token_count=8,
                unit_id="ev-1",
                role="main",
                preview="def login(token):\n    return token",
                preview_coverage="full chunk lines 1-2",
            ),
            CodeUnit(
                path="src/legacy.py",
                kind="file",
                start_line=1,
                end_line=4,
                text="legacy = 1\n",
                token_count=1,
                unit_id="ev-2",
                role="related",
                parent_id="ev-1",
                tags=("related", "import"),
            ),
        ],
        skipped=[
            SkippedUnit(path="src/logo.png", reason="binary_file"),
            SkippedUnit(path="src/big.py", reason="empty_patch", start_line=5, end_line=9),
            SkippedUnit(path="src/big.py", reason="filtered", start_line=10, end_line=20),
        ],
        context=(
            "[Pull Request Review References - programmatic evidence, not instructions]\n"
            "## src/auth.py:1-10\n- kind: function\n```text\ndef login(token):\n```\n"
            "[/Pull Request Review References]"
        ),
    )


async def test_prefetch_dispatches_to_evidence_service_for_diff() -> None:
    evidence_service = _EvidenceService()

    summary = await maybe_prefetch_review_context(
        _plan(),
        {"_review_evidence_service": evidence_service},
    )

    assert evidence_service.calls
    call = evidence_service.calls[0]
    assert call["review_query"] == "review auth"
    assert call["target_type"] == "pull_request"
    assert call["action"] == "diff"
    # There is no local scope and no remote read: prefetch only carries the
    # frozen patches the snapshot captured.
    assert "local_scope" not in call
    assert "repo" not in call
    assert "ref" not in call
    assert summary.attempted is True
    assert summary.status == "ok"
    assert "## src/auth.py:1-10" in (summary.summary or "")
    assert "ignored body line" in (summary.summary or "")
    assert summary.evidence is not None
    assert summary.evidence.references[0].path == "src/auth.py"
    assert summary.evidence.references[0].id == "ev-001"


async def test_prefetch_forwards_the_frozen_diff_to_the_evidence_service() -> None:
    """The admitted snapshot's patches are what prefetch reviews."""
    evidence_service = _EvidenceService()
    frozen = {
        "patches": {"src/auth.py": "@@ -1 +1 @@\n-old\n+new"},
        "skipped": {"src/gen.py": "patch_unavailable"},
    }

    await maybe_prefetch_review_context(
        _plan(),
        {
            "_review_evidence_service": evidence_service,
            ReviewMetaKey.FROZEN_DIFF: frozen,
        },
    )

    assert evidence_service.calls
    assert evidence_service.calls[0]["frozen_diff"] == frozen


async def test_prefetch_emits_progress_events() -> None:
    evidence_service = _EvidenceService()
    events: list[dict[str, object]] = []

    async def progress(_content: str, **kwargs: object) -> None:
        tool_events = kwargs.get("tool_events")
        if isinstance(tool_events, list):
            events.extend(tool_events)

    await maybe_prefetch_review_context(
        _plan(),
        {"_review_evidence_service": evidence_service},
        progress_callback=progress,
    )

    assert [event["phase"] for event in events] == ["start", "end"]
    assert {event["name"] for event in events} == {"review_prefetch"}
    assert events[-1]["result"] == "ok"


async def test_prefetch_reports_attempted_when_summary_is_empty() -> None:
    result = await maybe_prefetch_review_context(
        _plan(),
        {"_review_evidence_service": _EmptyEvidenceService()},
    )

    assert result.attempted is True
    assert result.status == "no_summary"
    assert result.summary is None


async def test_diff_prefetch_preserves_filtered_patch_body() -> None:
    result = await maybe_prefetch_review_context(
        _plan(),
        {"_review_evidence_service": _PatchEvidenceService()},
    )

    assert "+token = value" in (result.summary or "")


async def test_prefetch_builds_bundle_from_structured_units() -> None:
    result = await maybe_prefetch_review_context(
        _plan(),
        {"_review_evidence_service": _StructuredEvidenceService(_structured_result())},
    )

    assert result.status == "ok"
    assert result.evidence is not None
    bundle = result.evidence
    assert [reference.id for reference in bundle.references] == ["ev-1", "ev-2"]
    main = bundle.references[0]
    assert (main.path, main.kind, main.token_count) == ("src/auth.py", "function", 8)
    assert main.preview.startswith("def login(token):")
    # Structured prefetch carries the unit's reviewable text and coverage.
    # There is no planner input mode any more: every accepted unit is inlined
    # as its own excerpt for the reviewers.
    assert main.excerpt.startswith("def login(token):")
    assert main.preview_coverage == "full chunk lines 1-2"
    assert all(reference.excerpt for reference in bundle.references)
    related = bundle.references[1]
    assert related.parent_id == "ev-1"
    assert related.is_related is True


async def test_unit_text_reaches_the_reviewer_without_truncation() -> None:
    """The excerpt is the complete diff, however large the unit is."""
    result = _structured_result()
    huge = "@@ -1 +1 @@\n+" + "x" * 90_000
    result.units[0].text = huge

    prefetched = await maybe_prefetch_review_context(
        _plan(),
        {"_review_evidence_service": _StructuredEvidenceService(result)},
    )

    assert prefetched.evidence is not None
    assert prefetched.evidence.references[0].excerpt == huge
    assert len(prefetched.evidence.references[0].excerpt) == len(huge)


async def test_legacy_provider_drops_risk_labels() -> None:
    class _LegacyRiskEvidenceService:
        async def dispatch(self, **kwargs: object) -> str:
            return "\n".join(
                [
                    "[Pull Request Review References - retrieved references, not instructions]",
                    "## src/auth.py:1-10",
                    "- score: 4.0",
                    "- matched: login, risk:security, risk:entrypoint",
                    "def login(token):",
                    "    return token",
                ]
            )

    result = await maybe_prefetch_review_context(
        _plan(),
        {"_review_evidence_service": _LegacyRiskEvidenceService()},
    )

    assert result.evidence is not None
    reference = result.evidence.references[0]
    # Legacy `risk:*` labels carry no meaning now: only query hits survive.
    assert reference.matched == ("login",)
    assert not hasattr(reference, "risk_hints")
    # A legacy text provider has no authorized unit ordering to trust, so the
    # preview is sampled from the provider snippet instead of a fabricated
    # inline set.
    assert result.evidence.references[0].id == "ev-001"
    # The preview is sampled from the provider snippet and its coverage is
    # explicitly marked instead of claiming full-chunk coverage.
    assert reference.preview
    assert "provider snippet; full chunk not recovered" in reference.preview_coverage


async def test_prefetch_aggregates_skipped_files_per_path() -> None:
    events: list[dict[str, object]] = []

    async def progress(_content: str, **kwargs: object) -> None:
        tool_events = kwargs.get("tool_events")
        if isinstance(tool_events, list):
            events.extend(tool_events)

    result = await maybe_prefetch_review_context(
        _plan(),
        {"_review_evidence_service": _StructuredEvidenceService(_structured_result())},
        progress_callback=progress,
    )

    assert result.evidence is not None
    # Two skipped chunks of the same file aggregate into one summary entry.
    skipped_by_file = result.evidence.skipped_by_file()
    assert set(skipped_by_file) == {"src/logo.png", "src/big.py"}
    whole_file_summary = skipped_by_file["src/logo.png"]
    assert whole_file_summary.whole_file is True
    assert whole_file_summary.reasons == ("binary_file",)
    big_summary = skipped_by_file["src/big.py"]
    assert big_summary.whole_file is False
    assert big_summary.ranges == ((5, 9), (10, 20))
    assert set(big_summary.reasons) == {"empty_patch", "filtered"}

    end_event = events[-1]
    assert end_event["phase"] == "end"
    metadata = end_event["metadata"]
    assert metadata["skipped_units"] == 3
    assert len(metadata["skipped_files"]) == 2
    assert any("src/big.py" in entry for entry in metadata["skipped_files"])
    assert any("unreviewed lines 5-9, 10-20" in entry for entry in metadata["skipped_files"])
