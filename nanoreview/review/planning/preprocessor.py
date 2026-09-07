"""Deterministic repository evidence pre-processing for review workflows.

Pipeline (per review target):

1. **Inventory** — enumerate candidate files, classify each path as supported
   code (Python/JavaScript/TypeScript/JSX/TSX), unsupported code, document or
   config, and measure total size.
2. **Scale assessment** — three-state decision driven by the remaining token
   budget: small targets keep whole files/diffs directly, medium targets enter
   semantic chunking, oversized targets are filtered per file/chunk.
3. **Type/parse filtering** — unsupported code types and missing grammars are
   recorded as skipped units; supported files are parsed with tree-sitter and
   real syntax errors surface as standalone ``syntax_diagnostic`` evidence.
4. **Semantic chunking** — chunks follow function/method/class/module
   boundaries; oversized units are recursively split, tiny ones merged with
   adjacent same-level units.
5. **Related-context supplement** — at most one layer of import/export,
   caller, config and test-related chunks is attached to accepted main chunks.
6. **Budget filtering** — when the total exceeds the evidence budget, high
   priority units are kept and the remainder is recorded as unreviewed.

Every skipped file/chunk is reported with a reason so progress events and the
final report can show exactly what was not reviewed.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from nanoreview.review.file_filter import (
    DEFAULT_REVIEW_BINARY_EXTS,
    DEFAULT_REVIEW_IGNORE_DIRS,
    DEFAULT_REVIEW_IGNORE_GLOBS,
    review_file_filter_reason,
)

# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

#: Code languages the preprocessor can parse with tree-sitter.
SUPPORTED_SUFFIXES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

#: Code files outside the supported language set. They never fall back to a
#: line-window; they are skipped with ``unsupported_code_type``.
KNOWN_CODE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".go", ".rs", ".java", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
        ".cs", ".rb", ".kt", ".swift", ".scala", ".php", ".vue", ".svelte",
        ".sh", ".bash", ".zsh", ".ps1", ".bat", ".sql", ".css", ".scss",
        ".less", ".html", ".htm", ".m", ".mm", ".dart", ".ex", ".exs",
        ".erl", ".hs", ".clj", ".lua", ".pl", ".r", ".jl",
    }
)

DOCUMENT_SUFFIXES: frozenset[str] = frozenset({".md", ".txt", ".rst", ".adoc", ".org"})
CONFIG_SUFFIXES: frozenset[str] = frozenset(
    {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".xml", ".properties", ".editorconfig"}
)

# ---------------------------------------------------------------------------
# Grammar registry (loaded on demand; missing packs degrade per file)
# ---------------------------------------------------------------------------

_GRAMMAR_LOADERS: dict[str, str] = {
    "python": "tree_sitter_python:language",
    "javascript": "tree_sitter_javascript:language",
    "typescript": "tree_sitter_typescript:language_typescript",
    "tsx": "tree_sitter_typescript:language_tsx",
}

_DEFINITION_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({"function_definition", "class_definition"}),
    "javascript": frozenset(
        {"function_declaration", "class_declaration", "method_definition", "arrow_function"}
    ),
    "typescript": frozenset(
        {"function_declaration", "class_declaration", "method_definition", "arrow_function"}
    ),
    "tsx": frozenset(
        {"function_declaration", "class_declaration", "method_definition", "arrow_function"}
    ),
}

#: Statement-level node types used when a definition must be split further.
_STATEMENT_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset(
        {
            "function_definition", "class_definition", "decorated_definition",
            "expression_statement", "if_statement", "for_statement",
            "while_statement", "try_statement", "with_statement", "assignment",
        }
    ),
    "javascript": frozenset(
        {
            "function_declaration", "class_declaration", "method_definition",
            "expression_statement", "if_statement", "for_statement",
            "while_statement", "try_statement", "variable_declaration",
            "switch_statement", "export_statement",
        }
    ),
    "typescript": frozenset(
        {
            "function_declaration", "class_declaration", "method_definition",
            "expression_statement", "if_statement", "for_statement",
            "while_statement", "try_statement", "variable_declaration",
            "switch_statement", "export_statement",
        }
    ),
    "tsx": frozenset(
        {
            "function_declaration", "class_declaration", "method_definition",
            "expression_statement", "if_statement", "for_statement",
            "while_statement", "try_statement", "variable_declaration",
            "switch_statement", "export_statement",
        }
    ),
}

_NAME_NODE_TYPES = frozenset({"identifier", "name", "property_identifier", "type_identifier"})

#: Wrapper nodes holding the statement body of a definition; used when an
#: oversized single definition must be descended into for splitting.
_BODY_WRAPPER_TYPES = frozenset({"block", "statement_block", "function_body", "declaration_list"})

#: Risk routing hints and the word-boundary terms that trigger them. Hints are
#: program-generated candidate routing clues for the planner — never review
#: conclusions and never filters. Matching is word-boundary based so ``auth``
#: does not match ``author`` and ``sql`` would not match ``mysql``; a trailing
#: plural (``s``/``es``) is tolerated for natural code identifiers.
_RISK_TERMS: dict[str, tuple[str, ...]] = {
    "security:auth": ("auth",),
    "security:token": ("token",),
    "security:password": ("password",),
    "security:secret": ("secret",),
    "security:permission": ("permission",),
    "security:injection": ("inject", "injection"),
    "security:csrf": ("csrf",),
    "security:ssrf": ("ssrf",),
    "entrypoint:api": ("api",),
    "entrypoint:router": ("router", "controller"),
    "entrypoint:handler": ("handler", "server", "app", "main"),
    "config:env": ("env",),
    "config:settings": ("config", "settings", "pyproject", "workflow", "docker"),
    "config:package": ("package",),
    "tests:fixture": ("fixture", "spec"),
    "tests:regression": ("regression", "test"),
}
_RISK_HINT_PATTERNS: dict[str, re.Pattern[str]] = {
    hint: re.compile(
        "|".join(rf"(?<![a-z0-9]){re.escape(term)}(?:s|es)?(?![a-z0-9])" for term in terms)
    )
    for hint, terms in _RISK_TERMS.items()
}
_TERM_RE = re.compile(r"[A-Za-z0-9_]{2,}")

# Import/export target patterns used by the related-context supplement.
_PY_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([\w.]+)", re.MULTILINE)
_JS_IMPORT_RE = re.compile(r"""(?:from\s+|require\(\s*|import\s+)["']([^"']+)["']""")

# ---------------------------------------------------------------------------
# Token budget accounting
# ---------------------------------------------------------------------------

#: Static reservations subtracted from the provider context window before any
#: evidence token caps are derived (system prompt, tool descriptions, output
#: reserve). Keeps direct/chunk/task caps conservative.
SYSTEM_PROMPT_TOKENS = 2_000
TOOL_DESCRIPTIONS_TOKENS = 3_000
OUTPUT_RESERVE_TOKENS = 4_000
SAFETY_MARGIN_RATIO = 0.10

#: Hard clamps for derived caps.
MIN_CHUNK_TOKENS = 400
MAX_CHUNK_TOKENS = 4_000
MIN_USABLE_TOKENS = 1_000

#: Fixed context lines prepended/appended to semantic chunk text. Overlap is
#: comprehension context only; canonical start/end lines (and therefore the
#: authorized review scope) never widen because of it.
OVERLAP_CONTEXT_LINES = 10


def estimate_tokens(text: str) -> int:
    """Cheap deterministic token estimate (~4 chars per token)."""
    return max(1, (len(text) + 3) // 4) if text else 0


@dataclass(frozen=True, slots=True)
class EvidenceBudget:
    """Token caps derived from config and the provider context window."""

    usable_input_tokens: int
    evidence_budget_tokens: int
    direct_cap_tokens: int
    chunk_cap_tokens: int
    task_cap_tokens: int
    related_budget_tokens: int

    @classmethod
    def from_options(
        cls,
        *,
        token_budget: int,
        subagent_evidence_budget_chars: int,
        context_window_tokens: int | None,
    ) -> "EvidenceBudget":
        window = context_window_tokens or 65_536
        overhead = SYSTEM_PROMPT_TOKENS + TOOL_DESCRIPTIONS_TOKENS + OUTPUT_RESERVE_TOKENS
        margin = int(window * SAFETY_MARGIN_RATIO)
        usable = max(MIN_USABLE_TOKENS, window - overhead - margin)
        # Total accepted evidence also fits inside the review token budget.
        evidence_budget = max(MIN_USABLE_TOKENS, min(token_budget, usable))
        # Per-subagent evidence task cap (chars -> tokens).
        task_cap = max(MIN_CHUNK_TOKENS // 2, subagent_evidence_budget_chars // 4)
        task_cap = min(task_cap, usable)
        # A single chunk should leave room for sibling chunks in one task.
        chunk_cap = max(MIN_CHUNK_TOKENS, min(task_cap // 2, MAX_CHUNK_TOKENS))
        # Small targets below this stay whole-file (direct mode).
        direct_cap = min(evidence_budget, max(4 * MIN_CHUNK_TOKENS, usable // 4))
        return cls(
            usable_input_tokens=usable,
            evidence_budget_tokens=evidence_budget,
            direct_cap_tokens=direct_cap,
            chunk_cap_tokens=chunk_cap,
            task_cap_tokens=task_cap,
            related_budget_tokens=max(MIN_CHUNK_TOKENS, evidence_budget // 4),
        )


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProgrammaticEvidenceOptions:
    max_files: int = 2000
    max_file_chars: int = 80_000
    max_results: int = 8
    snippet_lines: int = 8
    include_tests: bool = True
    binary_extensions: set[str] = field(default_factory=lambda: set(DEFAULT_REVIEW_BINARY_EXTS))
    ignore_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_REVIEW_IGNORE_DIRS))
    ignore_globs: tuple[str, ...] = DEFAULT_REVIEW_IGNORE_GLOBS
    # Budget knobs mirrored from ReviewConfig so callers can inject config.
    token_budget: int = 100_000
    prefetch_budget_chars: int = 16_000
    subagent_evidence_budget_chars: int = 24_000
    prefetch_dense_backfill_limit: int = 256
    context_window_tokens: int | None = 65_536
    # Per-unit planner preview budget: ``preview_target_chars`` is a soft goal
    # (mid-size chunks may exceed it), ``preview_hard_limit`` is the absolute
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
            ("token_budget", "token_budget"),
            ("prefetch_budget_chars", "prefetch_budget_chars"),
            ("subagent_evidence_budget_chars", "subagent_evidence_budget_chars"),
            ("prefetch_dense_backfill_limit", "prefetch_dense_backfill_limit"),
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
class ProgrammaticEvidenceRequest:
    source_type: str
    review_query: str
    max_results: int | None = None
    files: Iterable[Path] | None = None
    snapshot_files: dict[str, str] | None = None
    snapshot_name: str | None = None
    include_tests: bool | None = None
    related_tests: bool = True
    touched_lines: dict[str, list[int]] | None = None
    trace_id: str = ""
    context_window_tokens: int | None = None


@dataclass(slots=True)
class CodeUnit:
    """One accepted processing unit (file, chunk, diff or diagnostic)."""

    path: str
    kind: str  # file|module|class|function|diff|document|config|syntax_diagnostic
    name: str = ""
    start_line: int = 1
    end_line: int = 1
    text: str = ""
    token_count: int = 0
    unit_id: str = ""
    role: str = "main"  # main | related
    parent_id: str | None = None
    score: float = 0.0
    # User review-query hit words only (risk clues live in risk_hints).
    matched: list[str] = field(default_factory=list)
    # Non-risk relation/source labels: related, import, caller, test, symbol:*.
    tags: tuple[str, ...] = ()
    # Program-generated candidate risk routing clues (e.g. "security:token").
    risk_hints: tuple[str, ...] = ()
    # Representative planner preview plus the real lines it actually covers.
    preview: str = ""
    preview_coverage: str = ""
    # First repository line covered by ``text``; None means ``start_line``.
    # Diverges from start_line only when context overlap extended the text.
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
class InventoryEntry:
    path: str
    size_chars: int
    file_class: str  # supported_code | unsupported_code | document | config | other
    language: str | None = None


@dataclass(slots=True)
class ProgrammaticEvidenceResult:
    units: list[CodeUnit]
    skipped: list[SkippedUnit]
    context: str
    mode: str = "chunked"  # direct | chunked | oversized
    inventory: list[InventoryEntry] = field(default_factory=list)
    total_tokens: int = 0
    accepted_tokens: int = 0
    cache_root: Path | None = None


def classify_path(path: str) -> tuple[str, str | None]:
    """Classify a repository path into a processing class and language."""
    suffix = Path(path).suffix.lower()
    if suffix in SUPPORTED_SUFFIXES:
        return "supported_code", SUPPORTED_SUFFIXES[suffix]
    if suffix in KNOWN_CODE_SUFFIXES:
        return "unsupported_code", None
    if suffix in DOCUMENT_SUFFIXES:
        return "document", None
    if suffix in CONFIG_SUFFIXES:
        return "config", None
    return "other", None


class _GrammarRegistry:
    """Lazy tree-sitter grammar loader with per-language availability."""

    def __init__(self) -> None:
        self._parsers: dict[str, Any] = {}
        self._unavailable: set[str] = set()

    def get_parser(self, language: str) -> Any | None:
        if language in self._parsers:
            return self._parsers[language]
        if language in self._unavailable:
            return None
        spec = _GRAMMAR_LOADERS.get(language)
        parser = None
        if spec is not None:
            parser = self._load(language, spec)
        if parser is None:
            self._unavailable.add(language)
        else:
            self._parsers[language] = parser
        return parser

    @staticmethod
    def _load(language: str, spec: str) -> Any | None:
        module_name, attr = spec.split(":", 1)
        try:
            import importlib

            import tree_sitter

            mod = importlib.import_module(module_name)
            tree_language = tree_sitter.Language(getattr(mod, attr)())
            return tree_sitter.Parser(tree_language)
        except Exception as exc:  # noqa: BLE001 - grammar packs are optional
            logger.debug("preprocessor.grammar_unavailable language={} reason={}", language, exc)
            return None


_GRAMMARS = _GrammarRegistry()


def _safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "repo"


def _snapshot_scope_digest(snapshot_name: str, files: dict[str, str]) -> str:
    hasher = hashlib.sha256()
    hasher.update(snapshot_name.encode("utf-8"))
    hasher.update(b"\0")
    for rel in sorted(files):
        hasher.update(rel.encode("utf-8", errors="surrogatepass"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(files[rel].encode("utf-8")).hexdigest().encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def detect_risk_hints(lowered_text: str) -> tuple[str, ...]:
    """Detect candidate risk routing hints on already-lowercased text.

    Word-boundary matching keeps ``auth`` from matching ``author`` and would
    keep ``sql`` from matching ``mysql``. Hints are routing clues only — never
    review conclusions and never inclusion filters.
    """
    if not lowered_text:
        return ()
    return tuple(hint for hint, pattern in _RISK_HINT_PATTERNS.items() if pattern.search(lowered_text))


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
    change-adjacent context, risk-hint hits and signatures. Whole-repository
    previews keep signatures, risk-hint hits with a local window, query hit
    lines, AST key-structure lines and the chunk edges.
    """
    priorities: list[int | None] = [None] * len(lines)
    terms = {term.lower() for term in query_terms if len(term) >= 2}

    def assign(index: int, priority: int) -> None:
        current = priorities[index]
        if current is None or priority < current:
            priorities[index] = priority

    def risk_hit(line: str) -> bool:
        # Strip the diff prefix so markers themselves do not look like code.
        body = line[1:] if line[:1] in {"+", "-", " "} and len(line) > 1 else line
        return bool(detect_risk_hints(body.lower()))

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
            elif risk_hit(line):
                assign(index, 4)
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
            elif risk_hit(line):
                assign(index, 2)
                for offset in range(-PREVIEW_HIT_CONTEXT_LINES, PREVIEW_HIT_CONTEXT_LINES + 1):
                    if offset == 0:
                        continue
                    neighbor = index + offset
                    if 0 <= neighbor < len(lines):
                        assign(neighbor, 2)
            elif terms and any(term in line.lower() for term in terms):
                assign(index, 3)
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

    Returns ``(preview, preview_coverage)``. Chunks at or below
    ``target_chars`` keep their full text. Larger chunks are sampled by line
    priority and may exceed the target up to ``hard_limit``, where the least
    important lines are dropped and every skipped range is recorded with a
    stable ``... omitted lines A-B ...`` marker plus a coverage description.
    Diff previews use new-file line numbers; repository previews use real file
    lines and never fabricate ``+``/``-`` diff markers. Related-role units stay
    compact because they only supplement the authorized review scope.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return "(empty)", "empty unit"
    lines = normalized.splitlines()
    is_diff = kind == "diff"
    last_line = start_line + len(lines) - 1

    if len(normalized) <= target_chars:
        # Small chunks keep their full text; nothing is sampled or omitted.
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

    # Related context stays compact: it supplements, never widens, the scope.
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
    """Bounded, in-memory evidence selection with semantic chunking."""

    def __init__(self, workspace: Path, options: ProgrammaticEvidenceOptions | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.options = options or ProgrammaticEvidenceOptions()

    # ------------------------------------------------------------------
    # File enumeration (kept for evidence service scope resolution)
    # ------------------------------------------------------------------

    def iter_candidate_files(self, root: Path | None = None) -> Iterable[Path]:
        base = (root or self.workspace).expanduser().resolve()
        count = 0
        for dirpath, dirnames, filenames in os.walk(base):
            current = Path(dirpath)
            rel_parts = current.relative_to(base).parts
            if self._ignored_dirs(rel_parts):
                dirnames[:] = []
                continue
            dirnames[:] = [name for name in dirnames if not self._ignored_dirs((*rel_parts, name))]
            for filename in sorted(filenames):
                if count >= self.options.max_files:
                    return
                path = current / filename
                if self.is_ignored(path, base=base) or path.suffix.lower() in self.options.binary_extensions:
                    continue
                count += 1
                yield path

    def is_ignored(self, path: Path, *, base: Path | None = None) -> bool:
        root = (base or self.workspace).resolve()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            return True
        return (
            review_file_filter_reason(rel) is not None
            or self._ignored_dirs(path.relative_to(root).parts)
            or any(fnmatch.fnmatch(path.name, pattern) for pattern in self.options.ignore_globs)
        )

    def _ignored_dirs(self, parts: tuple[str, ...]) -> bool:
        return bool(any(part in self.options.ignore_dirs for part in parts) or (parts and parts[0] == ".nanoreview"))

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    async def retrieve(self, request: ProgrammaticEvidenceRequest) -> ProgrammaticEvidenceResult:
        """Run the full preprocessing pipeline off the event loop."""
        return await asyncio.to_thread(self._retrieve_sync, request)

    def _retrieve_sync(self, request: ProgrammaticEvidenceRequest) -> ProgrammaticEvidenceResult:
        trace_id = request.trace_id or "preproc"
        started = time.perf_counter()
        budgets = EvidenceBudget.from_options(
            token_budget=self.options.token_budget,
            subagent_evidence_budget_chars=self.options.subagent_evidence_budget_chars,
            context_window_tokens=request.context_window_tokens or self.options.context_window_tokens,
        )
        files = self._collect_files(request)
        inventory = [
            InventoryEntry(
                path=rel,
                size_chars=len(text),
                file_class=file_class,
                language=language,
            )
            for rel, text in files.items()
            for file_class, language in (classify_path(rel),)
        ]
        total_tokens = sum(estimate_tokens(text) for text in files.values())
        mode = self._assess_scale(total_tokens, budgets)
        units: list[CodeUnit] = []
        skipped: list[SkippedUnit] = []
        for entry in inventory:
            file_units, file_skipped = self._process_file(entry, files[entry.path], mode, budgets)
            units.extend(file_units)
            skipped.extend(file_skipped)
        terms = self._query_terms(request.review_query)
        for unit in units:
            self._score_unit(unit, terms, (request.touched_lines or {}).get(unit.path) or [])

        if mode == "direct":
            mains = units
        else:
            mains, budget_skipped = self._apply_main_budget(units, budgets)
            skipped.extend(budget_skipped)
            # Hard guard against pathological chunk explosions.
            if len(mains) > self.options.prefetch_dense_backfill_limit:
                ordered = sorted(mains, key=lambda unit: (-unit.score, unit.path, unit.start_line))
                overflow = ordered[self.options.prefetch_dense_backfill_limit :]
                mains = ordered[: self.options.prefetch_dense_backfill_limit]
                skipped.extend(
                    SkippedUnit(unit.path, "budget_exhausted", unit.start_line, unit.end_line, detail="unit limit")
                    for unit in overflow
                )

        # Main IDs first so related units can reference their parent chunk.
        mains, empty_overflow = self._assign_ids(mains, start=1)
        skipped.extend(empty_overflow)
        related: list[CodeUnit] = []
        if mode != "direct":
            related = self._supplement_related(
                mains,
                files=files,
                inventory=inventory,
                budgets=budgets,
                include_tests=(
                    request.include_tests
                    if request.include_tests is not None
                    else self.options.include_tests
                ),
                related_tests=request.related_tests,
            )
            related, _ = self._assign_ids(related, start=len(mains) + 1)
        accepted = [*mains, *related]
        # Rich previews are built once, after ID assignment, so both the
        # manifest and the evidence bundle share the same sampled view.
        for unit in accepted:
            self._build_preview(unit, terms=terms)
        cache_root = None
        if request.snapshot_files is not None and request.snapshot_name:
            cache_root = self.write_snapshot(request.snapshot_name, request.snapshot_files)
        result = ProgrammaticEvidenceResult(
            units=accepted,
            skipped=skipped,
            context=self.render_manifest(accepted, skipped, budget_chars=self.options.prefetch_budget_chars),
            mode=mode,
            inventory=inventory,
            total_tokens=total_tokens,
            accepted_tokens=sum(unit.token_count for unit in accepted),
            cache_root=cache_root,
        )
        logger.info(
            "preprocessor.retrieve.done trace_id={} mode={} files={} units={} related={} skipped={} "
            "total_tokens={} accepted_tokens={} elapsed_ms={:.1f}",
            trace_id,
            mode,
            len(inventory),
            len(mains),
            len(related),
            len(skipped),
            total_tokens,
            result.accepted_tokens,
            (time.perf_counter() - started) * 1000,
        )
        return result

    def diff_units(
        self,
        patches: dict[str, str],
        *,
        review_query: str,
        context_window_tokens: int | None = None,
    ) -> ProgrammaticEvidenceResult:
        """Convert diff patches into bounded diff units with budget filtering."""
        budgets = EvidenceBudget.from_options(
            token_budget=self.options.token_budget,
            subagent_evidence_budget_chars=self.options.subagent_evidence_budget_chars,
            context_window_tokens=context_window_tokens or self.options.context_window_tokens,
        )
        units: list[CodeUnit] = []
        skipped: list[SkippedUnit] = []
        for path, patch in patches.items():
            # Unsupported code types never produce diff chunks; they are
            # recorded as file-level skipped so reports explain the exclusion.
            file_class, _ = classify_path(path)
            if file_class == "unsupported_code":
                skipped.append(
                    SkippedUnit(
                        path,
                        "unsupported_code_type",
                        detail=f"no parser for {Path(path).suffix}",
                    )
                )
                continue
            units.extend(self._patch_units(path, patch, budgets, skipped))
        terms = self._query_terms(review_query)
        for unit in units:
            self._score_unit(unit, terms, [])
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

    # ------------------------------------------------------------------
    # Stage helpers
    # ------------------------------------------------------------------

    def _collect_files(self, request: ProgrammaticEvidenceRequest) -> dict[str, str]:
        if request.snapshot_files is not None:
            return {
                str(path).replace("\\", "/"): text[: self.options.max_file_chars]
                for path, text in request.snapshot_files.items()
                if review_file_filter_reason(str(path), text) is None
            }
        files: dict[str, str] = {}
        for path in request.files or self.iter_candidate_files():
            try:
                rel = path.relative_to(self.workspace).as_posix()
                text = path.read_text(encoding="utf-8")[: self.options.max_file_chars]
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if review_file_filter_reason(rel, text) is None:
                files[rel] = text
        return files

    @staticmethod
    def _assess_scale(total_tokens: int, budgets: EvidenceBudget) -> str:
        if total_tokens <= budgets.direct_cap_tokens:
            return "direct"
        if total_tokens <= budgets.evidence_budget_tokens:
            return "chunked"
        return "oversized"

    def _process_file(
        self,
        entry: InventoryEntry,
        text: str,
        mode: str,
        budgets: EvidenceBudget,
    ) -> tuple[list[CodeUnit], list[SkippedUnit]]:
        if entry.file_class == "unsupported_code":
            return [], [
                SkippedUnit(
                    entry.path,
                    "unsupported_code_type",
                    detail=f"no parser for {Path(entry.path).suffix}",
                )
            ]
        if entry.file_class == "supported_code" and mode != "direct":
            return self._parse_and_chunk(entry, text, budgets)
        # Direct mode and auxiliary files: keep the whole file after the safety
        # filter when it fits a single chunk; otherwise record it as skipped.
        tokens = estimate_tokens(text)
        if tokens <= budgets.chunk_cap_tokens or mode == "direct" and tokens <= budgets.direct_cap_tokens:
            kind = (
                "document"
                if entry.file_class == "document"
                else "config"
                if entry.file_class == "config"
                else "file"
            )
            unit = CodeUnit(
                path=entry.path,
                kind=kind,
                name=Path(entry.path).name,
                start_line=1,
                end_line=len(text.replace("\r\n", "\n").splitlines()) or 1,
                text=text,
                token_count=tokens,
                tags=("auxiliary",) if entry.file_class in {"document", "config", "other"} else (),
            )
            return [unit], []
        return [], [SkippedUnit(entry.path, "token_limit_exceeded", detail=f"{tokens} tokens > chunk cap")]

    # -- parsing and semantic chunking ----------------------------------

    def _parse_and_chunk(
        self,
        entry: InventoryEntry,
        text: str,
        budgets: EvidenceBudget,
    ) -> tuple[list[CodeUnit], list[SkippedUnit]]:
        language = entry.language or ""
        parser = _GRAMMARS.get_parser(language)
        if parser is None:
            return [], [SkippedUnit(entry.path, "missing_grammar", detail=f"grammar for {language} unavailable")]
        try:
            tree = parser.parse(text.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - parser crashes must degrade per file
            logger.warning("preprocessor.parse_failed path={} reason={}", entry.path, exc)
            return [], [SkippedUnit(entry.path, "parse_error", detail=str(exc)[:200])]
        lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        root = tree.root_node
        definitions = self._walk_definitions(root, _DEFINITION_TYPES.get(language, frozenset()))
        error_ranges = self._error_ranges(root)
        if root.has_error:
            covered = sum(node.end_point[0] - node.start_point[0] + 1 for node in definitions)
            total_lines = len(lines) or 1
            if not definitions or covered < total_lines * 0.2:
                detail = (
                    "; ".join(f"lines {start}-{end}" for start, end in error_ranges[:3])
                    or "unrecoverable syntax errors"
                )
                return [], [SkippedUnit(entry.path, "parse_error", detail=detail)]

        skipped: list[SkippedUnit] = []
        units: list[CodeUnit] = []
        if error_ranges and definitions:
            # Standalone syntax diagnostic evidence for supported types.
            diag_text = "\n".join(
                f"{entry.path}:{start}-{end}: syntax error region" for start, end in error_ranges[:10]
            )
            first, last = error_ranges[0][0], error_ranges[-1][1]
            units.append(
                CodeUnit(
                    path=entry.path,
                    kind="syntax_diagnostic",
                    name=f"{Path(entry.path).name}:syntax",
                    start_line=first,
                    end_line=last,
                    text=diag_text,
                    token_count=estimate_tokens(diag_text),
                    tags=("syntax_diagnostic",),
                )
            )
        # Definition chunks plus module-level fragments between them.
        spans: list[tuple[int, int, str, str]] = []
        cursor = 1
        for node in definitions:
            start = node.start_point[0] + 1
            end = min(node.end_point[0] + 1, len(lines))
            if start > end:
                continue
            if start > cursor:
                spans.append((cursor, start - 1, "module", ""))
            spans.append((start, end, self._node_kind(node), self._node_name(node)))
            cursor = end + 1
        if cursor <= len(lines):
            spans.append((cursor, len(lines), "module", ""))
        for start, end, kind, name in spans:
            chunk_text = "\n".join(lines[start - 1 : end])
            if not chunk_text.strip():
                continue
            token_count = estimate_tokens(chunk_text)
            if token_count > budgets.chunk_cap_tokens:
                pieces, split_skipped = self._split_span(
                    entry.path, lines, start, end, language, budgets
                )
                units.extend(pieces)
                skipped.extend(split_skipped)
            else:
                units.append(
                    CodeUnit(
                        path=entry.path,
                        kind=kind,
                        name=name,
                        start_line=start,
                        end_line=end,
                        text=chunk_text,
                        token_count=token_count,
                    )
                )
        merged = self._merge_small_units(units, budgets)
        # Overlap is applied after merging so adjacent chunks never duplicate
        # context inside one another's authorized text.
        return [self._with_context_overlap(unit, lines) for unit in merged], skipped

    @staticmethod
    def _with_context_overlap(unit: CodeUnit, lines: list[str]) -> CodeUnit:
        """Extend a semantic chunk's text with bounded surrounding file lines.

        The canonical ``start_line``/``end_line`` range stays unchanged: overlap
        only helps understanding and never widens the authorized review scope.
        ``token_count`` also stays canonical so budget and merge decisions keep
        tracking the authorized scope; the actual overlapped text cost is
        measured downstream when subagent input tokens are estimated.
        """
        if unit.kind == "syntax_diagnostic" or not lines:
            return unit
        overlap_start = max(1, unit.start_line - OVERLAP_CONTEXT_LINES)
        overlap_end = min(len(lines), unit.end_line + OVERLAP_CONTEXT_LINES)
        if overlap_start >= unit.start_line and overlap_end <= unit.end_line:
            return unit
        # Track where the extended text actually begins so preview line
        # numbers stay aligned with real repository lines.
        unit.text_start_line = overlap_start
        unit.text = "\n".join(lines[overlap_start - 1 : overlap_end])
        return unit

    def _split_span(
        self,
        path: str,
        lines: list[str],
        start: int,
        end: int,
        language: str,
        budgets: EvidenceBudget,
    ) -> tuple[list[CodeUnit], list[SkippedUnit]]:
        """Recursively split an oversized span at nested semantic boundaries."""
        parser = _GRAMMARS.get_parser(language)
        if parser is None:
            return [], [SkippedUnit(path, "missing_grammar", start, end)]
        text = "\n".join(lines[start - 1 : end])
        try:
            tree = parser.parse(text.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("preprocessor.split_parse_failed path={} reason={}", path, exc)
            return [], [SkippedUnit(path, "parse_error", start, end, detail=str(exc)[:200])]
        nested_types = _DEFINITION_TYPES.get(language, frozenset()) | _STATEMENT_TYPES.get(
            language, frozenset()
        )
        children = [node for node in tree.root_node.children if node.type in nested_types]
        # A single definition covering the whole span cannot shrink it (e.g. one
        # huge function); descend into its body so its statements become the
        # split boundaries instead of recursing on the identical span forever.
        root_end = tree.root_node.end_point[0]
        while (
            len(children) == 1
            and children[0].start_point[0] == 0
            and children[0].end_point[0] >= root_end
        ):
            single = children[0]
            body = [
                node
                for node in single.children
                if node.type in _BODY_WRAPPER_TYPES
            ]
            children = body[0].children if body else list(single.children)
        if not children:
            # Cannot split safely at a semantic boundary.
            return [], [
                SkippedUnit(path, "token_limit_exceeded", start, end, detail="unsplittable oversized unit")
            ]
        units: list[CodeUnit] = []
        skipped: list[SkippedUnit] = []
        cursor = start

        def span_unit(span_start: int, span_end: int, kind: str = "module", name: str = "") -> CodeUnit:
            chunk_text = "\n".join(lines[span_start - 1 : span_end])
            return CodeUnit(
                path=path,
                kind=kind,
                name=name,
                start_line=span_start,
                end_line=span_end,
                text=chunk_text,
                token_count=estimate_tokens(chunk_text),
            )

        for node in children:
            child_start = start + node.start_point[0]
            child_end = min(start + node.end_point[0], end)
            if child_start > cursor:
                units.append(span_unit(cursor, child_start - 1))
            if child_start > child_end:
                continue
            child_text = "\n".join(lines[child_start - 1 : child_end])
            child_tokens = estimate_tokens(child_text)
            if child_tokens > budgets.chunk_cap_tokens:
                pieces, piece_skipped = self._split_span(
                    path, lines, child_start, child_end, language, budgets
                )
                units.extend(pieces)
                skipped.extend(piece_skipped)
            else:
                units.append(
                    CodeUnit(
                        path=path,
                        kind=self._node_kind(node),
                        name=self._node_name(node),
                        start_line=child_start,
                        end_line=child_end,
                        text=child_text,
                        token_count=child_tokens,
                    )
                )
            cursor = child_end + 1
        if cursor <= end:
            units.append(span_unit(cursor, end))
        return self._merge_small_units(units, budgets), skipped

    def _merge_small_units(self, units: list[CodeUnit], budgets: EvidenceBudget) -> list[CodeUnit]:
        """Merge adjacent same-file units below the merge floor."""
        merge_floor = max(80, budgets.chunk_cap_tokens // 8)
        merged: list[CodeUnit] = []
        buffer: list[CodeUnit] = []
        buffer_tokens = 0

        def flush() -> None:
            nonlocal buffer, buffer_tokens
            if not buffer:
                return
            if len(buffer) == 1:
                merged.append(buffer[0])
            else:
                first, last = buffer[0], buffer[-1]
                merged.append(
                    CodeUnit(
                        path=first.path,
                        kind=first.kind,
                        name=first.name,
                        start_line=first.start_line,
                        end_line=last.end_line,
                        text="\n".join(unit.text for unit in buffer),
                        token_count=buffer_tokens,
                        tags=first.tags,
                    )
                )
            buffer = []
            buffer_tokens = 0

        for unit in units:
            if unit.kind == "syntax_diagnostic":
                flush()
                merged.append(unit)
                continue
            buffer.append(unit)
            buffer_tokens += unit.token_count
            if buffer_tokens >= merge_floor:
                flush()
        flush()
        return merged

    @staticmethod
    def _walk_definitions(node: Any, types: frozenset[str]) -> list[Any]:
        """Depth-first definitions; do not descend into matched nodes."""
        results: list[Any] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in types:
                results.append(current)
            else:
                stack.extend(reversed(current.children))
        return results

    @staticmethod
    def _error_ranges(root: Any) -> list[tuple[int, int]]:
        """Collect top-most ERROR/missing node line ranges."""
        ranges: list[tuple[int, int]] = []
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "ERROR" or getattr(node, "is_missing", False):
                ranges.append((node.start_point[0] + 1, node.end_point[0] + 1))
                continue
            stack.extend(node.children)
        return sorted(ranges)

    @staticmethod
    def _node_kind(node: Any) -> str:
        node_type = node.type
        if any(marker in node_type for marker in ("class", "struct", "impl", "enum", "interface")):
            return "class"
        if any(marker in node_type for marker in ("function", "method", "arrow")):
            return "function"
        return "module"

    @staticmethod
    def _node_name(node: Any) -> str:
        for child in node.children:
            if child.type in _NAME_NODE_TYPES:
                text = child.text
                return text.decode("utf-8") if isinstance(text, bytes) else str(text)
        return ""

    # -- diff units -------------------------------------------------------

    @staticmethod
    def _patch_units(
        path: str,
        patch: str,
        budgets: EvidenceBudget,
        skipped: list[SkippedUnit],
    ) -> list[CodeUnit]:
        tokens = estimate_tokens(patch)
        if tokens <= budgets.chunk_cap_tokens:
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
        # Split oversized patches by hunks.
        pieces = re.split(r"(?=^@@ )", patch, flags=re.MULTILINE)
        units: list[CodeUnit] = []
        for piece in pieces:
            if not piece.strip():
                continue
            piece_tokens = estimate_tokens(piece)
            match = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))?", piece)
            start = int(match.group(1)) if match else 1
            count = int(match.group(2) or 1) if match else 1
            end = start + max(count - 1, 0)
            if piece_tokens > budgets.chunk_cap_tokens:
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

    # -- scoring and budget filtering ------------------------------------

    @staticmethod
    def _query_terms(query: str) -> set[str]:
        return {term.lower() for term in _TERM_RE.findall(query or "")}

    def _score_unit(self, unit: CodeUnit, terms: set[str], touched: list[int]) -> None:
        """Score one unit and split routing signals by responsibility.

        ``matched`` keeps only user review-query hit words; program-generated
        risk clues go to ``risk_hints`` and never leak into ``tags`` or
        ``matched``. Diff units detect hints on added/removed lines (plus the
        path signal); other units scan their full text.
        """
        lower = f"{unit.path}\n{unit.text}".lower()
        matched = sorted(term for term in terms if term in lower)
        risk_hints = self._unit_risk_hints(unit)
        score = float(len(matched) * 3 + len(risk_hints))
        if touched and unit.kind != "syntax_diagnostic":
            if set(range(unit.start_line, unit.end_line + 1)) & set(touched):
                score += 10.0
        if unit.kind == "syntax_diagnostic":
            score += 5.0
        if unit.kind in {"file", "module"}:
            score += 1.0
        unit.matched = matched
        unit.risk_hints = risk_hints
        unit.score = score

    @staticmethod
    def _unit_risk_hints(unit: CodeUnit) -> tuple[str, ...]:
        """Detect risk routing hints for one unit (diff: changed lines + path)."""
        if unit.kind == "diff":
            changed = "\n".join(
                line[1:] for line in unit.text.splitlines() if line[:1] in {"+", "-"}
            )
            haystack = f"{unit.path}\n{changed}".lower()
        else:
            haystack = f"{unit.path}\n{unit.text}".lower()
        return detect_risk_hints(haystack)

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
        """Keep high-priority main units within the evidence budget."""
        ordered = sorted(
            (unit for unit in units if unit.role == "main"),
            key=lambda unit: (-unit.score, unit.path, unit.start_line),
        )
        accepted: list[CodeUnit] = []
        skipped: list[SkippedUnit] = []
        used = 0
        for unit in ordered:
            if used + unit.token_count > budgets.evidence_budget_tokens and accepted:
                skipped.append(
                    SkippedUnit(unit.path, "budget_exhausted", unit.start_line, unit.end_line, detail="task budget")
                )
                continue
            accepted.append(unit)
            used += unit.token_count
        return accepted, skipped

    def _supplement_related(
        self,
        mains: list[CodeUnit],
        *,
        files: dict[str, str],
        inventory: list[InventoryEntry],
        budgets: EvidenceBudget,
        include_tests: bool,
        related_tests: bool,
    ) -> list[CodeUnit]:
        """Attach at most one layer of related context to accepted mains.

        Related chunks are supplementary context, not review scope: candidates
        that do not fit the related budget are dropped silently. Only supported
        code, documents and configs may serve as related context; unsupported
        code types stay excluded even when they reference a main symbol.
        """
        main_paths = {unit.path for unit in mains}
        allowed_paths = {
            entry.path
            for entry in inventory
            if entry.file_class in {"supported_code", "document", "config"}
        }
        related: list[CodeUnit] = []
        used = 0
        related_paths: set[str] = set()

        def add_related(path: str, relation: str, parent: CodeUnit | None, symbol: str = "") -> None:
            nonlocal used
            if path not in allowed_paths:
                return
            if path in main_paths or path in related_paths:
                return
            text = files.get(path)
            if not text:
                return
            token_count = estimate_tokens(text)
            if token_count > budgets.chunk_cap_tokens:
                return
            if used + token_count > budgets.related_budget_tokens:
                return
            lines = text.replace("\r\n", "\n").splitlines()
            tags = ("related", relation) if not symbol else ("related", relation, f"symbol:{symbol}")
            related.append(
                CodeUnit(
                    path=path,
                    kind="file",
                    name=Path(path).name,
                    start_line=1,
                    end_line=len(lines) or 1,
                    text=text,
                    token_count=token_count,
                    role="related",
                    parent_id=parent.unit_id if parent else None,
                    tags=tags,
                )
            )
            related_paths.add(path)
            used += token_count

        # Import/export targets: resolve module specifiers to inventoried paths.
        # Resolution stays inside the allowed-class set so imports pointing at
        # unsupported files never surface as related evidence.
        lookup = allowed_paths
        for unit in mains:
            for target in self._import_targets(unit.text):
                resolved = self._resolve_import(target, lookup)
                if resolved:
                    add_related(resolved, "import", unit)
        # Callers: files referencing a main unit's symbol outside its own file.
        symbols = sorted({unit.name for unit in mains if unit.kind in {"class", "function"} and unit.name})
        for symbol in symbols[:20]:
            for path in files:
                if path in main_paths or path in related_paths:
                    continue
                if symbol in files[path]:
                    add_related(path, "caller", None, symbol)
        # Config/document files colocated with accepted mains.
        for entry in inventory:
            if entry.file_class not in {"config", "document"}:
                continue
            if entry.path in main_paths or entry.path in related_paths:
                continue
            parent_dir = None
            for unit in mains:
                candidate = Path(unit.path).parent.as_posix()
                if candidate and (parent_dir is None or len(candidate) < len(parent_dir)):
                    parent_dir = candidate
            if parent_dir and entry.path.startswith(parent_dir):
                add_related(entry.path, "config", None)
        # Tests for accepted main paths.
        if include_tests and related_tests:
            for path in sorted(main_paths):
                for test_path in self._test_candidates(path):
                    add_related(test_path, "test", None)
        return related

    @staticmethod
    def _import_targets(text: str) -> list[str]:
        targets: list[str] = []
        for match in _PY_IMPORT_RE.finditer(text):
            targets.append(match.group(1))
        for match in _JS_IMPORT_RE.finditer(text):
            target = match.group(1)
            if not target.startswith("."):
                targets.append(target)
        return list(dict.fromkeys(targets))[:20]

    @staticmethod
    def _resolve_import(target: str, lookup: set[str]) -> str | None:
        normalized = target.replace(".", "/")
        candidates = (
            normalized,
            f"{normalized}.py",
            f"{normalized}.js",
            f"{normalized}.ts",
            f"{normalized}.tsx",
            f"{normalized}/__init__.py",
            f"{normalized}/index.ts",
            f"{normalized}/index.js",
        )
        for candidate in candidates:
            if candidate in lookup:
                return candidate
        return None

    @staticmethod
    def _test_candidates(rel_path: str) -> list[str]:
        path = Path(rel_path)
        if not path.suffix:
            return []
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        candidates = [
            parent / f"test_{stem}{suffix}",
            parent / f"{stem}_test{suffix}",
            Path("tests") / parent / f"test_{stem}{suffix}",
            Path("tests") / parent / f"{stem}_test{suffix}",
            Path("tests") / f"test_{stem}{suffix}",
            Path("tests") / f"{stem}_test{suffix}",
        ]
        if stem.startswith("test_"):
            base = stem[5:]
            candidates.extend([parent / f"{base}{suffix}", Path("tests") / f"{base}{suffix}"])
        return list(dict.fromkeys(candidate.as_posix() for candidate in candidates))

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
                    # matched = user review-query hit words only.
                    f"- matched: {', '.join(unit.matched) or 'none'}",
                    # risk_hints = program-generated candidate routing clues.
                    f"- risk_hints: {', '.join(unit.risk_hints) or 'none'}",
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

    # ------------------------------------------------------------------
    # Snapshot persistence for remote sources
    # ------------------------------------------------------------------

    def write_snapshot(self, snapshot_name: str, files: dict[str, str]) -> Path:
        """Persist accepted remote snapshot files under .nanoreview/review_github."""
        scope_digest = _snapshot_scope_digest(snapshot_name, files)
        cache_root = (
            self.workspace / ".nanoreview" / "review_github" / f"{_safe_slug(snapshot_name)}_{scope_digest}"
        )
        cache_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "snapshot": snapshot_name,
            "scope_digest": scope_digest,
            "files_count": len(files),
            "created_at": _now_iso(),
            "files": sorted(files),
        }
        for rel, text in files.items():
            target = (cache_root / rel).resolve()
            try:
                target.relative_to(cache_root)
            except ValueError:
                logger.warning("preprocessor.snapshot.skip unsafe_path={}", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
        (cache_root / ".nanoreview_snapshot.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        logger.info(
            "preprocessor.snapshot.done snapshot={} cache={} files={}",
            snapshot_name,
            cache_root,
            len(files),
        )
        return cache_root
