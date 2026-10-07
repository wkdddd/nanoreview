"""Evidence retrieval service for code-review workflows."""

from __future__ import annotations

import asyncio
import difflib
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.review.file_filter import review_file_filter_reason
from nanoreview.review.planning.preprocessor import (
    ProgrammaticEvidenceRequest,
    ProgrammaticEvidenceResult,
    ProgrammaticEvidenceService,
    SkippedUnit,
)
from nanoreview.review.source.utils import (
    changed_lines_from_patch,
    clean_scope_paths,
    path_matches_scope,
)
from nanoreview.review.types import LocalReviewScope
from nanoreview.utils.log_style import log_event

_DEFAULT_DIFF_QUERY = "code review bug security performance maintainability changed lines"


@dataclass(slots=True)
class LocalChangedSummary:
    files: list[str] = field(default_factory=list)
    touched_lines: dict[str, list[int]] = field(default_factory=dict)


class ReviewEvidenceService:
    """Compose local/git inputs with deterministic evidence preparation."""

    def __init__(
        self,
        preprocessor: ProgrammaticEvidenceService,
        *,
        workspace: Path | None = None,
    ) -> None:
        self.preprocessor = preprocessor
        self.workspace = (workspace or preprocessor.workspace).expanduser().resolve()
        self.last_cache_root: Path | None = None
        self.last_changed_files: list[str] = []
        # Structured preprocessing output of the most recent dispatch; the
        # prefetch layer builds the evidence bundle from it when present.
        self.last_result: ProgrammaticEvidenceResult | None = None

    async def dispatch(
        self,
        *,
        target_type: str,
        action: str,
        target_subpath: str | None = None,
        target_subpath_kind: str | None = None,
        review_query: str | None = None,
        max_results: int = 5,
        include_tests: bool | None = None,
        local_scope: LocalReviewScope | None = None,
        trace_id: str = "",
        context_window_tokens: int | None = None,
        frozen_diff: dict[str, Any] | None = None,
    ) -> str:
        """Unified entry point for local review evidence retrieval.

        ``frozen_diff`` carries the net diff captured at admission
        (``{"patches": {...}, "skipped": {...}}``). When present, the diff is
        read from the admitted snapshot rather than the live worktree so a
        post-admission edit/commit/rollback cannot change what is reviewed.
        """
        self.last_cache_root = None
        self.last_changed_files = []
        self.last_result = None
        if action == "diff":
            return await self.local_changed_context(
                review_query=review_query,
                max_results=max_results,
                include_tests=include_tests,
                local_scope=local_scope,
                context_window_tokens=context_window_tokens,
                frozen_diff=frozen_diff,
            )
        return await self.local_context(
            review_query=review_query,
            max_results=max_results,
            include_tests=include_tests,
            local_scope=local_scope,
            context_window_tokens=context_window_tokens,
        )

    def _preprocessor_for_scope(self, local_scope: LocalReviewScope | None) -> ProgrammaticEvidenceService:
        if local_scope is None:
            return self.preprocessor
        review_root = Path(local_scope.review_root).expanduser().resolve()
        if review_root == self.preprocessor.workspace:
            return self.preprocessor
        return ProgrammaticEvidenceService(review_root, options=self.preprocessor.options)

    def _scope_files(
        self,
        preprocessor: ProgrammaticEvidenceService,
        local_scope: LocalReviewScope | None,
        candidate_paths: list[str] | None = None,
    ) -> list[Path]:
        scopes = clean_scope_paths(candidate_paths or [])
        if not scopes and local_scope is not None:
            scopes = clean_scope_paths(local_scope.scope_paths)
        files: list[Path] = []
        if scopes:
            for rel in scopes:
                candidate = (preprocessor.workspace / rel).resolve()
                try:
                    candidate.relative_to(preprocessor.workspace)
                except ValueError:
                    raise PermissionError(f"target path is outside review root: {rel}") from None
                if candidate.is_file() and review_file_filter_reason(rel) is None:
                    files.append(candidate)
                elif candidate.is_dir():
                    files.extend(preprocessor.iter_candidate_files(candidate))
            return list(dict.fromkeys(files))
        return list(preprocessor.iter_candidate_files())

    async def local_context(
        self,
        *,
        review_query: str | None,
        max_results: int,
        include_tests: bool | None,
        local_scope: LocalReviewScope | None = None,
        context_window_tokens: int | None = None,
    ) -> str:
        trace_id = "local"
        started = time.perf_counter()
        if not review_query or not review_query.strip():
            log_event(
                logger,
                "info",
                "review.evidence.local.done",
                status="error",
                trace_id=trace_id,
                reason="missing_query",
                elapsed_ms=f"{(time.perf_counter() - started) * 1000:.1f}",
            )
            return "Error: review_query is required."

        preprocessor = self._preprocessor_for_scope(local_scope)
        files = self._scope_files(preprocessor, local_scope)
        result = await preprocessor.retrieve(
            ProgrammaticEvidenceRequest(
                source_type="local",
                review_query=review_query.strip(),
                files=files,
                max_results=max_results,
                include_tests=include_tests,
                related_tests=False,
                context_window_tokens=context_window_tokens,
            )
        )
        self.last_result = result
        if not result.units:
            log_event(
                logger,
                "info",
                "review.evidence.local.done",
                status="no_units",
                trace_id=trace_id,
                files_count=len(files),
                units_count=0,
                skipped_count=len(result.skipped),
                context_chars=len(result.context),
                elapsed_ms=f"{(time.perf_counter() - started) * 1000:.1f}",
            )
            return "No relevant repository review references found."
        log_event(
            logger,
            "info",
            "review.evidence.local.done",
            status="success",
            trace_id=trace_id,
            files_count=len(files),
            units_count=len(result.units),
            skipped_count=len(result.skipped),
            mode=result.mode,
            context_chars=len(result.context),
            elapsed_ms=f"{(time.perf_counter() - started) * 1000:.1f}",
        )
        return result.context

    async def local_changed_context(
        self,
        *,
        review_query: str | None,
        max_results: int,
        include_tests: bool | None,
        local_scope: LocalReviewScope | None = None,
        context_window_tokens: int | None = None,
        frozen_diff: dict[str, Any] | None = None,
    ) -> str:
        started = time.perf_counter()
        preprocessor = self._preprocessor_for_scope(local_scope)
        if frozen_diff is not None:
            # Review the change captured at admission, not the live worktree:
            # the snapshot's net diff is the authoritative review input.
            raw_patches = frozen_diff.get("patches")
            raw_skipped = frozen_diff.get("skipped")
            patches = dict(raw_patches) if isinstance(raw_patches, dict) else {}
            skipped = dict(raw_skipped) if isinstance(raw_skipped, dict) else {}
            frozen = True
        else:
            patches, skipped = await asyncio.to_thread(self.local_changed_patches, preprocessor.workspace)
            frozen = False
        scopes = clean_scope_paths(local_scope.scope_paths if local_scope else [])
        if scopes:
            patches = {path: patch for path, patch in patches.items() if path_matches_scope(path, scopes)}
            skipped = {path: reason for path, reason in skipped.items() if path_matches_scope(path, scopes)}
        result = await asyncio.to_thread(
            preprocessor.diff_units,
            patches,
            review_query=(review_query or "").strip() or _DEFAULT_DIFF_QUERY,
            context_window_tokens=context_window_tokens,
        )
        # File-level filter reasons from the patch collector become skipped units.
        result.skipped.extend(
            SkippedUnit(path=path, reason=str(reason) or "filtered")
            for path, reason in sorted(skipped.items())
        )
        self.last_result = result
        self.last_changed_files = list(patches)
        log_event(
            logger,
            "info",
            "review.evidence.local_changed.done",
            status="success" if patches else "empty",
            trace_id="local_changed",
            scopes_count=len(scopes),
            changed_files=len(patches),
            units_count=len(result.units),
            skipped_count=len(result.skipped),
            mode=result.mode,
            frozen=frozen,
            context_chars=len(result.context),
            elapsed_ms=f"{(time.perf_counter() - started) * 1000:.1f}",
        )
        return result.context

    def local_changed_patches(self, workspace: Path | None = None) -> tuple[dict[str, str], dict[str, str]]:
        """Read local changed patches without indexing or retrieval."""
        root = (workspace or self.workspace).expanduser().resolve()
        summary = self.local_changed_summary(root)
        try:
            untracked = set(self._git_cli("ls-files", "--others", "--exclude-standard", cwd=root).splitlines())
        except Exception:
            untracked = set()
        patches: dict[str, str] = {}
        skipped: dict[str, str] = {}
        for path in summary.files:
            reason = review_file_filter_reason(path)
            if reason:
                skipped[path] = reason
                continue
            if path in untracked:
                target = (root / path).resolve()
                try:
                    target.relative_to(root)
                    content = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError, ValueError):
                    skipped[path] = "unreadable_file"
                    continue
                patch = "".join(
                    difflib.unified_diff(
                        [],
                        content.splitlines(keepends=True),
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                    )
                )
            else:
                # Net change relative to HEAD, matching admission's net diff.
                # Concatenating ``HEAD -> index`` with ``index -> worktree``
                # would expose the intermediate staged snapshot alongside the
                # final worktree state, so the reviewer would see the same
                # change twice (once per staging state).
                try:
                    patch = self._git_cli(
                        "diff", "--no-ext-diff", "--unified=3", "HEAD", "--", path, cwd=root
                    )
                except Exception:
                    skipped[path] = "patch_unavailable"
                    continue
            if not patch.strip():
                skipped[path] = "patch_unavailable"
            elif review_file_filter_reason(path, patch) is not None:
                skipped[path] = review_file_filter_reason(path, patch) or "filtered"
            else:
                patches[path] = patch
        return patches, skipped

    def local_changed_summary(self, workspace: Path | None = None) -> LocalChangedSummary:
        root = (workspace or self.workspace).expanduser().resolve()
        try:
            from git import Repo  # type: ignore
        except Exception as exc:
            logger.debug("repo_review GitPython unavailable reason={}", exc)
            return self._local_changed_summary_cli(root)
        try:
            repo = Repo(root, search_parent_directories=True)
            worktree = Path(getattr(repo, "working_tree_dir", None) or root).resolve()
            raw_paths = set(repo.git.diff("--name-only").splitlines())
            raw_paths.update(repo.git.diff("--name-only", "--cached").splitlines())
            raw_paths.update(str(p) for p in repo.untracked_files)
            touched: dict[str, list[int]] = {}
            text_paths: list[str] = []
            for path in sorted(raw_paths):
                rel_path = self._path_relative_to_root(path, worktree=worktree, root=root)
                if rel_path is None or review_file_filter_reason(rel_path) is not None:
                    continue
                text_paths.append(rel_path)
                lines: set[int] = set()
                lines.update(
                    changed_lines_from_patch(path, self._local_diff_patch(repo, path, cached=False))
                )
                lines.update(
                    changed_lines_from_patch(path, self._local_diff_patch(repo, path, cached=True))
                )
                if path in repo.untracked_files:
                    lines.update(self._untracked_file_lines(repo, path))
                if lines:
                    touched[rel_path] = sorted(lines)
            return LocalChangedSummary(files=text_paths, touched_lines=touched)
        except Exception as exc:
            logger.warning("repo_review local git diff unavailable reason={}", exc)
            return LocalChangedSummary()

    def local_changed_files(self) -> list[str]:
        return self.local_changed_summary().files

    @staticmethod
    def _path_relative_to_root(path: str, *, worktree: Path, root: Path) -> str | None:
        try:
            target = (worktree / path).resolve()
            return target.relative_to(root).as_posix()
        except ValueError:
            return None

    def _local_changed_summary_cli(self, workspace: Path | None = None) -> LocalChangedSummary:
        root = (workspace or self.workspace).expanduser().resolve()
        try:
            worktree = Path(
                self._git_cli("rev-parse", "--show-toplevel", cwd=root).strip()
            ).resolve()
        except Exception as exc:
            logger.warning("repo_review local git cli unavailable reason={}", exc)
            return LocalChangedSummary()
        try:
            # Git reports paths relative to the worktree root (``diff``) while
            # ``ls-files --others`` reports them relative to the cwd. Query
            # everything from the worktree root and convert to review-root
            # coordinates so a subdirectory target still collects its changes.
            tracked = set(self._git_cli("diff", "--name-only", cwd=worktree).splitlines())
            tracked.update(
                self._git_cli("diff", "--name-only", "--cached", cwd=worktree).splitlines()
            )
            untracked = set(
                self._git_cli("ls-files", "--others", "--exclude-standard", cwd=worktree).splitlines()
            )
            rel_by_raw: dict[str, str] = {}
            for raw in sorted(tracked | untracked):
                rel_path = self._path_relative_to_root(raw, worktree=worktree, root=root)
                if rel_path is None or review_file_filter_reason(rel_path) is not None:
                    continue
                rel_by_raw[raw] = rel_path
            text_paths = sorted(rel_by_raw.values())
            touched: dict[str, list[int]] = {}
            for raw, rel_path in rel_by_raw.items():
                lines: set[int] = set()
                lines.update(
                    changed_lines_from_patch(
                        rel_path, self._local_diff_patch_cli(raw, cached=False, cwd=worktree)
                    )
                )
                lines.update(
                    changed_lines_from_patch(
                        rel_path, self._local_diff_patch_cli(raw, cached=True, cwd=worktree)
                    )
                )
                if raw in untracked:
                    lines.update(self._untracked_workspace_file_lines(rel_path, workspace=root))
                if lines:
                    touched[rel_path] = sorted(lines)
            return LocalChangedSummary(files=text_paths, touched_lines=touched)
        except Exception as exc:
            logger.warning("repo_review local git cli diff unavailable reason={}", exc)
            return LocalChangedSummary()

    def _git_cli(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.workspace,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout

    @staticmethod
    def _local_diff_patch(repo: object, path: str, *, cached: bool) -> str:
        args = ["--cached"] if cached else []
        args.extend(["--unified=0", "--", path])
        patch = repo.git.diff(*args)
        return ReviewEvidenceService._diff_hunk_lines(patch)

    def _local_diff_patch_cli(self, path: str, *, cached: bool, cwd: Path | None = None) -> str:
        args = ["diff"]
        if cached:
            args.append("--cached")
        args.extend(["--unified=0", "--", path])
        return self._diff_hunk_lines(self._git_cli(*args, cwd=cwd))

    @staticmethod
    def _diff_hunk_lines(patch: str) -> str:
        return "\n".join(
            line
            for line in patch.splitlines()
            if line.startswith("@@")
            or (line.startswith("+") and not line.startswith("+++"))
            or (line.startswith("-") and not line.startswith("---"))
            or line.startswith(" ")
        )

    def _untracked_file_lines(self, repo: object, path: str) -> list[int]:
        worktree = getattr(repo, "working_tree_dir", None)
        if not worktree:
            return []
        target = (Path(worktree) / path).resolve()
        try:
            target.relative_to(Path(worktree).resolve())
        except ValueError:
            return []
        if review_file_filter_reason(path) is not None:
            return []
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        return list(range(1, len(text.replace("\r\n", "\n").replace("\r", "\n").splitlines()) + 1))

    def _untracked_workspace_file_lines(self, path: str, *, workspace: Path | None = None) -> list[int]:
        root = (workspace or self.workspace).expanduser().resolve()
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return []
        if review_file_filter_reason(path) is not None:
            return []
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        return list(range(1, len(text.replace("\r\n", "\n").replace("\r", "\n").splitlines()) + 1))
