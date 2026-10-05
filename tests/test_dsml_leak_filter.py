import pytest

from nanoreview.agent.event_sink import build_callback_event_sink
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

    hook = AgentProgressHook(build_callback_event_sink(on_stream=on_stream))
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

    hook = AgentProgressHook(build_callback_event_sink(on_stream=on_stream))
    context = AgentHookContext(iteration=1, messages=[])
    raw = (
        '<｜DSML｜tool_calls> <｜DSML｜invoke name="read_file"> '
        '<｜DSML｜parameter name="path" string="false">C:\\Users\\demo\\file.txt'
        "\n\n现在我已经对项目有了理解。"
    )

    for ch in raw:
        await hook.on_stream(context, ch)

    assert "".join(streamed) == "现在我已经对项目有了理解。"


@pytest.mark.asyncio
async def test_progress_hook_wants_streaming_only_with_a_stream_consumer() -> None:
    progress_only = AgentProgressHook(
        build_callback_event_sink(on_progress=lambda *_a, **_k: _noop())
    )
    with_stream = AgentProgressHook(
        build_callback_event_sink(
            on_progress=lambda *_a, **_k: _noop(),
            on_stream=lambda _delta: _noop(),
        )
    )

    assert progress_only.wants_streaming() is False
    assert with_stream.wants_streaming() is True


@pytest.mark.asyncio
async def test_progress_hook_finalize_is_cleaning_not_replacement() -> None:
    """DSML cleaning must not be treated as an explicit content replacement."""
    from nanoreview.agent.hooks.lifecycle import finalize_content_result

    hook = AgentProgressHook()
    context = AgentHookContext(iteration=0, messages=[])
    raw = "<think>hmm</think>" "现在我已经对项目有了理解。"

    finalized = finalize_content_result(hook, context, raw)

    assert finalized.content == "现在我已经对项目有了理解。"
    assert finalized.is_replaced is False


async def _noop() -> None:
    return None
