from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from loguru import logger

from nanoreview.rag.review_service import (
    RepoReviewHit,
    RepositoryRAGOptions,
    RepositoryRAGRequest,
    RepositoryRAGService,
    rrf_merge,
)
from nanoreview.rag.utils import IndexedChunk, IndexedHit
from nanoreview.review.planning.evidence import ReviewEvidenceService
from nanoreview.review.planning.preprocessor import (
    CodeUnit,
    ProgrammaticEvidenceResult,
    ProgrammaticEvidenceService,
)
from nanoreview.review.source.utils import changed_lines_from_patch


class _LogSink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message: str) -> None:
        self.messages.append(message)

    @property
    def text(self) -> str:
        return "".join(self.messages)


class _FakeIndex:
    def __init__(self, broad_hits: list[IndexedHit], lane_hits: list[IndexedHit] | None = None) -> None:
        self.broad_hits = broad_hits
        self.lane_hits = lane_hits or []

    async def search(self, **_kwargs: object) -> list[IndexedHit]:
        return self.broad_hits

    def lexical_search(self, *_args: object, **_kwargs: object) -> list[IndexedHit]:
        return self.lane_hits


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_changed_lines_from_patch_fallback() -> None:
    patch = "@@ -1,2 +1,3 @@\n line\n+added\n-old\n+again"

    assert changed_lines_from_patch("src/app.py", patch) == [2, 3]


def test_rrf_merge_combines_ranked_lists() -> None:
    chunk_a = IndexedChunk("code_review", "a.py", 1, 2, "auth token", kind="text")
    chunk_b = IndexedChunk("code_review", "b.py", 1, 2, "config", kind="text")

    merged = rrf_merge(
        [
            ("bm25", [IndexedHit(chunk_a, 10, ["bm25"]), IndexedHit(chunk_b, 5, ["bm25"])]),
            ("risk", [IndexedHit(chunk_b, 9, ["risk"])]),
        ],
        limit=2,
    )

    assert merged[0].chunk.path == "b.py"
    assert "risk" in merged[0].reason


@pytest.mark.asyncio
async def test_review_evidence_requires_the_frozen_snapshot(tmp_path: Path) -> None:
    """Without a snapshot there is no review input: dispatch raises."""
    import pytest as _pytest

    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    with _pytest.raises(ValueError, match="frozen pull-request snapshot"):
        await service.dispatch(
            target_type="pull_request",
            action="diff",
            review_query="regression",
            frozen_diff=None,
        )


@pytest.mark.asyncio
async def test_local_diff_prefers_the_frozen_snapshot_over_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen diff is reviewed verbatim; the live worktree is never read."""
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))

    # The evidence service has no worktree reader at all: reviewing the frozen
    # patches is the only path.
    result = await service.dispatch(
        target_type="pull_request",
        action="diff",
        review_query="review",
        frozen_diff={
            "patches": {"src/auth.py": "@@ -1,2 +1,2 @@\n-old\n+frozen"},
            "skipped": {},
        },
    )

    assert "src/auth.py" in result
    assert "+frozen" in result
    assert service.last_changed_files == ["src/auth.py"]


@pytest.mark.asyncio
async def test_frozen_diff_evidence_reads_no_worktree(tmp_path: Path) -> None:
    """产物只由 frozen patch 决定；证据层没有任何工作树读取入口。"""
    target = tmp_path / "src" / "auth.py"
    target.parent.mkdir(parents=True)
    target.write_text("live_only_marker = 42\n", encoding="utf-8")
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))

    result = await service.dispatch(
        target_type="pull_request",
        action="diff",
        review_query="review",
        frozen_diff={
            "patches": {"src/auth.py": "@@ -1,2 +1,2 @@\n-old_marker = 1\n+frozen_token = 2"},
            "skipped": {},
        },
    )

    assert "+frozen_token = 2" in result
    assert "-old_marker = 1" in result
    assert "live_only_marker" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["repo", "", "folder", "diff "])
async def test_dispatch_rejects_non_diff_actions(tmp_path: Path, action: str) -> None:
    """预处理不再提供全仓读取入口：任何非 diff action 都明确拒绝。"""
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))

    with pytest.raises(ValueError, match="diff-only"):
        await service.dispatch(target_type="local", action=action, review_query="auth")

    assert service.last_result is None


@pytest.mark.asyncio
async def test_local_diff_keeps_an_oversized_patch_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    # However large a single-file patch is, it is one complete unit: the
    # evidence layer never slices or drops it, so budget fitting happens later.
    patch = "+" + ("x" * 40_000)
    result = await service.dispatch(
        target_type="pull_request",
        action="diff",
        review_query="review",
        frozen_diff={"patches": {"src/large.py": patch}, "skipped": {}},
    )

    assert service.last_result is not None
    assert service.last_result.skipped == []
    assert [(unit.path, unit.text) for unit in service.last_result.units] == [
        ("src/large.py", patch)
    ]
    # The rendered context is a bounded manifest for display only; the reviewer
    # reads ``unit.text`` through the evidence bundle excerpt.
    assert "src/large.py" in result


def test_repository_rag_prioritizes_chunks_overlapping_touched_lines() -> None:
    near = RepoReviewHit(path="src/app.py", score=1.0, start_line=20, end_line=30)
    overlapping = RepoReviewHit(path="src/app.py", score=1.0, start_line=3, end_line=8)
    other = RepoReviewHit(path="src/other.py", score=10.0, start_line=1, end_line=5)

    ranked = RepositoryRAGService.rank_touched_line_hits(
        [near, overlapping, other],
        {"src/app.py": [5]},
        limit=3,
    )

    assert ranked[0] is other
    assert ranked[1] is overlapping
    assert ranked[1].reason == ["diff-line-overlap", "diff-touched"]
    assert ranked[2] is near
    assert ranked[2].reason == ["diff-touched"]


@pytest.mark.asyncio
async def test_repository_rag_quality_filter_drops_duplicate_and_low_value_hits(tmp_path: Path) -> None:
    good = IndexedHit(
        IndexedChunk(
            "code_review",
            "src/auth.py",
            1,
            3,
            "def auth_token_check():\n    token = request.headers.get('token')\n    return token",
        ),
        3.0,
        ["bm25"],
    )
    duplicate = IndexedHit(good.chunk, 2.0, ["risk:security"])
    empty = IndexedHit(IndexedChunk("code_review", "src/empty.py", 1, 1, ""), 1.0, ["bm25"])
    short = IndexedHit(IndexedChunk("code_review", "src/short.py", 1, 1, "ok"), 1.0, ["bm25"])

    service = RepositoryRAGService(
        tmp_path,
        options=RepositoryRAGOptions(enable_chonkie=False, enable_rrf=False),
    )
    service.index = _FakeIndex([good, duplicate, empty, short])  # type: ignore[assignment]
    sink = _LogSink()
    handler_id = logger.add(sink, level="INFO", format="{message}")
    try:
        hits = await service.retrieve_hits(
            source_type="code_review",
            review_query="auth token",
            max_results=5,
        )
    finally:
        logger.remove(handler_id)

    assert [hit.path for hit in hits] == ["src/auth.py"]
    assert "raw_hits=4" in sink.text
    assert "kept_hits=1" in sink.text
    assert "dropped_hits=3" in sink.text
    assert "duplicate" in sink.text
    assert "empty_text" in sink.text
    assert "short_snippet" in sink.text


@pytest.mark.asyncio
async def test_repository_rag_quality_filter_keeps_semantic_hit_without_query_terms(tmp_path: Path) -> None:
    semantic = IndexedHit(
        IndexedChunk(
            "code_review",
            "src/session.py",
            1,
            3,
            "def verify_session():\n    cookie = load_signed_cookie()\n    return cookie.user_id",
        ),
        0.9,
        ["qdrant"],
    )

    service = RepositoryRAGService(
        tmp_path,
        options=RepositoryRAGOptions(enable_chonkie=False, enable_rrf=False),
    )
    service.index = _FakeIndex([semantic])  # type: ignore[assignment]

    hits = await service.retrieve_hits(
        source_type="code_review",
        review_query="jwt token",
        max_results=5,
    )

    assert [hit.path for hit in hits] == ["src/session.py"]
    assert hits[0].reason == ["qdrant", "weak-query-match"]


@pytest.mark.asyncio
async def test_repository_rag_quality_filter_returns_no_hits_when_all_low_value(tmp_path: Path) -> None:
    service = RepositoryRAGService(
        tmp_path,
        options=RepositoryRAGOptions(enable_chonkie=False, enable_rrf=False),
    )
    service.index = _FakeIndex(
        [
            IndexedHit(IndexedChunk("code_review", "src/empty.py", 1, 1, ""), 1.0, ["bm25"]),
            IndexedHit(IndexedChunk("code_review", "src/short.py", 1, 1, "x"), 1.0, ["bm25"]),
        ]
    )  # type: ignore[assignment]
    sink = _LogSink()
    handler_id = logger.add(sink, level="INFO", format="{message}")
    try:
        hits = await service.retrieve_hits(
            source_type="code_review",
            review_query="auth token",
            max_results=5,
        )
    finally:
        logger.remove(handler_id)

    assert hits == []
    assert "status=no_hits" in sink.text
    assert "raw_hits=2" in sink.text
    assert "kept_hits=0" in sink.text


def _unit(path: str = "src/auth.py", text: str = "token = 'x'\n") -> CodeUnit:
    return CodeUnit(
        path=path,
        kind="file",
        start_line=1,
        end_line=1,
        text=text,
        token_count=2,
        unit_id="ev-1",
    )


@pytest.mark.asyncio
async def test_dispatch_hands_the_whole_patch_to_the_preprocessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    patch = "@@ -1 +1 @@\n-old\n+new"
    captured: dict[str, object] = {}

    def fake_diff_units(
        patches: dict[str, str], *, review_query: str
    ) -> ProgrammaticEvidenceResult:
        captured["patches"] = dict(patches)
        captured["query"] = review_query
        return ProgrammaticEvidenceResult(
            units=[_unit(text="+new")], skipped=[], context="context"
        )

    monkeypatch.setattr(service.preprocessor, "diff_units", fake_diff_units)

    result = await service.dispatch(
        target_type="pull_request",
        action="diff",
        review_query="auth",
        frozen_diff={"patches": {"src/auth.py": patch}, "skipped": {}},
    )

    assert result == "context"
    # No window is threaded down: the diff reaches the preprocessor untouched.
    assert captured["patches"] == {"src/auth.py": patch}
    assert captured["query"] == "auth"


@pytest.mark.asyncio
async def test_repository_rag_logs_empty_query_and_no_terms(tmp_path: Path) -> None:
    service = RepositoryRAGService(tmp_path, options=RepositoryRAGOptions(enable_chonkie=False))
    sink = _LogSink()
    handler_id = logger.add(sink, level="INFO", format="{message}")
    try:
        await service.retrieve(
            RepositoryRAGRequest(
                source_type="code_review",
                review_query="",
                trace_id="empty-query",
            )
        )
        hits = await service.retrieve_hits(
            source_type="code_review",
            review_query="???",
            trace_id="no-terms",
        )
    finally:
        logger.remove(handler_id)

    assert hits == []
    assert "status=empty_query" in sink.text
    assert "status=no_terms" in sink.text
