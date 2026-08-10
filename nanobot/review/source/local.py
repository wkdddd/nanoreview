"""Local repository I/O for code-review evidence collection."""

from __future__ import annotations

import fnmatch
import os
from collections import Counter
from pathlib import Path

from loguru import logger

from nanobot.agent.tools.path_utils import WORKSPACE_BOUNDARY_NOTE, is_under
from nanobot.rag.review_service import RepositoryRAGOptions
from nanobot.review.file_filter import review_file_filter_reason

_KEY_FILES = (
    "README.md",
    "pyproject.toml",
    "package.json",
    "pnpm-lock.yaml",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    ".github/workflows",
)


class LocalRepoReader:
    """Read the current workspace through a GitHub-reader-like interface."""

    def __init__(self, workspace: Path, options: RepositoryRAGOptions | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.options = options or RepositoryRAGOptions()

    async def execute(
        self,
        *,
        action: str,
        path: str | None = None,
        pattern: str | None = None,
        max_entries: int = 500,
        offset: int = 1,
        limit: int | None = None,
    ) -> str:
        try:
            if action == "meta":
                return await self.meta(path=path)
            if action == "tree":
                return await self.tree(path=path, pattern=pattern, max_entries=max_entries)
            if action == "file":
                if not path:
                    return "Error: 'repo_path' parameter is required for local file action."
                return await self.file(path=path, offset=offset, limit=limit)
            if action == "diff":
                return await self.diff()
            return f"Error: unknown local action '{action}'. Use 'meta', 'tree', 'file', or 'diff'."
        except PermissionError as exc:
            return f"Error: {exc}"

    async def meta(self, *, path: str | None = None) -> str:
        root = self._resolve(path or ".")
        if not root.exists():
            return f"Error: Path not found: {path}"
        if root.is_file():
            root = root.parent

        branch, remote, head, dirty = self._git_meta(root)
        files = list(self._iter_files(root))
        extensions = Counter(p.suffix.lower() or "(none)" for p in files)
        key_files = [item for item in _KEY_FILES if (root / item).exists()]
        top_exts = ", ".join(f"{ext}: {count}" for ext, count in extensions.most_common(8)) or "(none)"
        lines = [
            f"Repository: {root}",
            f"Workspace: {self.workspace}",
            f"Git branch: {branch or 'unknown'}",
            f"Git remote: {remote or '(none)'}",
            f"Git HEAD: {head or 'unknown'}",
            f"Git dirty: {dirty}",
            f"Text files: {len(files)}",
            f"Top extensions: {top_exts}",
            f"Key files: {', '.join(key_files) if key_files else '(none found)'}",
        ]
        return "\n".join(lines)

    async def tree(
        self,
        *,
        path: str | None = None,
        pattern: str | None = None,
        max_entries: int = 500,
    ) -> str:
        root = self._resolve(path or ".")
        if not root.exists():
            return f"Error: Directory not found: {path or '.'}"
        if root.is_file():
            root = root.parent
        if not root.is_dir():
            return f"Error: Not a directory: {path or '.'}"

        limit = min(max(int(max_entries or 500), 1), self.options.max_files)
        entries: list[str] = []
        total = 0
        for item in self._walk(root):
            rel = item.relative_to(root).as_posix()
            display = f"{rel}/" if item.is_dir() else rel
            if pattern and not fnmatch.fnmatch(rel, pattern):
                continue
            total += 1
            if len(entries) < limit:
                entries.append(display)
        header = f"Tree for {root}"
        if pattern:
            header += f" (filter: {pattern})"
        header += f"\n{'-' * len(header)}\n"
        if total > limit:
            entries.append(f"... (truncated at {limit}, total {total} entries)")
        return header + ("\n".join(entries) if entries else "(no matching entries)")

    async def file(self, *, path: str, offset: int = 1, limit: int | None = None) -> str:
        target = self._resolve(path)
        if not target.exists():
            return f"Error: File not found: {path}"
        if target.is_dir():
            entries = []
            for item in sorted(target.iterdir()):
                if self._is_ignored(item, base=target):
                    continue
                suffix = "/" if item.is_dir() else ""
                entries.append(f"  {item.name}{suffix}")
                if len(entries) >= 200:
                    entries.append("... (truncated at 200 entries)")
                    break
            return f"Directory: {path}/\n" + ("\n".join(entries) if entries else "(empty)")
        if not target.is_file():
            return f"Error: Not a file: {path}"
        raw = target.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"Error: Cannot read binary file {path}. Only UTF-8 text is supported."
        text = text.replace("\r\n", "\n")
        rel = target.relative_to(self.workspace).as_posix()
        if limit is not None:
            all_lines = text.splitlines()
            total_lines = len(all_lines)
            offset = max(int(offset or 1), 1)
            limit = min(max(int(limit or 200), 1), 500)
            if total_lines and offset > total_lines:
                return f"Error: offset {offset} is beyond end of local file '{path}' ({total_lines} lines)."

            start = offset - 1
            end = min(start + limit, total_lines)
            numbered = [
                f"{start + idx + 1}| {line}"
                for idx, line in enumerate(all_lines[start:end])
            ]
            header = f"File: {rel} ({len(raw)} bytes, lines {offset}-{end} of {total_lines})\n{'-' * 40}\n"
            result = header + "\n".join(numbered)
            if end < total_lines:
                result += (
                    "\n\n(Continue with "
                    f"local_review(action='file', repo_path='{rel}', offset={end + 1}, limit={limit})"
                    ")"
                )
            else:
                result += f"\n\n(End of local file - {total_lines} lines total)"
            return result

        max_chars = self.options.max_file_chars
        truncated = ""
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = f"\n\n[Truncated at {max_chars} characters]"
        header = f"File: {rel} ({len(raw)} bytes)\n{'-' * 40}\n"
        return header + text + truncated

    async def diff(self) -> str:
        try:
            from git import Repo  # type: ignore
        except Exception as exc:
            logger.debug("local_review GitPython unavailable reason={}", exc)
            return "Error: GitPython is unavailable for local diff inspection."
        try:
            repo = Repo(self.workspace, search_parent_directories=True)
            changed = sorted(
                set(repo.git.diff("--name-only").splitlines())
                | set(repo.git.diff("--name-only", "--cached").splitlines())
                | set(str(path) for path in repo.untracked_files)
            )
            changed = [path for path in changed if review_file_filter_reason(path) is None]
            lines = ["Local Diff:", "-" * 40, f"Changed text files: {len(changed)}"]
            lines.extend(f"- {path}" for path in changed[:200])
            if len(changed) > 200:
                lines.append("... (truncated at 200 files)")
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("local_review local git diff unavailable reason={}", exc)
            return f"Error: local git diff unavailable: {exc}"

    def _resolve(self, path: str) -> Path:
        raw = Path(path).expanduser()
        candidate = raw if raw.is_absolute() else self.workspace / raw
        resolved = candidate.resolve()
        if not is_under(resolved, self.workspace):
            raise PermissionError(
                f"Path {path} is outside workspace {self.workspace}" + WORKSPACE_BOUNDARY_NOTE
            )
        return resolved

    def _git_meta(self, root: Path) -> tuple[str, str, str, bool | str]:
        try:
            from git import Repo  # type: ignore
        except Exception as exc:
            logger.debug("local_review GitPython unavailable reason={}", exc)
            return "", "", "", "unknown"
        try:
            repo = Repo(root, search_parent_directories=True)
            branch = repo.active_branch.name if not repo.head.is_detached else "detached"
            remote = ""
            try:
                remote = repo.git.config("--get", "remote.origin.url").strip()
            except Exception:
                remote = ""
            head = repo.head.commit.hexsha[:12] if repo.head.is_valid() else ""
            dirty = repo.is_dirty(untracked_files=True)
            return branch, remote, head, dirty
        except Exception as exc:
            logger.debug("local_review git meta unavailable root={} reason={}", root, exc)
            return "", "", "", "unknown"

    def _iter_files(self, root: Path) -> list[Path]:
        return [
            item
            for item in self._walk(root)
            if item.is_file()
            and review_file_filter_reason(item.relative_to(root).as_posix()) is None
            and item.suffix.lower() not in self.options.binary_extensions
        ]

    def _walk(self, root: Path) -> list[Path]:
        results: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            try:
                rel_parts = current.relative_to(root).parts
            except ValueError:
                dirnames[:] = []
                continue
            if self._ignored_dir_parts(rel_parts):
                dirnames[:] = []
                continue
            dirnames[:] = [
                name
                for name in dirnames
                if not self._ignored_dir_parts((*rel_parts, name))
            ]
            for dirname in dirnames:
                results.append(current / dirname)
            for filename in filenames:
                path = current / filename
                if not self._is_ignored(path, base=root):
                    results.append(path)
        return sorted(results, key=lambda item: item.relative_to(root).as_posix())

    def _is_ignored(self, path: Path, *, base: Path) -> bool:
        try:
            rel_parts = path.relative_to(base).parts
        except ValueError:
            return True
        if review_file_filter_reason(path.relative_to(base).as_posix()) is not None:
            return True
        if self._ignored_dir_parts(rel_parts):
            return True
        return any(fnmatch.fnmatch(path.name, pattern) for pattern in self.options.ignore_globs)

    def _ignored_dir_parts(self, rel_parts: tuple[str, ...]) -> bool:
        if any(part in self.options.ignore_dirs for part in rel_parts):
            return True
        if rel_parts[:3] == ("references", "web", "pages"):
            return True
        return bool(rel_parts and rel_parts[0] == ".nanoreview")
