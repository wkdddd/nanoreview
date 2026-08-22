import pytest

from nanoreview.agent.hooks import AgentHookContext, AgentProgressHook
from nanoreview.utils.helpers import strip_think


def test_strip_think_removes_leaked_dsml_tool_calls() -> None:
    raw = (
        '<｜DSML｜tool_calls> <｜DSML｜invoke name="read_file"> '
        '<｜DSML｜parameter name="path" string="false">C:\\Users\\demo\\file.txt'
        '</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>'
        "\n\n现在我已经对项目有了理解。"
    )

    assert strip_think(raw) == "现在我已经对项目有了理解。"


@pytest.mark.asyncio
async def test_progress_hook_does_not_stream_dsml_prefixes() -> None:
    streamed: list[str] = []

    async def on_stream(delta: str) -> None:
        streamed.append(delta)

    hook = AgentProgressHook(on_stream=on_stream)
    context = AgentHookContext(iteration=1, messages=[])
    raw = (
        '<｜DSML｜tool_calls> <｜DSML｜invoke name="read_file"> '
        '<｜DSML｜parameter name="path" string="false">C:\\Users\\demo\\file.txt'
    )

    for ch in raw:
        await hook.on_stream(context, ch)

    assert streamed == []


@pytest.mark.asyncio
async def test_progress_hook_resumes_after_unclosed_dsml_block() -> None:
    streamed: list[str] = []

    async def on_stream(delta: str) -> None:
        streamed.append(delta)

    hook = AgentProgressHook(on_stream=on_stream)
    context = AgentHookContext(iteration=1, messages=[])
    raw = (
        '<｜DSML｜tool_calls> <｜DSML｜invoke name="read_file"> '
        '<｜DSML｜parameter name="path" string="false">C:\\Users\\demo\\file.txt'
        "\n\n现在我已经对项目有了理解。"
    )

    for ch in raw:
        await hook.on_stream(context, ch)

    assert "".join(streamed) == "现在我已经对项目有了理解。"
