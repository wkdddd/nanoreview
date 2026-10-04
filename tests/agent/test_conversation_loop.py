"""Contract tests for the reserved ConversationLoop boundary.

The module is a placeholder: it freezes the conversation phase's input/output
shape before any of its execution exists. These tests pin two things the
review side is already built against:

* the boundary stays **execution-free** — no runner, no provider, no loop, only
  the two frozen dataclasses and one pure helper, so a later phase cannot
  accidentally start executing through it;
* the shapes are immutable and carry the review association *by reference*,
  never the report artifact contents, so the conversation side cannot rewrite
  the authoritative report or the review run state.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError

import pytest

import nanoreview.agent.conversation_loop as boundary
from nanoreview.agent.conversation_loop import (
    MAX_PENDING_CONVERSATION_MESSAGES,
    ConversationTurnRequest,
    ConversationTurnResult,
    review_context_for,
)
from nanoreview.agent.review_state import ReviewRunStatus
from nanoreview.review.result import ReviewHandoffState, ReviewResult
from nanoreview.agent.coordinator import ReviewHandoff

REPORT_REF = "review-artifacts/run-a.json"


def _result() -> ReviewResult:
    return ReviewResult(
        run_id="run-a",
        session_key="cli:review",
        status=ReviewRunStatus.COMPLETED,
        handoff=ReviewHandoffState.COMPLETE,
        report_ref=REPORT_REF,
        coverage=("security",),
    )


def test_the_pending_cap_is_the_documented_bound() -> None:
    assert MAX_PENDING_CONVERSATION_MESSAGES == 20


def test_the_boundary_exposes_no_execution() -> None:
    """Only the contract lives here: adding a runner here is a phase-3 change."""
    defined = {
        name
        for name, value in vars(boundary).items()
        if not name.startswith("_")
        and (inspect.isclass(value) or inspect.isfunction(value))
        and getattr(value, "__module__", None) == boundary.__name__
    }

    assert set(boundary.__all__) == {
        "MAX_PENDING_CONVERSATION_MESSAGES",
        "ConversationTurnRequest",
        "ConversationTurnResult",
        "review_context_for",
    }
    assert defined == {
        "ConversationTurnRequest",
        "ConversationTurnResult",
        "review_context_for",
    }
    assert {
        name for name in defined if inspect.isfunction(getattr(boundary, name))
    } == {"review_context_for"}

    # The boundary pulls in nothing that could execute a turn: the review-side
    # types are imported only under ``TYPE_CHECKING``, so a runtime import here
    # would already be a phase-3 change.
    tree = ast.parse(inspect.getsource(boundary))
    runtime_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            runtime_imports.add(node.module or "")
        elif isinstance(node, ast.Import):
            runtime_imports.update(alias.name for alias in node.names)
    assert runtime_imports == {"__future__", "dataclasses", "typing"}


def test_a_turn_request_defaults_to_no_handoff() -> None:
    """The handoff is populated only for the first turn after a review."""
    request = ConversationTurnRequest(session_key="cli:review", content="hello")

    assert request.handoff is None
    assert request.media == ()
    assert request.metadata == {}


def test_turn_dataclasses_are_immutable_and_do_not_share_defaults() -> None:
    request = ConversationTurnRequest(session_key="cli:review", content="hello")
    other = ConversationTurnRequest(session_key="cli:review", content="again")
    request.metadata["message_id"] = "m-1"

    assert other.metadata == {}
    with pytest.raises(FrozenInstanceError):
        request.content = "changed"  # type: ignore[misc]

    outcome = ConversationTurnResult(
        session_key="cli:review", conversation_turn_id="t-1"
    )
    assert outcome.repairs == ()
    with pytest.raises(FrozenInstanceError):
        outcome.final_content = "changed"  # type: ignore[misc]


def test_a_turn_request_accepts_the_coordinator_handoff() -> None:
    handoff = ReviewHandoff(result=_result(), report_markdown="## Report", fits=True)

    request = ConversationTurnRequest(
        session_key="cli:review",
        content="follow-up",
        handoff=handoff,
    )

    assert request.handoff is handoff
    assert request.handoff.block
    assert request.handoff.directive


def test_review_context_is_by_reference_and_never_the_report_body() -> None:
    result = _result()

    context = review_context_for(result)

    assert context == result.as_payload()
    assert context["run_id"] == "run-a"
    assert context["report_ref"] == REPORT_REF
    assert context["source"] == "review_agent"
    assert "report_markdown" not in context
    assert not any(isinstance(value, str) and value.startswith("##") for value in context.values())
