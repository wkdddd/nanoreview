"""Hard validation for review finding candidates."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from loguru import logger

from nanobot.review.types import (
    ALL_REVIEW_ROLES,
    SEVERITY_ORDER,
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    GitHubDiffEvidence,
)


@dataclass
class ValidationContext:
    """Context for validating candidates against a workspace."""

    workspace: str
    changed_files: list[str] = field(default_factory=list)
    max_line_lookup: bool = True
    local_target: str | None = None
    remote_diff: GitHubDiffEvidence | None = None


class ReviewValidator:
    """Validates candidate findings with hard checks (no LLM)."""

    def __init__(self, ctx: ValidationContext) -> None:
        self._ctx = ctx
        self._workspace = Path(ctx.workspace).resolve()
        self._local_target = self._resolve_local_target(ctx.local_target)
        self._changed_files = {self._normalize_rel_path(path) for path in ctx.changed_files}
        self._remote_diff = ctx.remote_diff
        self._seen_fingerprints: set[str] = set()
        self.stats = {"accepted": 0, "rejected": 0, "needs_confirmation": 0}

    def validate_candidates(
        self, candidates: list[ReviewFindingCandidate], dimension: str
    ) -> ReviewDimensionResult:
        result = ReviewDimensionResult(dimension=dimension, status="validated")
        # 收集可能被修正过行号的 candidate，确保下游拿到的 line 是修正后的值
        validated_candidates: list[ReviewFindingCandidate] = []
        for candidate in candidates:
            verdict, candidate = self._validate_one(candidate, dimension)
            validated_candidates.append(candidate)
            if verdict.verdict == FindingVerdict.ACCEPTED:
                result.accepted.append(candidate)
                self.stats["accepted"] += 1
            elif verdict.verdict == FindingVerdict.REJECTED:
                result.rejected.append((candidate, verdict))
                self.stats["rejected"] += 1
            else:
                result.uncertain.append((candidate, verdict))
                self.stats["needs_confirmation"] += 1
        result.candidates = validated_candidates
        logger.debug(
            "validator dimension={} accepted={} rejected={} needs_confirmation={}",
            dimension,
            len(result.accepted),
            len(result.rejected),
            len(result.uncertain),
        )
        return result

    def _validate_one(
        self, c: ReviewFindingCandidate, dimension: str
    ) -> tuple[ReviewFindingVerdict, ReviewFindingCandidate]:
        """assign a single candidate finding with accepted/rejected/uncertain"""
        if c.severity not in SEVERITY_ORDER:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED,
                reason=f"invalid severity: {c.severity}",
            ), c
        if not c.file or not c.title:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED,
                reason="missing required field (file or title)",
            ), c
        fp = self._fingerprint(c)
        if fp in self._seen_fingerprints:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED, reason="duplicate finding"
            ), c
        self._seen_fingerprints.add(fp)

        if self._remote_diff is not None:
            return self._validate_remote_diff(c)

        file_path = self._resolve_candidate_path(c.file)
        if file_path is None:
            boundary = "target" if self._local_target is not None else "workspace"
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED,
                reason=f"path outside {boundary}: {c.file}",
            ), c
        if not file_path.is_file():
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED,
                reason=f"file not found: {c.file}",
            ), c
        if c.line is not None and self._ctx.max_line_lookup:
            if not self._line_in_range(file_path, c.line):
                return ReviewFindingVerdict(
                    verdict=FindingVerdict.REJECTED,
                    reason=f"line {c.line} out of range for {c.file}",
                ), c
        normalized_file = self._workspace_relative_path(file_path)
        if self._changed_files and normalized_file not in self._changed_files:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.UNCERTAIN,
                reason="file not in changed set",
                missing_evidence="file is outside PR/diff scope",
            ), c
        if not c.evidence.strip():
            return ReviewFindingVerdict(
                verdict=FindingVerdict.UNCERTAIN,
                reason="no evidence provided",
                missing_evidence="candidate lacks supporting evidence snippet",
            ), c
        role = ALL_REVIEW_ROLES.get(dimension)
        needs_evidence = role.evidence_required if role else False
        if needs_evidence:
            matched, actual_line = self._evidence_matches(file_path, c.evidence, c.line)
            if not matched:
                return ReviewFindingVerdict(
                    verdict=FindingVerdict.UNCERTAIN,
                    reason="evidence not found in file",
                    missing_evidence="candidate evidence snippet does not match the target file",
                ), c
            if actual_line is not None and actual_line != c.line:
                logger.debug(
                    "validator corrected line {} -> {} for {}",
                    c.line,
                    actual_line,
                    c.title,
                )
                c = replace(c, line=actual_line)
        else:
            matched, actual_line = self._evidence_matches(file_path, c.evidence, c.line)
            if matched and actual_line is not None and actual_line != c.line:
                logger.debug(
                    "validator corrected line {} -> {} for {}",
                    c.line,
                    actual_line,
                    c.title,
                )
                c = replace(c, line=actual_line)
        return ReviewFindingVerdict(verdict=FindingVerdict.ACCEPTED), c

    def _validate_remote_diff(
        self, c: ReviewFindingCandidate
    ) -> tuple[ReviewFindingVerdict, ReviewFindingCandidate]:
        normalized = self._normalize_rel_path(c.file)
        changed = {self._normalize_rel_path(path) for path in self._remote_diff.changed_files}
        if normalized not in changed:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.UNCERTAIN,
                reason="file not in changed set",
                missing_evidence="file is outside PR/diff scope",
            ), c
        patch = self._remote_diff.patches.get(normalized)
        if not patch:
            reason = self._remote_diff.patch_unavailable_files.get(normalized, "patch_unavailable")
            return ReviewFindingVerdict(
                verdict=FindingVerdict.UNCERTAIN,
                reason=reason,
                missing_evidence="GitHub did not provide a patch for this file",
            ), c
        if c.line is None or c.line not in set(self._remote_diff.touched_lines.get(normalized, [])):
            return ReviewFindingVerdict(
                verdict=FindingVerdict.UNCERTAIN,
                reason="line is not an added diff line",
                missing_evidence="diff validation only accepts added target lines",
            ), c
        snippets = self._evidence_snippets(c.evidence)
        normalized_patch = " ".join(patch.split())
        if not snippets or not any(" ".join(snippet.split()) in normalized_patch for snippet in snippets):
            return ReviewFindingVerdict(
                verdict=FindingVerdict.UNCERTAIN,
                reason="evidence not found in diff patch",
                missing_evidence="candidate evidence snippet does not match the PR patch",
            ), c
        return ReviewFindingVerdict(verdict=FindingVerdict.ACCEPTED), c

    def _fingerprint(self, c: ReviewFindingCandidate) -> str:
        key = f"{c.file}:{c.line or 0}:{c.title.lower().strip()}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _line_in_range(self, path: Path, line: int) -> bool:
        if line < 1:
            return False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                count = sum(1 for _ in f)
            return line <= count
        except OSError as e:
            logger.warning("file open failed:{}", e)
            return False

    def _resolve_local_target(self, raw: str | None) -> Path | None:
        if not raw or not str(raw).strip():
            return None
        try:
            path = Path(str(raw)).expanduser()
            # 相对路径按 workspace 解析，避免被进程 CWD 错误地解析到其他目录
            if not path.is_absolute():
                path = self._workspace / path
            return path.resolve()
        except OSError:
            return None

    def _resolve_candidate_path(self, raw: str) -> Path | None:
        value = raw.strip()
        if not value:
            return None
        candidate = Path(value)
        if candidate.is_absolute():
            if self._local_target is None:
                return None
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                return None
            return resolved if self._is_under_allowed_target(resolved) else None
        try:
            resolved = (self._workspace / candidate).resolve()
        except (OSError, ValueError):
            return None
        if not self._is_under_allowed_target(resolved):
            return None
        return resolved

    def _is_under_allowed_target(self, resolved: Path) -> bool:
        if self._local_target is not None:
            target = self._local_target
            if not target.exists():
                return False
            if target.is_file():
                return resolved == target
            try:
                resolved.relative_to(target)
                return True
            except ValueError:
                return False
        try:
            resolved.relative_to(self._workspace)
            return True
        except ValueError:
            return False

    @staticmethod
    def _normalize_rel_path(raw: str) -> str:
        return raw.replace("\\", "/").strip().lstrip("./")

    def _workspace_relative_path(self, path: Path) -> str:
        try:
            return path.relative_to(self._workspace).as_posix()
        except ValueError:
            return self._normalize_rel_path(str(path))

    @classmethod
    def _evidence_matches(
        cls, path: Path, evidence: str, line: int | None = None
    ) -> tuple[bool, int | None]:
        """Check if evidence exists in the file near the reported line.

        Returns (matched, actual_line) where actual_line is the real 1-based
        line number where evidence was found, or None if not found.
        """
        snippets = cls._evidence_snippets(evidence)
        if not snippets:
            return False, None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False, None
        if line is not None:
            lines_list = text.splitlines()
            total = len(lines_list)
            start = max(line - 16, 0)
            end = min(line + 15, total)
            nearby = " ".join(" ".join(item.split()) for item in lines_list[start:end])
            for snippet in snippets:
                if snippet in nearby:
                    for i in range(start, end):
                        if snippet in " ".join(lines_list[i].split()):
                            return True, i + 1
                    return True, line
            # Narrow window missed — fall back to full-file search
            for snippet in snippets:
                for i, file_line in enumerate(lines_list):
                    if snippet in " ".join(file_line.split()):
                        return True, i + 1
            return False, None
        normalized_text = " ".join(text.split())
        for snippet in snippets:
            if snippet in normalized_text:
                return True, None
        return False, None

    @staticmethod
    def _evidence_snippets(evidence: str) -> list[str]:
        raw = evidence.strip()
        if not raw:
            return []
        candidates: list[str] = []
        candidates.extend(match.strip() for match in re.findall(r"`([^`\n]+)`", raw))
        cleaned = re.sub(
            r"^\s*(?:line|lines)\s+\d+(?:\s*[-:]\s*\d+)?\s*[:：-]\s*", "", raw, flags=re.I
        )
        cleaned = re.sub(r"^\s*[-*+>]\s*", "", cleaned)
        candidates.append(cleaned)
        snippets: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            snippet = " ".join(candidate.split()).strip()
            if not snippet or snippet in seen:
                continue
            seen.add(snippet)
            snippets.append(snippet)
        return snippets
