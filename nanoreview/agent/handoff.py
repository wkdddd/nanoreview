"""Review -> conversation handoff value objects and their session write.

The handoff is the boundary between ``ReviewLoop`` (which owns the report
artifact and the run state) and ``ConversationLoop`` (which owns the
conversation history). It is deliberately a plain, immutable value: the
coordinator *prepares* it read-only inside the session lock, the conversation
loop *consumes* it by writing one replayable history message, and neither side
may rewrite the authoritative report.

Keeping the value object and its single session write here lets both the
coordinator and the conversation loop share them without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from nanoreview.review.result import (
    ReviewResult,
    render_handoff_block,
    render_handoff_directive,
)
from nanoreview.review.types import ReviewMetaKey

if TYPE_CHECKING:
    from nanoreview.session.manager import Session, SessionManager

#: Index / handoff markers stored on the persisted session messages.
REVIEW_CONTEXT_EVENT = "review_context"
REVIEW_HANDOFF_EVENT = "review_handoff"


@dataclass(frozen=True, slots=True)
class ReviewHandoff:
    """The review result about to be handed to the first conversation turn."""

    result: ReviewResult
    report_markdown: str | None
    fits: bool

    @property
    def block(self) -> str:
        """Full injected context: framing, gaps, and the complete report."""
        return render_handoff_block(self.result, self.report_markdown)

    @property
    def directive(self) -> str:
        """Short system-level directive naming provenance and handoff state."""
        return render_handoff_directive(self.result)


def consume_handoff(
    session: "Session",
    handoff: ReviewHandoff,
    sessions: "SessionManager",
) -> None:
    """Persist the injected handoff so later turns keep the same context.

    The block enters the conversation history (an assistant message that names
    ReviewAgent as its source) instead of being re-added to every system
    prompt, so it stays available to later turns and to consolidation while the
    report artifact remains the authoritative copy.
    """
    session.add_message(
        "assistant",
        handoff.block,
        injected_event=REVIEW_HANDOFF_EVENT,
        review_run_id=handoff.result.run_id,
        review_report_ref=handoff.result.report_ref,
        review_source="review_agent",
        review_handoff=handoff.result.handoff.value,
    )
    session.metadata[ReviewMetaKey.HANDOFF_RUN_ID] = handoff.result.run_id
    sessions.save(session)
    logger.info(
        "review.handoff.injected session={} run_id={} handoff={} report_chars={}",
        session.key,
        handoff.result.run_id,
        handoff.result.handoff.value,
        len(handoff.report_markdown or ""),
    )


__all__ = [
    "REVIEW_CONTEXT_EVENT",
    "REVIEW_HANDOFF_EVENT",
    "ReviewHandoff",
    "consume_handoff",
]
