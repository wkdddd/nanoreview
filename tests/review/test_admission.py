"""Admission boundary tests for local review requests.

Admission is the single place where a review is validated, snapshotted, and
registered. These tests pin the invariants the transports depend on: rejection
is all-or-nothing (no session, run, snapshot, or history), a diff review reads
the net change relative to ``HEAD``, and accepted runs reuse one registration
so a duplicate submission can never overwrite the original metadata.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nanoreview.review.admission import (
    ReviewAdmissionCode,
    ReviewAdmissionError,
    ReviewAdmissionRequest,
    ReviewAdmissionService,
)
from nanoreview.review.input.snapshot import REVIEW_SNAPSHOTS_DIR_NAME
from nanoreview.review.types import ReviewMetaKey
from nanoreview.session.manager import SessionManager


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")


@pytest.fixture()
def service(tmp_path: Path) -> ReviewAdmissionService:
    return ReviewAdmissionService(sessions=SessionManager(tmp_path), workspace=tmp_path)


def _request(**overrides: Any) -> ReviewAdmissionRequest:
    payload: dict[str, Any] = {"session_key": "cli:review:test"}
    payload.update(overrides)
    return ReviewAdmissionRequest(**payload)


def _read_snapshot(tmp_path: Path, ref: str) -> dict[str, Any]:
    path = tmp_path / ref
    assert path.is_file(), f"snapshot missing: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Repository (repo) admission
# ---------------------------------------------------------------------------


def test_repo_file_target_is_accepted_and_snapshotted(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    # An explicit worktree keeps the review root deterministic: a file target
    # resolves to its enclosing Git root when one exists.
    _init_repo(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("print('hi')\n", encoding="utf-8")

    admission = service.admit(_request(target=str(source / "app.py")))

    assert admission.target_type == "local"
    assert admission.scope is not None
    assert admission.scope.kind == "file"
    assert admission.snapshot_ref.startswith(f"{REVIEW_SNAPSHOTS_DIR_NAME}/")
    snapshot = _read_snapshot(tmp_path, admission.snapshot_ref)
    assert snapshot["repo_content"]["src/app.py"] == "print('hi')\n"
    assert snapshot["input_fingerprint"] == admission.input_fingerprint
    assert snapshot["run_id"] == admission.run_id


def test_repo_admission_persists_navigation_metadata(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    target_dir = tmp_path / "pkg"
    target_dir.mkdir()
    (target_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")

    admission = service.admit(_request(target=str(target_dir)))
    session = SessionManager(tmp_path).get_or_create(admission.session_key)

    assert session.metadata[ReviewMetaKey.RUN_ID] == admission.run_id
    assert session.metadata[ReviewMetaKey.STATUS] == "running"
    assert session.metadata[ReviewMetaKey.PHASE] == "prepare"
    assert session.metadata[ReviewMetaKey.SNAPSHOT_REF] == admission.snapshot_ref
    assert session.metadata[ReviewMetaKey.LOCAL_ROOT] == str(target_dir.resolve())
    assert session.metadata[ReviewMetaKey.ACTION] == "repo"


def test_relative_target_is_rejected_when_absolute_required(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(_request(target="app.py", enforce_absolute=True, cwd=str(tmp_path)))

    assert excinfo.value.code is ReviewAdmissionCode.RELATIVE_PATH_NOT_ALLOWED
    assert excinfo.value.status == 400
    assert not list((tmp_path / REVIEW_SNAPSHOTS_DIR_NAME).glob("*.json"))


def test_relative_target_resolves_against_cwd_for_cli(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    admission = service.admit(_request(target="app.py", cwd=str(tmp_path)))

    assert admission.target == str((tmp_path / "app.py").resolve())


def test_missing_target_is_rejected_without_side_effects(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(_request(target=str(tmp_path / "nope")))

    assert excinfo.value.code is ReviewAdmissionCode.TARGET_NOT_FOUND
    assert excinfo.value.status == 404
    # Rejection writes no session metadata and no snapshot.
    session = SessionManager(tmp_path).get_or_create("cli:review:test")
    assert ReviewMetaKey.RUN_ID not in session.metadata
    assert not (tmp_path / REVIEW_SNAPSHOTS_DIR_NAME).exists()


def test_scope_outside_target_is_rejected(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    (outside / "other.py").write_text("y = 2\n", encoding="utf-8")

    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(
            _request(target=str(inside), scope=[str(outside / "other.py")])
        )

    assert excinfo.value.code is ReviewAdmissionCode.SCOPE_OUTSIDE_TARGET


def test_duplicate_review_is_rejected_without_touching_original(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    first = service.admit(_request(target=str(tmp_path / "app.py")))
    original_metadata = dict(SessionManager(tmp_path).get_or_create(first.session_key).metadata)

    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(
            _request(target=str(tmp_path / "app.py"), focus=["security"])
        )

    assert excinfo.value.code is ReviewAdmissionCode.DUPLICATE_REVIEW
    assert excinfo.value.status == 409
    unchanged = SessionManager(tmp_path).get_or_create(first.session_key)
    assert unchanged.metadata == original_metadata


# ---------------------------------------------------------------------------
# Diff admission
# ---------------------------------------------------------------------------


def test_diff_outside_git_repo_is_rejected(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(_request(target=str(tmp_path), action="diff"))

    assert excinfo.value.code is ReviewAdmissionCode.NOT_A_GIT_REPO


def test_diff_collapses_staged_and_unstaged_into_one_net_change(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("line1\nline2\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "init")
    # Stage one edit, then make a further unstaged edit on top of it.
    (tmp_path / "app.py").write_text("line1\nstaged\nline2\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    (tmp_path / "app.py").write_text("line1\nstaged\nunstaged\nline2\n", encoding="utf-8")

    service = ReviewAdmissionService(
        sessions=SessionManager(tmp_path), workspace=tmp_path
    )
    admission = service.admit(_request(target=str(tmp_path), action="diff"))

    snapshot = _read_snapshot(tmp_path, admission.snapshot_ref)
    patch = snapshot["net_diff"]["app.py"]
    # One snapshot per path: the staged and unstaged hunks are merged into a
    # single HEAD-relative diff rather than captured twice.
    assert patch.count("diff --git") == 1
    assert "staged" in patch and "unstaged" in patch
    assert snapshot["git_head"]


def test_diff_empty_workspace_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "init")

    service = ReviewAdmissionService(
        sessions=SessionManager(tmp_path), workspace=tmp_path
    )
    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(_request(target=str(tmp_path), action="diff"))

    assert excinfo.value.code is ReviewAdmissionCode.EMPTY_DIFF


def test_diff_scope_without_changes_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "init")
    # Change a file that is *outside* the requested scope.
    (tmp_path / "other.py").write_text("y = 2\n", encoding="utf-8")

    service = ReviewAdmissionService(
        sessions=SessionManager(tmp_path), workspace=tmp_path
    )
    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(
            _request(
                target=str(tmp_path),
                action="diff",
                scope=[str(tmp_path / "app.py")],
            )
        )

    assert excinfo.value.code is ReviewAdmissionCode.SCOPE_NO_CHANGES


def test_diff_untracked_file_is_included(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "new.py").write_text("print('new')\n", encoding="utf-8")

    service = ReviewAdmissionService(
        sessions=SessionManager(tmp_path), workspace=tmp_path
    )
    admission = service.admit(_request(target=str(tmp_path), action="diff"))

    snapshot = _read_snapshot(tmp_path, admission.snapshot_ref)
    assert "new.py" in snapshot["net_diff"]
    assert "print('new')" in snapshot["net_diff"]["new.py"]


# ---------------------------------------------------------------------------
# Remote passthrough
# ---------------------------------------------------------------------------


def test_github_target_still_admits_without_local_validation(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    """Remote targets keep their existing planning path in this round."""
    admission = service.admit(
        _request(target="https://github.com/owner/repo", target_type="github")
    )

    assert admission.target_type == "github"
    snapshot = _read_snapshot(tmp_path, admission.snapshot_ref)
    assert snapshot["extra"]["repo"] == "owner/repo"
