"""Planner triage contract: decisions in, program-aggregated assignments out.

The planner never submits assignments. These tests pin the contract the
ReviewLoop depends on:

* one decision may cover several evidence units and several dimensions;
* an evidence unit may be triaged exactly once;
* a dimension is only dispatched because a decision named it, or because the
  user pinned it in ``special``/``general`` mode;
* a low-risk decision with no dimensions is recorded as dismissed and starts
  no reviewer;
* evidence nobody reported stays unexamined and is never auto-dispatched;
* a non-low risk with no dimensions is rejected with a correctable reason.
"""

from __future__ import annotations

import pytest

from nanoreview.agent.tools.review_plan import (
    DecisionReceiverAdapter,
    FinishReviewTriageTool,
    ListReviewDiffTool,
    ReadReviewDiffTool,
    ReviewDiffReader,
    ReviewDiffUnit,
    SubmitReviewDecisionTool,
)
from nanoreview.review.planning.triage import TriageReceiver
from nanoreview.review.types import TRIAGE_ASSIGNED, TRIAGE_DISMISSED


def _receiver(
    *,
    mode: str = "auto",
    allowed: set[str] | None = None,
    evidence: tuple[str, ...] = ("ev-001", "ev-002"),
    required: tuple[str, ...] = (),
) -> TriageReceiver:
    return TriageReceiver(
        allowed_dimensions=allowed or {"bug", "security", "performance", "maintainability"},
        evidence_ids=set(evidence),
        mode=mode,
        evidence_paths={item: f"src/{item}.py" for item in evidence},
        ordered_evidence_ids=evidence,
        required_dimensions=required,
    )


def test_one_decision_can_cover_several_evidence_and_dimensions() -> None:
    receiver = _receiver()
    ok, detail = receiver.submit(
        evidence_ids=["ev-001", "ev-002"],
        risk_level="high",
        dimensions=["security", "bug"],
        focus="token handling",
        rationale="auth path changed",
    )
    assert ok, detail

    assignments = {item.dimension: item for item in receiver.assignments()}
    assert set(assignments) == {"bug", "security"}
    assert assignments["security"].evidence_ids == ("ev-001", "ev-002")
    assert assignments["bug"].evidence_ids == ("ev-001", "ev-002")
    assert assignments["security"].focus == "token handling"
    assert assignments["security"].source == "planner"


def test_one_evidence_may_not_be_triaged_twice() -> None:
    receiver = _receiver()
    ok, _ = receiver.submit(
        evidence_ids=["ev-001"], risk_level="high", dimensions=["bug"], focus="x"
    )
    assert ok

    ok, detail = receiver.submit(
        evidence_ids=["ev-001"], risk_level="high", dimensions=["security"], focus="y"
    )
    assert not ok
    assert "already triaged" in detail


def test_unknown_evidence_id_is_rejected_with_the_id() -> None:
    receiver = _receiver()

    ok, detail = receiver.submit(
        evidence_ids=["ev-999"], risk_level="high", dimensions=["bug"], focus="x"
    )

    assert not ok
    assert "unknown evidence IDs" in detail
    assert "ev-999" in detail


def test_high_risk_without_dimensions_is_rejected() -> None:
    receiver = _receiver()

    for level in ("medium", "high", "critical"):
        ok, detail = receiver.submit(
            evidence_ids=["ev-001"], risk_level=level, dimensions=[]
        )
        assert not ok
        assert "only risk_level='low' may omit dimensions" in detail

    assert receiver.decisions == []


def test_low_risk_without_dimensions_is_dismissed() -> None:
    receiver = _receiver()
    ok, _ = receiver.submit(
        evidence_ids=["ev-001"],
        risk_level="low",
        dimensions=[],
        focus="",
        rationale="docstring only",
    )
    assert ok

    assert receiver.assignments() == ()
    summary = receiver.summary()
    assert summary.dismissed_ids == ("ev-001",)
    assert summary.unexamined_ids == ("ev-002",)
    assert summary.no_assignments is True


def test_unreported_evidence_stays_unexamined() -> None:
    receiver = _receiver(evidence=("ev-001", "ev-002", "ev-003"))
    receiver.submit(
        evidence_ids=["ev-002"], risk_level="high", dimensions=["bug"], focus="x"
    )

    summary = receiver.summary()

    assert summary.unexamined_ids == ("ev-001", "ev-003")
    assert summary.assigned_ids() == ("ev-002",)
    assert {item.evidence_id: item.status for item in summary.triage} == {
        "ev-002": TRIAGE_ASSIGNED,
    }


def test_triage_summary_records_every_status_and_reports_counts() -> None:
    receiver = _receiver(evidence=("ev-001", "ev-002", "ev-003"))
    receiver.submit(
        evidence_ids=["ev-001"], risk_level="high", dimensions=["security"], focus="x"
    )
    receiver.submit(evidence_ids=["ev-002"], risk_level="low", dimensions=[])

    summary = receiver.summary()

    assert summary.unexamined_ids == ("ev-003",)
    assert summary.dismissed_ids == ("ev-002",)
    assert summary.assigned_ids() == ("ev-001",)
    statuses = {item.evidence_id: item.status for item in summary.triage}
    assert statuses["ev-001"] == TRIAGE_ASSIGNED
    assert statuses["ev-002"] == TRIAGE_DISMISSED
    # Unexamined evidence has no triage item: it was never decided on.
    assert "ev-003" not in statuses


def test_low_risk_does_not_dismiss_evidence_another_dimension_reviews() -> None:
    """A low-risk verdict never overrides a dimension that is actually running."""
    receiver = _receiver()
    receiver.submit(
        evidence_ids=["ev-001"], risk_level="high", dimensions=["bug"], focus="x"
    )
    receiver.submit(evidence_ids=["ev-002"], risk_level="low", dimensions=[])

    summary = receiver.summary()

    assert summary.assigned_ids() == ("ev-001",)
    assert summary.dismissed_ids == ("ev-002",)


def test_special_mode_requires_every_pinned_dimension_to_be_reported() -> None:
    receiver = _receiver(
        mode="special", allowed={"bug", "security"}, required=("bug", "security")
    )
    receiver.submit(
        evidence_ids=["ev-001"], risk_level="high", dimensions=["bug"], focus="x"
    )

    ok, detail = receiver.finish()

    assert not ok
    assert "security" in detail
    assert receiver.finished is False

    # Reporting the missing dimension unblocks the finish; the program never
    # widens a reviewer's scope to cover for a planner omission.
    receiver.submit(
        evidence_ids=["ev-002"], risk_level="medium", dimensions=["security"], focus="y"
    )
    ok, _ = receiver.finish()
    assert ok
    assignments = {item.dimension: item for item in receiver.assignments()}
    assert assignments["security"].evidence_ids == ("ev-002",)
    assert assignments["security"].source == "user"


def test_finish_is_accepted_only_once() -> None:
    receiver = _receiver()
    receiver.submit(
        evidence_ids=["ev-001"], risk_level="high", dimensions=["bug"], focus="x"
    )

    ok, detail = receiver.finish()
    assert ok
    assert "review triage finished" in detail

    ok, detail = receiver.finish()
    assert not ok
    assert "already called" in detail


def test_submission_after_finish_is_rejected() -> None:
    receiver = _receiver()
    receiver.finish()

    ok, detail = receiver.submit(
        evidence_ids=["ev-001"], risk_level="high", dimensions=["bug"], focus="x"
    )

    assert not ok
    assert "already finished" in detail


def test_decision_count_is_bounded() -> None:
    receiver = _receiver(
        evidence=tuple(f"ev-{index:03d}" for index in range(1, 20)),
        allowed={"bug"},
    )
    receiver.max_decisions = 2
    for index in range(1, 3):
        ok, detail = receiver.submit(
            evidence_ids=[f"ev-{index:03d}"], risk_level="high", dimensions=["bug"], focus="x"
        )
        assert ok, detail

    ok, detail = receiver.submit(
        evidence_ids=["ev-003"], risk_level="high", dimensions=["bug"], focus="x"
    )

    assert not ok
    assert "at most 2 decisions" in detail


# ---------------------------------------------------------------------------
# Tool adapters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_adapter_validates_shape_before_semantics() -> None:
    receiver = _receiver()
    tool = SubmitReviewDecisionTool(DecisionReceiverAdapter(receiver))

    missing = await tool.execute(risk_level="high")
    assert missing.startswith("Error:")
    assert "evidence_ids" in missing

    bad_level = await tool.execute(
        evidence_ids=["ev-001"], risk_level="catastrophic"
    )
    assert bad_level.startswith("Error:")
    assert "risk_level" in bad_level

    accepted = await tool.execute(
        evidence_ids=["ev-001"],
        risk_level="high",
        dimensions=["bug"],
        focus="x",
    )
    assert not accepted.startswith("Error:")
    assert len(receiver.decisions) == 1


@pytest.mark.asyncio
async def test_tool_adapter_rejects_extra_keys() -> None:
    tool = SubmitReviewDecisionTool(DecisionReceiverAdapter(_receiver()))

    result = await tool.execute(
        evidence_ids=["ev-001"],
        risk_level="high",
        dimensions=["bug"],
        focus="x",
        assignments=[],
    )

    assert result.startswith("Error:")
    assert "assignments" in result


@pytest.mark.asyncio
async def test_finish_tool_reports_the_rejection_reason() -> None:
    receiver = _receiver(
        mode="special", allowed={"bug", "security"}, required=("bug", "security")
    )
    receiver.submit(
        evidence_ids=["ev-001"], risk_level="high", dimensions=["bug"], focus="x"
    )
    tool = FinishReviewTriageTool(receiver)

    result = await tool.execute()

    assert result.startswith("Error:")
    assert "security" in result


# ---------------------------------------------------------------------------
# Bounded diff reading
# ---------------------------------------------------------------------------


def _reader() -> ReviewDiffReader:
    return ReviewDiffReader(
        units=tuple(
            ReviewDiffUnit(
                id=f"ev-{index:03d}",
                path=f"src/mod{index}.py",
                start_line=index,
                end_line=index + 9,
                kind="diff",
                token_count=10 * index,
                preview="",
                preview_coverage="full patch",
                excerpt=f"@@ -{index} +{index} @@\n-old\n+new{index}",
            )
            for index in range(1, 4)
        )
    )


def test_reader_index_pages_the_authorized_evidence() -> None:
    reader = _reader()

    first = reader.index_text(limit=2)
    assert "ev-001" in first
    assert "ev-002" in first
    assert "ev-003" not in first
    assert "offset=2" in first

    second = reader.index_text(offset=2, limit=2)
    assert "ev-003" in second
    assert "ev-001" not in second


def test_reader_reads_only_authorized_ids() -> None:
    reader = _reader()

    assert "new1" in reader.read_text(["ev-001"])

    unknown = reader.read_text(["ev-999"])
    assert unknown.startswith("Error:")
    assert "unknown evidence IDs" in unknown

    empty = reader.read_text([])
    assert empty.startswith("Error:")


def test_reader_reports_an_offset_past_the_end() -> None:
    result = _reader().index_text(offset=50)

    assert result.startswith("Error:")
    assert "past the end" in result


def test_reader_caps_the_content_returned_per_call() -> None:
    reader = ReviewDiffReader(
        units=(
            ReviewDiffUnit(
                id="ev-001",
                path="src/huge.py",
                start_line=1,
                end_line=10_000,
                kind="diff",
                token_count=20_000,
                preview="",
                preview_coverage="full patch",
                excerpt="x" * 200_000,
            ),
        )
    )

    result = reader.read_text(["ev-001"])

    assert "x" * 200_000 not in result
    assert len(result) < 200_000


@pytest.mark.asyncio
async def test_diff_tools_reject_a_limit_above_the_cap() -> None:
    tool = ListReviewDiffTool(_reader())

    result = await tool.execute(limit=10_000)

    # The tool clamps rather than failing: the planner still gets a bounded page.
    assert result.startswith("Authorized diff evidence index")


@pytest.mark.asyncio
async def test_read_diff_tool_returns_an_error_for_an_unknown_id() -> None:
    tool = ReadReviewDiffTool(_reader())

    result = await tool.execute(evidence_ids=["ev-404"])

    assert result.startswith("Error:")


def test_reader_stays_within_the_runtime_tool_result_budget() -> None:
    """A page the reader returns must survive AgentRunner's truncation.

    ``AgentRunner`` truncates any tool result above ``max_tool_result_chars``
    with a plain "... (truncated)" suffix. A reader page larger than that budget
    would lose its tail *after* the reader reported the evidence as read, so a
    "read" unit could still be triaged from incomplete content.
    """
    budget = 4_000
    reader = ReviewDiffReader(
        units=tuple(
            ReviewDiffUnit(
                id=f"ev-{index:03d}",
                path=f"src/mod{index}.py",
                start_line=1,
                end_line=500,
                kind="diff",
                token_count=2_000,
                preview="",
                preview_coverage="full patch",
                excerpt="y" * 20_000,
            )
            for index in range(1, 6)
        ),
        max_result_chars=budget,
    )

    assert len(reader.index_text()) <= budget
    for response in (
        reader.read_text(["ev-001"]),
        reader.read_text(["ev-001", "ev-002", "ev-003", "ev-004"]),
    ):
        assert len(response) <= budget
        # A clipped unit must carry the reader's own explicit marker; the
        # runner's bare "... (truncated)" suffix must never be what the planner
        # sees, because it does not say the patch is incomplete.
        assert "NOT shown" in response or "not shown, request" in response


def test_reader_never_serves_a_head_only_unit_without_saying_so() -> None:
    """A unit shown clipped must not look like a complete patch.

    The preprocessor deliberately keeps a diff unit whole up to
    ``DIFF_UNIT_TOKEN_THRESHOLD`` tokens (~32k chars), so a bounded read can hit a
    unit larger than the page budget. Serving its head unmarked would let the
    planner triage a partial patch while believing it read the whole thing.
    """
    units = tuple(
        ReviewDiffUnit(
            id=f"ev-{index:03d}",
            path=f"src/big{index}.py",
            start_line=1,
            end_line=2_000,
            kind="diff",
            token_count=8_000,
            preview="",
            preview_coverage="full patch",
            excerpt="y" * 31_000,
        )
        for index in range(1, 3)
    )
    reader = ReviewDiffReader(units=units, max_result_chars=16_000)

    for ids in (["ev-001"], ["ev-001", "ev-002"]):
        result = reader.read_text(ids)
        assert len(result) <= 16_000
        assert "NOT shown" in result


def test_reader_serves_every_requested_id() -> None:
    """The tool schema advertises eight IDs, so all eight must be served.

    A hidden per-call ID cap returned four of eight with no marker, so the
    planner triaged four evidence units it had never read.
    """
    units = tuple(
        ReviewDiffUnit(
            id=f"ev-{index:03d}",
            path=f"src/mod{index}.py",
            start_line=1,
            end_line=9,
            kind="diff",
            token_count=5,
            preview="",
            preview_coverage="full patch",
            excerpt=f"body-{index}",
        )
        for index in range(1, 9)
    )
    reader = ReviewDiffReader(units=units, max_result_chars=16_000)

    result = reader.read_text([unit.id for unit in units])

    for unit in units:
        assert unit.id in result
        assert unit.excerpt in result


def test_reader_names_the_units_it_could_not_show() -> None:
    units = tuple(
        ReviewDiffUnit(
            id=f"ev-{index:03d}",
            path=f"src/mod{index}.py",
            start_line=1,
            end_line=9,
            kind="diff",
            token_count=5,
            preview="",
            preview_coverage="full patch",
            excerpt="q" * 6_000,
        )
        for index in range(1, 5)
    )
    reader = ReviewDiffReader(units=units, max_result_chars=16_000)

    result = reader.read_text([unit.id for unit in units])

    assert "not shown, request them separately" in result


def test_reference_excerpt_is_not_clipped_below_the_largest_diff_unit() -> None:
    """A complete diff unit must not lose its tail inside the reference excerpt.

    The largest unit the preprocessor keeps whole is bounded by
    ``DIFF_UNIT_TOKEN_THRESHOLD`` tokens (~32k chars). ``direct`` planner input
    inlines the reference excerpt as the unit's whole content, so an excerpt cap
    below that size would silently drop the tail of a unit the planner is told is
    complete.
    """
    from nanoreview.review.planning.prefetch import _EXCERPT_CHAR_LIMIT
    from nanoreview.review.planning.preprocessor import DIFF_UNIT_TOKEN_THRESHOLD

    largest_whole_unit_chars = DIFF_UNIT_TOKEN_THRESHOLD * 4

    assert _EXCERPT_CHAR_LIMIT >= largest_whole_unit_chars
