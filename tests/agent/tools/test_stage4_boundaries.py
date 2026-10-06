"""Boundary tests for the Stage-4 scope reduction.

This round removed the per-tool approval machinery and GitHub as a review
input, while explicitly *retaining* every generic network capability. Both the
removal and the retention are contracts, so both are pinned here.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.agent.tools.safety_boundary import (
    is_ssrf_violation,
    ssrf_soft_payload,
)
from nanoreview.agent.tools.shell import ExecTool
from nanoreview.review.input.normalizers import normalize_review_target_type
from nanoreview.review.input.targets import infer_review_target_type


class TestApprovalMachineryIsGone:
    @pytest.mark.parametrize(
        "module_name",
        [
            "nanoreview.agent.tools.permissions",
        ],
    )
    def test_removed_modules_are_not_importable(self, module_name: str):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)

    def test_tools_config_has_no_approval_fields(self):
        from nanoreview.config.schema import ToolsConfig

        config = ToolsConfig()

        for gone in ("approval_enabled", "github_repo", "github_diff_enable"):
            assert not hasattr(config, gone), gone

    def test_agent_run_spec_has_no_permission_policy(self):
        import dataclasses

        from nanoreview.agent.runner import AgentRunSpec

        field_names = {f.name for f in dataclasses.fields(AgentRunSpec)}

        assert "permission_policy" not in field_names

    def test_conversation_loop_has_no_permission_requester(self):
        from nanoreview.agent.conversation_loop import ConversationLoop

        assert not hasattr(ConversationLoop, "_permission_requester")


class TestGithubReviewInputIsGone:
    @pytest.mark.parametrize(
        "module_name",
        [
            "nanoreview.agent.tools.github_review",
            "nanoreview.review.source.github",
        ],
    )
    def test_removed_github_modules_are_not_importable(self, module_name: str):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)

    def test_github_tool_is_not_exported(self):
        import nanoreview.agent.tools as tools_pkg

        assert "github_review" not in getattr(tools_pkg, "__all__", [])

    @pytest.mark.parametrize(
        "target",
        [
            "https://github.com/owner/repo",
            "https://github.com/owner/repo/pull/42",
            "https://github.com/owner/repo/blob/main/src/app.py",
            "git@github.com:owner/repo.git",
        ],
    )
    def test_github_urls_always_infer_local(self, target: str):
        assert infer_review_target_type(target) == "local"

    @pytest.mark.parametrize("raw", ["github", "remote"])
    def test_unknown_target_types_fall_through_to_local_inference(self, raw: str):
        """`github` is not a recognized type any more, so inference decides (always local)."""
        assert normalize_review_target_type(raw, target="src/app.py") == "local"

    def test_review_plan_has_no_remote_fields(self):
        import dataclasses

        from nanoreview.review.types import ReviewPlan

        field_names = {f.name for f in dataclasses.fields(ReviewPlan)}

        for gone in (
            "target_repo",
            "pr_number",
            "target_ref",
            "target_subpath",
            "target_subpath_kind",
        ):
            assert gone not in field_names, gone


class TestGithubMetadataKeysAreGone:
    def test_github_review_metadata_keys_are_absent(self):
        from nanoreview.review.types import ReviewMetaKey

        for gone in ("GITHUB_PREFETCH_READY", "GITHUB_PR_HEAD_REF", "TARGET_REF"):
            assert not hasattr(ReviewMetaKey, gone), gone

    def test_review_target_type_literal_excludes_github(self):
        from typing import get_args

        from nanoreview.review.types import ReviewTargetType

        assert set(get_args(ReviewTargetType)) == {"auto", "local"}


class TestNetworkCapabilitiesAreRetained:
    def test_ssrf_guard_still_detects_private_addresses(self):
        assert is_ssrf_violation("Error: blocked (internal/private url detected)")
        assert not is_ssrf_violation("Error: blocked (deny pattern filter)")

    def test_ssrf_soft_payload_keeps_the_user_facing_hint(self):
        payload = ssrf_soft_payload("internal/private url detected")

        assert "whitelist" in payload.lower()

    def test_mcp_transports_are_still_configurable(self):
        from nanoreview.config.schema import MCPServerConfig

        for transport in ("stdio", "sse", "streamableHttp"):
            config = MCPServerConfig(command="x", type=transport)
            assert config.type == transport

    def test_shell_still_blocks_internal_urls(self, tmp_path: Path):
        tool = ExecTool(working_dir=str(tmp_path))

        result = tool._guard_command(
            "curl http://127.0.0.1:8000",
            str(tmp_path),
            restricted=False,
            boundary_root=None,
        )

        assert result is not None
        assert "internal/private URL detected" in result

    def test_github_log_token_redaction_is_retained(self):
        """GitHub credentials still leak-proofed in logs; only review input was removed."""
        from nanoreview.utils.log_sanitization import sanitize_persisted_log_text

        token = "ghp_" + "A" * 24

        assert token not in sanitize_persisted_log_text(f"token={token}")

    def test_registry_is_the_single_tool_gateway(self):
        """Tools are gated by registration, not by an approval layer."""
        registry = ToolRegistry()

        assert registry.tool_names == []
