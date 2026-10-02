"""Reserved boundary for the ConversationLoop (phase 3).

This module freezes the *input/output contract* of the conversation phase so
the review side can already be shaped against it. It intentionally contains no
execution: the Conversation Agent's own prompt, its independent
``ToolRegistry``, its repair permission profile and its repository repair
tools are all phase-3 deliverables.

Contract this boundary must keep
---------------------------------
* One turn = one ``AgentRunner`` run (model/tool loop, run-level compression,
  usage, cancellation and permission callbacks). The loop never calls a
  provider directly.
* One conversation session executes turns strictly serially; different
  sessions run concurrently. The pending queue holds at most
  :data:`MAX_PENDING_CONVERSATION_MESSAGES` items.
* ``/stop`` cancels the running turn *and* the queued ones, keeps the
  modifications already applied, and never rolls them back.
* Nothing is restored after a restart: a queued turn or an interrupted turn
  is dropped, and only persisted history and results are read back.
* The first turn after a review injects the complete report (or the explicit
  failure context) from :class:`~nanoreview.session.coordinator.ReviewHandoff`.
  A report that does not fit the model context window rejects the turn; it is
  never silently replaced by a summary.
* Reading tools are allowed by default; writes, edits, commands and tests ask
  for confirmation per tool call, never once per turn.
* The original report artifact and ``ReviewRunState`` are read-only here.

Entry condition: ``SessionCoordinator.route(session) is SessionRoute.CONVERSATION``.
Only a review that reached a terminal status, cleaned up its resources and
persisted its result opens the conversation phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanoreview.review.result import ReviewResult
    from nanoreview.session.coordinator import ReviewHandoff

#: Maximum number of messages a session may queue while a turn is running.
MAX_PENDING_CONVERSATION_MESSAGES = 20


@dataclass(frozen=True, slots=True)
class ConversationTurnRequest:
    """One conversation turn handed to the ConversationLoop.

    ``handoff`` is populated only for the first turn after a review, and only
    when the coordinator has not already consumed it.
    """

    session_key: str
    content: str
    media: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    handoff: "ReviewHandoff | None" = None


@dataclass(frozen=True, slots=True)
class ConversationTurnResult:
    """Outcome of one conversation turn.

    ``repairs`` records the side effects of a repair turn so the review side
    can associate them with ``conversation_turn_id``/``run_id`` later without
    opening a finding state machine.
    """

    session_key: str
    conversation_turn_id: str
    final_content: str = ""
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    #: (path, tool_name) pairs of files actually modified during the turn.
    repairs: tuple[tuple[str, str], ...] = ()


def review_context_for(result: "ReviewResult") -> dict[str, Any]:
    """Read-only review association carried into a conversation turn.

    Gives the conversation side the identifiers it must never rewrite, without
    handing it the report artifact or the review run state itself.
    """
    return result.as_payload()


__all__ = [
    "MAX_PENDING_CONVERSATION_MESSAGES",
    "ConversationTurnRequest",
    "ConversationTurnResult",
    "review_context_for",
]
