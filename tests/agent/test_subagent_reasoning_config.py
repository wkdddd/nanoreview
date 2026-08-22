"""Tests for subagent reasoning effort config inheritance.

Config priority:
1. ``review.subagent_reasoning_effort`` (explicit, including ``"none"``)
2. ``agents.defaults.reasoning_effort``
3. ``None`` (provider default behaviour)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.loop import AgentLoop
from nanoreview.bus.queue import MessageBus
from nanoreview.config.schema import ReviewConfig
from nanoreview.providers.base import LLMProvider, LLMResponse


class _DummyProvider(LLMProvider):
    async def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


def _make_loop(
    tmp_path: Path,
    *,
    review_config: ReviewConfig | None = None,
    default_reasoning_effort: str | None = None,
) -> AgentLoop:
    return AgentLoop(
        MessageBus(),
        _DummyProvider(),
        tmp_path,
        review_config=review_config or ReviewConfig(),
        default_reasoning_effort=default_reasoning_effort,
    )


def test_inherits_default_when_review_unset(tmp_path: Path) -> None:
    """When review.subagent_reasoning_effort is None, inherit agents.defaults."""
    loop = _make_loop(tmp_path, default_reasoning_effort="high")
    assert loop.subagents.reasoning_effort == "high"


def test_review_explicit_overrides_default(tmp_path: Path) -> None:
    """review.subagent_reasoning_effort takes precedence over agents.defaults."""
    loop = _make_loop(
        tmp_path,
        review_config=ReviewConfig(subagent_reasoning_effort="medium"),
        default_reasoning_effort="high",
    )
    assert loop.subagents.reasoning_effort == "medium"


def test_review_none_overrides_default(tmp_path: Path) -> None:
    """Explicit 'none' in review config must NOT be treated as unset."""
    loop = _make_loop(
        tmp_path,
        review_config=ReviewConfig(subagent_reasoning_effort="none"),
        default_reasoning_effort="high",
    )
    assert loop.subagents.reasoning_effort == "none"


def test_both_unset_yields_none(tmp_path: Path) -> None:
    """When neither review nor defaults set reasoning, result is None."""
    loop = _make_loop(tmp_path)
    assert loop.subagents.reasoning_effort is None


def test_resolve_method_returns_correct_priority(tmp_path: Path) -> None:
    loop = _make_loop(
        tmp_path,
        review_config=ReviewConfig(subagent_reasoning_effort="low"),
        default_reasoning_effort="high",
    )
    assert loop._resolve_subagent_reasoning_effort() == "low"

    loop2 = _make_loop(tmp_path, default_reasoning_effort="adaptive")
    assert loop2._resolve_subagent_reasoning_effort() == "adaptive"

    loop3 = _make_loop(tmp_path)
    assert loop3._resolve_subagent_reasoning_effort() is None


# ---------------------------------------------------------------------------
# Execution-layer tests: verify reasoning_effort reaches the actual LLM call
# ---------------------------------------------------------------------------


class _RecordingProvider(LLMProvider):
    """Provider that records the reasoning_effort kwarg passed to chat()."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded_effort: str | None = None

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.recorded_effort = kwargs.get("reasoning_effort")
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


@pytest.mark.asyncio
async def test_reasoning_effort_high_reaches_provider() -> None:
    """Verify that reasoning_effort='high' reaches the actual LLM request."""
    from nanoreview.agent.hooks import AgentHookContext
    from nanoreview.agent.hooks.subagent import SubagentHook
    from nanoreview.agent.runner import AgentRunner, AgentRunSpec
    from nanoreview.agent.tools.registry import ToolRegistry

    provider = _RecordingProvider()
    runner = AgentRunner(provider)
    hook = SubagentHook("task-1")
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "review this"}],
        tools=ToolRegistry(),
        model="dummy",
        max_iterations=1,
        max_tool_result_chars=1000,
        reasoning_effort="high",
        hook=hook,
    )
    context = AgentHookContext(iteration=0, messages=list(spec.initial_messages))
    await runner._request_model(spec, spec.initial_messages, hook, context)
    assert provider.recorded_effort == "high"


@pytest.mark.asyncio
async def test_reasoning_effort_none_reaches_provider() -> None:
    """Verify that explicit reasoning_effort='none' reaches the actual LLM request."""
    from nanoreview.agent.hooks import AgentHookContext
    from nanoreview.agent.hooks.subagent import SubagentHook
    from nanoreview.agent.runner import AgentRunner, AgentRunSpec
    from nanoreview.agent.tools.registry import ToolRegistry

    provider = _RecordingProvider()
    runner = AgentRunner(provider)
    hook = SubagentHook("task-1")
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "review this"}],
        tools=ToolRegistry(),
        model="dummy",
        max_iterations=1,
        max_tool_result_chars=1000,
        reasoning_effort="none",
        hook=hook,
    )
    context = AgentHookContext(iteration=0, messages=list(spec.initial_messages))
    await runner._request_model(spec, spec.initial_messages, hook, context)
    assert provider.recorded_effort == "none"
