from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanoreview.bus.events import InboundMessage
from nanoreview.command.builtin import BUILTIN_COMMAND_SPECS, cmd_stop, register_builtin_commands
from nanoreview.command.router import CommandContext, CommandRouter


@pytest.mark.asyncio
async def test_stop_uses_effective_context_key() -> None:
    cancelled: list[str] = []

    async def cancel(key: str) -> int:
        cancelled.append(key)
        return 1

    async def settle_review_after_stop(key: str) -> str | None:
        return None

    msg = InboundMessage(
        channel="websocket",
        sender_id="client",
        chat_id="chat",
        content="/stop",
    )

    result = await cmd_stop(
        CommandContext(
            msg=msg,
            session=None,
            key="__unified__",
            raw="/stop",
            loop=SimpleNamespace(
                _cancel_active_tasks=cancel,
                _settle_review_run_after_stop=settle_review_after_stop,
            ),
        )
    )

    assert cancelled == ["__unified__"]
    assert result.content == "Stopped 1 task(s)."


@pytest.mark.asyncio
async def test_stop_with_no_task_settles_a_leftover_review_run() -> None:
    """A cancelled turn with no active task still reports the settled run."""
    settle_key: list[str] = []

    async def cancel(key: str) -> int:
        return 0

    async def settle_review_after_stop(key: str) -> str | None:
        settle_key.append(key)
        return "Settled review run run-x as stopped."

    result = await cmd_stop(
        CommandContext(
            msg=InboundMessage(
                channel="cli",
                sender_id="client",
                chat_id="chat",
                content="/stop",
            ),
            session=None,
            key="cli:review",
            raw="/stop",
            loop=SimpleNamespace(
                _cancel_active_tasks=cancel,
                _settle_review_run_after_stop=settle_review_after_stop,
            ),
        )
    )

    assert settle_key == ["cli:review"]
    assert result.content == "Settled review run run-x as stopped."


def test_removed_math_commands_are_not_advertised_or_routable() -> None:
    router = CommandRouter()
    register_builtin_commands(router)

    commands = {spec.command for spec in BUILTIN_COMMAND_SPECS}

    assert "/math-kb" not in commands
    assert "/mistake-add" not in commands
    assert router.is_dispatchable_command("/math-kb") is False
    assert router.is_dispatchable_command("/math-kb list") is False
    assert router.is_dispatchable_command("/mistake-add") is False
    assert router.is_dispatchable_command("/mistake-add reason") is False
