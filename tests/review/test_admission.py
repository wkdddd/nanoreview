"""Admission boundary tests for local diff review requests.

Admission is the single place where a review is validated, snapshotted, and
registered. These tests pin the invariants the transports depend on: rejection
is all-or-nothing (no session, run, snapshot, or history), a diff review reads
the net change relative to ``HEAD``, only local diff review is reachable, and
accepted runs reuse one registration so a duplicate submission can never
overwrite the original metadata.
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
from nanoreview.review.types import ReviewAction, ReviewMetaKey
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


def _commit_all(root: Path, message: str = "init") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


def _init_repo_with_commit(root: Path, files: dict[str, str]) -> None:
    """Init a repo with a committed baseline so a later change is diffable."""
    _init_repo(root)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _commit_all(root)


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
# Diff admission
# ---------------------------------------------------------------------------


def test_diff_file_target_is_accepted_and_snapshotted(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    # An explicit worktree keeps the review root deterministic: a file target
    # resolves to its enclosing Git root when one exists.
    _init_repo_with_commit(tmp_path, {"src/app.py": "print('hi')\n"})
    (tmp_path / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")

    admission = service.admit(
        _request(target=str(tmp_path / "src" / "app.py"), action="diff")
    )

    assert admission.target_type == "local"
    assert admission.action is ReviewAction.DIFF
    assert admission.scope is not None
    assert admission.scope.kind == "file"
    assert admission.snapshot_ref.startswith(f"{REVIEW_SNAPSHOTS_DIR_NAME}/")
    snapshot = _read_snapshot(tmp_path, admission.snapshot_ref)
    assert snapshot["action"] == "diff"
    assert "src/app.py" in snapshot["net_diff"]
    assert snapshot["changed_files"] == ["src/app.py"]
    assert snapshot["git_head"]
    assert snapshot["input_fingerprint"] == admission.input_fingerprint
    assert snapshot["run_id"] == admission.run_id


def test_default_action_is_diff(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    """A request without an action defaults to local diff review."""
    _init_repo_with_commit(tmp_path, {"base.py": "b = 0\n"})
    (tmp_path / "base.py").write_text("b = 1\n", encoding="utf-8")

    admission = service.admit(_request(target=str(tmp_path)))

    assert admission.action is ReviewAction.DIFF


def test_repo_action_is_rejected_as_invalid(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    """The removed repo entry point is rejected with a structured code."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(_request(target=str(tmp_path), action="repo"))

    assert excinfo.value.code is ReviewAdmissionCode.INVALID_ACTION
    assert excinfo.value.field == "action"
    assert not list((tmp_path / REVIEW_SNAPSHOTS_DIR_NAME).glob("*.json"))


def test_admission_persists_navigation_metadata(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    target_dir = tmp_path / "pkg"
    _init_repo_with_commit(tmp_path, {"pkg/mod.py": "x = 1\n"})
    (target_dir / "mod.py").write_text("x = 2\n", encoding="utf-8")

    admission = service.admit(_request(target=str(target_dir), action="diff"))
    session = SessionManager(tmp_path).get_or_create(admission.session_key)

    assert session.metadata[ReviewMetaKey.RUN_ID] == admission.run_id
    assert session.metadata[ReviewMetaKey.STATUS] == "running"
    assert session.metadata[ReviewMetaKey.PHASE] == "prepare"
    assert session.metadata[ReviewMetaKey.SNAPSHOT_REF] == admission.snapshot_ref
    assert session.metadata[ReviewMetaKey.LOCAL_ROOT] == str(target_dir.resolve())
    assert session.metadata[ReviewMetaKey.ACTION] == "diff"


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
    _init_repo_with_commit(tmp_path, {"base.py": "b = 0\n"})
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    admission = service.admit(_request(target="app.py", cwd=str(tmp_path), action="diff"))

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
    _init_repo_with_commit(tmp_path, {"base.py": "b = 0\n"})
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    first = service.admit(_request(target=str(tmp_path / "app.py"), action="diff"))
    original_metadata = dict(SessionManager(tmp_path).get_or_create(first.session_key).metadata)

    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(
            _request(target=str(tmp_path / "app.py"), focus=["security"], action="diff")
        )

    assert excinfo.value.code is ReviewAdmissionCode.DUPLICATE_REVIEW
    assert excinfo.value.status == 409
    unchanged = SessionManager(tmp_path).get_or_create(first.session_key)
    assert unchanged.metadata == original_metadata


# ---------------------------------------------------------------------------
# Diff input rejection
# ---------------------------------------------------------------------------


def test_diff_outside_git_repo_is_rejected(
    tmp_path: Path, service: ReviewAdmissionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    # Isolate the target from any enclosing repository (the pytest tmp dir can
    # itself live inside a worktree) so it is genuinely outside Git.
    monkeypatch.setattr(
        "nanoreview.review.input.local_git.find_git_root", lambda path: None
    )

    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(_request(target=str(tmp_path), action="diff"))

    assert excinfo.value.code is ReviewAdmissionCode.NOT_A_GIT_REPO


def test_diff_without_head_commit_is_rejected_as_unavailable(tmp_path: Path) -> None:
    """A worktree with no ``HEAD`` cannot produce a diff and fails admission."""
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    service = ReviewAdmissionService(
        sessions=SessionManager(tmp_path), workspace=tmp_path
    )
    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(_request(target=str(tmp_path), action="diff"))

    assert excinfo.value.code is ReviewAdmissionCode.DIFF_UNAVAILABLE


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
# Remote input rejection
# ---------------------------------------------------------------------------


def test_github_target_type_is_rejected_with_structured_code(
    service: ReviewAdmissionService,
) -> None:
    """`github` is no longer a valid review target type."""
    with pytest.raises(ReviewAdmissionError) as excinfo:
        service.admit(
            _request(target="https://github.com/owner/repo", target_type="github")
        )

    assert excinfo.value.code == "invalid_target_type"


def test_github_url_target_without_type_is_still_validated_locally(
    tmp_path: Path, service: ReviewAdmissionService
) -> None:
    """A GitHub URL is not special-cased: it is treated as a (non-existent) local path."""
    with pytest.raises(ReviewAdmissionError):
        service.admit(_request(target="https://github.com/owner/repo"))
