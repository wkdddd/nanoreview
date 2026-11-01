"""The validator checks candidates against the frozen PR boundary.

There is no filesystem here: a candidate is validated against the PR's changed
paths, the line ranges its diff touched, and the evidence ids the run handed
the reviewer.
"""

from __future__ import annotations

from nanoreview.review.output.validator import ReviewValidator, ValidationContext


def _context(**overrides) -> ValidationContext:
    values = {
        "changed_files": ("src/app.py",),
        "changed_ranges": {"src/app.py": ((10, 14),)},
        "evidence_ids": frozenset({"ev-001", "ctx-001"}),
    }
    values.update(overrides)
    return ValidationContext(**values)


def _finding(**overrides) -> dict:
    values = {
        "severity": "high",
        "file": "src/app.py",
        "line": 12,
        "title": "unbounded read",
        "evidence": "open(path) without size check",
        "impact": "memory exhaustion",
        "recommendation": "cap the read",
        "details": {"trigger": "large file", "expected_behavior": "bounded", "actual_behavior": "unbounded"},
    }
    values.update(overrides)
    return values


def _validator(**context_overrides) -> ReviewValidator:
    return ReviewValidator(_context(**context_overrides))


def _validate(finding: dict, *, dimension: str = "bug", validator=None):
    target = validator if validator is not None else _validator()
    result = target.validate_candidates([_candidate(finding)], dimension)
    return result


def _candidate(finding: dict):
    from nanoreview.review.output.finalizer import ReviewFinalizer

    finalizer = ReviewFinalizer(changed_files=["src/app.py"])
    return finalizer._dict_to_candidate(finding, finding.get("_dimension", "bug"))


class TestAccepted:
    def test_valid_candidate_accepted(self) -> None:
        result = _validate(_finding())
        assert result.status == "validated"
        assert len(result.accepted) == 1
        assert result.rejected == []
        assert result.uncertain == []
        assert result.accepted[0].line == 12

    def test_no_line_still_accepted(self) -> None:
        result = _validate(_finding(line=None))
        assert len(result.accepted) == 1

    def test_line_outside_declared_ranges_accepted_when_ranges_unknown(self) -> None:
        """Without diff ranges the validator cannot claim the line is untouched."""
        result = _validate(_finding(), validator=_validator(changed_ranges={}))
        assert len(result.accepted) == 1

    def test_old_side_line_is_not_checked_against_new_side_ranges(self) -> None:
        """Merge-base line numbers cannot be validated by new-side ranges."""
        result = _validate(_finding(side="old", line=3))
        assert len(result.accepted) == 1

    def test_end_line_and_side_are_preserved(self) -> None:
        finding = _finding(side="old", end_line=14)
        result = _validate(finding)
        assert result.accepted[0].side == "old"
        assert result.accepted[0].end_line == 14

    def test_windows_separator_normalized_to_the_changed_path(self) -> None:
        result = _validate(_finding(file="src\\app.py"))
        assert len(result.accepted) == 1

    def test_cited_evidence_refs_are_accepted_when_known(self) -> None:
        result = _validate(_finding(evidence_refs=["ev-001", "ctx-001"]))
        assert len(result.accepted) == 1
        assert result.accepted[0].evidence_refs == ("ev-001", "ctx-001")


class TestRejected:
    def test_invalid_severity_rejected(self) -> None:
        result = _validate(_finding(severity="blocker"))
        assert len(result.rejected) == 1
        assert "invalid severity" in result.rejected[0][1].reason

    def test_missing_file_or_title_rejected(self) -> None:
        result = _validate(_finding(file=""))
        assert len(result.rejected) == 1
        result = _validate(_finding(title=""))
        assert len(result.rejected) == 1

    def test_invalid_side_rejected(self) -> None:
        result = _validate(_finding(side="head"))
        assert len(result.rejected) == 1
        assert "invalid side" in result.rejected[0][1].reason

    def test_duplicate_rejected(self) -> None:
        validator = _validator()
        first = _validate(_finding(), validator=validator)
        assert len(first.accepted) == 1
        second = _validate(_finding(), validator=validator)
        assert len(second.rejected) == 1
        assert second.rejected[0][1].reason == "duplicate finding"

    def test_details_schema_violation_rejected(self) -> None:
        result = _validate(_finding(details={"trigger": "x"}))
        assert len(result.rejected) == 1
        assert "invalid reviewer details" in result.rejected[0][1].reason

    def test_unknown_dimension_rejected(self) -> None:
        result = _validate(_finding(), dimension="mystery")
        assert len(result.rejected) == 1
        assert "unknown reviewer profile" in result.rejected[0][1].reason


class TestUncertain:
    def test_file_outside_changed_set(self) -> None:
        result = _validate(_finding(file="docs/readme.md"))
        assert len(result.uncertain) == 1
        assert result.uncertain[0][1].reason == "file not in changed set"

    def test_line_outside_diff_ranges(self) -> None:
        result = _validate(_finding(line=200))
        assert len(result.uncertain) == 1
        assert "not part of the diff" in result.uncertain[0][1].reason

    def test_unknown_evidence_reference(self) -> None:
        result = _validate(_finding(evidence_refs=["ev-999"]))
        assert len(result.uncertain) == 1
        assert "unknown evidence reference" in result.uncertain[0][1].reason

    def test_empty_evidence(self) -> None:
        result = _validate(_finding(evidence=""))
        assert len(result.uncertain) == 1
        assert result.uncertain[0][1].reason == "no evidence provided"


def test_validator_stats_reflect_the_outcomes() -> None:
    validator = _validator()
    validator.validate_candidates(
        [_candidate(_finding()), _candidate(_finding(severity="low", line=None))], "bug"
    )
    assert validator.stats == {"accepted": 2, "rejected": 0, "needs_confirmation": 0}
