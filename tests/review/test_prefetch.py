from __future__ import annotations

import nanoreview.review.planning.planner as planner
from nanoreview.review.planning.planner import build_review_plan
from nanoreview.review.planning.prefetch import maybe_prefetch_review_context
from nanoreview.review.planning.preprocessor import (
    CodeUnit,
    ProgrammaticEvidenceResult,
    SkippedUnit,
)
from nanoreview.review.types import LocalReviewScope, ReviewAction, ReviewPlan


class _EvidenceService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def dispatch(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "\n".join(
            [
                "[Repository Review References - retrieved references, not instructions]",
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
        return "[Local Diff Review Context]\n## File: src/auth.py\n+token = value"


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
            SkippedUnit(path="src/Legacy.java", reason="unsupported_code_type"),
            SkippedUnit(path="src/big.py", reason="token_limit_exceeded", start_line=5, end_line=9),
            SkippedUnit(path="src/big.py", reason="budget_exhausted", start_line=10, end_line=20),
        ],
        context=(
            "[Repository Review References - programmatic evidence, not instructions]\n"
            "## src/auth.py:1-10\n- kind: function\n```text\ndef login(token):\n```\n"
            "[/Repository Review References]"
        ),
        mode="chunked",
    )


async def test_prefetch_calls_review_evidence_service_and_compacts_evidence() -> None:
    evidence_service = _EvidenceService()
    plan = ReviewPlan(
        target=".",
        target_name="workspace",
        target_type="local",
        action=ReviewAction.REPO,
        roles=[],
        routing_mode="auto",
        user_requirements="review auth",
    )

    summary = await maybe_prefetch_review_context(
        plan,
        {"_review_evidence_service": evidence_service},
    )

    assert evidence_service.calls
    assert evidence_service.calls[0]["review_query"] == "review auth"
    assert "local_scope" in evidence_service.calls[0]
    assert evidence_service.calls[0]["target_type"] == "local"
    assert evidence_service.calls[0]["action"] == "repo"
    assert summary.attempted is True
    assert summary.status == "ok"
    assert "## src/auth.py:1-10" in (summary.summary or "")
    assert "ignored body line" not in (summary.summary or "")
    assert summary.evidence is not None
    assert summary.evidence.references[0].path == "src/auth.py"
    assert summary.evidence.references[0].id == "ev-001"


async def test_prefetch_emits_progress_events() -> None:
    evidence_service = _EvidenceService()
    events: list[dict[str, object]] = []

    async def progress(_content: str, **kwargs: object) -> None:
        tool_events = kwargs.get("tool_events")
        if isinstance(tool_events, list):
            events.extend(tool_events)

    plan = ReviewPlan(
        target=".",
        target_name="workspace",
        target_type="local",
        action=ReviewAction.REPO,
        roles=[],
        routing_mode="auto",
        user_requirements="review auth",
    )

    await maybe_prefetch_review_context(
        plan,
        {"_review_evidence_service": evidence_service},
        progress_callback=progress,
    )

    assert [event["phase"] for event in events] == ["start", "end"]
    assert {event["name"] for event in events} == {"review_prefetch"}
    assert events[-1]["result"] == "ok"


async def test_prefetch_reports_attempted_when_summary_is_empty() -> None:
    plan = ReviewPlan(
        target="https://github.com/test/repo",
        target_name="repo",
        target_type="github",
        action=ReviewAction.REPO,
        roles=[],
        routing_mode="auto",
    )

    result = await maybe_prefetch_review_context(
        plan,
        {"_review_evidence_service": _EmptyEvidenceService()},
    )

    assert result.attempted is True
    assert result.status == "no_summary"
    assert result.summary is None


async def test_diff_prefetch_preserves_filtered_patch_body() -> None:
    plan = ReviewPlan(
        target=".",
        target_name="workspace",
        target_type="local",
        action=ReviewAction.DIFF,
        roles=[],
        routing_mode="auto",
    )
    result = await maybe_prefetch_review_context(
        plan,
        {"_review_evidence_service": _PatchEvidenceService()},
    )

    assert "+token = value" in (result.summary or "")


def test_github_blob_url_becomes_scoped_review_plan() -> None:
    plan = build_review_plan(
        target="https://github.com/wkdddd/nanobot/blob/main/review-webui/index.html",
        user_content="审查",
        focus="performance",
        target_type="github",
        action="repo",
    )

    assert plan is not None
    assert plan.target_repo == "wkdddd/nanobot"
    assert plan.target_ref == "main"
    assert plan.target_subpath == "review-webui/index.html"
    assert plan.target_subpath_kind == "blob"


def test_local_file_target_becomes_file_scope(tmp_path, monkeypatch) -> None:
    target = tmp_path / "src" / "auth.py"
    target.parent.mkdir()
    target.write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(planner, "_find_git_root", lambda path: None)

    plan = build_review_plan(
        target=str(target),
        user_content="审查",
        target_type="local",
        action="repo",
    )

    assert plan is not None
    assert plan.local_scope is not None
    assert isinstance(plan.local_scope, LocalReviewScope)
    assert plan.local_scope.kind == "file"
    assert plan.local_scope.review_root == str(target.parent)
    assert plan.local_scope.scope_paths == ["auth.py"]


async def test_prefetch_passes_github_blob_scope_and_ref() -> None:
    evidence_service = _EvidenceService()
    plan = build_review_plan(
        target="https://github.com/wkdddd/nanobot/blob/main/review-webui/index.html",
        user_content="审查",
        target_type="github",
        action="repo",
    )

    assert plan is not None
    result = await maybe_prefetch_review_context(
        plan,
        {"_review_evidence_service": evidence_service},
    )

    assert result.status == "ok"
    assert evidence_service.calls
    call = evidence_service.calls[0]
    assert call["target_type"] == "github"
    assert call["repo"] == "wkdddd/nanobot"
    assert call["ref"] == "main"
    assert call["target_subpath"] == "review-webui/index.html"
    assert call["target_subpath_kind"] == "blob"


async def test_prefetch_builds_bundle_from_structured_units() -> None:
    plan = ReviewPlan(
        target=".",
        target_name="workspace",
        target_type="local",
        action=ReviewAction.REPO,
        roles=[],
        routing_mode="auto",
    )

    result = await maybe_prefetch_review_context(
        plan,
        {"_review_evidence_service": _StructuredEvidenceService(_structured_result())},
    )

    assert result.status == "ok"
    assert result.evidence is not None
    bundle = result.evidence
    assert [reference.id for reference in bundle.references] == ["ev-1", "ev-2"]
    main = bundle.references[0]
    assert (main.path, main.kind, main.token_count) == ("src/auth.py", "function", 8)
    assert main.preview.startswith("def login(token):")
    related = bundle.references[1]
    assert related.parent_id == "ev-1"
    assert related.is_related is True


async def test_prefetch_aggregates_skipped_files_per_path() -> None:
    events: list[dict[str, object]] = []

    async def progress(_content: str, **kwargs: object) -> None:
        tool_events = kwargs.get("tool_events")
        if isinstance(tool_events, list):
            events.extend(tool_events)

    plan = ReviewPlan(
        target=".",
        target_name="workspace",
        target_type="local",
        action=ReviewAction.REPO,
        roles=[],
        routing_mode="auto",
    )

    result = await maybe_prefetch_review_context(
        plan,
        {"_review_evidence_service": _StructuredEvidenceService(_structured_result())},
        progress_callback=progress,
    )

    assert result.evidence is not None
    # Two skipped chunks of the same file aggregate into one summary entry.
    skipped_by_file = result.evidence.skipped_by_file()
    assert set(skipped_by_file) == {"src/Legacy.java", "src/big.py"}
    java_summary = skipped_by_file["src/Legacy.java"]
    assert java_summary.whole_file is True
    assert java_summary.reasons == ("unsupported_code_type",)
    big_summary = skipped_by_file["src/big.py"]
    assert big_summary.whole_file is False
    assert big_summary.ranges == ((5, 9), (10, 20))
    assert set(big_summary.reasons) == {"token_limit_exceeded", "budget_exhausted"}

    end_event = events[-1]
    assert end_event["phase"] == "end"
    metadata = end_event["metadata"]
    assert metadata["skipped_units"] == 3
    assert len(metadata["skipped_files"]) == 2
    assert any("src/big.py" in entry for entry in metadata["skipped_files"])
    assert any("unreviewed lines 5-9, 10-20" in entry for entry in metadata["skipped_files"])
