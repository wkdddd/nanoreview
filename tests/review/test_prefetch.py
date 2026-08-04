from __future__ import annotations

import nanobot.review.planning.planner as planner

from nanobot.review.planning.planner import build_review_plan
from nanobot.review.planning.prefetch import maybe_prefetch_review_context
from nanobot.review.types import LocalReviewScope, ReviewAction, ReviewPlan


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


async def test_prefetch_calls_review_evidence_service_and_compacts_evidence() -> None:
    evidence_service = _EvidenceService()
    plan = ReviewPlan(
        target=".",
        target_name="workspace",
        target_type="local",
        action=ReviewAction.REPO,
        depth="full",
        roles=[],
        forced_focus=False,
        max_subagents=1,
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
        depth="full",
        roles=[],
        forced_focus=False,
        max_subagents=1,
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
        depth="full",
        roles=[],
        forced_focus=False,
        max_subagents=1,
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
        depth="full",
        roles=[],
        forced_focus=False,
        max_subagents=1,
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
