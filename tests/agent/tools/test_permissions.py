"""Tests for deny-list tool execution policy."""

from __future__ import annotations

from pathlib import Path

from nanoreview.agent.tools.shell import ExecTool
from nanoreview.config.schema import Config


def _guard(tool: ExecTool, command: str, cwd: Path) -> str | None:
    return tool._guard_command(command, str(cwd))


class TestExecDenyListPolicy:
    def test_default_allows_commands_that_are_not_denied(self, tmp_path: Path):
        tool = ExecTool(working_dir=str(tmp_path))

        assert _guard(tool, "python script.py", tmp_path) is None
        assert _guard(tool, "npm install", tmp_path) is None

    def test_builtin_deny_patterns_block_dangerous_commands(self, tmp_path: Path):
        tool = ExecTool(working_dir=str(tmp_path))

        for command in ("rm -rf /", "format c:", "diskpart"):
            assert _guard(tool, command, tmp_path) == (
                "Error: Command blocked by deny pattern filter"
            )

    def test_repository_clone_commands_are_blocked(self, tmp_path: Path):
        tool = ExecTool(working_dir=str(tmp_path))

        for command in (
            "git clone https://github.com/test/repo",
            "git -C . clone https://github.com/test/repo",
            "gh repo clone test/repo",
        ):
            result = _guard(tool, command, tmp_path)
            assert result is not None
            assert "repository clone commands are disabled" in result

        assert _guard(tool, "git status", tmp_path) is None
        assert _guard(tool, "git diff -- src/app.py", tmp_path) is None

    def test_user_deny_patterns_block_matching_commands(self, tmp_path: Path):
        tool = ExecTool(
            working_dir=str(tmp_path),
            deny_patterns=[r"\bnpm\s+install\b"],
        )

        assert _guard(tool, "npm install", tmp_path) == (
            "Error: Command blocked by deny pattern filter"
        )
        assert _guard(tool, "python script.py", tmp_path) is None

    def test_allow_patterns_preserve_explicit_allowlist_semantics(self, tmp_path: Path):
        tool = ExecTool(
            working_dir=str(tmp_path),
            allow_patterns=[r"^echo\b"],
        )

        assert _guard(tool, "echo ok", tmp_path) is None
        assert _guard(tool, "python script.py", tmp_path) == (
            "Error: Command blocked by allowlist filter (not in allowlist)"
        )

    def test_internal_url_guard_still_blocks(self, tmp_path: Path):
        tool = ExecTool(working_dir=str(tmp_path))

        assert _guard(tool, "curl http://127.0.0.1:8000", tmp_path) == (
            "Error: Command blocked by safety guard (internal/private URL detected)"
        )

    def test_workspace_traversal_guard_still_blocks(self, tmp_path: Path):
        tool = ExecTool(
            working_dir=str(tmp_path),
            restrict_to_workspace=True,
        )

        result = _guard(tool, "cat ../secret.txt", tmp_path)
        assert result is not None
        assert result.startswith(
            "Error: Command blocked by safety guard (path traversal detected)"
        )


def test_legacy_permissions_config_is_ignored():
    config = Config.model_validate(
        {
            "permissions": {
                "approval_enabled": True,
                "max_risk_level": "high",
                "confirmation_level": "medium",
                "tool_overrides": {"exec": "high"},
            }
        }
    )

    assert not hasattr(config, "permissions")
