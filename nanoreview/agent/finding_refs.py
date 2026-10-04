"""Transient finding-reference collection for one conversation turn.

Confirmed findings carry a report-local ID (``F001``) written into the report
Markdown, the report artifact and ``ReviewResult.findings``. When the user
names such an ID in a message, the conversation loop records the reference.

References are deliberately ephemeral: they live on the turn's own context and
in the logs. They are never persisted, never associated with a finding record,
and never used to infer repair status or rewrite the report.

The valid ID set comes from the report already in context (the handoff block
written into history on the first turn) — the conversation loop must not read
the report artifact, which stays owned by ``ReviewLoop``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from nanoreview.review.result import REVIEW_HANDOFF_HEADER

#: Report-local finding IDs (``F001``); 3+ digits also allow reports over 999.
FINDING_ID_PATTERN = re.compile(r"\bF\d{3,}\b", re.IGNORECASE)


def extract_finding_ids(text: str) -> list[str]:
    """Ordered, de-duplicated finding IDs that appear literally in *text*."""
    seen: dict[str, None] = {}
    for token in FINDING_ID_PATTERN.findall(text or ""):
        seen.setdefault(token.upper(), None)
    return list(seen)


def report_finding_ids(history: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Finding IDs of the report currently in context, taken from history.

    The handoff block written on the first conversation turn stays in history,
    so a later turn can validate a user reference without re-reading the report
    artifact. Only the most recent block counts, so a re-review in the same
    session replaces the valid ID set instead of merging with the old one.
    """
    latest: str | None = None
    for message in history:
        content = message.get("content")
        if isinstance(content, str) and REVIEW_HANDOFF_HEADER in content:
            latest = content
    if latest is None:
        return frozenset()
    return frozenset(extract_finding_ids(latest))


def collect_finding_references(
    user_texts: Iterable[str],
    valid_ids: Iterable[str],
) -> list[str]:
    """Explicit finding IDs the user named that exist in the current report.

    Multiple mentions are de-duplicated in first-seen order; IDs absent from
    the report are dropped. Nothing is inferred from titles, paths, fix diffs
    or model output.
    """
    valid = {str(item).upper() for item in valid_ids}
    references: list[str] = []
    for text in user_texts:
        for finding_id in extract_finding_ids(text if isinstance(text, str) else ""):
            if finding_id in valid and finding_id not in references:
                references.append(finding_id)
    return references


__all__ = [
    "FINDING_ID_PATTERN",
    "collect_finding_references",
    "extract_finding_ids",
    "report_finding_ids",
]
