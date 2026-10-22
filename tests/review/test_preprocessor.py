"""Tests for the diff-only evidence preprocessor pipeline."""
from __future__ import annotations

from pathlib import Path

import pytest

from nanoreview.review.planning.preprocessor import (
    CodeUnit,
    EvidenceBudget,
    ProgrammaticEvidenceOptions,
    ProgrammaticEvidenceService,
    build_unit_preview,
    estimate_tokens,
)

# context_window_tokens=16_384 yields: usable=5746, evidence_budget=5746,
# direct_cap=1600.
SMALL_WINDOW = 16_384


def _service(tmp_path: Path) -> ProgrammaticEvidenceService:
    return ProgrammaticEvidenceService(tmp_path)


def _added_lines_patch(marker: str, count: int = 400) -> str:
    """A single-hunk patch far below the per-file hunk-splitting threshold."""
    body = "".join(f"+{marker}_{index} = compute({index})\n" for index in range(count))
    return f"@@ -1,{count} +1,{count} @@\n{body}"


# ---------------------------------------------------------------------------
# Diff units
# ---------------------------------------------------------------------------


def test_small_diff_uses_direct_mode(tmp_path: Path) -> None:
    patch = "@@ -1 +1 @@\n-old\n+def login(): pass"

    result = _service(tmp_path).diff_units({"src/auth.py": patch}, review_query="login")

    assert result.mode == "direct"
    assert [unit.kind for unit in result.units] == ["diff"]
    assert result.units[0].token_count == estimate_tokens(patch)
    assert result.units[0].start_line == 1
    assert result.skipped == []


def test_empty_diff_produces_no_units(tmp_path: Path) -> None:
    result = _service(tmp_path).diff_units({}, review_query="login")

    assert result.units == []
    assert result.skipped == []
    assert result.mode == "direct"
    assert result.accepted_tokens == 0
    assert "Skipped units" not in result.context


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


def test_small_changed_file_below_threshold_stays_one_diff_unit(tmp_path: Path) -> None:
    # A changed file whose patch is under 8k tokens is one complete diff unit,
    # even though the whole target may be large.
    patch = "@@ -1,3 +1,3 @@\n-a = 1\n+b = 2\n c = 3\n d = 4"

    result = _service(tmp_path).diff_units({"src/small.py": patch}, review_query="login")

    assert [unit.kind for unit in result.units] == ["diff"]
    assert result.units[0].text == patch
    assert result.units[0].token_count == estimate_tokens(patch)
    assert result.units[0].token_count < 8_000


def test_changed_file_at_threshold_splits_by_hunks(tmp_path: Path) -> None:
    # Two hunks, each under the threshold, summing above it: the file splits
    # into per-hunk units using the existing hunk strategy.
    big = "x" * 20_000
    patch = f"@@ -1,1 +1,1 @@\n-a\n+{big}\n@@ -50,1 +50,1 @@\n-b\n+{big}"

    result = _service(tmp_path).diff_units({"src/multi.py": patch}, review_query="login")

    assert estimate_tokens(patch) >= 8_000
    assert len(result.units) == 2
    assert all(unit.kind == "diff" for unit in result.units)
    assert {unit.start_line for unit in result.units} == {1, 50}
    assert result.skipped == []


def test_git_header_before_first_hunk_is_not_emitted_as_a_unit(tmp_path: Path) -> None:
    # A real ``git diff`` starts with header lines before the first ``@@``.
    # They must not become a bogus 1-1 diff unit.
    big = "x" * 20_000
    patch = (
        "diff --git a/src/big.py b/src/big.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/big.py\n"
        "+++ b/src/big.py\n"
        f"@@ -1,1 +1,1 @@\n-a\n+{big}\n"
        f"@@ -50,1 +50,1 @@\n-b\n+{big}"
    )

    result = _service(tmp_path).diff_units({"src/big.py": patch}, review_query="login")

    assert [unit.start_line for unit in result.units] == [1, 50]
    assert all(unit.text.startswith("@@ ") for unit in result.units)
    assert result.skipped == []


def test_oversized_hunk_without_safe_split_is_skipped(tmp_path: Path) -> None:
    # Above the 8k per-file diff threshold with no ``@@`` boundary to split at.
    result = _service(tmp_path).diff_units(
        {"src/large.py": "+" + "x" * 40_000},
        review_query="login",
        context_window_tokens=SMALL_WINDOW,
    )

    assert result.units == []
    assert [(skip.path, skip.reason) for skip in result.skipped] == [
        ("src/large.py", "token_limit_exceeded")
    ]


def test_total_budget_exhaustion_skips_tail_in_frozen_order(tmp_path: Path) -> None:
    # Three patches of ~2200 tokens each exceed the 5746 evidence budget, so the
    # tail of the frozen order is recorded as budget_exhausted.
    patches = {
        "aaa.py": _added_lines_patch("first"),
        "bbb.py": _added_lines_patch("second"),
        "ccc.py": _added_lines_patch("third"),
    }

    result = _service(tmp_path).diff_units(
        patches, review_query="compute", context_window_tokens=SMALL_WINDOW
    )

    assert result.mode == "oversized"
    assert result.accepted_tokens <= 5_746
    assert result.accepted_tokens < result.total_tokens
    reasons = {skip.reason for skip in result.skipped}
    assert "budget_exhausted" in reasons
    # The cut is positional: with equally sized units the retained set is a
    # prefix of the frozen order, so the leading files survive and the tail is
    # skipped.
    assert [unit.path for unit in result.units] == ["aaa.py", "bbb.py"]
    assert [unit.unit_id for unit in result.units] == [
        f"ev-{index:03d}" for index in range(1, len(result.units) + 1)
    ]
    assert {skip.path for skip in result.skipped} == {"ccc.py"}


def test_budget_keeps_a_later_smaller_unit_that_still_fits(tmp_path: Path) -> None:
    """顺序预算不是硬截尾：放不下的大 unit 跳过，后面仍放得下的小 unit 保留。"""
    big = _added_lines_patch("big", count=500)  # ~3500 tokens
    patches = {
        "first.py": big,
        "second.py": big,
        "third.py": "@@ -1 +1 @@\n-small = 1\n+small = 2",
    }

    result = _service(tmp_path).diff_units(
        patches, review_query="compute", context_window_tokens=SMALL_WINDOW
    )

    assert result.mode == "oversized"
    assert [unit.path for unit in result.units] == ["first.py", "third.py"]
    assert [(skip.path, skip.reason) for skip in result.skipped] == [
        ("second.py", "budget_exhausted")
    ]
    assert [unit.unit_id for unit in result.units] == ["ev-001", "ev-002"]


# ---------------------------------------------------------------------------
# Budget derivation
# ---------------------------------------------------------------------------


def test_estimate_tokens_and_budget_derivation() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2

    budget = EvidenceBudget.from_options(
        evidence_token_budget=100_000,
        context_window_tokens=SMALL_WINDOW,
    )
    # window 16384 - overhead 9000 - margin 1638 = 5746 usable.
    assert budget.usable_input_tokens == 5_746
    assert budget.evidence_budget_tokens == 5_746
    assert budget.direct_cap_tokens == 1_600


def test_review_window_derives_150k_evidence_budget() -> None:
    budget = EvidenceBudget.from_options(
        evidence_token_budget=150_000,
        context_window_tokens=200_000,
    )
    # window 200000 - overhead 9000 - margin 20000 = 171000 usable.
    assert budget.usable_input_tokens == 171_000
    assert budget.evidence_budget_tokens == 150_000
    # direct mode inlines everything below a quarter of the usable window.
    assert budget.direct_cap_tokens == 42_750


def test_options_from_review_config_maps_budget_fields() -> None:
    review_config = type(
        "ReviewConfig",
        (),
        {
            "evidence_token_budget": 50_000,
            "prefetch_budget_chars": 8_000,
            "preview_target_chars": 400,
            "preview_hard_limit": 2_000,
        },
    )()

    options = ProgrammaticEvidenceOptions.from_review_config(review_config)

    assert options.evidence_token_budget == 50_000
    assert options.prefetch_budget_chars == 8_000
    assert options.preview_target_chars == 400
    assert options.preview_hard_limit == 2_000
    # The context window stays a per-request dynamic parameter.
    assert options.context_window_tokens == ProgrammaticEvidenceOptions().context_window_tokens

    defaults = ProgrammaticEvidenceOptions.from_review_config(None)
    assert defaults.evidence_token_budget == 100_000
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
# Planner preview construction
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

    # The overall manifest cap stays the single safety limit.
    assert len(context) <= 2_000 + len("\n... (manifest truncated)")
    assert context.endswith("... (manifest truncated)")


# ---------------------------------------------------------------------------
# Removed whole-repository capabilities
# ---------------------------------------------------------------------------


def test_preprocessor_exposes_no_whole_repository_entry_point(tmp_path: Path) -> None:
    """预处理只保留 diff 入口：不再提供检索、目录枚举或完整文件读取。"""
    service = _service(tmp_path)

    assert not hasattr(service, "retrieve")
    assert not hasattr(service, "iter_candidate_files")
    assert not hasattr(service, "is_ignored")
    assert hasattr(service, "diff_units")
