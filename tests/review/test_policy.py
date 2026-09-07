"""Tests for the unified review strategy (no quick/full/deep depth).

The pipeline keeps every severity level; there is no depth-based policy
object anymore. These tests pin the observable contract: candidates of all
severities survive finalizer ingestion unchanged.
"""
from __future__ import annotations

import json

from nanoreview.review.output.finalizer import ReviewFinalizer
from nanoreview.review.types import SEVERITY_ORDER


def _submit(findings: list[dict[str, object]]) -> str:
    return json.dumps(
        {"submitted": True, "findings": findings, "errors": []},
        ensure_ascii=False,
    )


def test_unified_strategy_keeps_all_severity_levels(tmp_path) -> None:
    """All severities survive ingestion; nothing is filtered by policy."""
    workspace = tmp_path
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    findings = [
        {
            "severity": severity,
            "file": "src/app.py",
            "line": index + 1,
            "title": f"{severity} issue",
            "evidence": f"line{index + 1}",
            "impact": "bad",
            "recommendation": "fix",
            "details": {
                "trust_boundary": "public API boundary",
                "attack_preconditions": "attacker can reach login",
                "attack_path": "token reuse via line "
                f"{index + 1}",
            },
        }
        for index, severity in enumerate(SEVERITY_ORDER)
    ]
    finalizer = ReviewFinalizer(str(workspace))

    result = finalizer.ingest_subagent_output("security", _submit(findings))

    assert [candidate.severity for candidate in result.accepted] == list(SEVERITY_ORDER)
    assert [candidate.title for candidate in result.accepted] == [
        f"{severity} issue" for severity in SEVERITY_ORDER
    ]


def test_unified_strategy_covers_full_severity_order() -> None:
    """The severity order itself covers critical..low with no gaps."""
    assert SEVERITY_ORDER == ("critical", "high", "medium", "low")


def test_unified_strategy_has_no_depth_policy_api() -> None:
    """The old depth policy surface is gone from the review input package."""
    import nanoreview.review.input as review_input

    assert not hasattr(review_input, "policy_for_depth")
    assert not hasattr(review_input, "apply_policy_to_roles")
    assert not hasattr(review_input, "default_review_policy")
    assert not hasattr(review_input, "get_review_policy")
