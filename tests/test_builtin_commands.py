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
            loop=SimpleNamespace(_cancel_active_tasks=cancel),
        )
    )

    assert cancelled == ["__unified__"]
    assert result.content == "Stopped 1 task(s)."


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
