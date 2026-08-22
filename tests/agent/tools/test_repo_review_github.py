from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from loguru import logger

from nanoreview.agent.tools.context import (
    RequestContext,
    reset_current_request_context,
    set_current_request_context,
)
from nanoreview.agent.tools.filesystem import ReadFileTool
from nanoreview.agent.tools.github_review import GitHubReviewTool
from nanoreview.agent.tools.local_review import LocalReviewTool
from nanoreview.config import loader
from nanoreview.config.schema import Config
from nanoreview.rag.review_service import rrf_merge
from nanoreview.rag.utils import IndexedChunk, IndexedHit
from nanoreview.review.source.github import GitHubRepoConfig
from nanoreview.review.source.utils import changed_lines_from_patch, parse_pr_target, parse_repo
from nanoreview.review.types import ReviewMetaKey


class _LogSink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message: str) -> None:
        self.messages.append(message)

    @property
    def text(self) -> str:
        return "".join(self.messages)


def test_repo_review_github_token_loads_from_tools_config() -> None:
    config = Config.model_validate(
        {
            "tools": {
                "githubRepo": {
                    "token": "ghp_config_token",
                }
            }
        }
    )

    assert config.tools.github_repo.token == "ghp_config_token"


def test_parse_github_repo_from_url_and_owner_repo() -> None:
    assert parse_repo("https://github.com/test/repo.") == ("test", "repo")
    assert parse_repo("test/repo.git") == ("test", "repo")


@pytest.mark.asyncio
async def test_github_review_requires_target_repo(tmp_path: Path) -> None:
    tool = GitHubReviewTool(workspace=tmp_path)

    result = await tool.execute(action="repo")

    assert result == "Error: target_repo is required for github_review."


@pytest.mark.asyncio
async def test_github_review_respects_disabled_config(tmp_path: Path) -> None:
    tool = GitHubReviewTool(
        workspace=tmp_path,
        github_config=GitHubRepoConfig(enable=False),
    )

    result = await tool.execute(action="repo", target_repo="test/repo")

    assert result == "Error: GitHub repository access is disabled by tools.githubRepo.enable."


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["full_repo", "pr_diff", "local_changed", "targeted_files"])
async def test_local_review_rejects_unknown_or_old_actions(tmp_path: Path, action: str) -> None:
    tool = LocalReviewTool(workspace=tmp_path)

    result = await tool.execute(action=action, review_query="auth")

    assert f"unknown local_review action '{action}'" in result


@pytest.mark.asyncio
async def test_local_review_meta_tree_and_file_actions(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login(token):\n    return token\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    tool = LocalReviewTool(workspace=tmp_path)

    meta = await tool.execute(action="meta")
    tree = await tool.execute(action="tree", tree_pattern="*.py")
    content = await tool.execute(action="file", repo_path="src/auth.py")

    assert "Repository:" in meta
    assert "Text files:" in meta
    assert "src/auth.py" in tree
    assert "def login" in content


@pytest.mark.asyncio
async def test_local_review_file_supports_pagination_arguments(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 11)),
        encoding="utf-8",
    )
    tool = LocalReviewTool(workspace=tmp_path)

    result = await tool.execute(
        action="file",
        repo_path="src/app.py",
        offset=4,
        limit=3,
    )

    assert "File: src/app.py" in result
    assert "4| line 4" in result
    assert "6| line 6" in result
    assert "7| line 7" not in result
    assert "offset=7, limit=3" in result


@pytest.mark.asyncio
async def test_local_review_file_blocks_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_local_review.txt"
    outside.write_text("secret\n", encoding="utf-8")
    tool = LocalReviewTool(workspace=tmp_path)

    result = await tool.execute(action="file", repo_path=str(outside))

    assert result.startswith("Error:")
    assert "outside workspace" in result


@pytest.mark.asyncio
async def test_github_review_reader_actions_pass_aligned_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GitHubReviewTool(workspace=tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_execute(**kwargs: object) -> str:
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(tool.github, "execute", fake_execute)

    result = await tool.execute(
        action="file",
        target_repo="test/repo",
        repo_path="README.md",
        ref="main",
        tree_pattern="*.md",
        tree_limit=25,
    )

    assert result == "ok"
    assert calls == [
        {
            "action": "file",
            "repo": "test/repo",
            "path": "README.md",
            "ref": "main",
            "pattern": "*.md",
            "max_entries": 25,
            "pr_number": 0,
            "offset": 1,
            "limit": 200,
        }
    ]


@pytest.mark.asyncio
async def test_github_review_file_passes_pagination_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GitHubReviewTool(workspace=tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_execute(**kwargs: object) -> str:
        calls.append(kwargs)
        return "page"

    monkeypatch.setattr(tool.github, "execute", fake_execute)

    result = await tool.execute(
        action="file",
        target_repo="test/repo",
        repo_path="src/app.py",
        offset=401,
        limit=50,
    )

    assert result == "page"
    assert calls[0]["offset"] == 401
    assert calls[0]["limit"] == 50


@pytest.mark.asyncio
async def test_github_reader_file_returns_remote_line_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = GitHubReviewTool(workspace=tmp_path)
    content = "\n".join(f"line {i}" for i in range(1, 11))

    async def fake_api_get(*_args, **_kwargs):
        import base64

        return {
            "encoding": "base64",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "size": len(content),
            "sha": "abcdef123456",
        }

    monkeypatch.setattr(tool.github, "_api_get", fake_api_get)

    result = await tool.github.execute(
        action="file",
        repo="test/repo",
        path="src/app.py",
        offset=4,
        limit=3,
    )

    assert "GitHub File: test/repo/src/app.py" in result
    assert "4| line 4" in result
    assert "6| line 6" in result
    assert "7| line 7" not in result
    assert "offset=7, limit=3" in result


@pytest.mark.asyncio
async def test_github_review_blocks_duplicate_repo_after_prefetch(tmp_path: Path) -> None:
    token = set_current_request_context(
        RequestContext(
            channel="websocket",
            chat_id="review",
            metadata={
                ReviewMetaKey.TARGET_TYPE: "github",
                ReviewMetaKey.TARGET: "https://github.com/test/repo",
                ReviewMetaKey.GITHUB_PREFETCH_READY: True,
            },
        )
    )
    try:
        tool = GitHubReviewTool(workspace=tmp_path)

        result = await tool.execute(action="repo", target_repo="test/repo")
    finally:
        reset_current_request_context(token)

    assert result.startswith("Error:")
    assert "already prefetched" in result


@pytest.mark.asyncio
async def test_github_review_blocks_rag_repo_action_during_diff_review(tmp_path: Path) -> None:
    token = set_current_request_context(
        RequestContext(
            channel="websocket",
            chat_id="review",
            metadata={ReviewMetaKey.ACTION: "diff"},
        )
    )
    try:
        result = await GitHubReviewTool(workspace=tmp_path).execute(action="repo", target_repo="test/repo")
    finally:
        reset_current_request_context(token)

    assert result.startswith("Error:")
    assert "unavailable during diff review" in result


@pytest.mark.asyncio
async def test_local_review_blocks_rag_repo_action_during_diff_review(tmp_path: Path) -> None:
    token = set_current_request_context(
        RequestContext(
            channel="websocket",
            chat_id="review",
            metadata={ReviewMetaKey.ACTION: "diff"},
        )
    )
    try:
        result = await LocalReviewTool(workspace=tmp_path).execute(action="repo")
    finally:
        reset_current_request_context(token)

    assert result.startswith("Error:")
    assert "unavailable during diff review" in result


@pytest.mark.asyncio
async def test_github_diff_file_reads_use_pr_head_ref(tmp_path: Path) -> None:
    token = set_current_request_context(
        RequestContext(
            channel="websocket",
            chat_id="review",
            metadata={
                ReviewMetaKey.ACTION: "diff",
                ReviewMetaKey.GITHUB_PR_HEAD_REF: "head-sha",
            },
        )
    )
    calls: list[dict[str, object]] = []
    try:
        tool = GitHubReviewTool(workspace=tmp_path)

        async def fake_execute(**kwargs: object) -> str:
            calls.append(kwargs)
            return "file"

        tool.github.execute = fake_execute  # type: ignore[method-assign]
        result = await tool.execute(
            action="file",
            target_repo="owner/repo",
            repo_path="src/auth.py",
        )
    finally:
        reset_current_request_context(token)

    assert result == "file"
    assert calls[0]["ref"] == "head-sha"


@pytest.mark.asyncio
async def test_local_review_blocked_for_github_review_context(tmp_path: Path) -> None:
    token = set_current_request_context(
        RequestContext(
            channel="websocket",
            chat_id="review",
            metadata={ReviewMetaKey.TARGET_TYPE: "github"},
        )
    )
    try:
        tool = LocalReviewTool(workspace=tmp_path)

        result = await tool.execute(action="meta", target=str(tmp_path))
    finally:
        reset_current_request_context(token)

    assert result.startswith("Error:")
    assert "disabled for GitHub review targets" in result


@pytest.mark.asyncio
async def test_github_review_blocked_for_local_review_context(tmp_path: Path) -> None:
    token = set_current_request_context(
        RequestContext(
            channel="websocket",
            chat_id="review",
            metadata={ReviewMetaKey.TARGET_TYPE: "local"},
        )
    )
    try:
        tool = GitHubReviewTool(workspace=tmp_path)

        result = await tool.execute(action="meta", target_repo="owner/repo")
    finally:
        reset_current_request_context(token)

    assert result.startswith("Error:")
    assert "disabled for local review targets" in result


@pytest.mark.asyncio
async def test_read_file_blocks_workspace_files_for_github_review_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "_current_config_path", tmp_path / "config.json")
    target = tmp_path / ".nanoreview" / "tool-results" / "session" / "call.txt"
    target.parent.mkdir(parents=True)
    target.write_text("remote output cache\n", encoding="utf-8")
    token = set_current_request_context(
        RequestContext(
            channel="websocket",
            chat_id="review",
            metadata={ReviewMetaKey.TARGET_TYPE: "github"},
        )
    )
    try:
        tool = ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path)

        result = await tool.execute(path=str(target))
    finally:
        reset_current_request_context(token)

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "github_review(action='file'" in result


@pytest.mark.asyncio
async def test_github_review_pr_url_defaults_to_diff(tmp_path: Path) -> None:
    tool = GitHubReviewTool(workspace=tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_dispatch(**kwargs: object) -> str:
        calls.append(kwargs)
        return "diff context"

    tool.evidence_service.dispatch = fake_dispatch  # type: ignore[method-assign]

    result = await tool.execute(target="https://github.com/test/repo/pull/42")

    assert result == "diff context"
    assert calls[0]["target_type"] == "github"
    assert calls[0]["action"] == "diff"
    assert calls[0]["repo"] == "test/repo"
    assert calls[0]["pr_number"] == 42


def test_repo_review_github_token_prefers_workspace_config_json(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        '{"tools":{"githubRepo":{"token":"workspace-token"}}}',
        encoding="utf-8",
    )
    tool = GitHubReviewTool(
        workspace=tmp_path,
        github_config=GitHubRepoConfig(token="runtime-token"),
    )

    assert tool.github._workspace_config_token() == "workspace-token"


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
async def test_github_api_success_logs_structured_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GitHubReviewTool(workspace=tmp_path)

    async def fake_token() -> None:
        return None

    monkeypatch.setattr(tool.github, "_get_token", fake_token)
    sink = _LogSink()
    handler_id = logger.add(sink, level="DEBUG", format="{message}")

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, *args, **kwargs):
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    try:
        result = await tool.github._api_get("repos/test/repo", trace_id="trace-gh")
    finally:
        logger.remove(handler_id)

    assert result == {"ok": True}
    assert "repo_review.github.api.success" in sink.text
    assert "trace_id=trace-gh" in sink.text
    assert "status=success" in sink.text
    assert "status_code=200" in sink.text
