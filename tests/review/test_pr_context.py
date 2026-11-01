"""PR context tool: pinned reads, role policies, budgets and shared matcher."""

from __future__ import annotations

import pytest

from nanoreview.agent.tools.pr_context import PrContextTool
from nanoreview.review.input.github import GitHubFileContent, GitHubTreeEntry
from nanoreview.review.source.pr_context import (
    JUDGE_CONTEXT_LIMITS,
    REVIEWER_CONTEXT_LIMITS,
    PrContextSession,
)
from nanoreview.storage.records import ChangedFile, ReviewSnapshot

_HEAD = "a" * 40
_BASE = "b" * 40
_MERGE = "e" * 40


def _snapshot() -> ReviewSnapshot:
    return ReviewSnapshot(
        id="snap_1",
        run_id="run1",
        repository_id=1,
        pr_number=3,
        base_repo="acme/widgets",
        head_repo="acme/widgets",
        base_sha=_BASE,
        head_sha=_HEAD,
        merge_base_sha=_MERGE,
        changed_files=(
            ChangedFile(
                path="src/a.py",
                old_path=None,
                status="modified",
                patch="@@ -1 +1 @@\n-old\n+new\n",
            ),
            ChangedFile(path="docs/readme.md", old_path=None, status="added", patch="@@ -0,0 +1 @@\n+hi\n"),
        ),
        input_fingerprint="f" * 64,
    )


class FakeGitHub:
    def __init__(
        self,
        *,
        tree: dict[str, int],
        texts: dict[tuple[str, str, str], str | None] | None = None,
        truncated: bool = False,
    ) -> None:
        self.tree = tree
        self.texts = texts or {}
        self.truncated = truncated
        self.reads: list[tuple[str, str, str]] = []

    async def list_tree(self, owner: str, name: str, ref: str):
        entries = [
            GitHubTreeEntry(path=path, entry_type="blob", size=size, sha=f"s{index}")
            for index, (path, size) in enumerate(sorted(self.tree.items()))
        ]
        return entries, self.truncated

    async def get_directory(self, owner: str, name: str, path: str, ref: str):
        return [
            GitHubTreeEntry(path=item, entry_type="file", size=self.tree[item], sha="s")
            for item in sorted(self.tree)
            if item.startswith(f"{path}/")
        ]

    async def get_file(self, owner: str, name: str, path: str, ref: str) -> GitHubFileContent:
        self.reads.append((owner, path, ref))
        text = self.texts.get((owner, path, ref))
        return GitHubFileContent(path=path, sha=ref, text=text)


def _session(
    *,
    limits=REVIEWER_CONTEXT_LIMITS,
    path_scopes: tuple[str, ...] = (),
    tree: dict[str, int] | None = None,
    texts: dict[tuple[str, str, str], str | None] | None = None,
    truncated: bool = False,
) -> tuple[PrContextSession, FakeGitHub]:
    github = FakeGitHub(
        tree=tree if tree is not None else {"src/a.py": 20, "docs/readme.md": 10},
        texts=texts,
        truncated=truncated,
    )
    session = PrContextSession(
        snapshot=_snapshot(),
        github=github,  # type: ignore[arg-type]
        limits=limits,
        path_scopes=path_scopes,
    )
    return session, github


@pytest.mark.asyncio
async def test_read_pins_new_side_to_head_and_old_side_to_merge_base() -> None:
    session, github = _session(
        texts={
            ("acme", "src/a.py", _HEAD): "line one\nline two\nline three\n",
            ("acme", "src/a.py", _MERGE): "old one\nold two\n",
        }
    )

    new = await session.read("src/a.py", side="new", offset=1, limit=2)
    old = await session.read("src/a.py", side="old", offset=1, limit=2)

    assert "1| line one" in new.text
    assert "Use offset=3 to continue" in new.text
    assert "1| old one" in old.text
    assert new.evidence is not None and new.evidence.evidence_id == "ctx-001"
    assert old.evidence is not None and old.evidence.evidence_id == "ctx-002"
    assert ("acme", "src/a.py", _HEAD) in github.reads
    assert ("acme", "src/a.py", _MERGE) in github.reads


@pytest.mark.asyncio
async def test_read_without_pinned_text_is_an_explicit_error() -> None:
    session, _ = _session(texts={})

    result = await session.read("src/a.py")

    assert result.is_error
    assert "cannot read" in result.text


@pytest.mark.asyncio
async def test_read_line_and_byte_budgets_are_enforced() -> None:
    long_lines = "\n".join("x" * 200 for _ in range(300))
    session, _ = _session(texts={("acme", "src/a.py", _HEAD): long_lines})

    result = await session.read("src/a.py", limit=200)

    assert result.evidence is not None
    returned = result.evidence.end_line or 0
    assert returned <= 200
    assert len(result.text.encode("utf-8")) < 200 * 200 * 2


@pytest.mark.asyncio
async def test_reviewer_reads_are_not_call_capped() -> None:
    """No tool-call count limit: only token budgets bound how much is read."""
    session, _ = _session(texts={("acme", "src/a.py", _HEAD): "line\n"})
    for _ in range(40):
        result = await session.read("src/a.py")
        assert not result.is_error
    assert session.tool_calls == 40


@pytest.mark.asyncio
async def test_judge_cannot_list_actions_outside_its_role() -> None:
    session, _ = _session(
        limits=JUDGE_CONTEXT_LIMITS,
        texts={("acme", "src/a.py", _HEAD): "line\n"},
    )

    blocked = await session.list("")
    assert blocked.is_error
    assert "may only use" in blocked.text

    # Its allowed actions work without a call cap either.
    for _ in range(10):
        assert not (await session.read("src/a.py")).is_error


@pytest.mark.asyncio
async def test_search_defaults_to_literal_and_supports_regex() -> None:
    text = "a.b literal\naxb other\n"
    session, _ = _session(texts={("acme", "src/a.py", _HEAD): text})

    literal = await session.search("a.b")
    assert "> 1| a.b literal" in literal.text
    assert "> 2|" not in literal.text

    regex = await session.search("a.b", fixed_strings=False)
    assert "axb other" in regex.text


@pytest.mark.asyncio
async def test_search_reports_candidates_scope_and_zero_match_coverage() -> None:
    session, _ = _session(
        tree={"src/a.py": 20, "src/b.py": 20, "docs/readme.md": 10},
        texts={
            ("acme", "src/a.py", _HEAD): "needle\n",
            ("acme", "src/b.py", _HEAD): "nothing\n",
            ("acme", "docs/readme.md", _HEAD): "needle\n",
        },
    )

    result = await session.search("needle", scope="src", output_mode="files_with_matches")
    assert "src/a.py" in result.text
    assert "docs/readme.md" not in result.text
    assert "candidates=2" in result.text

    missing = await session.search("absent", scope="src")
    assert "No matches found" in missing.text
    assert "searched 2 of 2 candidate files" in missing.text


@pytest.mark.asyncio
async def test_search_count_mode_and_candidate_cap_note() -> None:
    many = {f"src/f{index}.py": 10 for index in range(5)}
    texts = {("acme", path, _HEAD): "needle\nneedle\n" for path in many}
    from dataclasses import replace

    session, _ = _session(tree=many, texts=texts)
    session._limits = replace(REVIEWER_CONTEXT_LIMITS, max_candidates=2)  # noqa: SLF001

    result = await session.search("needle", scope="src", output_mode="count")

    assert "src/f0.py: 2" in result.text
    assert "candidate list truncated to 2 of 5" in result.text


@pytest.mark.asyncio
async def test_judge_path_scope_blocks_reads_outside_the_bound_paths() -> None:
    session, _ = _session(
        limits=JUDGE_CONTEXT_LIMITS,
        path_scopes=("src",),
        texts={("acme", "docs/readme.md", _HEAD): "hi\n"},
    )

    blocked = await session.read("docs/readme.md")
    assert blocked.is_error
    assert "outside this role's bounded scope" in blocked.text


@pytest.mark.asyncio
async def test_path_traversal_is_rejected() -> None:
    session, _ = _session()

    for path in ("/etc/passwd", "../secret.py", "src/../../secret.py"):
        result = await session.read(path)
        assert result.is_error
        assert "inside the repository" in result.text


@pytest.mark.asyncio
async def test_diff_reads_the_frozen_patch_only() -> None:
    session, _ = _session()

    single = await session.diff("src/a.py")
    assert "@@ -1 +1 @@" in single.text
    assert "src/a.py" in single.text

    missing = await session.diff("nope.py")
    assert missing.is_error
    assert "not part of this pull request" in missing.text


@pytest.mark.asyncio
async def test_tool_wrapper_dispatches_actions() -> None:
    session, _ = _session(texts={("acme", "src/a.py", _HEAD): "needle\n"})
    tool = PrContextTool(session)

    diff = await tool.execute(action="diff", path="src/a.py")
    assert "src/a.py" in diff

    listing = await tool.execute(action="list", path="src")
    assert "src/a.py" in listing

    found = await tool.execute(action="search", query="needle", scope="src")
    assert "needle" in found

    unknown = await tool.execute(action="write")
    assert unknown.startswith("Error: unknown action")
