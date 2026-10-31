"""Tests for the diff-only evidence preprocessor pipeline."""
from __future__ import annotations

from pathlib import Path

import pytest

from nanoreview.review.planning.preprocessor import (
    CodeUnit,
    ProgrammaticEvidenceOptions,
    ProgrammaticEvidenceService,
    build_unit_preview,
    estimate_tokens,
)


def _service(tmp_path: Path) -> ProgrammaticEvidenceService:
    return ProgrammaticEvidenceService(tmp_path)


def _added_lines_patch(marker: str, count: int = 400) -> str:
    body = "".join(f"+{marker}_{index} = compute({index})\n" for index in range(count))
    return f"@@ -1,{count} +1,{count} @@\n{body}"


def test_estimate_tokens_contract() -> None:
    """Empty text is zero tokens; non-empty text rounds up.

    ``estimate_tokens`` is the only token meter the preprocessor has, and its
    ``0`` is what marks a changed path as carrying no diff text, so the empty
    case is a decision input rather than a formatting detail.
    """
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# ---------------------------------------------------------------------------
# Diff units: one complete patch per changed file
# ---------------------------------------------------------------------------


def test_small_changed_file_is_one_complete_diff_unit(tmp_path: Path) -> None:
    patch = "@@ -1 +1 @@\n-old\n+def login(): pass"

    result = _service(tmp_path).diff_units({"src/auth.py": patch}, review_query="login")

    assert [unit.kind for unit in result.units] == ["diff"]
    assert result.units[0].text == patch
    assert result.units[0].token_count == estimate_tokens(patch)
    assert result.units[0].start_line == 1
    assert result.skipped == []


def test_multi_hunk_file_stays_one_complete_unit(tmp_path: Path) -> None:
    # Two large hunks: previously this file was split at ``@@`` boundaries and
    # each piece was budgeted on its own. It now arrives whole.
    big = "x" * 20_000
    patch = f"@@ -1,1 +1,1 @@\n-a\n+{big}\n@@ -50,1 +50,1 @@\n-b\n+{big}"

    result = _service(tmp_path).diff_units({"src/multi.py": patch}, review_query="login")

    assert len(result.units) == 1
    unit = result.units[0]
    assert unit.kind == "diff"
    assert unit.text == patch
    assert "\n@@ -50,1 +50,1 @@\n" in unit.text
    assert estimate_tokens(patch) >= 8_000
    # The unit spans the first hunk's start to the last hunk's end.
    assert (unit.start_line, unit.end_line) == (1, 50)
    assert result.skipped == []


def test_git_header_and_hunk_headers_are_preserved(tmp_path: Path) -> None:
    patch = (
        "diff --git a/src/big.py b/src/big.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/big.py\n"
        "+++ b/src/big.py\n"
        "@@ -1,1 +1,1 @@\n-a\n+" + "x" * 20_000 + "\n"
        "@@ -50,1 +50,1 @@\n-b\n+" + "y" * 20_000
    )

    result = _service(tmp_path).diff_units({"src/big.py": patch}, review_query="login")

    assert len(result.units) == 1
    # File status/header lines stay in the unit: the reviewer sees the real
    # patch, and the report can still name the file it came from.
    assert result.units[0].text.startswith("diff --git a/src/big.py b/src/big.py")
    assert result.units[0].text.count("@@ -") == 2
    assert result.skipped == []


def test_oversized_file_is_not_split_or_skipped(tmp_path: Path) -> None:
    # No token threshold, no oversized-hunk skip, no total-budget trim: a very
    # large single-file diff still becomes one unit carrying every byte.
    patch = "@@ -1 +1 @@\n+" + "x" * 400_000

    result = _service(tmp_path).diff_units({"src/large.py": patch}, review_query="login")

    assert [unit.path for unit in result.units] == ["src/large.py"]
    assert result.units[0].text == patch
    assert len(result.units[0].text) == len(patch)
    assert result.skipped == []


def test_many_files_are_all_kept_whole(tmp_path: Path) -> None:
    patches = {
        "aaa.py": _added_lines_patch("first"),
        "bbb.py": _added_lines_patch("second"),
        "ccc.py": _added_lines_patch("third"),
    }

    result = _service(tmp_path).diff_units(patches, review_query="compute")

    # Nothing is dropped for budget reasons; the accepted set is exactly the
    # admitted change, in frozen order.
    assert [unit.path for unit in result.units] == ["aaa.py", "bbb.py", "ccc.py"]
    assert [unit.unit_id for unit in result.units] == ["ev-001", "ev-002", "ev-003"]
    assert result.skipped == []
    assert result.total_tokens == sum(unit.token_count for unit in result.units)


def test_empty_diff_produces_no_units(tmp_path: Path) -> None:
    result = _service(tmp_path).diff_units({}, review_query="login")

    assert result.units == []
    assert result.skipped == []
    assert result.total_tokens == 0
    assert "Skipped units" not in result.context


def test_patch_without_diff_text_is_reported_as_skipped(tmp_path: Path) -> None:
    result = _service(tmp_path).diff_units({"src/blank.py": ""}, review_query="login")

    assert result.units == []
    assert [(skip.path, skip.reason) for skip in result.skipped] == [
        ("src/blank.py", "empty_patch")
    ]


def test_no_language_gate_keeps_every_changed_path(tmp_path: Path) -> None:
    # Preprocessing makes no language decision: a diff in a language without a
    # bundled grammar is still reviewed as diff text, not skipped.
    patch = "@@ -1 +1 @@\n-class Old\n+class New"

    result = _service(tmp_path).diff_units({"src/Legacy.java": patch}, review_query="login")

    assert [unit.path for unit in result.units] == ["src/Legacy.java"]
    assert result.units[0].text == patch
    assert result.skipped == []


def test_mixed_language_diff_files_all_produce_units(tmp_path: Path) -> None:
    py_patch = "@@ -1 +1 @@\n-old\n+def login(): pass"
    java_patch = "@@ -1 +1 @@\n-old\n+class New"

    result = _service(tmp_path).diff_units(
        {
            "src/auth.py": py_patch,
            "src/Legacy.java": java_patch,
        },
        review_query="login",
    )

    assert [unit.path for unit in result.units] == ["src/auth.py", "src/Legacy.java"]
    assert result.skipped == []


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def test_options_from_review_config_maps_preview_fields() -> None:
    review_config = type(
        "ReviewConfig",
        (),
        {
            "prefetch_budget_chars": 8_000,
            "preview_target_chars": 400,
            "preview_hard_limit": 2_000,
        },
    )()

    options = ProgrammaticEvidenceOptions.from_review_config(review_config)

    assert options.prefetch_budget_chars == 8_000
    assert options.preview_target_chars == 400
    assert options.preview_hard_limit == 2_000

    defaults = ProgrammaticEvidenceOptions.from_review_config(None)
    assert defaults.prefetch_budget_chars == 16_000
    assert defaults.preview_target_chars == 600
    assert defaults.preview_hard_limit == 3_000


def test_review_config_rejects_inverted_preview_limits() -> None:
    from pydantic import ValidationError

    from nanoreview.config.schema import ReviewConfig

    with pytest.raises(ValidationError, match="preview_hard_limit"):
        ReviewConfig(preview_target_chars=600, preview_hard_limit=300)
    # Equal limits and well-ordered limits stay valid.
    assert ReviewConfig(preview_target_chars=600, preview_hard_limit=600).preview_hard_limit == 600
    assert ReviewConfig().preview_hard_limit == 3_000


def test_programmatic_options_reject_inverted_preview_limits() -> None:
    with pytest.raises(ValueError, match="preview_hard_limit"):
        ProgrammaticEvidenceOptions(preview_target_chars=600, preview_hard_limit=100)


# ---------------------------------------------------------------------------
# Preview construction (display only, never the reviewer's input)
# ---------------------------------------------------------------------------


def test_small_diff_preview_keeps_full_text(tmp_path: Path) -> None:
    patch = "@@ -1 +1 @@\n-old\n+def login(): pass"

    result = _service(tmp_path).diff_units({"src/auth.py": patch}, review_query="login")

    unit = result.units[0]
    assert unit.preview == patch
    assert unit.preview_coverage.startswith("full patch")
    # The manifest carries query hits and coverage as plain metadata and no
    # program-generated risk signal of any kind.
    assert "- matched: login" in result.context
    assert "risk_hints" not in result.context
    assert "- preview_coverage: full patch" in result.context


def test_oversized_diff_preview_keeps_changed_lines_and_omits_filler() -> None:
    patch = "\n".join(
        [
            "@@ -1,160 +1,161 @@",
            *[f" context_{index} = compute_step(index, payload_{index})" for index in range(80)],
            "+api_key = load_secret()",
            *[f" tail_{index} = finalize_step(index, payload_{index})" for index in range(80)],
        ]
    )
    assert len(patch) > 3_000

    preview, coverage = build_unit_preview(
        patch, kind="diff", start_line=1, query_terms=set(), target_chars=600, hard_limit=3_000
    )

    # Changed lines survive sampling even deep inside a large hunk.
    assert "+api_key = load_secret()" in preview
    assert "... omitted lines" in preview
    assert len(preview) <= 3_000
    assert coverage.startswith("hunks span new-file lines 1-161")


def test_diff_preview_keeps_hunk_headers_and_changed_lines() -> None:
    patch = "\n".join(
        [
            "@@ -10,7 +10,9 @@ def module():",
            " context_one = 1",
            " context_two = 2",
            "-removed_line = compute()",
            "+def login(token):",
            "+    return verify(token)",
            *[f" context_filler_{index} = {index}" for index in range(40)],
            "+api_key = load_secret()",
            " trailing_context = 0",
        ]
    )

    preview, coverage = build_unit_preview(
        patch, kind="diff", start_line=10, query_terms=set(), target_chars=600, hard_limit=3_000
    )

    assert "@@ -10,7 +10,9 @@ def module():" in preview
    assert "-removed_line = compute()" in preview
    assert "+def login(token):" in preview
    assert "+api_key = load_secret()" in preview
    assert "... omitted lines" in preview
    assert "hunks span new-file lines 10-18" in coverage
    assert "preview covers new-file lines" in coverage


def test_single_line_preview_respects_hard_limit() -> None:
    # Minified or generated code may have no safe line boundary to drop.
    preview, _coverage = build_unit_preview(
        "x" * 200,
        kind="diff",
        start_line=1,
        query_terms=set(),
        target_chars=10,
        hard_limit=32,
    )

    assert len(preview) <= 32
    assert preview.endswith("\n... (preview truncated)")


def test_diff_preview_coverage_aligns_with_hunk_new_file_lines() -> None:
    patch = "\n".join(
        [
            "@@ -5,3 +5,4 @@",
            " ctx = 1",
            "-old = 2",
            "+new = 2",
            "+added = 3",
        ]
    )

    _preview, coverage = build_unit_preview(
        patch, kind="diff", start_line=5, query_terms=set(), target_chars=600, hard_limit=3_000
    )

    # Small patch keeps full text; hunks address new-file lines 5-8.
    assert coverage == "full patch; new-file lines 5-8"


# ---------------------------------------------------------------------------
# Evidence metadata and ordering
# ---------------------------------------------------------------------------


def test_matched_keeps_only_query_hit_words(tmp_path: Path) -> None:
    patch = "@@ -1,2 +1,2 @@\n-login_old = 1\n+login(token)\n token = 2"

    result = _service(tmp_path).diff_units({"src/auth.py": patch}, review_query="login")

    unit = result.units[0]
    assert unit.matched == ["login"]
    assert not any(tag.startswith("risk:") for tag in unit.tags)
    assert unit.tags == ()  # diff units carry no relation tags


def test_evidence_order_is_frozen_and_not_risk_ranked(tmp_path: Path) -> None:
    """A risky-looking file must not jump ahead of an earlier plain file.

    Ordering is the frozen diff order (patch mapping order); nothing in
    preprocessing re-ranks evidence by risk keywords or query hits.
    """
    patches = {
        "aaa_plain.py": "@@ -1 +1 @@\n-value = 1\n+value = 2",
        "zzz_auth.py": "@@ -1 +1 @@\n-password = 1\n+login(password, token)",
    }

    result = _service(tmp_path).diff_units(patches, review_query="login")

    assert [unit.path for unit in result.units] == ["aaa_plain.py", "zzz_auth.py"]
    assert [unit.unit_id for unit in result.units] == ["ev-001", "ev-002"]


def test_diff_without_query_hits_still_reaches_planner(tmp_path: Path) -> None:
    # A patch with no query hits still produces units and manifest entries.
    patch = "@@ -1 +1 @@\n-old\n+total = 1 + 2"

    result = _service(tmp_path).diff_units({"src/plain.py": patch}, review_query="nonexistentterm")

    assert result.units
    unit = result.units[0]
    assert unit.matched == []
    assert "src/plain.py" in result.context
    assert "- matched: none" in result.context


def test_manifest_keeps_single_budget_truncation() -> None:
    service = ProgrammaticEvidenceService(Path("."))
    units = [
        CodeUnit(
            path=f"src/file_{index}.py",
            kind="diff",
            start_line=1,
            end_line=2,
            text="x",
            unit_id=f"ev-{index:03d}",
            preview="@@ -1 +1 @@\n-old\n+new\n" * 50,
            preview_coverage="full patch; new-file lines 1",
        )
        for index in range(1, 30)
    ]

    context = service.render_manifest(units, [], budget_chars=2_000)

    # The overall manifest cap stays the single safety limit of the display text.
    assert len(context) <= 2_000 + len("\n... (manifest truncated)")
    assert context.endswith("... (manifest truncated)")


# ---------------------------------------------------------------------------
# Removed capabilities
# ---------------------------------------------------------------------------


def test_preprocessor_exposes_no_whole_repository_entry_point(tmp_path: Path) -> None:
    """预处理只保留 diff 入口：不再提供检索、目录枚举或完整文件读取。"""
    service = _service(tmp_path)

    assert not hasattr(service, "retrieve")
    assert not hasattr(service, "iter_candidate_files")
    assert not hasattr(service, "is_ignored")
    assert hasattr(service, "diff_units")


def test_preprocessor_has_no_budget_slicing_api() -> None:
    """预算决策已上移到 review loop 的请求预算，预处理不再具备切分能力。"""
    from nanoreview.review.planning import preprocessor

    assert not hasattr(ProgrammaticEvidenceService, "_apply_main_budget")
    assert not hasattr(preprocessor, "EvidenceBudget")
    assert not hasattr(preprocessor, "DIFF_UNIT_TOKEN_THRESHOLD")
