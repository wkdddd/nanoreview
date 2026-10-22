"""Deterministic diff evidence pre-processing for review workflows.

Pipeline (per review run):

1. **Patch/hunk splitting** — a changed file whose patch is below
   ``DIFF_UNIT_TOKEN_THRESHOLD`` stays one complete diff unit; at or above it
   the patch is split at ``@@`` boundaries and an oversized hunk is skipped.
2. **Query annotation** — the user's review-query hit words are recorded on
   each unit as display metadata; they never order or route evidence.
3. **Budget filtering** — units keep the frozen order; the ones that no longer
   fit are recorded as unreviewed (a later, smaller unit may still be kept).
4. **ID, preview and manifest** — stable ``ev-NNN`` IDs, one representative
   preview per unit and the manifest text handed to the Planner.

Every changed path is processed: preprocessing makes no language decision, so
a diff in a language without a bundled grammar is still reviewed as diff text.

Preprocessing consumes the diff only: it never enumerates the repository,
never reads whole source files and never parses ASTs. Reading a changed file's
surrounding context is the reviewer's own restricted-scope job, and repository
indexing belongs to the independent RAG module.

Every skipped file/hunk is reported with a reason so progress events and the
final report can show exactly what was not reviewed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

# ---------------------------------------------------------------------------
# Query term extraction
# ---------------------------------------------------------------------------

#: User review-query hit words extracted from a unit. This is plain evidence
#: metadata for display; it never routes or orders evidence, and the review
#: pipeline decides purely from the frozen diff the planner actually reads.
_TERM_RE = re.compile(r"[A-Za-z0-9_]{2,}")

# ---------------------------------------------------------------------------
# Token budget accounting
# ---------------------------------------------------------------------------

#: Static reservations subtracted from the provider context window before any
#: evidence token caps are derived (system prompt, tool descriptions, output
#: reserve). Keeps direct caps conservative.
SYSTEM_PROMPT_TOKENS = 2_000
TOOL_DESCRIPTIONS_TOKENS = 3_000
OUTPUT_RESERVE_TOKENS = 4_000
SAFETY_MARGIN_RATIO = 0.10

#: Hard clamps for derived caps.
MIN_DIRECT_UNIT_TOKENS = 1_600
MIN_USABLE_TOKENS = 1_000

#: Per-file diff unit threshold. A changed file whose patch is below this many
#: tokens is kept as one complete diff unit; at or above it the hunk-splitting
#: strategy applies. Judged per file, so a large target does not force every
#: small changed file into hunks.
DIFF_UNIT_TOKEN_THRESHOLD = 8_000


def estimate_tokens(text: str) -> int:
    """Cheap deterministic token estimate (~4 chars per token)."""
    return max(1, (len(text) + 3) // 4) if text else 0


@dataclass(frozen=True, slots=True)
class EvidenceBudget:
    """Token caps derived from config and the provider context window."""

    usable_input_tokens: int
    evidence_budget_tokens: int
    direct_cap_tokens: int

    @classmethod
    def from_options(
        cls,
        *,
        evidence_token_budget: int,
        context_window_tokens: int | None,
    ) -> "EvidenceBudget":
        window = context_window_tokens or 65_536
        overhead = SYSTEM_PROMPT_TOKENS + TOOL_DESCRIPTIONS_TOKENS + OUTPUT_RESERVE_TOKENS
        margin = int(window * SAFETY_MARGIN_RATIO)
        usable = max(MIN_USABLE_TOKENS, window - overhead - margin)
        # Total accepted evidence also fits inside the review token budget.
        # A configured value above ``usable`` cannot widen the window, so
        # record the clamp source instead of failing silently.
        if evidence_token_budget <= MIN_USABLE_TOKENS:
            clamp_source = "min_floor"
        elif evidence_token_budget < usable:
            clamp_source = "evidence_token_budget"
        else:
            clamp_source = "usable_window"
        evidence_budget = max(MIN_USABLE_TOKENS, min(evidence_token_budget, usable))
        logger.info(
            "review.evidence.budget evidence_budget={} source={} configured={} usable_window={}",
            evidence_budget,
            clamp_source,
            evidence_token_budget,
            usable,
        )
        # Small diffs below this stay in direct mode (whole evidence inlined).
        direct_cap = min(evidence_budget, max(MIN_DIRECT_UNIT_TOKENS, usable // 4))
        return cls(
            usable_input_tokens=usable,
            evidence_budget_tokens=evidence_budget,
            direct_cap_tokens=direct_cap,
        )


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProgrammaticEvidenceOptions:
    # Budget knobs mirrored from ReviewConfig so callers can inject config.
    evidence_token_budget: int = 100_000
    prefetch_budget_chars: int = 16_000
    context_window_tokens: int | None = 65_536
    # Per-unit planner preview budget: ``preview_target_chars`` is a soft goal
    # (mid-size units may exceed it), ``preview_hard_limit`` is the absolute
    # per-preview ceiling. The overall manifest cap stays prefetch_budget_chars.
    preview_target_chars: int = 600
    preview_hard_limit: int = 3_000

    def __post_init__(self) -> None:
        if self.preview_target_chars < 1:
            raise ValueError("preview_target_chars must be >= 1")
        if self.preview_hard_limit < self.preview_target_chars:
            raise ValueError("preview_hard_limit must be >= preview_target_chars")

    @classmethod
    def from_review_config(cls, review_config: Any) -> "ProgrammaticEvidenceOptions":
        """Build options from a ReviewConfig instance, falling back to defaults."""
        options = cls()
        if review_config is None:
            return options
        for name, attr in (
            ("evidence_token_budget", "evidence_token_budget"),
            ("prefetch_budget_chars", "prefetch_budget_chars"),
            ("preview_target_chars", "preview_target_chars"),
            ("preview_hard_limit", "preview_hard_limit"),
        ):
            value = getattr(review_config, attr, None)
            if isinstance(value, int) and value > 0:
                setattr(options, name, value)
        if options.preview_hard_limit < options.preview_target_chars:
            raise ValueError("preview_hard_limit must be >= preview_target_chars")
        return options


@dataclass(slots=True)
class CodeUnit:
    """One accepted diff processing unit."""

    path: str
    kind: str  # diff
    name: str = ""
    start_line: int = 1
    end_line: int = 1
    text: str = ""
    token_count: int = 0
    unit_id: str = ""
    role: str = "main"  # main | related
    parent_id: str | None = None
    # User review-query hit words only. Plain display metadata: it neither
    # routes nor orders evidence.
    matched: list[str] = field(default_factory=list)
    # Non-risk relation/source labels; no producer emits them for diff units.
    tags: tuple[str, ...] = ()
    # Representative planner preview plus the real lines it actually covers.
    preview: str = ""
    preview_coverage: str = ""
    # First line covered by ``text``; None means ``start_line``.
    text_start_line: int | None = None


@dataclass(slots=True)
class SkippedUnit:
    """One file-level or line-range unit excluded from the review pipeline."""

    path: str
    reason: str
    start_line: int | None = None
    end_line: int | None = None
    detail: str = ""


@dataclass(slots=True)
class ProgrammaticEvidenceResult:
    units: list[CodeUnit]
    skipped: list[SkippedUnit]
    context: str
    mode: str = "direct"  # direct | oversized
    total_tokens: int = 0
    accepted_tokens: int = 0


def preview_text(text: str, *, max_lines: int = 12, max_chars: int = 600) -> str:
    """Render a bounded head-only preview (fallback when no rich preview exists)."""
    lines = text.replace("\r\n", "\n").splitlines()
    preview = "\n".join(lines[:max_lines])
    if len(preview) > max_chars:
        preview = preview[:max_chars].rstrip() + "..."
    if len(lines) > max_lines:
        preview += "\n... (truncated)"
    return preview or "(empty)"


# ---------------------------------------------------------------------------
# Planner preview construction
# ---------------------------------------------------------------------------

#: Lines of local context kept around risk-hint hits inside previews.
PREVIEW_HIT_CONTEXT_LINES = 2

#: Head/tail lines kept when sampling whole-repository chunks.
PREVIEW_EDGE_LINES = 5

#: Gaps up to this many lines are bridged instead of emitting omission markers.
PREVIEW_BRIDGE_GAP_LINES = 2

#: Highest priority still selected by the sampler (6 = filler, always dropped).
_PREVIEW_KEEP_THRESHOLD = 5

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

#: Function/method/class declarations (preview priority 1 everywhere).
_SIGNATURE_RE = re.compile(
    r"^\s*(?:export\s+|default\s+|abstract\s+|public\s+|private\s+|protected\s+|static\s+|async\s+)*"
    r"(?:def|class|function|interface|enum|struct|impl|trait)\b"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*(?::[^=]*)?="
    r"\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
)

#: AST key-structure lines (decorators, module entry guards, export statements).
_STRUCTURE_RE = re.compile(r"^\s*@[\w.]+|^\s*if\s+__name__\s*==|^\s*export\s+(?:default\s+)?")


def _hunk_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """New-file line ranges covered by each ``@@`` hunk header."""
    ranges: list[tuple[int, int]] = []
    for line in lines:
        header = _HUNK_HEADER_RE.match(line)
        if header:
            start = int(header.group(1))
            count = int(header.group(2) or 1)
            ranges.append((start, start + max(count - 1, 0)))
    return ranges


def _diff_line_numbers(lines: list[str]) -> list[int | None]:
    """Map each patch line to its new-file line number (None when undefined).

    Added and context lines consume a new-file line; removed lines anchor at
    the surrounding new-file position. File meta before the first hunk and
    ``\\ No newline`` markers carry no new-file number.
    """
    numbers: list[int | None] = []
    new_line: int | None = None
    in_hunk = False
    for line in lines:
        header = _HUNK_HEADER_RE.match(line)
        if header:
            in_hunk = True
            new_line = int(header.group(1))
            numbers.append(new_line)
            continue
        if not in_hunk:
            numbers.append(None)
            continue
        prefix = line[:1]
        if prefix == "+":
            numbers.append(new_line)
            if new_line is not None:
                new_line += 1
        elif prefix == "-":
            numbers.append(new_line)
        else:  # context line (" text") or a whitespace-stripped empty one
            numbers.append(new_line)
            if new_line is not None:
                new_line += 1
    return numbers


def _preview_line_priorities(
    lines: list[str],
    *,
    is_diff: bool,
    query_terms: set[str],
) -> list[int | None]:
    """Rank preview lines: 1 = must keep … 6 = filler the sampler drops.

    Diff previews keep hunk headers, every added/removed line (even mid-hunk),
    change-adjacent context and signatures. Plain-text previews (used only by
    text-only evidence providers) keep signatures, query hit lines, structural
    lines and the edges. Ranking is structural: nothing here infers risk or
    routes the review.
    """
    priorities: list[int | None] = [None] * len(lines)
    terms = {term.lower() for term in query_terms if len(term) >= 2}

    def assign(index: int, priority: int) -> None:
        current = priorities[index]
        if current is None or priority < current:
            priorities[index] = priority

    if is_diff:
        changed: list[int] = []
        for index, line in enumerate(lines):
            if line.startswith("@@"):
                assign(index, 1)
            elif line[:1] in {"+", "-"}:
                assign(index, 2)
                changed.append(index)
            elif _SIGNATURE_RE.match(line):
                assign(index, 5)
        for index in changed:
            for offset in range(-PREVIEW_HIT_CONTEXT_LINES, PREVIEW_HIT_CONTEXT_LINES + 1):
                if offset == 0:
                    continue
                neighbor = index + offset
                if 0 <= neighbor < len(lines):
                    assign(neighbor, 3)
    else:
        for index, line in enumerate(lines):
            if _SIGNATURE_RE.match(line):
                assign(index, 1)
            elif terms and any(term in line.lower() for term in terms):
                assign(index, 2)
                for offset in range(-PREVIEW_HIT_CONTEXT_LINES, PREVIEW_HIT_CONTEXT_LINES + 1):
                    if offset == 0:
                        continue
                    neighbor = index + offset
                    if 0 <= neighbor < len(lines):
                        assign(neighbor, 2)
            elif _STRUCTURE_RE.match(line):
                assign(index, 4)
        edges = (*range(min(PREVIEW_EDGE_LINES, len(lines))), *range(max(0, len(lines) - PREVIEW_EDGE_LINES), len(lines)))
        for index in edges:
            assign(index, 5)
    return priorities


def _format_line_ranges(ranges: list[tuple[int, int]]) -> str:
    return ", ".join(f"{start}-{end}" if start != end else str(start) for start, end in ranges)


def _omission_marker(numbers: list[int | None], first: int, last: int) -> str:
    """Stable omission marker naming the real lines that were skipped."""
    start_no, end_no = numbers[first], numbers[last]
    if start_no is not None and end_no is not None:
        if start_no == end_no:
            return f"... omitted line {start_no} ..."
        return f"... omitted lines {start_no}-{end_no} ..."
    return "... omitted ..."


def build_unit_preview(
    text: str,
    *,
    kind: str,
    start_line: int,
    role: str = "main",
    query_terms: Iterable[str] = (),
    target_chars: int = 600,
    hard_limit: int = 3_000,
    coverage_note: str = "",
) -> tuple[str, str]:
    """Build a representative, line-annotated preview for one evidence unit.

    Returns ``(preview, preview_coverage)``. Units at or below ``target_chars``
    keep their full text. Larger units are sampled by line priority and may
    exceed the target up to ``hard_limit``, where the least important lines are
    dropped and every skipped range is recorded with a stable
    ``... omitted lines A-B ...`` marker plus a coverage description.
    Diff previews use new-file line numbers; text-only previews use the real
    lines they were rendered from and never fabricate ``+``/``-`` markers.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return "(empty)", "empty unit"
    lines = normalized.splitlines()
    is_diff = kind == "diff"
    last_line = start_line + len(lines) - 1

    if len(normalized) <= target_chars:
        # Small units keep their full text; nothing is sampled or omitted.
        if is_diff:
            hunks = _hunk_ranges(lines)
            coverage = "full patch"
            if hunks:
                coverage += f"; new-file lines {_format_line_ranges(hunks)}"
        else:
            coverage = f"full chunk lines {start_line}-{last_line}"
        if coverage_note:
            coverage = f"lines {start_line}-{last_line}; {coverage_note}"
        return normalized, coverage

    if is_diff:
        numbers = _diff_line_numbers(lines)
    else:
        numbers = [start_line + index for index in range(len(lines))]

    priorities = _preview_line_priorities(lines, is_diff=is_diff, query_terms=set(query_terms))
    selected = {
        index
        for index, priority in enumerate(priorities)
        if priority is not None and priority <= _PREVIEW_KEEP_THRESHOLD
    }
    if not selected:
        # Nothing ranked (e.g. a context-only patch): keep the leading lines.
        selected = set(range(min(8, len(lines))))

    # Supplementary context stays compact: it never widens the scope.
    budget = target_chars if role == "related" else hard_limit

    def assemble(sel: set[int], *, bridge: bool) -> tuple[str, list[tuple[int, int]]]:
        ordered = sorted(sel)
        segments: list[list[int]] = []
        for index in ordered:
            if bridge and segments and index - segments[-1][-1] - 1 <= PREVIEW_BRIDGE_GAP_LINES:
                segments[-1].append(index)
            else:
                segments.append([index])
        parts: list[str] = []
        covered: list[tuple[int, int]] = []
        previous_last: int | None = None
        for segment in segments:
            first, last = segment[0], segment[-1]
            if previous_last is not None:
                parts.append(_omission_marker(numbers, previous_last + 1, first - 1))
            parts.append("\n".join(lines[first : last + 1]))
            covered.append((first, last))
            previous_last = last
        return "\n".join(parts), covered

    preview, covered = assemble(selected, bridge=True)
    if len(preview) > budget:
        # Disable bridging first so each drop actually shrinks the preview.
        preview, covered = assemble(selected, bridge=False)
        while len(preview) > budget and len(selected) > 1:
            # Drop the least important line; ties drop later lines first.
            drop = max(selected, key=lambda index: (priorities[index] or 6, index))
            selected.remove(drop)
            preview, covered = assemble(selected, bridge=False)
    if len(preview) > budget:
        # Pathological single-line overflow (e.g. minified code): hard slice.
        truncation_marker = "\n... (preview truncated)"
        if budget <= len(truncation_marker):
            # Keep the absolute bound even for unusually small caller budgets.
            preview = truncation_marker.strip()[:budget]
        else:
            body_limit = budget - len(truncation_marker)
            preview = preview[:body_limit].rstrip() + truncation_marker

    covered_ranges = [
        (numbers[first], numbers[last])
        for first, last in covered
        if numbers[first] is not None and numbers[last] is not None
    ]
    if is_diff:
        hunks = _hunk_ranges(lines)
        coverage = f"hunks span new-file lines {_format_line_ranges(hunks)}" if hunks else "patch"
        if covered_ranges:
            coverage += f"; preview covers new-file lines {_format_line_ranges(covered_ranges)}"
    else:
        coverage = f"chunk lines {start_line}-{last_line}"
        if covered_ranges:
            coverage += f"; preview covers lines {_format_line_ranges(covered_ranges)}"
    if coverage_note:
        coverage = f"{coverage}; {coverage_note}"
    return preview, coverage


class ProgrammaticEvidenceService:
    """Bounded, deterministic diff evidence selection.

    ``workspace`` is kept so callers can resolve the review root per scope; the
    service itself never touches the working tree, it only transforms patches.
    """

    def __init__(self, workspace: Path, options: ProgrammaticEvidenceOptions | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.options = options or ProgrammaticEvidenceOptions()

    def diff_units(
        self,
        patches: dict[str, str],
        *,
        review_query: str,
        context_window_tokens: int | None = None,
    ) -> ProgrammaticEvidenceResult:
        """Convert diff patches into bounded diff units with budget filtering."""
        budgets = EvidenceBudget.from_options(
            evidence_token_budget=self.options.evidence_token_budget,
            context_window_tokens=context_window_tokens or self.options.context_window_tokens,
        )
        units: list[CodeUnit] = []
        skipped: list[SkippedUnit] = []
        for path, patch in patches.items():
            units.extend(self._patch_units(path, patch, skipped))
        terms = self._query_terms(review_query)
        for unit in units:
            self._annotate_unit(unit, terms)
        total_tokens = sum(unit.token_count for unit in units)
        mode = "direct" if total_tokens <= budgets.direct_cap_tokens else "oversized"
        if mode == "oversized":
            units, budget_skipped = self._apply_main_budget(units, budgets)
            skipped.extend(budget_skipped)
        units, _ = self._assign_ids(units, start=1)
        for unit in units:
            self._build_preview(unit, terms=terms)
        return ProgrammaticEvidenceResult(
            units=units,
            skipped=skipped,
            context=self.render_manifest(units, skipped, budget_chars=self.options.prefetch_budget_chars),
            mode=mode,
            total_tokens=total_tokens,
            accepted_tokens=sum(unit.token_count for unit in units),
        )

    # -- diff units -------------------------------------------------------

    @staticmethod
    def _patch_units(
        path: str,
        patch: str,
        skipped: list[SkippedUnit],
    ) -> list[CodeUnit]:
        tokens = estimate_tokens(patch)
        if tokens < DIFF_UNIT_TOKEN_THRESHOLD:
            start, end = ProgrammaticEvidenceService._patch_range(patch)
            return [
                CodeUnit(
                    path=path,
                    kind="diff",
                    name=f"{Path(path).name} (diff)",
                    start_line=start,
                    end_line=end,
                    text=patch,
                    token_count=tokens,
                )
            ]
        # At or above the per-file threshold, split oversized patches by hunks.
        pieces = re.split(r"(?=^@@ )", patch, flags=re.MULTILINE)
        # Text before the first ``@@`` is the git file header, not a hunk:
        # emitting it would create a bogus 1-1 unit that only carries
        # ``diff --git`` / ``+++`` lines. A patch with no hunk at all is still
        # handled as one piece so an oversized body stays reported.
        has_hunks = any(piece.lstrip("\r\n").startswith("@@") for piece in pieces)
        units: list[CodeUnit] = []
        for piece in pieces:
            if not piece.strip():
                continue
            if has_hunks and not piece.lstrip("\r\n").startswith("@@"):
                continue
            piece_tokens = estimate_tokens(piece)
            match = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))?", piece)
            start = int(match.group(1)) if match else 1
            count = int(match.group(2) or 1) if match else 1
            end = start + max(count - 1, 0)
            if piece_tokens > DIFF_UNIT_TOKEN_THRESHOLD:
                skipped.append(
                    SkippedUnit(path, "token_limit_exceeded", start, end, detail="oversized hunk")
                )
                continue
            units.append(
                CodeUnit(
                    path=path,
                    kind="diff",
                    name=f"{Path(path).name} (diff hunk)",
                    start_line=start,
                    end_line=end,
                    text=piece,
                    token_count=piece_tokens,
                )
            )
        return units

    @staticmethod
    def _patch_range(patch: str) -> tuple[int, int]:
        hunks = re.findall(r"^@@ .+$", patch, flags=re.MULTILINE)
        if not hunks:
            return 1, 1

        def parse(hunk: str) -> tuple[int, int]:
            match = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))?", hunk)
            if not match:
                return 1, 1
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            return start, start + max(count - 1, 0)

        first = parse(hunks[0])
        last = parse(hunks[-1])
        return first[0], max(first[1], last[1])

    # -- annotation and budget filtering ---------------------------------

    @staticmethod
    def _query_terms(query: str) -> set[str]:
        return {term.lower() for term in _TERM_RE.findall(query or "")}

    @staticmethod
    def _annotate_unit(unit: CodeUnit, terms: set[str]) -> None:
        """Record the user's review-query hit words on one unit.

        This is display metadata only. Evidence keeps the order the frozen diff
        produced it in: nothing is re-ranked by risk, query hits or touched
        lines, so what the planner triages is exactly the admitted change in a
        stable sequence.
        """
        lower = f"{unit.path}\n{unit.text}".lower()
        unit.matched = sorted(term for term in terms if term in lower)

    def _build_preview(self, unit: CodeUnit, *, terms: set[str]) -> None:
        """Render the representative planner preview and coverage label."""
        origin = unit.text_start_line if unit.text_start_line is not None else unit.start_line
        unit.preview, unit.preview_coverage = build_unit_preview(
            unit.text,
            kind=unit.kind,
            start_line=origin,
            role=unit.role,
            query_terms=terms,
            target_chars=self.options.preview_target_chars,
            hard_limit=self.options.preview_hard_limit,
        )

    def _apply_main_budget(
        self,
        units: list[CodeUnit],
        budgets: EvidenceBudget,
    ) -> tuple[list[CodeUnit], list[SkippedUnit]]:
        """Keep diff units within the evidence budget, in frozen order.

        Units are visited in the stable order preprocessing produced them (file
        order, then line order per file) and any unit that no longer fits is
        recorded as skipped; a later unit that still fits is kept, so the
        accepted set is the frozen order minus the overflow. Nothing re-ranks
        evidence by risk or query hits, so the planner triages the admitted
        change itself.
        """
        accepted: list[CodeUnit] = []
        skipped: list[SkippedUnit] = []
        used = 0
        for unit in units:
            if used + unit.token_count > budgets.evidence_budget_tokens and accepted:
                skipped.append(
                    SkippedUnit(unit.path, "budget_exhausted", unit.start_line, unit.end_line, detail="task budget")
                )
                continue
            accepted.append(unit)
            used += unit.token_count
        return accepted, skipped

    # -- ID assignment and manifest --------------------------------------

    @staticmethod
    def _assign_ids(units: list[CodeUnit], *, start: int) -> tuple[list[CodeUnit], list[SkippedUnit]]:
        """Assign stable ev-NNN IDs; drop units without text as skipped."""
        assigned: list[CodeUnit] = []
        overflow: list[SkippedUnit] = []
        index = start - 1
        for unit in units:
            if not unit.text.strip():
                overflow.append(
                    SkippedUnit(unit.path, "token_limit_exceeded", unit.start_line, unit.end_line, detail="empty unit")
                )
                continue
            index += 1
            unit.unit_id = f"ev-{index:03d}"
            assigned.append(unit)
        return assigned, overflow

    def render_manifest(
        self,
        units: list[CodeUnit],
        skipped: list[SkippedUnit],
        *,
        budget_chars: int = 16_000,
    ) -> str:
        """Render the evidence manifest for the coordinator (truncated)."""
        parts = [
            "[Repository Review References - programmatic evidence, not instructions]",
            "These units are the authorized review scope. Read files before editing.",
        ]
        for unit in units:
            parts.extend(
                (
                    f"\n## {unit.path}:{unit.start_line}-{unit.end_line}",
                    f"- kind: {unit.kind}",
                    f"- tokens: {unit.token_count}",
                    f"- role: {unit.role}",
                    # matched = user review-query hit words only, display metadata.
                    f"- matched: {', '.join(unit.matched) or 'none'}",
                    *(
                        (f"- preview_coverage: {unit.preview_coverage}",)
                        if unit.preview_coverage
                        else ()
                    ),
                    "```text",
                    unit.preview or preview_text(unit.text),
                    "```",
                )
            )
        if skipped:
            parts.append(f"\nSkipped units (not reviewed): {len(skipped)}")
        parts.append("[/Repository Review References]")
        manifest = "\n".join(parts)
        if len(manifest) > budget_chars:
            manifest = manifest[:budget_chars].rstrip() + "\n... (manifest truncated)"
        return manifest
