from __future__ import annotations

import pytest

from nanoreview.agent.tools.context import RequestContext, ToolContext
from nanoreview.agent.tools.loader import ToolLoader
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.agent.tools.spawn import SpawnTool
from nanoreview.config.schema import ToolsConfig
from nanoreview.review.types import ReviewMetaKey


class FakeSubagentManager:
    max_concurrent_subagents = 4

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = (
            "Review subagent [security] started (id: test). "
            "The coordinator will wait for and integrate its result before finalizing."
        )

    def get_running_count(self) -> int:
        return 0

    async def spawn(self, **kwargs: object) -> str:
        self.calls.append({"method": "spawn", **kwargs})
        return self.result


class BusySubagentManager(FakeSubagentManager):
    def get_running_count(self) -> int:
        return self.max_concurrent_subagents


def test_core_tools_expose_spawn_not_review_submitter(tmp_path) -> None:
    registry = ToolRegistry()
    ctx = ToolContext(
        config=ToolsConfig(),
        workspace=str(tmp_path),
        subagent_manager=FakeSubagentManager(),
    )

    ToolLoader().load(ctx, registry, scope="core")

    assert registry.has("spawn")
    assert not registry.has("spawn_review_subagent")
    assert not registry.has("review_submit")
    assert not registry.has("review_judge")


@pytest.mark.asyncio
async def test_spawn_rejects_when_concurrency_limit_reached() -> None:
    manager = BusySubagentManager()
    tool = SpawnTool(manager)  # type: ignore[arg-type]
    tool.set_context(RequestContext(channel="websocket", chat_id="chat", metadata={}))

    result = await tool.execute(task="review security", label="security")

    assert "concurrency limit reached" in result
    assert "4/4" in result
    assert manager.calls == []


@pytest.mark.asyncio
async def test_spawn_surfaces_manager_error_result() -> None:
    manager = FakeSubagentManager()
    manager.result = (
        "Error: Cannot spawn review subagent: dimension 'security' has already "
        "completed for this review."
    )
    tool = SpawnTool(manager)  # type: ignore[arg-type]
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat",
            metadata={ReviewMetaKey.ALLOWED_DIMENSIONS: ["security"]},
        )
    )

    result = await tool.execute(task="review security again", label="security")

    assert "already completed" in result
    assert manager.calls[0]["label"] == "security"


@pytest.mark.asyncio
async def test_spawn_passes_review_metadata_through() -> None:
    """SpawnTool forwards request metadata to SubagentManager.spawn()."""
    manager = FakeSubagentManager()
    tool = SpawnTool(manager)  # type: ignore[arg-type]
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat",
            metadata={
                ReviewMetaKey.ALLOWED_DIMENSIONS: ["security"],
                ReviewMetaKey.TARGET_TYPE: "local",
                ReviewMetaKey.LOCAL_ROOT: "/some/path",
                ReviewMetaKey.ACTION: "review",
            },
        )
    )

    await tool.execute(task="review security", label="security")

    forwarded = manager.calls[0]["origin_metadata"]
    assert forwarded[ReviewMetaKey.TARGET_TYPE] == "local"
    assert forwarded[ReviewMetaKey.LOCAL_ROOT] == "/some/path"
    assert forwarded[ReviewMetaKey.ACTION] == "review"
    assert forwarded[ReviewMetaKey.ALLOWED_DIMENSIONS] == ["security"]
