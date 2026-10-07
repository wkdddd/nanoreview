"""Golden-diff replay scoring for review quality."""

from __future__ import annotations

from nanoreview.review.quality import (
    GOLDEN_CASES,
    ActualFinding,
    ExpectedFinding,
    ReplayStep,
    SubmitStep,
    evaluate_case,
    format_quality_report,
    load_golden_cases,
    match_findings,
    normalize_review_path,
    precision_recall_f1,
    replay_case,
)


def _golden():
    case = load_golden_cases()["sql_and_off_by_one"]
    return case


def _read_step(path: str = "app/users.py") -> ReplayStep:
    return ReplayStep(
        name="read_file",
        arguments={"path": path},
        result="(file body)",
        duration_ms=5.0,
        targets=(path,),
    )


# ---------------------------------------------------------------------------
# path + title normalization
# ---------------------------------------------------------------------------


def test_normalize_review_path_equates_dot_slash_and_backslash() -> None:
    assert normalize_review_path("./app/users.py") == "app/users.py"
    assert normalize_review_path("app\\users.py") == "app/users.py"
    assert normalize_review_path("app//users.py") == "app/users.py"


def test_precision_recall_f1_handles_empty_sets() -> None:
    assert precision_recall_f1(0, 0, 0) == (0.0, 0.0, 0.0)
    assert precision_recall_f1(1, 1, 1) == (1.0, 1.0, 1.0)


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def test_match_findings_pairs_across_path_aliases() -> None:
    expected = (ExpectedFinding("./app/users.py", 6, "SQL injection in find_user"),)
    actual = (ActualFinding("app\\users.py", 8, "SQL injection in find_user"),)

    matched, missing, spurious = match_findings(expected, actual)

    assert len(matched) == 1
    assert matched[0].line_delta == 2
    assert missing == ()
    assert spurious == ()


def test_match_findings_respects_line_tolerance() -> None:
    expected = (ExpectedFinding("app/users.py", 6, "SQL injection"),)
    actual = (ActualFinding("app/users.py", 40, "SQL injection"),)

    matched, missing, spurious = match_findings(expected, actual, line_tolerance=3)

    assert matched == ()
    assert len(missing) == 1
    assert len(spurious) == 1


def test_match_findings_requires_title_similarity() -> None:
    expected = (ExpectedFinding("app/users.py", 6, "SQL injection in find_user"),)
    actual = (ActualFinding("app/users.py", 6, "Missing type annotation"),)

    matched, missing, spurious = match_findings(expected, actual, title_similarity=0.5)

    assert matched == ()
    assert len(missing) == 1


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def test_evaluate_case_scores_a_perfect_replay() -> None:
    case = _golden()
    script = [
        _read_step(),
        SubmitStep(
            (
                ActualFinding(
                    "app/users.py",
                    6,
                    "SQL injection via string concatenation in find_user",
                ),
                ActualFinding(
                    "app/users.py",
                    11,
                    "Off-by-one in page_bounds end offset",
                ),
            ),
            dimension="bug",
        ),
    ]

    score = evaluate_case(case, replay_case(case, script))

    assert (score.precision, score.recall, score.f1) == (1.0, 1.0, 1.0)
    assert score.coverage == 1.0
    assert score.undecidable is False
    assert score.incomplete_rate == 0.0
    assert score.tool_calls == 1


def test_evaluate_case_penalizes_a_missed_finding() -> None:
    case = _golden()
    script = [
        _read_step(),
        SubmitStep(
            (
                ActualFinding(
                    "app/users.py",
                    6,
                    "SQL injection via string concatenation in find_user",
                ),
            ),
            dimension="bug",
        ),
    ]

    score = evaluate_case(case, replay_case(case, script))

    assert score.precision == 1.0
    assert score.recall == 0.5
    assert abs(score.f1 - (2 / 3)) < 1e-9
    assert len(score.missing) == 1


def test_evaluate_case_penalizes_spurious_findings() -> None:
    case = _golden()
    script = [
        _read_step(),
        SubmitStep(
            (
                ActualFinding(
                    "app/users.py",
                    6,
                    "SQL injection via string concatenation in find_user",
                ),
                ActualFinding(
                    "app/users.py",
                    11,
                    "Off-by-one in page_bounds end offset",
                ),
                ActualFinding("app/users.py", 1, "Unused import sqlite3"),
            ),
            dimension="bug",
        ),
    ]

    score = evaluate_case(case, replay_case(case, script))

    assert abs(score.precision - (2 / 3)) < 1e-9
    assert score.recall == 1.0
    assert len(score.spurious) == 1


def test_evaluate_case_is_undecidable_without_tool_coverage() -> None:
    case = _golden()
    script = [
        SubmitStep(
            (ActualFinding("app/users.py", 6, "SQL injection"),),
            dimension="bug",
        ),
    ]

    score = evaluate_case(case, replay_case(case, script))

    assert score.undecidable is True
    assert score.coverage == 0.0


def test_incomplete_rate_tracks_incomplete_dimensions() -> None:
    case = _golden()
    outcome = replay_case(
        case,
        [_read_step(), SubmitStep((), dimension="bug")],
        dimension_statuses={"bug": "completed", "security": "error"},
    )

    score = evaluate_case(case, outcome)

    assert score.incomplete_rate == 0.5


def test_usage_and_elapsed_are_reported() -> None:
    case = _golden()
    outcome = replay_case(
        case,
        [_read_step(), SubmitStep((), duration_ms=12.5, dimension="bug")],
        usage={"input_tokens": 100, "output_tokens": 20},
    )

    score = evaluate_case(case, outcome)

    assert score.usage == {"input_tokens": 100, "output_tokens": 20}
    assert score.elapsed_ms == 17.5
    assert score.to_dict()["elapsed_ms"] == 17.5


# ---------------------------------------------------------------------------
# determinism + rendering
# ---------------------------------------------------------------------------


def test_replay_is_deterministic() -> None:
    case = _golden()
    script = [
        _read_step(),
        ReplayStep("grep", {"pattern": "SELECT"}, "app/users.py:5", duration_ms=1.0),
        SubmitStep((ActualFinding("app/users.py", 6, "SQL injection"),), dimension="bug"),
    ]

    first = evaluate_case(case, replay_case(case, script)).to_dict()
    second = evaluate_case(case, replay_case(case, script)).to_dict()

    assert first == second


def test_format_quality_report_renders_key_metrics() -> None:
    case = _golden()
    score = evaluate_case(
        case,
        replay_case(case, [_read_step(), SubmitStep((), dimension="bug")]),
    )

    report = format_quality_report(score)

    assert "case: sql_and_off_by_one" in report
    assert "precision=" in report
    assert "tool_calls=" in report


def test_golden_cases_are_registered() -> None:
    assert set(GOLDEN_CASES) == {"sql_and_off_by_one"}
    assert set(load_golden_cases()) == set(GOLDEN_CASES)
