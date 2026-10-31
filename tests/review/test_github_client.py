"""GitHub client contract tests (no network: httpx MockTransport)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from nanoreview.review.input.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    parse_repository_target,
)

_TOKEN = "ghp_super_secret_value"


def _client(handler: "callable") -> GitHubClient:
    return GitHubClient(
        token=_TOKEN,
        transport=httpx.MockTransport(handler),
        api_base="https://api.github.com",
    )


def _json(payload: object, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **(headers or {})},
    )


def test_parse_repository_target_accepts_url_and_slug() -> None:
    assert parse_repository_target("https://github.com/acme/widgets") == ("acme", "widgets")
    assert parse_repository_target("https://github.com/acme/widgets.git") == ("acme", "widgets")
    assert parse_repository_target("acme/widgets") == ("acme", "widgets")


@pytest.mark.parametrize(
    "value",
    ["", "https://gitlab.com/acme/widgets", "widgets", "/tmp/repo", "acme/"],
)
def test_parse_repository_target_rejects_non_github(value: str) -> None:
    with pytest.raises(GitHubError):
        parse_repository_target(value)


@pytest.mark.asyncio
async def test_resolve_repository_uses_server_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["path"] = request.url.path
        return _json(
            {
                "name": "widgets",
                "owner": {"login": "acme"},
                "html_url": "https://github.com/acme/widgets",
                "default_branch": "main",
                "private": True,
            }
        )

    client = _client(handler)
    try:
        repo = await client.resolve_repository("https://github.com/acme/widgets")
    finally:
        await client.aclose()

    assert repo.full_name == "acme/widgets"
    assert repo.private is True
    assert seen["path"] == "/repos/acme/widgets"
    assert seen["auth"] == f"Bearer {_TOKEN}"


@pytest.mark.asyncio
async def test_list_pull_requests_forwards_state_and_pagination() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({key: value for key, value in request.url.params.items()})
        return _json(
            [
                {
                    "number": 5,
                    "title": "Fix",
                    "state": "open",
                    "draft": False,
                    "user": {"login": "octocat"},
                    "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "acme/widgets"}},
                    "head": {"ref": "fix", "sha": "a" * 40, "repo": {"full_name": "acme/widgets"}},
                    "html_url": "https://github.com/acme/widgets/pull/5",
                    "updated_at": "2026-10-01T10:00:00Z",
                }
            ]
        )

    client = _client(handler)
    try:
        pulls = await client.list_pull_requests("acme", "widgets", state="open", page=2, page_size=30)
    finally:
        await client.aclose()

    assert pulls[0].number == 5
    assert pulls[0].base_sha == "b" * 40
    assert pulls[0].head_sha == "a" * 40
    assert seen["state"] == "open"
    assert seen["page"] == "2"
    assert seen["per_page"] == "30"


@pytest.mark.asyncio
async def test_pull_request_files_follow_pagination() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        count = 100 if page == 1 else 2
        return _json(
            [
                {
                    "filename": f"src/f{page}_{index}.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "@@ -1 +1 @@",
                }
                for index in range(count)
            ]
        )

    client = _client(handler)
    try:
        files = await client.list_pull_request_files("acme", "widgets", 5)
    finally:
        await client.aclose()

    assert pages == [1, 2]
    assert len(files) == 102


@pytest.mark.asyncio
async def test_get_file_reads_the_pinned_ref_and_decodes_text() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode().split("?")[0]
        seen["ref"] = request.url.params["ref"]
        return _json(
            {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode("héllo\nworld".encode()).decode(),
                "sha": "c" * 40,
            }
        )

    client = _client(handler)
    try:
        content = await client.get_file("acme", "widgets", "src/dir/a b.py", "f" * 40)
    finally:
        await client.aclose()

    assert content.text == "héllo\nworld"
    assert seen["ref"] == "f" * 40
    assert seen["path"] == "/repos/acme/widgets/contents/src/dir/a%20b.py"


@pytest.mark.asyncio
async def test_get_file_reports_oversized_blob_without_fetching() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "This API returns blobs up to 1 MB in size"})

    client = _client(handler)
    try:
        content = await client.get_file("acme", "widgets", "big.bin", "f" * 40)
    finally:
        await client.aclose()

    assert content.text is None
    assert content.too_large is True


@pytest.mark.asyncio
async def test_auth_and_rate_limit_errors_never_leak_the_token() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = _client(unauthorized)
    try:
        with pytest.raises(GitHubAuthError) as error:
            await client.get_pull_request("acme", "widgets", 1)
    finally:
        await client.aclose()
    assert _TOKEN not in str(error.value)

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
        )

    client = _client(limited)
    try:
        with pytest.raises(GitHubRateLimitError):
            await client.get_pull_request("acme", "widgets", 1)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_not_found_is_a_stable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = _client(handler)
    try:
        with pytest.raises(GitHubNotFoundError):
            await client.resolve_repository("acme/missing")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_merge_base_requires_a_value() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return _json({"status": "diverged"})

    client = _client(missing)
    try:
        with pytest.raises(GitHubUnavailableError):
            await client.get_merge_base("acme", "widgets", "a" * 40, "b" * 40)
    finally:
        await client.aclose()

    def present(request: httpx.Request) -> httpx.Response:
        return _json({"merge_base_commit": {"sha": "e" * 40}})

    client = _client(present)
    try:
        assert await client.get_merge_base("acme", "widgets", "a" * 40, "b" * 40) == "e" * 40
    finally:
        await client.aclose()
