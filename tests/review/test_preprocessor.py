"""Tests for the programmatic evidence preprocessor pipeline."""
from __future__ import annotations

from pathlib import Path

import pytest

from nanoreview.review.planning import preprocessor as preprocessor_module
from nanoreview.review.planning.preprocessor import (
    EvidenceBudget,
    ProgrammaticEvidenceRequest,
    ProgrammaticEvidenceService,
    build_unit_preview,
    detect_risk_hints,
    estimate_tokens,
)

# context_window_tokens=16_384 yields: usable=5746, evidence_budget=5746,
# direct_cap=1600, chunk_cap=2873, task_cap=5746, related_budget=1436.
SMALL_WINDOW = 16_384


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _request(query: str = "login token", **kwargs: object) -> ProgrammaticEvidenceRequest:
    return ProgrammaticEvidenceRequest("local", query, context_window_tokens=SMALL_WINDOW, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_small_project_uses_direct_mode_with_whole_file_units(tmp_path: Path) -> None:
    text = "def login(token):\n    return token\n"
    _write(tmp_path / "src" / "auth.py", text)

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.mode == "direct"
    assert [unit.path for unit in result.units] == ["src/auth.py"]
    unit = result.units[0]
    assert unit.kind == "file"
    assert unit.token_count == estimate_tokens(text)
    assert result.skipped == []
    assert "src/auth.py" in result.context


@pytest.mark.asyncio
async def test_unsupported_code_type_is_filtered_at_file_level(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "auth.py", "def login(token):\n    return token\n")
    _write(tmp_path / "src" / "Legacy.java", "class Legacy { void login() {} }\n")

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert [unit.path for unit in result.units] == ["src/auth.py"]
    assert [(skip.path, skip.reason) for skip in result.skipped] == [
        ("src/Legacy.java", "unsupported_code_type")
    ]


@pytest.mark.asyncio
async def test_missing_grammar_is_filtered_at_file_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A python file large enough to require chunking under the small window.
    _write(
        tmp_path / "big.py",
        "def login(token):\n    return token\n\n" * 220,
    )
    monkeypatch.setattr(preprocessor_module._GRAMMARS, "_unavailable", {"python"})

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.units == []
    assert [(skip.path, skip.reason) for skip in result.skipped] == [
        ("big.py", "missing_grammar")
    ]


@pytest.mark.asyncio
async def test_chunked_mode_splits_python_at_semantic_boundaries(tmp_path: Path) -> None:
    # ~8k chars -> ~2k tokens: above the direct cap, below the evidence budget.
    functions = "\n".join(
        f"def handler_{index}(token):\n    value = compute(token)\n    return value\n"
        for index in range(120)
    )
    _write(tmp_path / "service.py", functions)

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.mode == "chunked"
    assert result.units
    assert all(unit.path == "service.py" for unit in result.units)
    assert all(unit.token_count <= 2873 for unit in result.units)
    assert {unit.kind for unit in result.units} <= {"module", "function", "class"}
    # Adjacent tiny functions were merged into fewer, larger chunks.
    assert len(result.units) < 120
    assert any(unit.token_count >= 80 for unit in result.units)


@pytest.mark.asyncio
async def test_oversized_function_is_recursively_split(tmp_path: Path) -> None:
    # One function whose body far exceeds the chunk cap.
    body = "\n".join(f"    value_{index} = compute_{index}(token)" for index in range(600))
    _write(tmp_path / "huge.py", f"def heavy(token):\n{body}\n")

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.mode == "chunked"
    pieces = [unit for unit in result.units if unit.kind in {"function", "module"}]
    assert len(pieces) > 1
    assert all(unit.token_count <= 2873 for unit in result.units)
    # The split pieces cover the original function line span.
    assert min(unit.start_line for unit in pieces) == 1
    assert max(unit.end_line for unit in pieces) == 601


@pytest.mark.asyncio
async def test_local_syntax_error_keeps_valid_nodes_and_adds_diagnostic(tmp_path: Path) -> None:
    helpers = "\n".join(
        f"def helper_{index}(token):\n    return token\n" for index in range(200)
    )
    source = (
        "def login(token):\n"
        "    return token\n"
        "\n"
        "def broken(:\n"
        "    pass\n"
        "\n"
        "class Auth:\n"
        "    def check(self, token):\n"
        "        return token\n"
        f"{helpers}\n"
    )
    _write(tmp_path / "auth.py", source)

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    diagnostics = [unit for unit in result.units if unit.kind == "syntax_diagnostic"]
    assert diagnostics, "expected a standalone syntax_diagnostic unit"
    assert "syntax error region" in diagnostics[0].text
    names = {unit.name for unit in result.units if unit.kind == "function"}
    assert "login" in names
    assert any("helper_0(" in unit.text for unit in result.units)


@pytest.mark.asyncio
async def test_whole_file_parse_error_is_skipped_without_chunks(tmp_path: Path) -> None:
    _write(tmp_path / "broken.py", "def ((( ???\n!!! ||| }}\n" * 400)

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.units == []
    assert [skip.reason for skip in result.skipped] == ["parse_error"]
    assert "broken.py" not in result.context


@pytest.mark.asyncio
async def test_typescript_jsx_and_tsx_are_parsed(tmp_path: Path) -> None:
    # Each file ~1500 tokens so the pair forces chunked mode under the window.
    ts_body = "\n".join(
        f"export function handler{index}(token: string): string {{\n  return process(token);\n}}"
        for index in range(80)
    )
    jsx_body = "\n".join(
        f"export function Panel{index}() {{\n  return <div>item {index}</div>;\n}}"
        for index in range(80)
    )
    tsx_body = "\n".join(
        f"export function Card{index}(props: {{ id: number }}) {{\n  return <span>{{props.id}}</span>;\n}}"
        for index in range(80)
    )
    js_body = "\n".join(
        f"export function route{index}(req) {{\n  return handle(req);\n}}"
        for index in range(80)
    )
    _write(tmp_path / "service.ts", ts_body)
    _write(tmp_path / "panel.jsx", jsx_body)
    _write(tmp_path / "card.tsx", tsx_body)
    _write(tmp_path / "router.js", js_body)

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.mode == "chunked"
    paths = {unit.path for unit in result.units}
    assert {"service.ts", "panel.jsx", "card.tsx", "router.js"} <= paths
    assert all(unit.token_count <= 2873 for unit in result.units)


@pytest.mark.asyncio
async def test_small_supported_caller_is_main_and_unsupported_is_excluded(tmp_path: Path) -> None:
    main_body = "\n".join(
        f"def login_{index}(token):\n    return verify(token)\n" for index in range(200)
    )
    _write(tmp_path / "src" / "app.py", main_body)
    _write(tmp_path / "caller.py", "from src.app import login_0\n\nlogin_0(token)\n")
    # Unsupported code that references a main symbol must stay excluded from
    # both the review scope and the related context.
    _write(tmp_path / "legacy.java", "class Legacy { void login_0(String t) {} }\n")

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    # caller.py is small enough to be kept whole as a main module unit.
    assert any(unit.path == "caller.py" and unit.role == "main" for unit in result.units)
    # legacy.java is neither a main nor a related unit.
    assert all(unit.path != "legacy.java" for unit in result.units)
    assert [(skip.path, skip.reason) for skip in result.skipped] == [
        ("legacy.java", "unsupported_code_type")
    ]


def test_supplement_related_attaches_supported_callers_only() -> None:
    from nanoreview.review.planning.preprocessor import (
        CodeUnit,
        EvidenceBudget,
        InventoryEntry,
        ProgrammaticEvidenceService,
        classify_path,
    )

    service = ProgrammaticEvidenceService(Path("."))
    files = {
        "src/app.py": "def login(token):\n    return verify(token)\n",
        "caller.py": "from src.app import login\n\nlogin(token)\n",
        "legacy.java": "class Legacy { void login(String t) {} }\n",
    }
    inventory = [
        InventoryEntry(path, len(text), *classify_path(path)) for path, text in files.items()
    ]
    budgets = EvidenceBudget.from_options(
        token_budget=100_000,
        subagent_evidence_budget_chars=24_000,
        context_window_tokens=SMALL_WINDOW,
    )
    mains = [
        CodeUnit(
            path="src/app.py",
            kind="function",
            name="login",
            start_line=1,
            end_line=2,
            text=files["src/app.py"],
            token_count=10,
            unit_id="ev-001",
        )
    ]

    related = service._supplement_related(
        mains, files=files, inventory=inventory, budgets=budgets, include_tests=True, related_tests=True
    )

    # The supported caller file is attached; the unsupported Java file that
    # references the same symbol is rejected by the allowed-class gate.
    assert [unit.path for unit in related] == ["caller.py"]
    assert related[0].tags[:2] == ("related", "caller")
    assert related[0].kind == "file"


@pytest.mark.asyncio
async def test_unsupported_files_never_enter_related_context(tmp_path: Path) -> None:
    # Main chunk imports a module whose only on-disk match is an unsupported
    # code file; it must not surface as related import evidence.
    main_body = "\n".join(
        f"def login_{index}(token):\n    return verify(token)\n" for index in range(200)
    )
    _write(tmp_path / "src" / "app.py", main_body + "\nimport legacy\n")
    _write(tmp_path / "legacy.java", "class Legacy { void login_0(String t) {} }\n")
    _write(tmp_path / "legacy.go", "package legacy\n")

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert all("legacy" not in unit.path for unit in result.units)
    assert {skip.reason for skip in result.skipped} == {"unsupported_code_type"}


@pytest.mark.asyncio
async def test_semantic_chunks_carry_bounded_context_overlap(tmp_path: Path) -> None:
    # Two adjacent functions force chunked mode; the merged chunk in the middle
    # must carry surrounding file lines as overlap while keeping its canonical
    # line range unchanged.
    functions = "\n".join(
        f"def handler_{index}(token):\n    value = compute(token)\n    return value\n"
        for index in range(120)
    )
    source = "MODULE_HEADER = 1\n\n" + functions + "\n"
    _write(tmp_path / "service.py", source)

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.mode == "chunked"
    file_lines = source.splitlines()
    for unit in result.units:
        if unit.kind == "syntax_diagnostic":
            continue
        # Canonical range stays within the file and matches authorization.
        assert 1 <= unit.start_line <= unit.end_line <= len(file_lines)
        # Overlap adds up to OVERLAP_CONTEXT_LINES lines of surrounding file
        # text on each side of the canonical range.
        expected_start = max(1, unit.start_line - preprocessor_module.OVERLAP_CONTEXT_LINES)
        expected_end = min(
            len(file_lines), unit.end_line + preprocessor_module.OVERLAP_CONTEXT_LINES
        )
        assert unit.text == "\n".join(file_lines[expected_start - 1 : expected_end])
    middle = [unit for unit in result.units if unit.kind != "syntax_diagnostic"][0]
    assert middle.start_line > 1 or middle.end_line < len(file_lines)


def test_unsupported_diff_path_produces_no_units(tmp_path: Path) -> None:
    service = ProgrammaticEvidenceService(tmp_path)
    patch = "@@ -1 +1 @@\n-class Old\n+class New"

    result = service.diff_units({"src/Legacy.java": patch}, review_query="login")

    assert result.units == []
    assert [(skip.path, skip.reason) for skip in result.skipped] == [
        ("src/Legacy.java", "unsupported_code_type")
    ]


def test_supported_and_unsupported_diff_files_are_isolated(tmp_path: Path) -> None:
    service = ProgrammaticEvidenceService(tmp_path)
    supported_patch = "@@ -1 +1 @@\n-old\n+def login(): pass"
    unsupported_patch = "@@ -1 +1 @@\n-old\n+class New"

    result = service.diff_units(
        {
            "src/auth.py": supported_patch,
            "src/Legacy.java": unsupported_patch,
        },
        review_query="login",
    )

    assert [unit.path for unit in result.units] == ["src/auth.py"]
    assert [(skip.path, skip.reason) for skip in result.skipped] == [
        ("src/Legacy.java", "unsupported_code_type")
    ]


@pytest.mark.asyncio
async def test_total_budget_exhaustion_keeps_high_priority_units(tmp_path: Path) -> None:
    # Two files of ~3300 tokens each: total ~6600 exceeds the 5746 budget, so
    # the lowest priority chunks are recorded as budget_exhausted.
    first = "\n".join(
        f"def login_{index}(token):\n    return verify(token)\n" for index in range(300)
    )
    second = "\n".join(
        f"def other_{index}(value):\n    return compute(value)\n" for index in range(300)
    )
    _write(tmp_path / "auth.py", first)
    _write(tmp_path / "misc.py", second)

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.mode == "oversized"
    assert result.units
    assert result.accepted_tokens <= 5746
    reasons = {skip.reason for skip in result.skipped}
    assert "budget_exhausted" in reasons
    # High-priority login chunks survive budget filtering.
    assert any("login" in unit.matched for unit in result.units)


@pytest.mark.asyncio
async def test_all_units_skipped_when_nothing_is_reviewable(tmp_path: Path) -> None:
    _write(tmp_path / "Legacy.java", "class Legacy {}\n")
    _write(tmp_path / "legacy.go", "package main\n")

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.units == []
    assert {skip.reason for skip in result.skipped} == {"unsupported_code_type"}


def test_small_diff_uses_direct_mode(tmp_path: Path) -> None:
    service = ProgrammaticEvidenceService(tmp_path)
    patch = "@@ -1 +1 @@\n-old\n+def login(): pass"

    result = service.diff_units({"src/auth.py": patch}, review_query="login")

    assert result.mode == "direct"
    assert [unit.kind for unit in result.units] == ["diff"]
    assert result.units[0].token_count == estimate_tokens(patch)
    assert result.units[0].start_line == 1
    assert result.skipped == []


def test_oversized_hunk_without_safe_split_is_skipped(tmp_path: Path) -> None:
    service = ProgrammaticEvidenceService(tmp_path)

    result = service.diff_units(
        {"src/large.py": "+" + "x" * 12_000},
        review_query="login",
        context_window_tokens=SMALL_WINDOW,
    )

    assert result.units == []
    assert [(skip.path, skip.reason) for skip in result.skipped] == [
        ("src/large.py", "token_limit_exceeded")
    ]


def test_estimate_tokens_and_budget_derivation() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2

    budget = EvidenceBudget.from_options(
        token_budget=100_000,
        subagent_evidence_budget_chars=24_000,
        context_window_tokens=SMALL_WINDOW,
    )
    # window 16384 - overhead 9000 - margin 1638 = 5746 usable.
    assert budget.usable_input_tokens == 5_746
    assert budget.evidence_budget_tokens == 5_746
    assert budget.direct_cap_tokens == 1_600
    assert budget.chunk_cap_tokens == 2_873
    assert budget.task_cap_tokens == 5_746
    assert budget.related_budget_tokens == 1_436


def test_options_from_review_config_maps_budget_fields() -> None:
    from nanoreview.review.planning.preprocessor import ProgrammaticEvidenceOptions

    review_config = type(
        "ReviewConfig",
        (),
        {
            "token_budget": 50_000,
            "prefetch_budget_chars": 8_000,
            "subagent_evidence_budget_chars": 12_000,
            "prefetch_dense_backfill_limit": 64,
            "preview_target_chars": 400,
            "preview_hard_limit": 2_000,
        },
    )()

    options = ProgrammaticEvidenceOptions.from_review_config(review_config)

    assert options.token_budget == 50_000
    assert options.prefetch_budget_chars == 8_000
    assert options.subagent_evidence_budget_chars == 12_000
    assert options.prefetch_dense_backfill_limit == 64
    assert options.preview_target_chars == 400
    assert options.preview_hard_limit == 2_000
    # The context window stays a per-request dynamic parameter.
    assert options.context_window_tokens == ProgrammaticEvidenceOptions().context_window_tokens

    defaults = ProgrammaticEvidenceOptions.from_review_config(None)
    assert defaults.token_budget == 100_000
    assert defaults.subagent_evidence_budget_chars == 24_000
    assert defaults.preview_target_chars == 600
    assert defaults.preview_hard_limit == 3_000


def test_review_config_rejects_inverted_preview_limits() -> None:
    from pydantic import ValidationError

    from nanoreview.config.schema import ReviewConfig

    with pytest.raises(ValidationError, match="preview_hard_limit"):
        ReviewConfig(preview_target_chars=600, preview_hard_limit=300)
    # Equal limits and well-ordered limits stay valid.
    assert ReviewConfig(preview_target_chars=600, preview_hard_limit=600).preview_hard_limit == 600
    assert ReviewConfig().preview_hard_limit == 3_000


def test_programmatic_options_reject_inverted_preview_limits() -> None:
    from nanoreview.review.planning.preprocessor import ProgrammaticEvidenceOptions

    with pytest.raises(ValueError, match="preview_hard_limit"):
        ProgrammaticEvidenceOptions(preview_target_chars=600, preview_hard_limit=100)


# ---------------------------------------------------------------------------
# Planner preview construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_chunk_preview_keeps_full_text(tmp_path: Path) -> None:
    text = "def login(token):\n    return token\n"
    _write(tmp_path / "auth.py", text)

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    unit = result.units[0]
    assert unit.preview == text
    assert unit.preview_coverage == "full chunk lines 1-2"
    # The path "auth.py" and the code both contribute risk hints to the manifest.
    assert "- risk_hints: security:auth, security:token" in result.context
    assert "- preview_coverage: full chunk lines 1-2" in result.context


def test_mid_size_chunk_sampling_keeps_risk_line_without_premature_truncation() -> None:
    # 600 < len(text) <= 3000: sampling may exceed the 600 target.
    text = "\n".join(
        [
            "def handler(payload):",
            *[f"    value_{index} = compute_{index}(payload)" for index in range(30)],
            "    api_token = load_secret()",
            *[f"    tail_{index} = finalize_{index}(payload)" for index in range(30)],
            "    return payload",
        ]
    )
    assert 600 < len(text) <= 3_000

    preview, coverage = build_unit_preview(
        text, kind="function", start_line=10, query_terms=set(), target_chars=600, hard_limit=3_000
    )

    # The mid-chunk risk line and its local window survive sampling.
    assert "api_token = load_secret()" in preview
    assert "... omitted lines" in preview
    assert len(preview) <= 3_000
    assert "(truncated)" not in preview  # no premature hard truncation at 600
    assert coverage.startswith("chunk lines 10-")
    assert "preview covers lines" in coverage


def test_oversized_chunk_is_trimmed_by_priority_with_omission_ranges() -> None:
    text = "\n".join(
        [
            "def handler(payload):",
            *[f"    value_{index} = compute_{index}(payload)" for index in range(250)],
            "    password = load_secret()",
            *[f"    tail_{index} = finalize_{index}(payload)" for index in range(250)],
            "    return payload",
        ]
    )
    assert len(text) > 3_000

    preview, coverage = build_unit_preview(
        text, kind="function", start_line=1, query_terms=set(), target_chars=600, hard_limit=3_000
    )

    assert len(preview) <= 3_000
    # Priority trim still keeps the signature and the risk line.
    assert preview.startswith("def handler(payload):")
    assert "password = load_secret()" in preview
    assert "... omitted lines" in preview
    assert "chunk lines 1-" in coverage


def test_single_line_preview_respects_hard_limit() -> None:
    # Minified or generated code may have no safe line boundary to drop.
    preview, _coverage = build_unit_preview(
        "x" * 200,
        kind="function",
        start_line=1,
        query_terms=set(),
        target_chars=10,
        hard_limit=32,
    )

    assert len(preview) <= 32
    assert preview.endswith("\n... (preview truncated)")


def test_risk_line_in_chunk_middle_includes_local_context_window() -> None:
    text = "\n".join(
        [
            "def handler(payload):",
            *[f"    filler_{index} = step(index)" for index in range(40)],
            "    csrf_token = rotate()",
            "    before = payload",
            "    after = csrf_token",
            "    after2 = payload",
            *[f"    more_{index} = step(index)" for index in range(40)],
            "    return payload",
        ]
    )

    preview, _coverage = build_unit_preview(
        text, kind="function", start_line=1, query_terms=set(), target_chars=600, hard_limit=3_000
    )

    preview_lines = preview.splitlines()
    hit_index = next(index for index, line in enumerate(preview_lines) if "csrf_token = rotate()" in line)
    # The ±2 window lines around the risk hit are kept alongside it.
    assert "before = payload" in preview_lines[hit_index + 1]
    assert "after = csrf_token" in preview_lines[hit_index + 2]


def test_diff_preview_keeps_hunk_headers_and_changed_lines() -> None:
    patch = "\n".join(
        [
            "@@ -10,7 +10,9 @@ def module():",
            " context_one = 1",
            " context_two = 2",
            "-removed_line = compute()",
            "+def login(token):",
            "+    return verify(token)",
            *[f" context_filler_{index} = {index}" for index in range(40)],
            "+api_key = load_secret()",
            " trailing_context = 0",
        ]
    )

    preview, coverage = build_unit_preview(
        patch, kind="diff", start_line=10, query_terms=set(), target_chars=600, hard_limit=3_000
    )

    assert "@@ -10,7 +10,9 @@ def module():" in preview
    assert "-removed_line = compute()" in preview
    assert "+def login(token):" in preview
    assert "+api_key = load_secret()" in preview
    assert "... omitted lines" in preview
    assert "hunks span new-file lines 10-18" in coverage
    assert "preview covers new-file lines" in coverage


def test_repo_preview_never_fabricates_diff_markers() -> None:
    text = "\n".join(
        [
            "def handler(payload):",
            *[f"    filler_{index} = step(index)" for index in range(80)],
            "    csrf_token = rotate()",
            *[f"    more_{index} = step(index)" for index in range(80)],
            "    return payload",
        ]
    )

    preview, _coverage = build_unit_preview(
        text, kind="function", start_line=1, query_terms=set(), target_chars=600, hard_limit=3_000
    )

    code_lines = [line for line in preview.splitlines() if not line.startswith("...")]
    assert code_lines
    assert not any(line.startswith(("+", "-")) for line in code_lines)


@pytest.mark.asyncio
async def test_preview_coverage_lines_align_with_semantic_chunk(tmp_path: Path) -> None:
    functions = "\n".join(
        f"def handler_{index}(token):\n    value = compute(token)\n    return value\n"
        for index in range(120)
    )
    source = "MODULE_HEADER = 1\n\n" + functions + "\n"
    _write(tmp_path / "service.py", source)

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request())

    assert result.mode == "chunked"
    file_lines = source.splitlines()
    for unit in result.units:
        if unit.kind == "syntax_diagnostic":
            continue
        origin = unit.text_start_line if unit.text_start_line is not None else unit.start_line
        text_lines = unit.text.splitlines()
        # Coverage references exactly the lines the (possibly overlap-extended)
        # text spans in the real file.
        assert unit.preview_coverage.startswith(f"chunk lines {origin}-{origin + len(text_lines) - 1}")
        assert origin >= 1 and origin + len(text_lines) - 1 <= len(file_lines)


def test_diff_preview_coverage_aligns_with_hunk_new_file_lines() -> None:
    patch = "\n".join(
        [
            "@@ -5,3 +5,4 @@",
            " ctx = 1",
            "-old = 2",
            "+new = 2",
            "+added = 3",
        ]
    )

    _preview, coverage = build_unit_preview(
        patch, kind="diff", start_line=5, query_terms=set(), target_chars=600, hard_limit=3_000
    )

    # Small patch keeps full text; hunks address new-file lines 5-8.
    assert coverage == "full patch; new-file lines 5-8"


# ---------------------------------------------------------------------------
# Risk hints
# ---------------------------------------------------------------------------


def test_risk_hints_use_word_boundaries() -> None:
    # auth must not match author / authenticate.
    assert "security:auth" not in detect_risk_hints("def author(credential): pass")
    assert "security:auth" not in detect_risk_hints("authenticated_user = get_user()")
    assert "security:auth" in detect_risk_hints("def auth(user): pass")
    # sql/path style false positives stay silent (not hint terms at all).
    assert detect_risk_hints("mysql_query(xpath_expr)") == ()
    # Real hits are detected, including snake_case identifiers.
    hints = detect_risk_hints("password = get_password()\napi_router = Router()\n")
    assert "security:password" in hints
    assert "entrypoint:router" in hints


@pytest.mark.asyncio
async def test_matched_keeps_only_query_hit_words(tmp_path: Path) -> None:
    _write(tmp_path / "auth.py", "def login(token):\n    password = load_secret()\n    return token\n")

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request(query="login"))

    unit = result.units[0]
    assert unit.matched == ["login"]
    assert "security:password" in unit.risk_hints
    assert not any(tag.startswith("risk:") for tag in unit.tags)
    assert unit.tags == ()  # whole-file direct unit carries no relation tags


def test_risk_hints_stay_independent_from_relation_tags(tmp_path: Path) -> None:
    from nanoreview.review.planning.preprocessor import (
        CodeUnit,
        EvidenceBudget,
        InventoryEntry,
        classify_path,
    )

    service = ProgrammaticEvidenceService(Path("."))
    files = {
        "src/app.py": "def login(token):\n    return verify(token)\n",
        "caller.py": "from src.app import login\n\nlogin(token)\n",
    }
    inventory = [InventoryEntry(path, len(text), *classify_path(path)) for path, text in files.items()]
    budgets = EvidenceBudget.from_options(
        token_budget=100_000,
        subagent_evidence_budget_chars=24_000,
        context_window_tokens=SMALL_WINDOW,
    )
    mains = [
        CodeUnit(
            path="src/app.py",
            kind="function",
            name="login",
            start_line=1,
            end_line=2,
            text=files["src/app.py"],
            token_count=10,
            unit_id="ev-001",
        )
    ]
    service._score_unit(mains[0], {"login"}, [])

    related = service._supplement_related(
        mains, files=files, inventory=inventory, budgets=budgets, include_tests=True, related_tests=True
    )

    # Main unit: risk clues in risk_hints, tags untouched. The path signal
    # "src/app.py" also yields the entrypoint:handler hint by design.
    assert mains[0].risk_hints == ("security:token", "entrypoint:handler")
    assert mains[0].tags == ()
    # Related unit: relation tags only, no risk labels.
    assert related[0].tags[:2] == ("related", "caller")
    assert not any(tag.startswith("risk:") for tag in related[0].tags)
    assert related[0].risk_hints == ()


@pytest.mark.asyncio
async def test_chunks_without_risk_hints_still_reach_planner(tmp_path: Path) -> None:
    # Plain code with no risk terms still produces units and manifest entries.
    _write(tmp_path / "plain.py", "def compute(value):\n    return value * 2\n")

    result = await ProgrammaticEvidenceService(tmp_path).retrieve(_request(query="compute"))

    assert result.units
    unit = result.units[0]
    assert unit.risk_hints == ()
    assert unit.matched == ["compute"]
    assert "plain.py" in result.context
    assert "- risk_hints: none" in result.context


def test_manifest_keeps_single_budget_truncation() -> None:
    from nanoreview.review.planning.preprocessor import CodeUnit

    service = ProgrammaticEvidenceService(Path("."))
    units = [
        CodeUnit(
            path=f"src/file_{index}.py",
            kind="function",
            start_line=1,
            end_line=2,
            text="x",
            unit_id=f"ev-{index:03d}",
            preview="def f():\n    pass\n" * 50,
            preview_coverage="full chunk lines 1-2",
        )
        for index in range(1, 30)
    ]

    context = service.render_manifest(units, [], budget_chars=2_000)

    # The overall manifest cap stays the single safety limit.
    assert len(context) <= 2_000 + len("\n... (manifest truncated)")
    assert context.endswith("... (manifest truncated)")
