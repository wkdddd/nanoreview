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
    ProgrammaticEvidenceRequest,
    ProgrammaticEvidenceResult,
    ProgrammaticEvidenceService,
)
from nanoreview.review.source.utils import changed_lines_from_patch
from nanoreview.review.types import LocalReviewScope


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
async def test_review_evidence_uses_local_file_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("token = 'x'\n", encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_retrieve(request: ProgrammaticEvidenceRequest) -> ProgrammaticEvidenceResult:
        captured["request"] = request
        return ProgrammaticEvidenceResult(
            units=[
                CodeUnit(
                    path="src/auth.py",
                    kind="file",
                    start_line=1,
                    end_line=1,
                    text="token = 'x'\n",
                    token_count=2,
                    unit_id="ev-1",
                )
            ],
            skipped=[],
            context="context",
            mode="direct",
        )

    monkeypatch.setattr(service.preprocessor, "retrieve", fake_retrieve)

    result = await service.local_context(
        review_query="auth",
        max_results=5,
        include_tests=True,
        local_scope=LocalReviewScope(
            kind="file",
            review_root=str(tmp_path),
            scope_paths=["src/auth.py"],
            target_path="src/auth.py",
            reason="file_target",
        ),
    )

    assert result == "context"
    request = captured["request"]
    assert isinstance(request, ProgrammaticEvidenceRequest)
    assert request.review_query == "auth"
    assert [path.relative_to(tmp_path).as_posix() for path in request.files or []] == ["src/auth.py"]


@pytest.mark.asyncio
async def test_review_evidence_dispatches_local_changed_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    monkeypatch.setattr(
        service,
        "local_changed_patches",
        lambda _workspace=None: (
            {"src/auth.py": "@@ -1 +1 @@\n-old\n+new", "docs/readme.md": "+ignored"},
            {},
        ),
    )

    async def fail_retrieve(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("diff review must not call programmatic file retrieval")

    monkeypatch.setattr(service.preprocessor, "retrieve", fail_retrieve)

    result = await service.local_changed_context(
        review_query="regression",
        max_results=5,
        include_tests=True,
        local_scope=LocalReviewScope(
            kind="directory",
            review_root=str(tmp_path),
            scope_paths=["src"],
            target_path=".",
            reason="directory_target",
        ),
    )

    assert result.startswith("[Repository Review References")
    assert "src/auth.py" in result
    assert "docs/readme.md" not in result
    assert "+new" in result


@pytest.mark.asyncio
async def test_local_diff_prefers_the_frozen_snapshot_over_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen diff is reviewed verbatim; the live worktree is never read."""
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))

    def _must_not_read(_workspace=None):  # noqa: ANN001 - patched method
        raise AssertionError("frozen diff review must not re-read the worktree")

    monkeypatch.setattr(service, "local_changed_patches", _must_not_read)

    result = await service.local_changed_context(
        review_query="review",
        max_results=5,
        include_tests=True,
        frozen_diff={
            "patches": {"src/auth.py": "@@ -1,2 +1,2 @@\n-old\n+frozen"},
            "skipped": {},
        },
    )

    assert "src/auth.py" in result
    assert "+frozen" in result
    assert service.last_changed_files == ["src/auth.py"]


def test_local_changed_patches_uses_net_head_diff_not_staged_concat(
    tmp_path: Path,
) -> None:
    """A staged-then-edited file yields one HEAD->worktree patch.

    Concatenating the staged (``HEAD -> index``) and unstaged
    (``index -> worktree``) diffs would expose the intermediate staged snapshot
    (``STAGED``) alongside the final worktree state, so the reviewer would see
    two states of the same change.
    """
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    target = tmp_path / "app.py"
    target.write_text("one\nold two\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "init")

    target.write_text("one\nSTAGED\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")  # intermediate staged state
    target.write_text("one\nFINAL\n", encoding="utf-8")  # final worktree state

    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    patches, skipped = service.local_changed_patches()

    assert skipped == {}
    patch = patches["app.py"]
    assert "-old two" in patch
    assert "+FINAL" in patch
    # The intermediate staged state must never leak into the review.
    assert "STAGED" not in patch


@pytest.mark.asyncio
async def test_local_diff_skips_patch_over_diff_unit_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    # Above the fixed 8k per-file diff threshold and with no ``@@`` boundary to
    # split at, the single hunk is recorded as an unreviewed oversized unit.
    monkeypatch.setattr(
        service,
        "local_changed_patches",
        lambda _workspace=None: ({"src/large.py": "+" + ("x" * 40_000)}, {}),
    )

    result = await service.local_changed_context(
        review_query="review",
        max_results=5,
        include_tests=True,
        context_window_tokens=8,
    )

    assert service.last_result is not None
    assert service.last_result.units == []
    assert [(unit.path, unit.reason) for unit in service.last_result.skipped] == [
        ("src/large.py", "token_limit_exceeded")
    ]
    assert "src/large.py" not in result


def test_local_changed_summary_cli_fallback_maps_subdirectory_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without GitPython, diff paths map to the directory target root.

    Git reports ``diff --name-only`` paths relative to the worktree root, so a
    subdirectory target must have them converted (and the untracked listing,
    which is cwd-relative, must be queried from the same root) before the
    evidence service can attribute the change to the target.
    """
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "root_only.py").write_text("x = 1\n", encoding="utf-8")
    tracked = tmp_path / "pkg" / "app.py"
    tracked.write_text("one\nold two\n", encoding="utf-8")
    _git(tmp_path, "add", "pkg/app.py")
    _git(tmp_path, "add", "root_only.py")
    _git(tmp_path, "commit", "-m", "init")

    tracked.write_text("one\nnew two\n", encoding="utf-8")  # unstaged
    staged = tmp_path / "pkg" / "staged.py"
    staged.write_text("a\n", encoding="utf-8")
    _git(tmp_path, "add", "pkg/staged.py")  # staged
    untracked = tmp_path / "pkg" / "new_file.py"
    untracked.write_text("alpha\nbeta\n", encoding="utf-8")  # untracked

    # Force the CLI fallback by making ``import git`` fail.
    monkeypatch.setitem(sys.modules, "git", None)

    target = tmp_path / "pkg"
    service = ReviewEvidenceService(ProgrammaticEvidenceService(target))
    summary = service.local_changed_summary()

    # Every path is relative to the *target* (``pkg``), never the worktree.
    assert summary.files == ["app.py", "new_file.py", "staged.py"]
    assert summary.touched_lines["app.py"] == [2]
    assert summary.touched_lines["staged.py"] == [1]
    assert summary.touched_lines["new_file.py"] == [1, 2]
    assert "root_only.py" not in summary.files


def test_local_changed_summary_parses_staged_unstaged_and_untracked_lines(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "src").mkdir()
    tracked = tmp_path / "src" / "app.py"
    tracked.write_text("one\nold two\nthree\nold four\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "init")

    tracked.write_text("one\nnew two\nthree\nold four\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")
    tracked.write_text("one\nnew two\nthree\nnew four\n", encoding="utf-8")
    untracked = tmp_path / "src" / "new_file.py"
    untracked.write_text("alpha\nbeta\n", encoding="utf-8")

    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    summary = service.local_changed_summary()

    assert summary.files == ["src/app.py", "src/new_file.py"]
    assert summary.touched_lines["src/app.py"] == [2, 4]
    assert summary.touched_lines["src/new_file.py"] == [1, 2]


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


@pytest.mark.asyncio
async def test_review_evidence_local_context_logs_no_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    sink = _LogSink()
    handler_id = logger.add(sink, level="INFO", format="{message}")

    async def fake_retrieve(*_args: object, **_kwargs: object) -> ProgrammaticEvidenceResult:
        return ProgrammaticEvidenceResult(
            units=[],
            skipped=[],
            context="No relevant repository review references found.",
        )

    monkeypatch.setattr(service.preprocessor, "retrieve", fake_retrieve)
    try:
        result = await service.local_context(
            review_query="auth",
            max_results=5,
            include_tests=True,
        )
    finally:
        logger.remove(handler_id)

    assert result == "No relevant repository review references found."
    assert "review.evidence.local.done" in sink.text
    assert "status=no_units" in sink.text


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
async def test_dispatch_local_repo_forwards_context_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    captured: dict[str, object] = {}

    async def fake_retrieve(request: ProgrammaticEvidenceRequest) -> ProgrammaticEvidenceResult:
        captured["request"] = request
        return ProgrammaticEvidenceResult(units=[_unit()], skipped=[], context="context", mode="direct")

    monkeypatch.setattr(service.preprocessor, "retrieve", fake_retrieve)

    result = await service.dispatch(
        target_type="local",
        action="repo",
        review_query="auth",
        max_results=5,
        include_tests=True,
        context_window_tokens=32_768,
    )

    assert result == "context"
    request = captured["request"]
    assert isinstance(request, ProgrammaticEvidenceRequest)
    assert request.context_window_tokens == 32_768


@pytest.mark.asyncio
async def test_dispatch_local_diff_forwards_context_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    monkeypatch.setattr(
        service,
        "local_changed_patches",
        lambda _workspace=None: ({"src/auth.py": "@@ -1 +1 @@\n-old\n+new"}, {}),
    )
    captured: dict[str, object] = {}

    def fake_diff_units(
        patches: dict[str, str], *, review_query: str, context_window_tokens: int | None = None
    ) -> ProgrammaticEvidenceResult:
        captured["window"] = context_window_tokens
        return ProgrammaticEvidenceResult(
            units=[_unit(text="+new")], skipped=[], context="context", mode="direct"
        )

    monkeypatch.setattr(service.preprocessor, "diff_units", fake_diff_units)

    result = await service.dispatch(
        target_type="local",
        action="diff",
        review_query="auth",
        max_results=5,
        include_tests=True,
        context_window_tokens=16_384,
    )

    assert result == "context"
    assert captured["window"] == 16_384


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
