from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from loguru import logger

from nanoreview.rag.review_service import (
    REMOTE_SOURCE_TYPE,
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
from nanoreview.review.source.utils import (
    changed_lines_from_patch,
    parse_pr_target,
    parse_repo,
)
from nanoreview.review.types import GitHubDiffEvidence, LocalReviewScope


class _GitHub:
    def __init__(self, *, enable: bool = True) -> None:
        self.config = type("GitHubConfig", (), {"enable": enable, "max_index_files": 400})()


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


def test_parse_github_repo_from_url_and_owner_repo() -> None:
    assert parse_repo("https://github.com/test/repo.") == ("test", "repo")
    assert parse_repo("test/repo.git") == ("test", "repo")


def test_parse_pr_target_from_url() -> None:
    assert parse_pr_target("https://github.com/test/repo/pull/42") == ("test/repo", 42)
    assert parse_pr_target("test/repo") == (None, None)


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
async def test_local_diff_skips_single_patch_over_context_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    monkeypatch.setattr(
        service,
        "local_changed_patches",
        lambda _workspace=None: ({"src/large.py": "+" + ("x" * 4_000)}, {}),
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
async def test_dispatch_github_repo_forwards_context_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    captured: dict[str, object] = {}

    async def fake_fetch_text_files(*_args: object, **_kwargs: object):
        return "test/repo@main", {"src/auth.py": "token = 'x'\n"}

    async def fake_retrieve(request: ProgrammaticEvidenceRequest) -> ProgrammaticEvidenceResult:
        captured["request"] = request
        return ProgrammaticEvidenceResult(units=[_unit()], skipped=[], context="context", mode="direct")

    monkeypatch.setattr(service.github, "fetch_text_files", fake_fetch_text_files)
    monkeypatch.setattr(service.preprocessor, "retrieve", fake_retrieve)

    result = await service.dispatch(
        target_type="github",
        action="repo",
        repo="test/repo",
        review_query="auth",
        max_results=5,
        include_tests=True,
        trace_id="trace-1",
        context_window_tokens=131_072,
    )

    assert result == "context"
    request = captured["request"]
    assert isinstance(request, ProgrammaticEvidenceRequest)
    assert request.context_window_tokens == 131_072


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
async def test_dispatch_github_diff_forwards_context_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    captured: dict[str, object] = {}

    async def fake_fetch_pr_files(*_args: object, **_kwargs: object):
        return GitHubDiffEvidence(
            snapshot="test/repo#42",
            head_sha="abc123",
            patches={"src/auth.py": "@@ -1 +1 @@\n-old\n+new"},
            changed_files=["src/auth.py"],
            touched_lines={"src/auth.py": [1]},
        )

    def fake_diff_units(
        patches: dict[str, str], *, review_query: str, context_window_tokens: int | None = None
    ) -> ProgrammaticEvidenceResult:
        captured["window"] = context_window_tokens
        return ProgrammaticEvidenceResult(
            units=[_unit(text="+new")], skipped=[], context="context", mode="direct"
        )

    monkeypatch.setattr(service.github, "fetch_pr_files", fake_fetch_pr_files)
    monkeypatch.setattr(service.preprocessor, "diff_units", fake_diff_units)

    result = await service.dispatch(
        target_type="github",
        action="diff",
        repo="test/repo",
        pr_number=42,
        review_query="auth",
        max_results=5,
        include_tests=True,
        trace_id="trace-1",
        context_window_tokens=65_536,
    )

    assert result == "context"
    assert captured["window"] == 65_536


@pytest.mark.asyncio
async def test_review_evidence_github_diff_uses_patches_without_rag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ReviewEvidenceService(ProgrammaticEvidenceService(tmp_path))
    sink = _LogSink()
    handler_id = logger.add(sink, level="INFO", format="{message}")

    async def fake_fetch_pr_files(*_args: object, **_kwargs: object):
        return GitHubDiffEvidence(
            snapshot="test/repo#42",
            head_sha="abc123",
            patches={"src/auth.py": "@@ -1 +1 @@\n-old\n+def auth(): pass"},
            changed_files=["src/auth.py", "src/big.py"],
            touched_lines={"src/auth.py": [1]},
            patch_unavailable_files={"src/big.py": "patch_unavailable"},
        )

    async def fail_snapshot_context(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("diff review must not create a snapshot")

    monkeypatch.setattr(service.github, "fetch_pr_files", fake_fetch_pr_files)
    monkeypatch.setattr(service, "retrieve_snapshot_context", fail_snapshot_context)
    try:
        result = await service.github_diff_context(
            repo="test/repo",
            pr_number=42,
            review_query="auth",
            max_results=5,
            include_tests=True,
            trace_id="trace-1",
        )
    finally:
        logger.remove(handler_id)

    assert "def auth(): pass" in result
    assert service.last_result is not None
    assert [unit.path for unit in service.last_result.units] == ["src/auth.py"]
    assert [(unit.path, unit.reason) for unit in service.last_result.skipped] == [
        ("src/big.py", "patch_unavailable")
    ]
    assert "review.evidence.github_diff.done" in sink.text
    assert "status=success" in sink.text
    assert "trace_id=trace-1" in sink.text


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


def test_repository_rag_snapshot_cache_is_scoped_by_file_set(tmp_path: Path) -> None:
    service = RepositoryRAGService(tmp_path, options=RepositoryRAGOptions(enable_chonkie=False))

    broad = service.write_snapshot(
        "owner/repo@main",
        {
            "review-webui/index.html": "<html></html>",
            "review-webui/src/App.tsx": "export function App() {}",
        },
    )
    narrow = service.write_snapshot(
        "owner/repo@main",
        {"review-webui/index.html": "<html></html>"},
    )
    narrow_again = service.write_snapshot(
        "owner/repo@main",
        {"review-webui/index.html": "<html></html>"},
    )

    assert broad != narrow
    assert narrow == narrow_again
    assert [path.relative_to(narrow).as_posix() for path in service.iter_candidate_files(narrow)] == [
        "review-webui/index.html"
    ]
    manifest = (narrow / ".nanoreview_snapshot.json").read_text(encoding="utf-8")
    assert '"scope_digest"' in manifest
    assert '"files_count": 1' in manifest


def test_repository_rag_snapshot_sync_prunes_previous_scope(tmp_path: Path) -> None:
    service = RepositoryRAGService(tmp_path, options=RepositoryRAGOptions(enable_chonkie=False))
    broad = service.write_snapshot(
        "owner/repo@main",
        {
            "review-webui/index.html": "<html></html>",
            "review-webui/src/App.tsx": "export function App() {}",
        },
    )
    narrow = service.write_snapshot(
        "owner/repo@main",
        {"review-webui/index.html": "<html></html>"},
    )

    service.sync_files(
        source_type=REMOTE_SOURCE_TYPE,
        files=list(service.iter_candidate_files(broad)),
        trace_id="broad",
    )
    assert service.index.count(REMOTE_SOURCE_TYPE) == 2

    service.sync_files(
        source_type=REMOTE_SOURCE_TYPE,
        files=list(service.iter_candidate_files(narrow)),
        trace_id="narrow",
    )

    chunk_paths = [chunk.path for chunk in service.index.list_chunks(REMOTE_SOURCE_TYPE)]
    assert len(chunk_paths) == 1
    assert chunk_paths[0].endswith("review-webui/index.html")
