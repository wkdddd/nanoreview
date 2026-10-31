"""PR snapshot preparation tests with a fake GitHub client."""

from __future__ import annotations

import pytest

from nanoreview.review.input.github import (
    GitHubFileContent,
    GitHubPullRequest,
    GitHubPullRequestFile,
)
from nanoreview.review.input.pr_snapshot import (
    PullRequestSnapshotService,
    SnapshotPreparationError,
    compute_pr_fingerprint,
    rebuild_patch,
)
from nanoreview.storage.records import ChangedFile

_A = "a" * 40
_B = "b" * 40
_C = "c" * 40
_MERGE = "e" * 40


def _pull(head_sha: str = _A) -> GitHubPullRequest:
    return GitHubPullRequest(
        number=9,
        title="Add widget",
        state="open",
        draft=False,
        author="octocat",
        base_ref="main",
        head_ref="widget",
        base_sha=_B,
        head_sha=head_sha,
        base_repo="acme/widgets",
        head_repo="fork/widgets",
        html_url="https://github.com/acme/widgets/pull/9",
        updated_at=None,
    )


class FakeGitHub:
    def __init__(
        self,
        *,
        files: list[GitHubPullRequestFile],
        texts: dict[tuple[str, str, str], str | None] | None = None,
        head_sequence: list[str] | None = None,
    ) -> None:
        self.files = files
        self.texts = texts or {}
        self.head_sequence = list(head_sequence or [_A])
        self.pull_calls = 0
        self.file_calls = 0
        self.reads: list[tuple[str, str, str]] = []

    async def get_pull_request(self, owner: str, name: str, number: int) -> GitHubPullRequest:
        index = min(self.pull_calls, len(self.head_sequence) - 1)
        self.pull_calls += 1
        return _pull(self.head_sequence[index])

    async def list_pull_request_files(
        self, owner: str, name: str, number: int
    ) -> list[GitHubPullRequestFile]:
        self.file_calls += 1
        return list(self.files)

    async def get_merge_base(self, owner: str, name: str, base_sha: str, head_sha: str) -> str:
        return _MERGE

    async def get_file(self, owner: str, name: str, path: str, ref: str) -> GitHubFileContent:
        self.reads.append((owner, path, ref))
        text = self.texts.get((owner, path, ref))
        return GitHubFileContent(path=path, sha=ref, text=text)


@pytest.mark.asyncio
async def test_snapshot_keeps_provided_patch_and_pins_all_shas() -> None:
    files = [
        GitHubPullRequestFile(
            path="src/a.py",
            previous_path=None,
            status="modified",
            additions=1,
            deletions=1,
            patch="@@ -1 +1 @@\n-old\n+new\n",
        )
    ]
    github = FakeGitHub(files=files)
    service = PullRequestSnapshotService(github)  # type: ignore[arg-type]

    snapshot = await service.prepare(run_id="run1", owner="acme", name="widgets", number=9)

    assert snapshot.base_sha == _B
    assert snapshot.head_sha == _A
    assert snapshot.merge_base_sha == _MERGE
    assert snapshot.base_repo == "acme/widgets"
    assert snapshot.head_repo == "fork/widgets"
    assert snapshot.patch_map == {"src/a.py": "@@ -1 +1 @@\n-old\n+new\n"}
    assert snapshot.gaps == ()
    assert len(snapshot.input_fingerprint) == 64


@pytest.mark.asyncio
async def test_missing_patch_is_rebuilt_from_both_pinned_sides() -> None:
    files = [
        GitHubPullRequestFile(
            path="src/a.py",
            previous_path=None,
            status="modified",
            additions=1,
            deletions=1,
            patch=None,
        )
    ]
    texts = {
        ("acme", "src/a.py", _MERGE): "old line\n",
        ("fork", "src/a.py", _A): "new line\n",
    }
    github = FakeGitHub(files=files, texts=texts)
    service = PullRequestSnapshotService(github)  # type: ignore[arg-type]

    snapshot = await service.prepare(run_id="run1", owner="acme", name="widgets", number=9)

    patch = snapshot.changed_files[0].patch
    assert patch is not None
    assert "-old line" in patch and "+new line" in patch
    assert ("acme", "src/a.py", _MERGE) in github.reads
    assert ("fork", "src/a.py", _A) in github.reads


@pytest.mark.asyncio
async def test_unreadable_side_records_an_explicit_gap() -> None:
    files = [
        GitHubPullRequestFile(
            path="src/b.py",
            previous_path=None,
            status="modified",
            additions=1,
            deletions=1,
            patch=None,
        )
    ]
    github = FakeGitHub(files=files, texts={})
    service = PullRequestSnapshotService(github)  # type: ignore[arg-type]

    snapshot = await service.prepare(run_id="run1", owner="acme", name="widgets", number=9)

    assert snapshot.gaps
    assert snapshot.gaps[0].startswith("patch_missing: src/b.py")
    assert snapshot.changed_files[0].patch is None


@pytest.mark.asyncio
async def test_head_moved_twice_fails_instead_of_mixing_versions() -> None:
    files = [
        GitHubPullRequestFile(
            path="src/a.py",
            previous_path=None,
            status="modified",
            additions=1,
            deletions=1,
            patch="@@ -1 +1 @@\n",
        )
    ]
    github = FakeGitHub(files=files, head_sequence=[_A, _C, "d" * 40])
    service = PullRequestSnapshotService(github)  # type: ignore[arg-type]

    with pytest.raises(SnapshotPreparationError) as error:
        await service.prepare(run_id="run1", owner="acme", name="widgets", number=9)
    assert error.value.code == "pull_request_changed"
    assert github.file_calls == 2


@pytest.mark.asyncio
async def test_head_moved_once_is_retried_against_the_new_head() -> None:
    files = [
        GitHubPullRequestFile(
            path="src/a.py",
            previous_path=None,
            status="modified",
            additions=1,
            deletions=1,
            patch="@@ -1 +1 @@\n",
        )
    ]
    github = FakeGitHub(files=files, head_sequence=[_A, _C])
    service = PullRequestSnapshotService(github)  # type: ignore[arg-type]

    snapshot = await service.prepare(run_id="run1", owner="acme", name="widgets", number=9)

    assert snapshot.head_sha == _C
    # The file list is re-fetched for the new head; the snapshot never mixes them.
    assert github.file_calls == 2


def test_fingerprint_is_stable_and_version_sensitive() -> None:
    files = [
        ChangedFile(path="a.py", old_path=None, status="modified", patch="@@ -1 +1 @@\n")
    ]
    first = compute_pr_fingerprint(
        repository="acme/widgets",
        number=1,
        base_sha=_B,
        head_sha=_A,
        merge_base_sha=_MERGE,
        files=files,
    )
    same = compute_pr_fingerprint(
        repository="acme/widgets",
        number=1,
        base_sha=_B,
        head_sha=_A,
        merge_base_sha=_MERGE,
        files=files,
    )
    changed = compute_pr_fingerprint(
        repository="acme/widgets",
        number=1,
        base_sha=_B,
        head_sha=_C,
        merge_base_sha=_MERGE,
        files=files,
    )

    assert first == same
    assert first != changed


def test_rebuild_patch_produces_a_unified_diff() -> None:
    patch = rebuild_patch("a\nb\n", "a\nc\n", old_path="x.py", new_path="x.py")

    assert patch.startswith("--- a/x.py")
    assert "-b" in patch and "+c" in patch
