"""Single structured evidence manifest for the review planner.

The Planner sees exactly one manifest. It is generated from the same structured
:class:`~nanoreview.review.types.EvidenceReference` data the reviewer
assignments reference, so there is no parallel "summary" string and reference
list to keep in sync. The manifest is budgeted to ``context_window_tokens *
40%`` (80k for the fixed 200k review window): high-priority references are kept
and low-priority ones are dropped with an explicit omission record so the
coverage gap stays visible.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from nanoreview.review.planning.preprocessor import estimate_tokens
from nanoreview.review.types import EvidenceReference, ReviewEvidenceBundle

#: Manifest format version, persisted with review snapshots for replay.
MANIFEST_VERSION = "evidence-manifest/1"

#: Description of how the manifest's token counts are produced.
MANIFEST_TOKEN_COUNTER = "estimate_tokens(ceil(chars/4))"

#: Fraction of the review context window reserved for the planner manifest.
MANIFEST_BUDGET_RATIO = 0.40

#: Fixed planner manifest budget for the 200k review window.
PLANNER_MANIFEST_BUDGET_TOKENS = 80_000

#: Hard cap on the number of skipped-file descriptions rendered in the manifest.
_MAX_SKIPPED_LINES = 50


def manifest_budget_for(context_window_tokens: int) -> int:
    """Return the planner manifest budget for *context_window_tokens*."""
    if context_window_tokens <= 0:
        return PLANNER_MANIFEST_BUDGET_TOKENS
    return int(context_window_tokens * MANIFEST_BUDGET_RATIO)


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One retained evidence reference as the planner receives it."""

    id: str
    path: str
    start_line: int | None
    end_line: int | None
    kind: str
    role: str
    token_count: int
    matched: tuple[str, ...] = ()
    risk_hints: tuple[str, ...] = ()
    preview_coverage: str = ""
    preview: str = ""


@dataclass(frozen=True, slots=True)
class OmittedEntry:
    """One evidence reference dropped to fit the manifest budget."""

    id: str
    path: str
    reason: str
    token_count: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceManifest:
    """The budgeted, single structured input handed to the planner."""

    version: str = MANIFEST_VERSION
    token_counter: str = MANIFEST_TOKEN_COUNTER
    budget_tokens: int = PLANNER_MANIFEST_BUDGET_TOKENS
    entries: tuple[ManifestEntry, ...] = ()
    omitted: tuple[OmittedEntry, ...] = ()
    skipped: tuple[str, ...] = ()
    used_tokens: int = 0
    omitted_tokens: int = 0

    @property
    def retained_count(self) -> int:
        return len(self.entries)

    @property
    def omitted_count(self) -> int:
        return len(self.omitted)

    def authorized_ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.entries)

    def stats(self) -> dict[str, object]:
        """Budget/coverage statistics for persistence and logging."""
        return {
            "version": self.version,
            "token_counter": self.token_counter,
            "budget_tokens": self.budget_tokens,
            "used_tokens": self.used_tokens,
            "retained": self.retained_count,
            "omitted": self.omitted_count,
            "omitted_tokens": self.omitted_tokens,
            "skipped": len(self.skipped),
            "total_references": self.retained_count + self.omitted_count,
        }

    def snapshot_payload(self) -> dict[str, object]:
        """JSON-serializable manifest record for snapshot persistence.

        Previews are summarized by character length rather than copied: the
        snapshot records *what the planner saw* (ids, ranges, budget, coverage)
        without duplicating reviewed source into the artifact.
        """
        return {
            **self.stats(),
            "entries": [
                {
                    "id": entry.id,
                    "path": entry.path,
                    "start_line": entry.start_line,
                    "end_line": entry.end_line,
                    "kind": entry.kind,
                    "role": entry.role,
                    "token_count": entry.token_count,
                    "matched": list(entry.matched),
                    "risk_hints": list(entry.risk_hints),
                    "preview_coverage": entry.preview_coverage,
                    "preview_chars": len(entry.preview),
                }
                for entry in self.entries
            ],
            "omitted_entries": [
                {
                    "id": entry.id,
                    "path": entry.path,
                    "reason": entry.reason,
                    "token_count": entry.token_count,
                }
                for entry in self.omitted
            ],
            "unreviewed": list(self.skipped),
        }


def reference_priority(reference: EvidenceReference) -> tuple:
    """Sort key: main before related, then richer routing signals, then size."""
    return (
        0 if not reference.is_related else 1,
        -(len(reference.risk_hints) + len(reference.matched)),
        reference.token_count,
        reference.id,
    )


def _entry_from_reference(reference: EvidenceReference) -> ManifestEntry:
    return ManifestEntry(
        id=reference.id,
        path=reference.path,
        start_line=reference.start_line,
        end_line=reference.end_line,
        kind=reference.kind,
        role="related" if reference.is_related else "main",
        token_count=reference.token_count,
        matched=tuple(reference.matched),
        risk_hints=tuple(reference.risk_hints),
        preview_coverage=reference.preview_coverage,
        preview=reference.preview or "(no preview)",
    )


def _range_part(entry: ManifestEntry) -> str:
    if entry.start_line is not None and entry.end_line is not None:
        return f":{entry.start_line}-{entry.end_line}"
    return ""


def render_entry(entry: ManifestEntry) -> str:
    """Render one manifest entry to its planner-visible text block."""
    return (
        "- {id}: {path}{range_part} [{kind}] tokens={tokens} role={role}\n"
        "  matched: {matched}\n"
        "  risk_hints: {hints}\n"
        "  preview_coverage: {coverage}\n"
        "  {preview}".format(
            id=entry.id,
            path=entry.path,
            range_part=_range_part(entry),
            kind=entry.kind,
            tokens=entry.token_count or "?",
            role=entry.role,
            matched=", ".join(entry.matched) or "none",
            hints=", ".join(entry.risk_hints) or "none",
            coverage=entry.preview_coverage or "unknown",
            preview=entry.preview,
        )
    )


def _trim_preview(preview: str, budget_tokens: int) -> str:
    """Hard-clip a single oversized preview so at least one entry fits."""
    char_budget = max(1, budget_tokens * 4 - 200)
    if len(preview) <= char_budget:
        return preview
    return preview[:char_budget].rstrip() + "\n... (preview trimmed to manifest budget)"


def build_evidence_manifest(
    evidence: ReviewEvidenceBundle | None,
    *,
    budget_tokens: int = PLANNER_MANIFEST_BUDGET_TOKENS,
) -> EvidenceManifest:
    """Build the single budgeted manifest from structured evidence references."""
    references = list(evidence.references) if evidence is not None else []
    ordered = sorted(references, key=reference_priority)
    entries: list[ManifestEntry] = []
    omitted: list[OmittedEntry] = []
    omitted_tokens = 0
    used = 0
    for reference in ordered:
        entry = _entry_from_reference(reference)
        cost = estimate_tokens(render_entry(entry))
        if entries and used + cost > budget_tokens:
            omitted.append(
                OmittedEntry(reference.id, reference.path, "budget_exhausted", reference.token_count)
            )
            omitted_tokens += reference.token_count
            continue
        if not entries and cost > budget_tokens:
            # The single highest-priority entry alone overflows the budget: keep
            # it but trim its preview instead of dropping all evidence.
            entry = replace(entry, preview=_trim_preview(entry.preview, budget_tokens))
            cost = estimate_tokens(render_entry(entry))
        entries.append(entry)
        used += cost

    skipped: list[str] = []
    if evidence is not None and evidence.skipped:
        skipped = [
            summary.describe()
            for summary in list(evidence.skipped_by_file().values())[:_MAX_SKIPPED_LINES]
        ]
        # The skipped/coverage note shares the manifest budget.
        while skipped and used + estimate_tokens("\n".join(skipped)) > budget_tokens:
            skipped.pop()
    return EvidenceManifest(
        budget_tokens=budget_tokens,
        entries=tuple(entries),
        omitted=tuple(omitted),
        skipped=tuple(skipped),
        used_tokens=used,
        omitted_tokens=omitted_tokens,
    )


def render_manifest(manifest: EvidenceManifest) -> str:
    """Render the planner-visible manifest text (references + coverage notes)."""
    parts: list[str] = []
    if manifest.entries:
        parts.extend(render_entry(entry) for entry in manifest.entries)
    else:
        parts.append("(no program-authorized evidence references)")
    if manifest.skipped:
        parts.append(
            "\n## Not Reviewed (out of scope)\n"
            + "\n".join(f"- {line}" for line in manifest.skipped)
        )
    if manifest.omitted:
        parts.append(
            "\n## Manifest Omitted (budget)\n"
            f"- {manifest.omitted_count} reference(s) omitted to fit the "
            f"{manifest.budget_tokens}-token manifest budget "
            f"(~{manifest.omitted_tokens} evidence tokens unreviewed): "
            + ", ".join(f"{entry.id} ({entry.path})" for entry in manifest.omitted[:10])
            + ("..." if manifest.omitted_count > 10 else "")
        )
    return "\n".join(parts)


__all__ = [
    "EvidenceManifest",
    "MANIFEST_BUDGET_RATIO",
    "MANIFEST_TOKEN_COUNTER",
    "MANIFEST_VERSION",
    "ManifestEntry",
    "OmittedEntry",
    "PLANNER_MANIFEST_BUDGET_TOKENS",
    "build_evidence_manifest",
    "manifest_budget_for",
    "reference_priority",
    "render_entry",
    "render_manifest",
]
