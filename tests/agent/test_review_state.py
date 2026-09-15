"""Tests for in-process ReviewRunState, input fingerprint, and report artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoreview.agent.review_state import (
    REVIEW_ARTIFACTS_DIR_NAME,
    JudgeBatchState,
    ReviewArtifactError,
    ReviewArtifactStore,
    ReviewerRunState,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
    build_report_artifact,
    compute_input_fingerprint,
    compute_review_input_fingerprint,
    new_review_run_id,
)
from nanoreview.review.types import ReviewMetaKey


def _run_state(**overrides) -> ReviewRunState:
    params: dict = {
        "run_id": "run-000111222333",
        "session_key": "websocket:chat-1",
        "input_fingerprint": "fp-1",
    }
    params.update(overrides)
    return ReviewRunState(**params)


def test_review_run_state_tracks_phase_and_warnings() -> None:
    state = _run_state()
    assert state.phase is ReviewPhase.PREPARE
    assert state.status is ReviewRunStatus.RUNNING

    state.enter_phase(ReviewPhase.PLAN)
    state.enter_phase(ReviewPhase.REVIEW)
    assert state.phase is ReviewPhase.REVIEW

    state.add_warning("w-1")
    state.add_warning("w-1")  # duplicate warnings are deduplicated
    state.add_warning("  ")  # blank warnings are ignored
    assert state.warnings == ["w-1"]


def test_review_run_state_enter_phase_ignored_after_terminal_status() -> None:
    state = _run_state(status=ReviewRunStatus.STOPPED)
    state.enter_phase(ReviewPhase.RESPOND)
    assert state.phase is ReviewPhase.PREPARE


def test_review_run_state_metadata_payload_shape() -> None:
    state = _run_state()
    payload = state.metadata_payload()
    assert payload == {
        ReviewMetaKey.RUN_ID: "run-000111222333",
        ReviewMetaKey.STATUS: "running",
        ReviewMetaKey.PHASE: "prepare",
        ReviewMetaKey.INPUT_FINGERPRINT: "fp-1",
    }
    assert ReviewMetaKey.REPORT_REF not in payload

    state.report_ref = f"{REVIEW_ARTIFACTS_DIR_NAME}/run-000111222333.json"
    payload = state.metadata_payload()
    assert payload[ReviewMetaKey.REPORT_REF] == state.report_ref


def test_review_run_state_child_states() -> None:
    state = _run_state()
    reviewer = state.reviewer_state("security")
    assert reviewer is state.reviewer_state("security")
    assert reviewer.status == "pending"

    state.judge_batches["b-1"] = JudgeBatchState(batch_id="b-1")
    state.judge_batches["b-1"].stats["total"] = 2
    assert state.judge_batches["b-1"].stats == {"total": 2}

    reviewer.status = "completed"
    assert state.reviewers["security"] == ReviewerRunState(
        dimension="security", status="completed"
    )


def test_new_review_run_id_is_filesystem_safe() -> None:
    run_id = new_review_run_id()
    assert run_id.startswith("run-")
    assert len(run_id) == len("run-") + 12


def test_input_fingerprint_is_stable_and_input_sensitive() -> None:
    kwargs = {
        "target": "owner/repo",
        "target_type": "github",
        "action": "repo",
        "roles": ["security", "tests"],
        "scope": {"repo": "owner/repo"},
        "evidence_manifest": [{"id": "ev-1", "path": "a.py", "tokens": 10}],
    }
    first = compute_input_fingerprint(**kwargs)
    # Role order and duplicate roles must not change the fingerprint.
    second = compute_input_fingerprint(**{**kwargs, "roles": ["tests", "security", "tests"]})
    assert first == second
    assert len(first) == 64

    different_target = compute_input_fingerprint(**{**kwargs, "target": "other/repo"})
    assert different_target != first

    different_evidence = compute_input_fingerprint(
        **{**kwargs, "evidence_manifest": [{"id": "ev-2", "path": "a.py", "tokens": 10}]}
    )
    assert different_evidence != first


def test_input_fingerprint_normalizes_defaults() -> None:
    assert compute_input_fingerprint(
        target=None, target_type=None, action=None
    ) == compute_input_fingerprint(target="", target_type="AUTO", action="REPO")


def test_compute_review_input_fingerprint_with_empty_inputs() -> None:
    assert compute_review_input_fingerprint(None, None) == compute_input_fingerprint(
        target=None, target_type=None, action=None, roles=None, scope=None,
        evidence_manifest=None, extra_metadata=None,
    )


def test_build_report_artifact_uses_run_state_fields() -> None:
    state = _run_state(status=ReviewRunStatus.COMPLETED)
    state.findings.append({"title": "finding-1"})
    state.usage["total_tokens"] = 42
    state.warnings.append("w-1")
    artifact = build_report_artifact(
        state, report_markdown="# report", verdicts=[{"final_verdict": "accepted"}]
    )
    assert artifact["run_id"] == state.run_id
    assert artifact["session_key"] == state.session_key
    assert artifact["status"] == "completed"
    assert artifact["input_fingerprint"] == state.input_fingerprint
    assert artifact["report_markdown"] == "# report"
    assert artifact["findings"] == [{"title": "finding-1"}]
    assert artifact["verdicts"] == [{"final_verdict": "accepted"}]
    assert artifact["usage"] == {"total_tokens": 42}
    assert artifact["warnings"] == ["w-1"]
    assert artifact["created_at"]


def test_build_report_artifact_status_overrides_running_state() -> None:
    """The artifact is persisted before the run status flips to terminal.

    ``AgentLoop`` must therefore pass the decided terminal status, otherwise
    a completed report would be written with ``status: running`` and later
    consumers would treat a finished report as still in flight.
    """
    state = _run_state(status=ReviewRunStatus.RUNNING)
    artifact = build_report_artifact(
        state,
        report_markdown="# report",
        status=ReviewRunStatus.COMPLETED,
    )
    assert artifact["status"] == "completed"
    # Writing the artifact never mutates the in-process run state.
    assert state.status is ReviewRunStatus.RUNNING


def test_build_report_artifact_can_record_a_failed_run() -> None:
    state = _run_state(status=ReviewRunStatus.RUNNING)
    artifact = build_report_artifact(
        state, report_markdown="", status=ReviewRunStatus.ERROR
    )
    assert artifact["status"] == "error"


def test_review_run_state_add_usage_accumulates_counters() -> None:
    state = _run_state()
    state.add_usage({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
    state.add_usage({"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60})
    assert state.usage == {
        "prompt_tokens": 150,
        "completion_tokens": 30,
        "total_tokens": 180,
    }
    # Empty, non-numeric, negative and None payloads are ignored.
    state.add_usage(None)
    state.add_usage({})
    state.add_usage({"total_tokens": "not-a-number"})
    state.add_usage({"total_tokens": -5})
    assert state.usage["total_tokens"] == 180


def test_reviewer_and_judge_state_accumulate_usage() -> None:
    reviewer = ReviewerRunState(dimension="security")
    reviewer.add_usage({"total_tokens": 11})
    reviewer.add_usage({"prompt_tokens": 4, "total_tokens": 9})
    assert reviewer.usage == {"prompt_tokens": 4, "total_tokens": 20}

    batch = JudgeBatchState(batch_id="judge")
    batch.add_usage({"total_tokens": 7})
    assert batch.usage == {"total_tokens": 7}


class TestReviewArtifactStore:
    def _store(self, tmp_path: Path) -> ReviewArtifactStore:
        return ReviewArtifactStore(tmp_path / "workspace")

    def _artifact(self, **overrides) -> dict:
        payload: dict = {
            "run_id": "run-abc123def456",
            "session_key": "websocket:chat-1",
            "status": "completed",
            "input_fingerprint": "fp-1",
            "report_markdown": "# report",
            "findings": [],
            "verdicts": [],
            "usage": {},
            "warnings": [],
            "created_at": "2026-09-15T00:00:00+00:00",
        }
        payload.update(overrides)
        return payload

    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        artifact = self._artifact()
        ref = store.write(artifact)
        assert ref == f"{REVIEW_ARTIFACTS_DIR_NAME}/run-abc123def456.json"

        loaded = store.read(
            run_id="run-abc123def456",
            session_key="websocket:chat-1",
            input_fingerprint="fp-1",
        )
        assert loaded == artifact

    def test_write_rejects_invalid_run_id(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        assert store.write(self._artifact(run_id="../escape")) is None
        assert store.write(self._artifact(run_id="")) is None
        assert not (tmp_path / "workspace" / REVIEW_ARTIFACTS_DIR_NAME).exists()

    def test_write_rejects_oversized_artifact(self, tmp_path: Path) -> None:
        store = ReviewArtifactStore(tmp_path, max_bytes=64)
        assert store.write(self._artifact()) is None

    def test_read_missing_artifact_raises_404(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        with pytest.raises(ReviewArtifactError) as excinfo:
            store.read(run_id="run-missing", session_key="s", input_fingerprint="f")
        assert excinfo.value.status == 404

    def test_read_corrupt_artifact_raises_409(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.write(self._artifact())
        path = store.path_for("run-abc123def456")
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ReviewArtifactError) as excinfo:
            store.read(
                run_id="run-abc123def456",
                session_key="websocket:chat-1",
                input_fingerprint="fp-1",
            )
        assert excinfo.value.status == 409

    def test_read_non_dict_artifact_raises_409(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.write(self._artifact())
        path = store.path_for("run-abc123def456")
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(ReviewArtifactError) as excinfo:
            store.read(
                run_id="run-abc123def456",
                session_key="websocket:chat-1",
                input_fingerprint="fp-1",
            )
        assert excinfo.value.status == 409

    def test_read_oversized_artifact_raises_409(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.write(self._artifact())
        path = store.path_for("run-abc123def456")
        path.write_text("x" * 128, encoding="utf-8")
        small = ReviewArtifactStore(tmp_path / "workspace", max_bytes=64)
        with pytest.raises(ReviewArtifactError) as excinfo:
            small.read(
                run_id="run-abc123def456",
                session_key="websocket:chat-1",
                input_fingerprint="fp-1",
            )
        assert excinfo.value.status == 409

    @pytest.mark.parametrize(
        ("content_overrides", "reason"),
        [
            ({"run_id": "run-other789012"}, "artifact run id mismatch"),
            ({"session_key": "websocket:chat-2"}, "artifact session mismatch"),
            ({"input_fingerprint": "fp-2"}, "artifact fingerprint mismatch"),
        ],
    )
    def test_read_mismatched_artifact_raises_409(
        self, tmp_path: Path, content_overrides: dict, reason: str
    ) -> None:
        store = self._store(tmp_path)
        # Write under the requested run id so the file exists at the expected
        # path, then rewrite its content to simulate a mismatched artifact.
        store.write(self._artifact())
        path = store.path_for("run-abc123def456")
        tampered = self._artifact(**content_overrides)
        path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ReviewArtifactError, match=reason) as excinfo:
            store.read(
                run_id="run-abc123def456",
                session_key="websocket:chat-1",
                input_fingerprint="fp-1",
            )
        assert excinfo.value.status == 409

    def test_path_for_rejects_unsafe_run_ids(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        with pytest.raises(ReviewArtifactError):
            store.path_for("../../etc/passwd")
        with pytest.raises(ReviewArtifactError):
            store.path_for("")

    def test_reference_for_uses_wire_relative_path(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        ref = store.reference_for("run-abc123def456")
        assert ref == f"{REVIEW_ARTIFACTS_DIR_NAME}/run-abc123def456.json"
        assert not Path(ref).is_absolute()
