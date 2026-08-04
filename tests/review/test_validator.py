"""Tests for ReviewValidator hard validation logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.review.output.validator import ReviewValidator, ValidationContext
from nanobot.review.types import GitHubDiffEvidence, ReviewFindingCandidate


@pytest.fixture
def workspace(tmp_path):
    """Create a workspace with sample files."""
    (tmp_path / "src").mkdir()
    src_file = tmp_path / "src" / "main.py"
    src_file.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Hello\n", encoding="utf-8")
    return str(tmp_path)


def _make_candidate(**overrides) -> ReviewFindingCandidate:
    defaults = {
        "severity": "high",
        "dimension": "security",
        "file": "src/main.py",
        "line": 2,
        "title": "SQL Injection risk",
        "evidence": "line2",
        "impact": "Remote code execution",
        "recommendation": "Use parameterized queries",
    }
    defaults.update(overrides)
    return ReviewFindingCandidate(**defaults)


class TestValidatorAccepts:
    def test_github_diff_candidate_uses_patch_evidence_without_workspace_file(self, tmp_path):
        evidence = GitHubDiffEvidence(
            snapshot="owner/repo#1",
            head_sha="abc123",
            patches={"src/auth.py": "@@ -1 +1 @@\n-old\n+token = request.token"},
            changed_files=["src/auth.py"],
            touched_lines={"src/auth.py": [1]},
        )
        validator = ReviewValidator(
            ValidationContext(workspace=str(tmp_path), remote_diff=evidence)
        )
        result = validator.validate_candidates(
            [_make_candidate(file="src/auth.py", line=1, evidence="token = request.token")],
            "security",
        )
        assert len(result.accepted) == 1

    def test_valid_candidate_accepted(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        result = v.validate_candidates([_make_candidate()], "security")
        assert len(result.accepted) == 1
        assert len(result.rejected) == 0
        assert result.status == "validated"

    def test_windows_separator_candidate_matches_changed_file(self, workspace):
        v = ReviewValidator(
            ValidationContext(
                workspace=workspace,
                changed_files=["src/main.py"],
            )
        )
        c = _make_candidate(file="src\\main.py")

        result = v.validate_candidates([c], "security")

        assert len(result.accepted) == 1
        assert len(result.uncertain) == 0

    def test_no_line_still_accepted(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(line=None)
        result = v.validate_candidates([c], "security")
        assert len(result.accepted) == 1

    def test_line_prefixed_backtick_evidence_accepted(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(
            line=2,
            evidence="Line 2: `line2` appears in the reviewed file.",
        )

        result = v.validate_candidates([c], "security")

        assert len(result.accepted) == 1
        assert len(result.uncertain) == 0

    def test_nearby_line_code_snippet_evidence_accepted(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(
            line=3,
            evidence="Lines 2-3: `line2 line3`",
        )

        result = v.validate_candidates([c], "security")

        assert len(result.accepted) == 1

    def test_local_file_target_accepts_equivalent_relative_and_absolute_paths(self, workspace):
        target = Path(workspace) / "src" / "main.py"
        v = ReviewValidator(
            ValidationContext(
                workspace=workspace,
                changed_files=["src/main.py"],
                local_target=str(target),
            )
        )

        result = v.validate_candidates(
            [
                _make_candidate(title="relative path"),
                _make_candidate(
                    file=str(target),
                    title="absolute path",
                    line=3,
                    evidence="line3",
                ),
            ],
            "security",
        )

        assert len(result.accepted) == 2
        assert len(result.rejected) == 0

    def test_local_directory_target_accepts_paths_inside_directory(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / "app.py").write_text("line1\nline2\n", encoding="utf-8")
        v = ReviewValidator(
            ValidationContext(
                workspace=str(target_dir),
                local_target=str(target_dir),
            )
        )

        result = v.validate_candidates(
            [
                _make_candidate(file="app.py", evidence="line1", title="relative", line=1),
                _make_candidate(
                    file=str(target_dir / "app.py"), evidence="line2", title="absolute", line=2
                ),
            ],
            "security",
        )

        assert len(result.accepted) == 2
        assert len(result.rejected) == 0

    def test_line_corrected_when_evidence_on_different_line(self, workspace):
        """Bug 1 回归：evidence 在另一行找到时，不应抛 FrozenInstanceError，且修正后的行号应流入 accepted/candidates。"""
        v = ReviewValidator(ValidationContext(workspace=workspace))
        # 真实行号是 2，candidate 故意填 1，evidence 文本能匹配到 line2
        c = _make_candidate(line=1, evidence="line2")

        result = v.validate_candidates([c], "security")

        assert len(result.accepted) == 1
        # accepted 中 candidate.line 应为修正后的 2
        assert result.accepted[0].line == 2
        # candidates 中也应反映修正后的行号
        assert result.candidates[0].line == 2

    def test_evidence_at_exact_narrow_window_boundary_accepted(self, tmp_path):
        """Bug 2 回归：evidence 距离报告行正好 5 行时，应能匹配（narrow 窗口边界）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        # 第 1 行放唯一标记 MARKER_AT_LINE_1，第 6 行是报告行
        # 距离正好 5 行，旧实现会漏掉
        src = ws / "main.py"
        src.write_text(
            "MARKER_AT_LINE_1\npadding\npadding\npadding\npadding\nreport_line\n",
            encoding="utf-8",
        )
        v = ReviewValidator(ValidationContext(workspace=str(ws)))
        c = _make_candidate(
            file="main.py",
            line=6,
            evidence="MARKER_AT_LINE_1",
            title="boundary evidence",
        )

        result = v.validate_candidates([c], "security")

        assert len(result.accepted) == 1
        assert len(result.uncertain) == 0

    def test_relative_local_target_resolved_against_workspace(self, tmp_path, monkeypatch):
        """Bug 3 回归：相对路径的 local_target 应按 workspace 解析，而非 CWD。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        subdir = ws / "subdir"
        subdir.mkdir()
        (subdir / "app.py").write_text("line1\nline2\n", encoding="utf-8")
        # 在 workspace 之外创建同名文件，确保 CWD 解析会指向错误的目标
        outside = tmp_path / "subdir"
        outside.mkdir()
        (outside / "app.py").write_text("outside\n", encoding="utf-8")
        # 把 CWD 切到 tmp_path（workspace 之外），使相对路径 "subdir" 在 CWD 下指向 outside
        monkeypatch.chdir(tmp_path)

        v = ReviewValidator(
            ValidationContext(
                workspace=str(ws),
                local_target="subdir",
            )
        )

        result = v.validate_candidates(
            [
                _make_candidate(
                    file="subdir/app.py",
                    evidence="line1",
                    title="inside workspace subdir",
                    line=1,
                ),
            ],
            "security",
        )

        # 应 accepted（说明相对路径被按 workspace 解析到 ws/subdir）
        assert len(result.accepted) == 1
        assert len(result.rejected) == 0


class TestValidatorRejects:
    def test_github_diff_without_patch_needs_confirmation(self, tmp_path):
        evidence = GitHubDiffEvidence(
            snapshot="owner/repo#1",
            head_sha="abc123",
            changed_files=["src/auth.py"],
            patch_unavailable_files={"src/auth.py": "patch_unavailable"},
        )
        validator = ReviewValidator(
            ValidationContext(workspace=str(tmp_path), remote_diff=evidence)
        )
        result = validator.validate_candidates(
            [_make_candidate(file="src/auth.py", line=1, evidence="token")],
            "security",
        )
        assert len(result.uncertain) == 1
        assert result.uncertain[0][1].reason == "patch_unavailable"

    def test_invalid_severity_rejected(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(severity="extreme")
        result = v.validate_candidates([c], "security")
        assert len(result.rejected) == 1
        assert "invalid severity" in result.rejected[0][1].reason

    def test_missing_file_field_rejected(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(file="")
        result = v.validate_candidates([c], "security")
        assert len(result.rejected) == 1

    def test_file_not_found_rejected(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(file="nonexistent.py")
        result = v.validate_candidates([c], "security")
        assert len(result.rejected) == 1
        assert "not found" in result.rejected[0][1].reason

    def test_absolute_path_rejected(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(file=str(Path(workspace) / "src" / "main.py"))
        result = v.validate_candidates([c], "security")
        assert len(result.rejected) == 1
        assert "outside workspace" in result.rejected[0][1].reason

    def test_parent_traversal_rejected(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(file="../outside.py")
        result = v.validate_candidates([c], "security")
        assert len(result.rejected) == 1
        assert "outside workspace" in result.rejected[0][1].reason

    def test_line_out_of_range_rejected(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(line=999)
        result = v.validate_candidates([c], "security")
        assert len(result.rejected) == 1
        assert "out of range" in result.rejected[0][1].reason

    def test_duplicate_rejected(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c1 = _make_candidate()
        c2 = _make_candidate()
        result = v.validate_candidates([c1, c2], "security")
        assert len(result.accepted) == 1
        assert len(result.rejected) == 1
        assert "duplicate" in result.rejected[0][1].reason

    def test_local_file_target_rejects_other_workspace_files(self, workspace):
        target = Path(workspace) / "src" / "main.py"
        v = ReviewValidator(
            ValidationContext(
                workspace=workspace,
                local_target=str(target),
            )
        )

        result = v.validate_candidates(
            [
                _make_candidate(file="README.md", evidence="Hello", title="wrong file", line=1),
            ],
            "security",
        )

        assert len(result.rejected) == 1
        assert "outside target" in result.rejected[0][1].reason

    def test_local_directory_target_rejects_paths_outside_directory(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("line1\n", encoding="utf-8")
        v = ReviewValidator(
            ValidationContext(
                workspace=str(target_dir),
                local_target=str(target_dir),
            )
        )

        result = v.validate_candidates(
            [
                _make_candidate(file=str(outside), evidence="line1", title="outside", line=1),
            ],
            "security",
        )

        assert len(result.rejected) == 1
        assert "outside target" in result.rejected[0][1].reason


class TestValidatorUncertain:
    def test_file_outside_changed_set(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace, changed_files=["other.py"]))
        c = _make_candidate()
        result = v.validate_candidates([c], "security")
        assert len(result.uncertain) == 1
        assert "not in changed set" in result.uncertain[0][1].reason

    def test_empty_evidence_uncertain(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(evidence="")
        result = v.validate_candidates([c], "security")
        assert len(result.uncertain) == 1
        assert "no evidence" in result.uncertain[0][1].reason

    def test_evidence_not_found_uncertain(self, workspace):
        v = ReviewValidator(ValidationContext(workspace=workspace))
        c = _make_candidate(evidence="not present in file")
        result = v.validate_candidates([c], "security")
        assert len(result.uncertain) == 1
        assert "evidence not found" in result.uncertain[0][1].reason
