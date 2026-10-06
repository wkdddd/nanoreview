"""Contract tests for the turn-level workspace access scope.

The scope replaced per-tool approval in this round: instead of pausing each
tool call for confirmation, every conversation turn resolves one immutable
``WorkspaceScope`` up front and the generic tools honour it for the whole turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoreview.agent.tools.workspace_scope import (
    ACCESS_FULL,
    ACCESS_RESTRICTED,
    WORKSPACE_SCOPE_KEY,
    resolve_workspace_scope,
    review_workspace_scope,
)


def _payload(path: Path, mode: str) -> dict[str, object]:
    return {WORKSPACE_SCOPE_KEY: {"project_path": str(path), "access_mode": mode}}


class TestConfigDefault:
    def test_restrict_disabled_defaults_to_full(self, tmp_path: Path):
        scope = resolve_workspace_scope(
            default_project_path=tmp_path,
            restrict_to_workspace=False,
        )

        assert scope.access_mode == ACCESS_FULL
        assert scope.is_restricted is False

    def test_restrict_enabled_defaults_to_restricted(self, tmp_path: Path):
        scope = resolve_workspace_scope(
            default_project_path=tmp_path,
            restrict_to_workspace=True,
        )

        assert scope.access_mode == ACCESS_RESTRICTED
        assert scope.is_restricted is True


class TestPrecedence:
    def test_message_metadata_wins_over_session(self, tmp_path: Path):
        other = tmp_path / "other"
        other.mkdir()

        scope = resolve_workspace_scope(
            default_project_path=tmp_path,
            restrict_to_workspace=True,
            message_metadata=_payload(other, "full"),
            session_metadata=_payload(tmp_path, "restricted"),
        )

        assert scope.project_path == other.resolve()
        assert scope.access_mode == ACCESS_FULL

    def test_session_metadata_wins_over_config(self, tmp_path: Path):
        scope = resolve_workspace_scope(
            default_project_path=tmp_path,
            restrict_to_workspace=True,
            session_metadata=_payload(tmp_path, "full"),
        )

        assert scope.access_mode == ACCESS_FULL

    def test_alias_values_are_normalized(self, tmp_path: Path):
        for alias, expected in (
            ("restrict", ACCESS_RESTRICTED),
            ("restricted", ACCESS_RESTRICTED),
            ("full", ACCESS_FULL),
            ("full-access", ACCESS_FULL),
            ("  FULL  ", ACCESS_FULL),
        ):
            scope = resolve_workspace_scope(
                default_project_path=tmp_path,
                restrict_to_workspace=False,
                message_metadata=_payload(tmp_path, alias),
            )
            assert scope.access_mode == expected, alias


class TestInvalidPayloadFallback:
    @pytest.mark.parametrize(
        "payload",
        [
            "not-a-mapping",
            {},
            {"project_path": None, "access_mode": "full"},
            {"project_path": "  ", "access_mode": "full"},
            {"project_path": "relative/path", "access_mode": "full"},
            {"access_mode": "full"},
            {"project_path": "x", "access_mode": "bogus"},
            {"project_path": "x", "access_mode": 3},
        ],
    )
    def test_invalid_message_payload_falls_back_to_config(self, tmp_path: Path, payload):
        scope = resolve_workspace_scope(
            default_project_path=tmp_path,
            restrict_to_workspace=True,
            message_metadata={"workspace_scope": payload},
        )

        assert scope.project_path == tmp_path
        assert scope.access_mode == ACCESS_RESTRICTED

    def test_nonexistent_directory_is_rejected(self, tmp_path: Path):
        missing = tmp_path / "does-not-exist"

        scope = resolve_workspace_scope(
            default_project_path=tmp_path,
            restrict_to_workspace=False,
            message_metadata=_payload(missing, "restricted"),
        )

        assert scope.access_mode == ACCESS_FULL

    def test_invalid_message_payload_does_not_fall_through_to_valid_session(
        self, tmp_path: Path
    ):
        """A malformed payload is skipped, so resolution continues to the next level."""
        other = tmp_path / "other"
        other.mkdir()

        scope = resolve_workspace_scope(
            default_project_path=tmp_path,
            restrict_to_workspace=False,
            message_metadata={"workspace_scope": {"access_mode": "full"}},
            session_metadata=_payload(other, "restricted"),
        )

        assert scope.project_path == other.resolve()
        assert scope.access_mode == ACCESS_RESTRICTED

    def test_absent_key_does_not_override(self, tmp_path: Path):
        scope = resolve_workspace_scope(
            default_project_path=tmp_path,
            restrict_to_workspace=True,
            message_metadata={"something_else": 1},
            session_metadata=None,
        )

        assert scope.access_mode == ACCESS_RESTRICTED

    def test_dot_path_is_resolved_to_absolute(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        scope = resolve_workspace_scope(
            default_project_path=tmp_path,
            restrict_to_workspace=False,
            message_metadata=_payload(Path("."), "restricted"),
        )

        assert scope.project_path.is_absolute()
        assert scope.project_path == tmp_path.resolve()


class TestReviewScope:
    def test_review_scope_is_always_restricted(self, tmp_path: Path):
        scope = review_workspace_scope(tmp_path)

        assert scope.access_mode == ACCESS_RESTRICTED
        assert scope.project_path == tmp_path.resolve()

    def test_review_scope_resolves_relative_path(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        scope = review_workspace_scope(Path("."))

        assert scope.access_mode == ACCESS_RESTRICTED
        assert scope.project_path.is_absolute()

    def test_review_scope_is_frozen(self, tmp_path: Path):
        scope = review_workspace_scope(tmp_path)

        with pytest.raises(Exception):
            scope.access_mode = ACCESS_FULL  # type: ignore[misc]
