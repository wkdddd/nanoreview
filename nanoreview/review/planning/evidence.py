"""Evidence retrieval for pull-request reviews.

Review only consumes the frozen snapshot: the patches captured at admission are
handed to the diff preprocessor, which produces one evidence unit per changed
file. Nothing here enumerates a repository, reads a working tree or shells out
to git — the review input is the PR at its pinned SHAs.

Context beyond the diff is the reviewer's own ``pr_context`` tool, which is a
capability boundary, not a preprocessing product.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.review.file_filter import review_file_filter_reason
from nanoreview.review.planning.preprocessor import (
    ProgrammaticEvidenceResult,
    ProgrammaticEvidenceService,
    SkippedUnit,
)
from nanoreview.utils.log_style import log_event

_DEFAULT_DIFF_QUERY = "code review bug security performance maintainability changed lines"


class ReviewEvidenceService:
    """Turn the frozen PR patches into deterministic program evidence."""

    def __init__(
        self,
        preprocessor: ProgrammaticEvidenceService,
        *,
        workspace: Path | None = None,
    ) -> None:
        self.preprocessor = preprocessor
        self.workspace = (workspace or preprocessor.workspace).expanduser().resolve()
        self.last_changed_files: list[str] = []
        # Structured preprocessing output of the most recent dispatch; the
        # prefetch layer builds the evidence bundle from it when present.
        self.last_result: ProgrammaticEvidenceResult | None = None

    async def dispatch(
        self,
        *,
        target_type: str,
        action: str,
        review_query: str | None = None,
        max_results: int = 5,
        include_tests: bool | None = None,
        trace_id: str = "",
        frozen_diff: dict[str, Any] | None = None,
    ) -> str:
        """Build review evidence from the admitted snapshot's patches.

        ``frozen_diff`` carries ``{"patches": {path: patch}, "skipped": {...}}``
        captured at admission. It is required: without a snapshot there is no
        review input, and falling back to a live tree would review something the
        user never submitted.
        """
        self.last_changed_files = []
        self.last_result = None
        if action != "diff":
            raise ValueError(
                f"unsupported review action: {action!r}; review evidence is diff-only"
            )
        if not isinstance(frozen_diff, dict):
            raise ValueError(
                "review evidence requires the frozen pull-request snapshot patches"
            )
        started = time.perf_counter()
        raw_patches = frozen_diff.get("patches")
        raw_skipped = frozen_diff.get("skipped")
        patches = {
            str(path): str(patch)
            for path, patch in (raw_patches.items() if isinstance(raw_patches, dict) else ())
            if patch
        }
        skipped = {
            str(path): str(reason)
            for path, reason in (raw_skipped.items() if isinstance(raw_skipped, dict) else ())
        }
        filtered: dict[str, str] = {}
        for path, patch in patches.items():
            reason = review_file_filter_reason(path, patch)
            if reason:
                skipped[path] = reason
                continue
            filtered[path] = patch
        result = await asyncio.to_thread(
            self.preprocessor.diff_units,
            filtered,
            review_query=(review_query or "").strip() or _DEFAULT_DIFF_QUERY,
        )
        # File-level skip reasons from the snapshot become skipped units too, so
        # the coverage block states why a changed file carries no evidence.
        result.skipped.extend(
            SkippedUnit(path=path, reason=str(reason) or "filtered")
            for path, reason in sorted(skipped.items())
        )
        self.last_result = result
        self.last_changed_files = list(filtered)
        log_event(
            logger,
            "info",
            "review.evidence.snapshot.done",
            status="success" if filtered else "empty",
            trace_id=trace_id or "snapshot",
            changed_files=len(filtered),
            skipped_files=len(skipped),
            units_count=len(result.units),
            skipped_count=len(result.skipped),
            total_tokens=result.total_tokens,
            context_chars=len(result.context),
            elapsed_ms=f"{(time.perf_counter() - started) * 1000:.1f}",
        )
        return result.context


__all__ = ["ReviewEvidenceService"]
