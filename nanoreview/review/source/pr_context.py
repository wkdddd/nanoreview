"""Host-bound PR context reader used by reviewers and the Judge.

One :class:`PrContextSession` belongs to exactly one agent run. It owns that
run's tool-call budget, source-read budget and evidence list, so two reviewers
sharing a network client can never share mutable permissions or counters.

Every read is pinned to the snapshot: the new side comes from the head commit,
the old side from the merge base. There is no branch fallback and no path that
escapes the repository tree.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from nanoreview.agent.tools.search_core import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_HEAD_LIMIT,
    DEFAULT_LINE_LIMIT,
    ContentPage,
    MatchConfig,
    SearchBudget,
    SearchBudgetExceededError,
    SearchCancelledError,
    SearchLine,
    count_matches,
    match_glob,
    matches_type,
    text_page,
)
from nanoreview.review.input.github import GitHubClient, GitHubError
from nanoreview.storage.records import ReviewSnapshot

_ALL_ACTIONS = frozenset({"diff", "read", "list", "search"})
_SIDE_NEW = "new"
_SIDE_OLD = "old"


@dataclass(frozen=True, slots=True)
class PrContextLimits:
    """Per-run budgets frozen when the agent starts."""

    allowed_actions: frozenset[str] = _ALL_ACTIONS
    max_tool_calls: int = 24
    max_read_lines: int = 200
    max_read_bytes: int = 8 * 1024
    max_source_files: int = 128
    max_source_bytes: int = 10 * 1024 * 1024
    max_candidates: int = 200
    max_tree_paths: int = 2000
    max_search_chars: int = 12_000
    max_line_chars: int = DEFAULT_LINE_LIMIT
    scan_seconds: float = 30.0


REVIEWER_CONTEXT_LIMITS = PrContextLimits(
    allowed_actions=_ALL_ACTIONS, max_tool_calls=24
)
JUDGE_CONTEXT_LIMITS = PrContextLimits(
    allowed_actions=frozenset({"diff", "read", "search"}), max_tool_calls=6
)


@dataclass(slots=True)
class PrContextEvidence:
    evidence_id: str
    kind: str
    path: str | None
    side: str | None
    start_line: int | None
    end_line: int | None
    payload: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "path": self.path,
            "side": self.side,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "payload": self.payload,
        }


@dataclass(slots=True)
class ContextResult:
    text: str
    evidence: PrContextEvidence | None = None
    is_error: bool = False


@dataclass(slots=True)
class _TreeView:
    paths: dict[str, int | None]
    truncated: bool
    supplemented: bool = False
    gap: str | None = None


class PrContextSession:
    """Read-only, budgeted view over one frozen PR snapshot."""

    def __init__(
        self,
        *,
        snapshot: ReviewSnapshot,
        github: GitHubClient,
        limits: PrContextLimits = REVIEWER_CONTEXT_LIMITS,
        path_scopes: tuple[str, ...] = (),
    ) -> None:
        self._snapshot = snapshot
        self._github = github
        self._limits = limits
        self._path_scopes = tuple(_normalize_path(item) for item in path_scopes if item)
        self._text_cache: dict[tuple[str, str], Any] = {}
        self._tree_cache: dict[str, _TreeView] = {}
        self.evidence: list[PrContextEvidence] = []
        self.tool_calls = 0
        self.network_reads = 0
        self.source_files = 0
        self.source_bytes = 0
        self.failures: list[str] = []
        self.coverage_notes: list[str] = []

    # -- public state -------------------------------------------------------

    @property
    def snapshot(self) -> ReviewSnapshot:
        return self._snapshot

    @property
    def run_id(self) -> str:
        return self._snapshot.run_id

    # -- actions ------------------------------------------------------------

    async def diff(self, path: str | None = None) -> ContextResult:
        gate = self._enter("diff")
        if gate is not None:
            return gate
        if path:
            item = self._snapshot.file_for(_normalize_path(path))
            if item is None:
                return self._error(
                    f"diff: '{path}' is not part of this pull request; "
                    f"changed files: {', '.join(self._snapshot.changed_paths) or '(none)'}"
                )
            if not item.patch:
                return self._error(
                    f"diff: no patch is available for '{item.path}' (recorded gap)"
                )
            return self._with_evidence(
                kind="diff",
                path=item.path,
                side=None,
                start_line=None,
                end_line=None,
                text=(
                    f"[pr_context diff] path={item.path} status={item.status} "
                    f"old_path={item.old_path or '-'} head={self._short(self._snapshot.head_sha)}\n"
                    f"{item.patch.rstrip()}"
                ),
                payload={"status": item.status, "patch": item.patch},
            )
        lines = [
            f"[pr_context diff] pr=#{self._snapshot.pr_number} "
            f"base={self._short(self._snapshot.base_sha)} "
            f"head={self._short(self._snapshot.head_sha)} "
            f"merge_base={self._short(self._snapshot.merge_base_sha)} "
            f"files={len(self._snapshot.changed_files)}"
        ]
        budget = self._limits.max_search_chars
        used = len(lines[0])
        omitted: list[str] = []
        for item in self._snapshot.changed_files:
            block = f"\n--- {item.path} ({item.status}) ---\n{(item.patch or '').rstrip()}"
            if used + len(block) > budget:
                omitted.append(item.path)
                continue
            lines.append(block)
            used += len(block)
        if omitted:
            lines.append(
                f"\n(diff truncated to {budget} chars; read the rest with "
                f"action=diff path=<file>): {', '.join(omitted)}"
            )
        return self._with_evidence(
            kind="diff",
            path=None,
            side=None,
            start_line=None,
            end_line=None,
            text="\n".join(lines),
            payload={"files": len(self._snapshot.changed_files)},
        )

    async def read(
        self,
        path: str,
        *,
        side: str = _SIDE_NEW,
        offset: int = 1,
        limit: int | None = None,
    ) -> ContextResult:
        gate = self._enter("read")
        if gate is not None:
            return gate
        normalized = _normalize_path(path)
        invalid = self._validate_path(normalized)
        if invalid:
            return self._error(invalid)
        if side not in {_SIDE_NEW, _SIDE_OLD}:
            return self._error(f"read: side must be 'new' or 'old', got {side!r}")

        content = await self._fetch_text(side, normalized)
        if content is None:
            return self._error(
                f"read: cannot read '{normalized}' at {side} side "
                f"({self._last_reason(normalized)})"
            )
        lines = content.splitlines()
        total = len(lines)
        offset = max(1, int(offset))
        if offset > total:
            return self._error(f"read: offset {offset} is beyond end of file ({total} lines)")
        count = min(
            self._limits.max_read_lines,
            max(1, int(limit)) if limit else self._limits.max_read_lines,
        )
        start = offset - 1
        end = min(start + count, total)
        numbered: list[str] = []
        chars = 0
        for index in range(start, end):
            render = f"{index + 1}| {lines[index]}"
            if chars + len(render) + 1 > self._limits.max_read_bytes:
                end = index
                break
            numbered.append(render)
            chars += len(render) + 1
        if not numbered:
            return self._error("read: requested range exceeds the per-read size budget")
        header = (
            f"[pr_context read] path={normalized} side={side} "
            f"ref={self._short(self._ref_for(side)[2])} lines={offset}-{end}/{total}"
        )
        body = "\n".join(numbered)
        trailer = (
            f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
            if end < total
            else f"\n\n(End of file — {total} lines total)"
        )
        return self._with_evidence(
            kind="read",
            path=normalized,
            side=side,
            start_line=offset,
            end_line=end,
            text=header + "\n" + body + trailer,
            payload={"ref": self._ref_for(side)[2], "total_lines": total},
        )

    async def list(
        self,
        path: str = "",
        *,
        recursive: bool = True,
        head_limit: int | None = 200,
        offset: int = 0,
    ) -> ContextResult:
        gate = self._enter("list")
        if gate is not None:
            return gate
        normalized = _normalize_path(path)
        invalid = self._validate_path(normalized)
        if invalid:
            return self._error(invalid)
        tree = await self._tree(_SIDE_NEW)
        prefix = f"{normalized}/" if normalized else ""
        entries = sorted(item for item in tree.paths if item.startswith(prefix))
        if not recursive:
            entries = [
                item
                for item in entries
                if "/" not in item[len(prefix) :]
            ]
        limit = 200 if head_limit is None else (None if head_limit == 0 else head_limit)
        page, truncated = text_page(entries, limit, max(0, offset), self._limits.max_search_chars)
        header = (
            f"[pr_context list] path={normalized or '.'} ref={self._short(self._snapshot.head_sha)} "
            f"entries={len(entries)} truncated={str(truncated).lower()}"
        )
        body = "\n".join(page) if page else "(no entries)"
        notes: list[str] = []
        if truncated:
            notes.append(
                f"(pagination: limit={limit}, offset={offset}; "
                f"use offset={offset + len(page)} to continue)"
            )
        if tree.truncated:
            notes.append(
                "(the pinned tree was truncated by GitHub; results cover only fetched paths)"
            )
        text = header + "\n" + body
        if notes:
            text += "\n\n" + "\n".join(notes)
        return ContextResult(text=text)

    async def search(
        self,
        query: str,
        *,
        scope: str = "",
        glob: str | None = None,
        type: str | None = None,
        case_insensitive: bool = False,
        fixed_strings: bool = True,
        output_mode: str = "content",
        context_before: int = DEFAULT_CONTEXT_LINES,
        context_after: int = DEFAULT_CONTEXT_LINES,
        head_limit: int | None = None,
        offset: int = 0,
    ) -> ContextResult:
        gate = self._enter("search")
        if gate is not None:
            return gate
        if not (query or "").strip():
            return self._error("search: query must not be empty")
        normalized_scope = _normalize_path(scope)
        invalid = self._validate_path(normalized_scope)
        if invalid:
            return self._error(invalid)
        if output_mode not in {"content", "files_with_matches", "count"}:
            return self._error(
                "search: output_mode must be content, files_with_matches or count"
            )
        if glob:
            try:
                match_glob("", "", glob)
            except ValueError as exc:
                return self._error(f"search: {exc}")

        config = MatchConfig(
            pattern=query,
            case_insensitive=case_insensitive,
            fixed_strings=fixed_strings,
            output_mode=output_mode,
            context_before=max(0, context_before),
            context_after=max(0, context_after),
            head_limit=(
                DEFAULT_HEAD_LIMIT
                if head_limit is None
                else (None if head_limit == 0 else head_limit)
            ),
            offset=max(0, offset),
            max_chars=self._limits.max_search_chars,
            line_limit=self._limits.max_line_chars,
        )
        try:
            regex = config.compile()
        except Exception as exc:  # re.error, kept as a model-visible correction
            return self._error(f"search: invalid pattern: {exc}")

        tree = await self._tree(_SIDE_NEW)
        candidates = self._candidates(tree, normalized_scope, glob, type)
        candidates_truncated = len(candidates) > self._limits.max_candidates
        selected = candidates[: self._limits.max_candidates]

        budget = SearchBudget(
            cancelled=threading.Event(),
            deadline=time.monotonic() + self._limits.scan_seconds,
            max_paths=max(1, len(selected) + 1),
        )
        page = ContentPage(
            limit=config.head_limit,
            offset=config.offset,
            max_chars=config.max_chars,
            line_limit=config.line_limit,
        )
        counts: dict[str, int] = {}
        matched_files: list[str] = []
        searched = 0
        unread: list[str] = []
        for path in selected:
            try:
                budget.checkpoint()
            except SearchBudgetExceededError:
                unread.append("(search stopped at the 30 s scan budget)")
                break
            except SearchCancelledError:  # pragma: no cover - no external cancel here
                break
            text = await self._fetch_text(_SIDE_NEW, path, search=True)
            if text is None:
                unread.append(f"{path}: {self._last_reason(path)}")
                continue
            searched += 1
            lines = (SearchLine(index, value) for index, value in enumerate(text.splitlines(), 1))
            if output_mode == "content":
                page.scan(path, lines, regex, config.context_before, config.context_after)
                if page.stopped:
                    break
            elif output_mode == "count":
                count = count_matches(lines, regex)
                if count:
                    counts[path] = count
                    matched_files.append(path)
            else:
                if count_matches(lines, regex, stop_after=1):
                    matched_files.append(path)
                    if config.head_limit is not None and len(matched_files) > (
                        config.offset + config.head_limit
                    ):
                        break

        header = (
            f"[pr_context search] pattern={query!r} scope={normalized_scope or '.'} "
            f"mode={output_mode} ref={self._short(self._snapshot.head_sha)} "
            f"candidates={len(candidates)} searched={searched}"
        )
        notes: list[str] = []
        if output_mode == "content":
            no_matches = (
                f"No matches found for pattern {query!r} in {normalized_scope or '.'} "
                f"(searched {searched} of {len(candidates)} candidate files)"
            )
            body, note = page.render(no_matches)
            if note:
                notes.append(note)
            matched = page.returned
        else:
            ordered = sorted(matched_files)
            entries = (
                [f"{name}: {counts[name]}" for name in ordered]
                if output_mode == "count"
                else ordered
            )
            paged, truncated = text_page(
                entries, config.head_limit, config.offset, config.max_chars
            )
            body = "\n".join(paged) if entries else (
                f"No matches found for pattern {query!r} in {normalized_scope or '.'} "
                f"(searched {searched} of {len(candidates)} candidate files)"
            )
            if truncated:
                notes.append(
                    f"(pagination: limit={config.head_limit}, offset={config.offset}; "
                    f"use offset={config.offset + len(paged)} to continue)"
                )
            elif config.offset:
                notes.append(f"(pagination: offset={config.offset})")
            matched = len(ordered)
        if candidates_truncated:
            notes.append(
                f"(candidate list truncated to {self._limits.max_candidates} of "
                f"{len(candidates)} files; narrow scope, glob or type)"
            )
        if unread:
            notes.extend(f"(unread: {item})" for item in unread[:5])
            if len(unread) > 5:
                notes.append(f"(unread: {len(unread) - 5} more files)")
        if tree.truncated:
            notes.append("(the pinned tree was truncated by GitHub; coverage is partial)")
        text = header + "\n" + body
        if notes:
            text += "\n\n" + "\n".join(notes)
        self.coverage_notes.extend(notes)
        return self._with_evidence(
            kind="search",
            path=normalized_scope or None,
            side=_SIDE_NEW,
            start_line=None,
            end_line=None,
            text=text,
            payload={
                "query": query,
                "scope": normalized_scope,
                "glob": glob,
                "type": type,
                "output_mode": output_mode,
                "candidates": len(candidates),
                "searched_files": searched,
                "matched": matched,
                "unread": unread,
                "notes": notes,
            },
        )

    # -- budget -------------------------------------------------------------

    def _enter(self, action: str) -> ContextResult | None:
        if action not in self._limits.allowed_actions:
            return self._error(
                f"{action}: this role may only use {sorted(self._limits.allowed_actions)}"
            )
        self.tool_calls += 1
        if self.tool_calls > self._limits.max_tool_calls:
            return self._error(
                f"{action}: tool-call budget exhausted "
                f"({self._limits.max_tool_calls} calls); answer with the evidence already read"
            )
        return None

    def _error(self, message: str) -> ContextResult:
        return ContextResult(text=f"Error: {message}", is_error=True)

    # -- snapshot plumbing --------------------------------------------------

    def _ref_for(self, side: str) -> tuple[str, str, str]:
        if side == _SIDE_OLD:
            owner, name = _split(self._snapshot.base_repo)
            return owner, name, self._snapshot.merge_base_sha
        owner, name = _split(self._snapshot.head_repo)
        return owner, name, self._snapshot.head_sha

    def _validate_path(self, path: str) -> str | None:
        if not path:
            return None
        if path.startswith("/") or path.startswith("../") or "/../" in path:
            return f"path must stay inside the repository: {path!r}"
        if self._path_scopes and not any(
            path == scope or path.startswith(f"{scope}/") for scope in self._path_scopes
        ):
            return (
                f"path {path!r} is outside this role's bounded scope "
                f"({', '.join(self._path_scopes)})"
            )
        return None

    async def _tree(self, side: str) -> _TreeView:
        owner, name, ref = self._ref_for(side)
        cached = self._tree_cache.get(ref)
        if cached is not None:
            return cached
        try:
            entries, truncated = await self._github.list_tree(owner, name, ref)
        except GitHubError as exc:
            view = _TreeView(paths={}, truncated=True, gap=f"tree_unavailable: {exc.code}")
            self._tree_cache[ref] = view
            self.coverage_notes.append(view.gap or "tree_unavailable")
            return view
        paths = {
            entry.path: entry.size
            for entry in entries
            if entry.entry_type == "blob"
        }
        view = _TreeView(paths=paths, truncated=truncated)
        if truncated and self._path_scopes:
            await self._supplement(owner, name, ref, view)
        if truncated:
            view.gap = "tree_truncated: GitHub limits the recursive tree response"
            self.coverage_notes.append(view.gap)
        self._tree_cache[ref] = view
        return view

    async def _supplement(
        self, owner: str, name: str, ref: str, view: _TreeView
    ) -> None:
        """Recover the scoped subtree when the recursive listing was truncated."""
        queue = list(self._path_scopes)
        visited = 0
        while queue and visited < self._limits.max_tree_paths:
            current = queue.pop(0)
            try:
                entries = await self._github.get_directory(owner, name, current, ref)
            except GitHubError:
                continue
            for entry in entries:
                visited += 1
                if entry.entry_type == "dir":
                    queue.append(entry.path)
                elif entry.entry_type == "file":
                    view.paths[entry.path] = entry.size
        view.supplemented = True

    def _candidates(
        self,
        tree: _TreeView,
        scope: str,
        glob: str | None,
        file_type: str | None,
    ) -> list[str]:
        prefix = f"{scope}/" if scope else ""
        selected: list[str] = []
        for path in sorted(tree.paths):
            if prefix and not path.startswith(prefix):
                continue
            name = path.rsplit("/", 1)[-1]
            if glob and not match_glob(path, name, glob):
                continue
            if not matches_type(name, file_type):
                continue
            selected.append(path)
        return selected

    async def _fetch_text(self, side: str, path: str, *, search: bool = False) -> str | None:
        owner, name, ref = self._ref_for(side)
        key = (ref, path)
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached.text
        tree = await self._tree(side)
        size = tree.paths.get(path)
        if size is not None and size > self._limits.max_source_bytes:
            self._set_reason(path, "file exceeds the per-run source byte budget")
            return None
        if self.source_files >= self._limits.max_source_files:
            self._set_reason(path, "per-run source file budget exhausted")
            return None
        try:
            content = await self._github.get_file(owner, name, path, ref)
        except GitHubError as exc:
            self._set_reason(path, f"github read failed ({exc.code})")
            self.failures.append(f"{path}: {exc.code}")
            return None
        self.network_reads += 1
        if content.text is None:
            self._set_reason(
                path,
                "binary or oversized file"
                if not content.too_large
                else "file is too large for the GitHub contents API",
            )
            self._text_cache[key] = content
            return None
        encoded = content.text.encode("utf-8")
        if self.source_bytes + len(encoded) > self._limits.max_source_bytes:
            self._set_reason(path, "per-run source byte budget exhausted")
            return None
        self.source_files += 1
        self.source_bytes += len(encoded)
        self._text_cache[key] = content
        return content.text

    def _set_reason(self, path: str, reason: str) -> None:
        marker = f"{path}#{reason}"
        if marker not in self.failures:
            self.failures.append(marker)

    def _last_reason(self, path: str) -> str:
        for item in reversed(self.failures):
            name, _, reason = item.partition("#")
            if name == path:
                return reason
        return "unavailable"

    def _with_evidence(
        self,
        *,
        kind: str,
        path: str | None,
        side: str | None,
        start_line: int | None,
        end_line: int | None,
        text: str,
        payload: dict[str, Any],
    ) -> ContextResult:
        evidence = PrContextEvidence(
            evidence_id=f"ctx-{len(self.evidence) + 1:03d}",
            kind=kind,
            path=path,
            side=side,
            start_line=start_line,
            end_line=end_line,
            payload=payload,
        )
        self.evidence.append(evidence)
        return ContextResult(
            text=f"{text}\n\nevidence_id={evidence.evidence_id}",
            evidence=evidence,
        )

    @staticmethod
    def _short(sha: str) -> str:
        return sha[:12]


def _normalize_path(path: str) -> str:
    """Normalize separators without hiding an absolute or traversing path.

    A leading ``/`` is preserved so validation can reject it; stripping it
    would silently turn ``/etc/passwd`` into a repository-relative path.
    """
    value = str(path or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.rstrip("/")


def _split(repo: str) -> tuple[str, str]:
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return owner, name
    return repo, ""


__all__ = [
    "ContextResult",
    "JUDGE_CONTEXT_LIMITS",
    "PrContextEvidence",
    "PrContextLimits",
    "PrContextSession",
    "REVIEWER_CONTEXT_LIMITS",
]
