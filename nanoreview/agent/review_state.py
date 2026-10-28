"""Review run phases, input fingerprint, and report artifacts.

One review session executes at most one review run. The run's *live* state is
the coordinator-owned blackboard (``agent/review_board.py``); this module keeps
only what outlives an individual in-process board: the phase/status vocabulary,
the input fingerprint, and the persisted report artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoreview.utils.helpers import safe_filename

if TYPE_CHECKING:
    from nanoreview.agent.review_board import ReviewBoardView
    from nanoreview.review.output.finalizer import ReviewFinalizerResult
    from nanoreview.review.types import ReviewEvidenceBundle, ReviewPlan

# Serialized artifact size limit, mirroring the shared media upload guard.
REVIEW_ARTIFACT_MAX_BYTES = 10 * 1024 * 1024

# Directory (relative to the workspace) that stores review report artifacts.
REVIEW_ARTIFACTS_DIR_NAME = "review-artifacts"

#: Bound on the persisted run summary — a digest is carried in the structured
#: result so transports and the handoff context can describe a finished review
#: without loading the full report artifact.
REVIEW_SUMMARY_MAX_CHARS = 1200

# run_id charset: hex prefix keeps filenames filesystem-safe on every platform.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ReviewPhase(StrEnum):
    """Supervisor phases of one review run.

    The phases are strictly ordered:

    ``PREPARE -> PLAN -> REVIEW -> FINALIZE -> CLEANUP -> DONE``

    ``CLEANUP`` runs after the report artifact is persisted and before the run
    turns terminal, so resource cleanup is never skipped on a success, error or
    stop exit. The wire values of the pre-existing phases are unchanged.
    """

    PREPARE = "prepare"
    PLAN = "plan"
    REVIEW = "review"
    FINALIZE = "finalize"
    CLEANUP = "cleanup"
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

#: Child (reviewer/judge) error/failure reason length cap. Error text stored on
#: the board must be bounded so a huge subagent transcript or provider trace
#: cannot inflate the in-process audit trail.
CHILD_ERROR_MAX_CHARS = 300


def bound_child_error(reason: Any) -> str:
    """Bound an error/failure reason to ``CHILD_ERROR_MAX_CHARS`` characters."""
    if reason is None:
        return ""
    text = " ".join(str(reason).split())
    return text[:CHILD_ERROR_MAX_CHARS]


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

    The same fingerprint value must be carried by the run's board, the report
    artifact, and session metadata so the report API can verify that an
    artifact belongs to the requested session and run.
    """
    payload: dict[str, Any] = {
        "target": (target or "").strip() or None,
        "target_type": (target_type or "auto").strip().lower(),
        "action": (action or "diff").strip().lower(),
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
    if plan is not None and plan.local_scope is not None:
        scope = {
            "kind": plan.local_scope.kind,
            "review_root": plan.local_scope.review_root,
            "scope_paths": sorted(plan.local_scope.scope_paths),
            "target_path": plan.local_scope.target_path,
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


def _serialize_candidate(candidate: Any, *, finding_id: str | None = None) -> dict[str, Any]:
    payload = {
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
    #: Only confirmed findings get a user-facing report-local ID (``F001``).
    if finding_id:
        payload["id"] = finding_id
    return payload


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
) -> tuple[tuple[dict[str, Any], ...], list[dict[str, Any]]]:
    """Extract (findings, verdicts) wire payloads from a finalizer result.

    Confirmed findings carry their stable report-local ID (``F001``) so the
    report artifact, ``ReviewResult.findings`` and the Web/API state agree with
    the rendered Markdown. Rejected and uncertain candidates get no ID: those
    IDs are user-facing and only name actionable findings.
    """
    # Local import: ``report`` -> ``judge`` -> this module would otherwise cycle.
    from nanoreview.review.output.report import (
        confirmed_finding_ids_by_key,
        finding_key,
    )

    ids_by_key = confirmed_finding_ids_by_key(result.dimensions)
    findings: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    for dimension in result.dimensions:
        for judged in dimension.judged:
            findings.append(
                _serialize_candidate(
                    judged.candidate,
                    finding_id=ids_by_key.get(finding_key(judged.candidate)),
                )
            )
            verdicts.append(_serialize_verdict(judged))
    return tuple(findings), verdicts


def build_report_artifact(
    view: "ReviewBoardView",
    *,
    report_markdown: str,
    verdicts: list[dict[str, Any]] | None = None,
    status: ReviewRunStatus | None = None,
) -> dict[str, Any]:
    """Assemble the JSON artifact payload for a finished review run.

    An artifact describes a finished run, so the caller must decide the
    terminal status *before* serialization: the board's status can still be
    ``running`` while the artifact is being written, and baking that in would
    leave a permanently "running" report on disk. Pass ``status`` explicitly;
    the board's own value is only a fallback.

    The ``coverage`` block carries the run's review boundary and per-reviewer
    execution/cost summary (evidence budget, changed/skipped files, duplicate
    reads, incomplete dimensions) without copying any source or full session.
    """
    return {
        "run_id": view.run_id,
        "session_key": view.session_key,
        "status": (status or view.status).value,
        "input_fingerprint": view.input_fingerprint,
        "snapshot_ref": view.snapshot_ref,
        "report_markdown": report_markdown,
        "findings": list(view.findings),
        "verdicts": list(verdicts or []),
        "usage": dict(view.usage),
        "warnings": list(view.warnings),
        "coverage": view.coverage_payload(),
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
