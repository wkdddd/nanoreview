"""GitHub REST client for pull-request-only review.

Every read is pinned to a commit SHA supplied by the caller. The client never
falls back to a branch name, never clones, and never pages into a different
ref, so a force-push after admission cannot change the reviewed content.

Credentials are server-side only: the token lives on the client instance and is
never returned in errors, records or logs.
"""

from __future__ import annotations

import base64
import os
import re
from urllib.parse import quote
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from loguru import logger

_GITHUB_TOKEN_ENV = "NANOREVIEW_GITHUB_TOKEN"
_REPO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_REPO_SLUG_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?$")


class GitHubError(RuntimeError):
    """Base class for GitHub failures carrying a stable code."""

    code = "github_error"
    status = 502

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


class GitHubAuthError(GitHubError):
    code = "github_auth_failed"
    status = 502


class GitHubNotFoundError(GitHubError):
    code = "github_not_found"
    status = 404


class GitHubRateLimitError(GitHubError):
    code = "github_rate_limited"
    status = 429


class GitHubFileTooLargeError(GitHubError):
    """The contents API refuses blobs above 1 MB."""

    code = "github_file_too_large"
    status = 413


class GitHubUnavailableError(GitHubError):
    code = "github_unavailable"
    status = 502


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    owner: str
    name: str
    url: str
    default_branch: str | None
    private: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class GitHubPullRequest:
    number: int
    title: str
    state: str
    draft: bool
    author: str | None
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    base_repo: str
    head_repo: str
    html_url: str | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class GitHubPullRequestFile:
    path: str
    previous_path: str | None
    status: str
    additions: int
    deletions: int
    patch: str | None


@dataclass(frozen=True, slots=True)
class GitHubTreeEntry:
    path: str
    entry_type: str
    size: int | None
    sha: str


@dataclass(frozen=True, slots=True)
class GitHubFileContent:
    path: str
    sha: str | None
    text: str | None
    binary: bool = False
    too_large: bool = False

    @property
    def available(self) -> bool:
        return self.text is not None


def resolve_token(configured: str | None) -> str | None:
    return (os.environ.get(_GITHUB_TOKEN_ENV) or configured or "").strip() or None


def parse_repository_target(target: str) -> tuple[str, str]:
    """Accept a github.com URL or ``owner/name``; reject everything else."""
    value = (target or "").strip()
    match = _REPO_URL_RE.match(value) or _REPO_SLUG_RE.match(value)
    if match is None:
        raise GitHubError(
            "Review target must be a GitHub repository URL "
            "(https://github.com/owner/name) or owner/name.",
            status=400,
        )
    return match.group("owner"), match.group("name")


class GitHubClient:
    """Thin async wrapper over the GitHub REST endpoints review needs."""

    def __init__(
        self,
        *,
        token: str | None,
        api_base: str = "https://api.github.com",
        timeout_seconds: float = 30.0,
        max_pages: int = 50,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = resolve_token(token)
        self._api_base = api_base.rstrip("/")
        self._max_pages = max_pages
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nanoreview",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._client = httpx.AsyncClient(
            base_url=self._api_base,
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_config(cls, config: Any) -> "GitHubClient":
        github = getattr(config, "github", None)
        return cls(
            token=getattr(github, "token", None),
            api_base=getattr(github, "api_base", "https://api.github.com"),
            timeout_seconds=float(getattr(github, "timeout_seconds", 30.0)),
            max_pages=int(getattr(github, "max_pages", 50)),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- HTTP ---------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise GitHubUnavailableError(f"GitHub request timed out: {path}") from exc
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(
                f"GitHub request failed: {path} ({exc.__class__.__name__})"
            ) from exc
        if response.status_code == 401:
            raise GitHubAuthError("GitHub rejected the configured token")
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset", "unknown")
            raise GitHubRateLimitError(f"GitHub rate limit exhausted until {reset}")
        if response.status_code == 404:
            raise GitHubNotFoundError(f"GitHub resource not found: {path}")
        if response.status_code == 403:
            body = response.text.lower()
            if "too large" in body or "larger than" in body or "blobs up to" in body:
                raise GitHubFileTooLargeError(f"GitHub file is too large to read: {path}")
            raise GitHubAuthError(
                f"GitHub denied access to {path}; check the server token's repository scope"
            )
        if response.status_code >= 400:
            raise GitHubUnavailableError(
                f"GitHub returned {response.status_code} for {path}"
            )
        return response

    async def _get_json(self, path: str, **kwargs: Any) -> Any:
        response = await self._request("GET", path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubUnavailableError(f"GitHub returned invalid JSON for {path}") from exc

    async def _paginate(self, path: str, *, page_size: int = 100, max_items: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, self._max_pages + 1):
            payload = await self._get_json(
                path, params={"per_page": page_size, "page": page}
            )
            if not isinstance(payload, list):
                raise GitHubUnavailableError(f"Unexpected paginated payload for {path}")
            items.extend(payload)
            if len(payload) < page_size:
                break
            if max_items is not None and len(items) >= max_items:
                break
        return items

    # -- repositories and pull requests -------------------------------------

    async def resolve_repository(self, target: str) -> GitHubRepository:
        owner, name = parse_repository_target(target)
        payload = await self._get_json(f"/repos/{owner}/{name}")
        logger.info("review.github.repository.resolved repo={}/{}", owner, name)
        return _repository(payload)

    async def get_pull_request(
        self, owner: str, name: str, number: int
    ) -> GitHubPullRequest:
        payload = await self._get_json(f"/repos/{owner}/{name}/pulls/{int(number)}")
        return _pull_request(payload)

    async def list_pull_requests(
        self,
        owner: str,
        name: str,
        *,
        state: str = "open",
        page: int = 1,
        page_size: int = 30,
    ) -> list[GitHubPullRequest]:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        payload = await self._get_json(
            f"/repos/{owner}/{name}/pulls",
            params={
                "state": state,
                "per_page": page_size,
                "page": page,
                "sort": "updated",
                "direction": "desc",
            },
        )
        if not isinstance(payload, list):
            raise GitHubUnavailableError("Unexpected pull request list payload")
        return [_pull_request(item) for item in payload]

    async def list_pull_request_files(
        self, owner: str, name: str, number: int
    ) -> list[GitHubPullRequestFile]:
        """List every file of a PR, following pagination (GitHub caps at 3000)."""
        payload = await self._paginate(f"/repos/{owner}/{name}/pulls/{int(number)}/files")
        files: list[GitHubPullRequestFile] = []
        for item in payload:
            path = str(item.get("filename") or "")
            if not path:
                continue
            files.append(
                GitHubPullRequestFile(
                    path=path,
                    previous_path=item.get("previous_filename"),
                    status=str(item.get("status") or "modified"),
                    additions=int(item.get("additions") or 0),
                    deletions=int(item.get("deletions") or 0),
                    patch=item.get("patch"),
                )
            )
        return files

    async def get_merge_base(self, owner: str, name: str, base_sha: str, head_sha: str) -> str:
        """Return the merge-base commit SHA between two pinned commits.

        Uses the compare endpoint, which reports ``merge_base_commit`` even when
        the file list is truncated. A compare that omits it is a hard failure:
        reading the base side at the wrong commit would mislabel unchanged code.
        """
        payload = await self._get_json(f"/repos/{owner}/{name}/compare/{base_sha}...{head_sha}")
        merge_base = (payload.get("merge_base_commit") or {}).get("sha")
        if not merge_base:
            raise GitHubUnavailableError(
                "GitHub compare response did not include a merge base commit"
            )
        return str(merge_base)

    # -- pinned content -----------------------------------------------------

    async def list_tree(self, owner: str, name: str, ref: str) -> tuple[list[GitHubTreeEntry], bool]:
        """List the tree at *ref*; the boolean reports GitHub's truncation flag."""
        payload = await self._get_json(
            f"/repos/{owner}/{name}/git/trees/{ref}", params={"recursive": "1"}
        )
        entries: list[GitHubTreeEntry] = []
        for item in payload.get("tree") or ():
            path = str(item.get("path") or "")
            if not path:
                continue
            entries.append(
                GitHubTreeEntry(
                    path=path,
                    entry_type=str(item.get("type") or ""),
                    size=item.get("size"),
                    sha=str(item.get("sha") or ""),
                )
            )
        return entries, bool(payload.get("truncated"))

    async def get_directory(
        self, owner: str, name: str, path: str, ref: str
    ) -> list[GitHubTreeEntry]:
        """List one directory at *ref*.

        Used to recover subtrees when the recursive tree response was
        truncated, so a scoped search still sees the selected directory.
        """
        encoded_path = "/".join(quote(segment, safe="") for segment in path.split("/"))
        payload = await self._get_json(
            f"/repos/{owner}/{name}/contents/{encoded_path}", params={"ref": ref}
        )
        if not isinstance(payload, list):
            return []
        entries: list[GitHubTreeEntry] = []
        for item in payload:
            entry_path = str(item.get("path") or "")
            if not entry_path:
                continue
            entries.append(
                GitHubTreeEntry(
                    path=entry_path,
                    entry_type=str(item.get("type") or ""),
                    size=item.get("size"),
                    sha=str(item.get("sha") or ""),
                )
            )
        return entries

    async def get_file(self, owner: str, name: str, path: str, ref: str) -> GitHubFileContent:
        """Read one file at a pinned ref.

        The contents API refuses blobs above 1 MB. Review reads are capped at
        200 lines / 8 KiB, so such a file is reported as unavailable unless the
        caller supplies nothing else — the budget is never expanded to fetch it.
        """
        encoded_path = "/".join(quote(segment, safe="") for segment in path.split("/"))
        quoted = f"/repos/{owner}/{name}/contents/{encoded_path}"
        try:
            payload = await self._get_json(quoted, params={"ref": ref})
        except GitHubFileTooLargeError:
            return GitHubFileContent(path=path, sha=None, text=None, too_large=True)
        if isinstance(payload, list):
            return GitHubFileContent(path=path, sha=None, text=None, binary=True)
        if payload.get("type") != "file":
            return GitHubFileContent(path=path, sha=None, text=None, binary=True)
        encoded = str(payload.get("encoding") or "")
        content = payload.get("content") or ""
        if encoded != "base64" or not content:
            size = int(payload.get("size") or 0)
            if size > 1_000_000:
                return GitHubFileContent(
                    path=path, sha=payload.get("sha"), text=None, too_large=True
                )
            return GitHubFileContent(path=path, sha=payload.get("sha"), text="")
        try:
            raw = base64.b64decode(content, validate=False)
        except (ValueError, TypeError):
            return GitHubFileContent(path=path, sha=payload.get("sha"), text=None, binary=True)
        if b"\x00" in raw[:4096]:
            return GitHubFileContent(path=path, sha=payload.get("sha"), text=None, binary=True)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return GitHubFileContent(path=path, sha=payload.get("sha"), text=None, binary=True)
        return GitHubFileContent(path=path, sha=payload.get("sha"), text=text)


def _repository(payload: dict[str, Any]) -> GitHubRepository:
    owner = (payload.get("owner") or {}).get("login") or ""
    name = payload.get("name") or ""
    if not owner or not name:
        raise GitHubUnavailableError("GitHub repository payload is missing owner/name")
    return GitHubRepository(
        owner=str(owner),
        name=str(name),
        url=str(payload.get("html_url") or f"https://github.com/{owner}/{name}"),
        default_branch=payload.get("default_branch"),
        private=bool(payload.get("private")),
    )


def _pull_request(payload: dict[str, Any]) -> GitHubPullRequest:
    base = payload.get("base") or {}
    head = payload.get("head") or {}
    if not base.get("sha") or not head.get("sha"):
        raise GitHubUnavailableError("GitHub pull request payload is missing commit SHAs")
    return GitHubPullRequest(
        number=int(payload.get("number") or 0),
        title=str(payload.get("title") or ""),
        state=str(payload.get("state") or "open"),
        draft=bool(payload.get("draft")),
        author=(payload.get("user") or {}).get("login"),
        base_ref=str(base.get("ref") or ""),
        head_ref=str(head.get("ref") or ""),
        base_sha=str(base["sha"]),
        head_sha=str(head["sha"]),
        base_repo=str((base.get("repo") or {}).get("full_name") or ""),
        head_repo=str((head.get("repo") or {}).get("full_name") or ""),
        html_url=payload.get("html_url"),
        updated_at=_parse_time(payload.get("updated_at")),
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = [
    "GitHubAuthError",
    "GitHubClient",
    "GitHubError",
    "GitHubFileContent",
    "GitHubNotFoundError",
    "GitHubPullRequest",
    "GitHubPullRequestFile",
    "GitHubRateLimitError",
    "GitHubRepository",
    "GitHubTreeEntry",
    "GitHubUnavailableError",
    "parse_repository_target",
    "resolve_token",
]
