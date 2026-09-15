"""In-process ReviewAgent run state, input fingerprint, and report artifacts.

One review session executes at most one review run. ``ReviewRunState`` tracks
that run inside the agent process; the final report is persisted as an
independent JSON artifact under ``review-artifacts/`` in the workspace and
referenced from session metadata via ``review_report_ref``.

This module intentionally stores only plain data — no asyncio tasks, provider
clients, callbacks, locks, or in-flight coroutines.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoreview.utils.helpers import merge_token_usage, safe_filename

if TYPE_CHECKING:
    from nanoreview.review.output.finalizer import ReviewFinalizerResult
    from nanoreview.review.types import (
        ReviewAssignment,
        ReviewEvidenceBundle,
        ReviewPlan,
    )

# Serialized artifact size limit, mirroring the shared media upload guard.
REVIEW_ARTIFACT_MAX_BYTES = 10 * 1024 * 1024

# Directory (relative to the workspace) that stores review report artifacts.
REVIEW_ARTIFACTS_DIR_NAME = "review-artifacts"

# run_id charset: hex prefix keeps filenames filesystem-safe on every platform.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ReviewPhase(StrEnum):
    """Supervisor phases of one review run."""

    PREPARE = "prepare"
    PLAN = "plan"
    REVIEW = "review"
    FINALIZE = "finalize"
    SAVE = "save"
    RESPOND = "respond"
    DONE = "done"


class ReviewRunStatus(StrEnum):
    """Terminal-able status of one review run."""

    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    STOPPED = "stopped"


REVIEW_TERMINAL_STATUSES = frozenset(
    {ReviewRunStatus.COMPLETED, ReviewRunStatus.ERROR, ReviewRunStatus.STOPPED}
)


@dataclass(slots=True)
class ReviewerRunState:
    """Per-reviewer (dimension) execution state."""

    dimension: str
    status: str = "pending"  # pending | running | completed | error
    error: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    def add_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Accumulate this reviewer's token usage for the run audit trail."""
        merge_token_usage(self.usage, usage)


@dataclass(slots=True)
class JudgeBatchState:
    """Per-batch judge execution state."""

    batch_id: str
    status: str = "pending"  # pending | completed | error
    stats: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)

    def add_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Accumulate this judge batch's token usage."""
        merge_token_usage(self.usage, usage)


@dataclass(slots=True)
class ReviewRunState:
    """In-process state of one review run owned by ``AgentLoop``."""

    run_id: str
    session_key: str
    input_fingerprint: str
    phase: ReviewPhase = ReviewPhase.PREPARE
    status: ReviewRunStatus = ReviewRunStatus.RUNNING
    plan: "ReviewPlan | None" = None
    assignments: tuple["ReviewAssignment", ...] = ()
    reviewers: dict[str, ReviewerRunState] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    judge_batches: dict[str, JudgeBatchState] = field(default_factory=dict)
    report_ref: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def enter_phase(self, phase: ReviewPhase) -> None:
        if self.status is not ReviewRunStatus.RUNNING:
            return
        self.phase = phase

    def add_warning(self, message: str) -> None:
        text = str(message).strip()
        if text and text not in self.warnings:
            self.warnings.append(text)

    def add_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Accumulate one agent run's token usage into this review run."""
        merge_token_usage(self.usage, usage)

    def reviewer_state(self, dimension: str) -> ReviewerRunState:
        state = self.reviewers.get(dimension)
        if state is None:
            state = ReviewerRunState(dimension=dimension)
            self.reviewers[dimension] = state
        return state

    def metadata_payload(self) -> dict[str, Any]:
        """Stable session metadata fields describing this run.

        Only the artifact reference, run id, status and phase cross the wire —
        never the full report or any absolute server path.
        """
        payload: dict[str, Any] = {
            "review_run_id": self.run_id,
            "review_status": self.status.value,
            "review_phase": self.phase.value,
            "review_input_fingerprint": self.input_fingerprint,
        }
        if self.report_ref is not None:
            payload["review_report_ref"] = self.report_ref
        return payload


def new_review_run_id() -> str:
    """Generate a filesystem-safe unique review run id."""
    return f"run-{uuid.uuid4().hex[:12]}"


def _canonical_json(data: Any) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _evidence_manifest(
    evidence: "ReviewEvidenceBundle | None",
) -> list[dict[str, Any]]:
    if evidence is None:
        return []
    manifest = [
        {
            "id": reference.id,
            "path": reference.path,
            "start_line": reference.start_line,
            "end_line": reference.end_line,
            "kind": reference.kind,
            "tokens": reference.token_count,
        }
        for reference in evidence.references
    ]
    manifest.sort(key=lambda item: item["id"])
    return manifest


def compute_input_fingerprint(
    *,
    target: str | None,
    target_type: str | None,
    action: str | None,
    roles: list[str] | tuple[str, ...] | None = None,
    scope: dict[str, Any] | None = None,
    evidence_manifest: list[dict[str, Any]] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> str:
    """SHA-256 over the canonical JSON of the normalized review input.

    The same fingerprint value must be carried by ``ReviewRunState``, the
    report artifact, and session metadata so the report API can verify that an
    artifact belongs to the requested session and run.
    """
    payload: dict[str, Any] = {
        "target": (target or "").strip() or None,
        "target_type": (target_type or "auto").strip().lower(),
        "action": (action or "repo").strip().lower(),
        "roles": sorted({str(role) for role in roles or []}),
        "scope": scope or {},
        "evidence_manifest": evidence_manifest or [],
        "extra": extra_metadata or {},
    }
    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def compute_review_input_fingerprint(
    plan: "ReviewPlan | None",
    evidence: "ReviewEvidenceBundle | None",
    *,
    extra_metadata: dict[str, Any] | None = None,
) -> str:
    """Build the input fingerprint from a resolved review plan and evidence."""
    scope: dict[str, Any] = {}
    if plan is not None:
        if plan.local_scope is not None:
            scope = {
                "kind": plan.local_scope.kind,
                "review_root": plan.local_scope.review_root,
                "scope_paths": sorted(plan.local_scope.scope_paths),
                "target_path": plan.local_scope.target_path,
            }
        else:
            scope = {
                "repo": plan.target_repo,
                "pr_number": plan.pr_number,
                "target_ref": plan.target_ref,
                "subpath": plan.target_subpath,
            }
    return compute_input_fingerprint(
        target=plan.target if plan is not None else None,
        target_type=plan.target_type if plan is not None else None,
        action=plan.action.value if plan is not None else None,
        roles=[role.name for role in plan.roles] if plan is not None else None,
        scope=scope,
        evidence_manifest=_evidence_manifest(evidence),
        extra_metadata=extra_metadata,
    )


# ---------------------------------------------------------------------------
# Report artifact serialization
# ---------------------------------------------------------------------------


def _serialize_candidate(candidate: Any) -> dict[str, Any]:
    return {
        "severity": candidate.severity,
        "dimension": candidate.dimension,
        "file": candidate.file,
        "line": candidate.line,
        "title": candidate.title,
        "evidence": candidate.evidence,
        "impact": candidate.impact,
        "recommendation": candidate.recommendation,
        "confidence": candidate.confidence,
        "source": candidate.source,
    }


def _serialize_verdict(judged: Any) -> dict[str, Any]:
    judge_verdict = judged.judge_verdict
    return {
        "dimension": judged.candidate.dimension,
        "file": judged.candidate.file,
        "line": judged.candidate.line,
        "title": judged.candidate.title,
        "final_verdict": judged.final_verdict.value,
        "hard_verdict": judged.hard_verdict.verdict.value,
        "hard_reason": judged.hard_verdict.reason,
        "judge_decision": (
            judge_verdict.decision.value if judge_verdict is not None else None
        ),
        "judge_reason": judge_verdict.reason if judge_verdict is not None else "",
        "judge_confidence": (
            judge_verdict.confidence if judge_verdict is not None else ""
        ),
    }


def serialize_finalizer_result(
    result: "ReviewFinalizerResult",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract (findings, verdicts) wire payloads from a finalizer result."""
    findings: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    for dimension in result.dimensions:
        for judged in dimension.judged:
            findings.append(_serialize_candidate(judged.candidate))
            verdicts.append(_serialize_verdict(judged))
    return findings, verdicts


def build_report_artifact(
    state: ReviewRunState,
    *,
    report_markdown: str,
    verdicts: list[dict[str, Any]] | None = None,
    status: ReviewRunStatus | None = None,
) -> dict[str, Any]:
    """Assemble the JSON artifact payload for a finished review run.

    An artifact describes a finished run, so the caller must decide the
    terminal status *before* serialization: ``ReviewRunState.status`` can
    still be ``running`` while the artifact is being written, and baking
    that in would leave a permanently "running" report on disk. Pass
    ``status`` explicitly; the state's own value is only a fallback.
    """
    return {
        "run_id": state.run_id,
        "session_key": state.session_key,
        "status": (status or state.status).value,
        "input_fingerprint": state.input_fingerprint,
        "report_markdown": report_markdown,
        "findings": state.findings,
        "verdicts": list(verdicts or []),
        "usage": dict(state.usage),
        "warnings": list(state.warnings),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


class ReviewArtifactError(RuntimeError):
    """Raised when a report artifact is missing, corrupt, or mismatched."""

    def __init__(self, reason: str, *, status: int = 409) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class ReviewArtifactStore:
    """Persist and verify review report artifacts inside the workspace."""

    def __init__(
        self,
        workspace: Path,
        *,
        max_bytes: int = REVIEW_ARTIFACT_MAX_BYTES,
    ) -> None:
        self._workspace = Path(workspace)
        self._max_bytes = max_bytes

    @property
    def directory(self) -> Path:
        return self._workspace / REVIEW_ARTIFACTS_DIR_NAME

    def path_for(self, run_id: str) -> Path:
        """Resolve the artifact path for *run_id*; reject unsafe ids."""
        if not _RUN_ID_RE.match(run_id or ""):
            raise ReviewArtifactError("invalid run id", status=409)
        return self.directory / f"{safe_filename(run_id)}.json"

    def reference_for(self, run_id: str) -> str:
        """Wire-safe relative reference (never a server absolute path)."""
        return f"{REVIEW_ARTIFACTS_DIR_NAME}/{safe_filename(run_id)}.json"

    def write(self, artifact: dict[str, Any]) -> str | None:
        """Atomically persist *artifact*; return its wire reference.

        Returns ``None`` when the write fails (size limit or I/O error) — the
        caller must then leave ``review_report_ref`` unset, mark the session as
        ``error`` and record a warning.
        """
        run_id = str(artifact.get("run_id") or "")
        if not _RUN_ID_RE.match(run_id):
            logger.warning("review.artifact.write.rejected reason=invalid_run_id")
            return None
        try:
            payload = json.dumps(artifact, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            logger.warning("review.artifact.write.rejected reason=serialize error={}", exc)
            return None
        if len(payload.encode("utf-8")) > self._max_bytes:
            logger.warning(
                "review.artifact.write.rejected reason=size_limit bytes={} limit={}",
                len(payload.encode("utf-8")),
                self._max_bytes,
            )
            return None
        path = self.path_for(run_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("review.artifact.write.failed run_id={} error={}", run_id, exc)
            return None
        reference = self.reference_for(run_id)
        logger.info(
            "review.artifact.written run_id={} ref={} bytes={}",
            run_id,
            reference,
            len(payload.encode("utf-8")),
        )
        return reference

    def read(
        self,
        *,
        run_id: str,
        session_key: str,
        input_fingerprint: str,
    ) -> dict[str, Any]:
        """Load and verify the artifact for a review run.

        Raises ``ReviewArtifactError`` with a 404 status when the artifact is
        missing, and 409 when it is corrupt or does not match the requesting
        session/run (run id, session key, fingerprint).
        """
        path = self.path_for(run_id)
        if not path.is_file():
            raise ReviewArtifactError("artifact not found", status=404)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReviewArtifactError("artifact unreadable", status=409) from exc
        if len(raw.encode("utf-8")) > self._max_bytes:
            raise ReviewArtifactError("artifact exceeds size limit", status=409)
        try:
            artifact = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReviewArtifactError("artifact corrupt", status=409) from exc
        if not isinstance(artifact, dict):
            raise ReviewArtifactError("artifact corrupt", status=409)
        if artifact.get("run_id") != run_id:
            raise ReviewArtifactError("artifact run id mismatch", status=409)
        if artifact.get("session_key") != session_key:
            raise ReviewArtifactError("artifact session mismatch", status=409)
        if artifact.get("input_fingerprint") != input_fingerprint:
            raise ReviewArtifactError("artifact fingerprint mismatch", status=409)
        return artifact
