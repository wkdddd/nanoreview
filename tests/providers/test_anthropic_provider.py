from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanoreview.providers.anthropic_provider import AnthropicProvider
from nanoreview.providers.base import LLMProvider


def _provider() -> AnthropicProvider:
    provider = object.__new__(AnthropicProvider)
    provider.default_model = "claude-sonnet-4-20250514"
    provider.extra_headers = {}
    return provider


def test_build_kwargs_translates_json_object_response_format() -> None:
    kwargs = _provider()._build_kwargs(
        [{"role": "user", "content": "Return JSON"}],
        None,
        None,
        100,
        0.0,
        None,
        None,
        response_format={"type": "json_object"},
    )

    assert kwargs["extra_body"] == {
        "output_config": {
            "format": {"type": "json_schema", "schema": {"type": "object"}},
        },
    }


def test_build_kwargs_translates_json_schema_response_format() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "required": ["status"],
    }

    kwargs = _provider()._build_kwargs(
        [{"role": "user", "content": "Return JSON"}],
        None,
        None,
        100,
        0.0,
        None,
        None,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema, "strict": True},
        },
    )

    assert kwargs["extra_body"]["output_config"]["format"]["schema"] == schema


def test_response_format_unsupported_error_matches_anthropic_output_config() -> None:
    assert LLMProvider._is_response_format_unsupported_error(
        "Error: unknown parameter output_config"
    )


@pytest.mark.asyncio
async def test_chat_sends_anthropic_output_config() -> None:
    captured: dict[str, Any] = {}

    class Messages:
        async def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(content=[], stop_reason="end_turn", usage=None)

    provider = _provider()
    provider._client = SimpleNamespace(messages=Messages())

    response = await provider.chat(
        [{"role": "user", "content": "Return JSON"}],
        response_format={"type": "json_object"},
    )

    assert response.finish_reason == "stop"
    assert captured["extra_body"]["output_config"]["format"]["type"] == "json_schema"
