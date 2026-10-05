"""Run-level and turn-level hook contract.

Covers the two halves of the migrated hook lifecycle: what ``AgentRunner``
guarantees about ``before_run``/``after_run``/``on_error``/``on_finally``
ordering on every exit path, and what ``CompositeHook`` plus the turn builder
guarantee about ordering, isolation and per-turn instance freshness.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent import hooks as hooks_module
from nanoreview.agent.hooks import (
    AgentHook,
    AgentHookContext,
    AgentProgressHook,
    AgentRunHookContext,
    AgentTurnHookContext,
    AgentTurnHookSpec,
    CompositeHook,
    FinalizeContentResult,
    build_agent_turn_hook,
    finalize_content_result,
)
from nanoreview.agent.runner import AgentRunner, AgentRunSpec
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.events import NO_EVENTS, ProgressEvent, StreamDeltaEvent
from nanoreview.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class ScriptedProvider(LLMProvider):
    """Replays scripted replies; the last one repeats once exhausted."""

    def __init__(self, responses: list[LLMResponse], *, raises: BaseException | None = None):
        super().__init__()
        self.responses = list(responses)
        self.raises = raises
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        index = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[index]

    def get_default_model(self) -> str:
        return "dummy"


class RecordingHook(AgentHook):
    """Records every lifecycle call in the order the runner makes them."""

    def __init__(self, name: str, calls: list[str], *, fail: str | None = None) -> None:
        super().__init__()
        self.name = name
        self.calls = calls
        self.fail = fail
        self.run_contexts: list[AgentRunHookContext] = []

    def _record(self, method: str) -> None:
        self.calls.append(f"{self.name}.{method}")
        if self.fail == method:
            raise RuntimeError(f"{self.name}.{method} failed")

    async def before_run(self, context: AgentRunHookContext) -> None:
        self.run_contexts.append(context)
        self._record("before_run")

    async def after_run(self, context: AgentRunHookContext) -> None:
        self._record("after_run")

    async def on_error(self, context: AgentRunHookContext) -> None:
        self._record("on_error")

    async def on_finally(self, context: AgentRunHookContext) -> None:
        self._record("on_finally")

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._record("before_iteration")

    async def after_iteration(self, context: AgentHookContext) -> None:
        self._record("after_iteration")

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        self._record("before_execute_tools")

    async def before_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
    ) -> None:
        self._record("before_execute_tool")

    async def after_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        self._record("after_execute_tool")

    async def on_execute_tool_error(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        error: Any,
    ) -> None:
        self._record("on_execute_tool_error")


class ExplodingBeforeRunHook(RecordingHook):
    """Fails the run before the model is ever called.

    A provider-level exception is a *business* outcome (``_safe_chat`` turns it
    into an error response), so the uncaught-exception path is exercised from
    the hook side instead.
    """


class ExplodingProvider(LLMProvider):
    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


class CancellingProvider(LLMProvider):
    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        raise asyncio.CancelledError

    def get_default_model(self) -> str:
        return "dummy"


def make_spec(**overrides: Any) -> AgentRunSpec:
    values: dict[str, Any] = {
        "frozen_messages": [],
        "working_messages": [],
        "tools": ToolRegistry(),
        "model": "dummy",
        "max_iterations": 1,
        "max_tool_result_chars": 1000,
    }
    values.update(overrides)
    return AgentRunSpec(**values)


# ---------------------------------------------------------------------------
# Run-level lifecycle
# ---------------------------------------------------------------------------


async def test_successful_run_calls_before_after_and_finally_in_order() -> None:
    calls: list[str] = []
    provider = ScriptedProvider([LLMResponse(content="done")])

    result = await AgentRunner(provider).run(make_spec(hook=RecordingHook("h", calls)))

    assert result.stop_reason == "completed"
    assert calls == [
        "h.before_run",
        "h.before_iteration",
        "h.after_iteration",
        "h.after_run",
        "h.on_finally",
    ]


async def test_model_error_calls_on_error_before_after_run_and_finally() -> None:
    """A model-level error is a business outcome: both ``on_error`` and
    ``after_run`` run, in that order, before the finally sweep."""
    calls: list[str] = []
    provider = ScriptedProvider([
        LLMResponse(content="Error: upstream unavailable", finish_reason="error")
    ])

    result = await AgentRunner(provider).run(make_spec(hook=RecordingHook("h", calls)))

    assert result.stop_reason == "error"
    assert calls == [
        "h.before_run",
        "h.before_iteration",
        "h.after_iteration",
        "h.on_error",
        "h.after_run",
        "h.on_finally",
    ]


async def test_uncaught_exception_calls_on_error_then_finally_and_reraises() -> None:
    calls: list[str] = []
    hook = ExplodingBeforeRunHook("h", calls, fail="before_run")

    with pytest.raises(RuntimeError, match="h.before_run failed"):
        await AgentRunner(ExplodingProvider()).run(make_spec(hook=hook))

    # No ``after_run``: the run never completed.
    assert calls == ["h.before_run", "h.on_error", "h.on_finally"]


async def test_cancellation_skips_on_error_and_after_run_but_runs_finally() -> None:
    """A cancelled run is not a failure: error reporting stays silent so it
    never races the caller's own cancellation."""
    calls: list[str] = []
    hook = RecordingHook("h", calls)

    with pytest.raises(asyncio.CancelledError):
        await AgentRunner(CancellingProvider()).run(make_spec(hook=hook))

    assert calls == ["h.before_run", "h.before_iteration", "h.on_finally"]


# ---------------------------------------------------------------------------
# Run-hook isolation and timing
# ---------------------------------------------------------------------------


class MutatingObserverHook(AgentHook):
    """An observer that abuses its context to prove it is isolated."""

    def __init__(self) -> None:
        super().__init__()
        self.after_usage: dict[str, int] | None = None
        self.finally_usage: dict[str, int] | None = None

    async def before_run(self, context: AgentRunHookContext) -> None:
        context.messages.append({"role": "assistant", "content": "INJECTED BY OBSERVER"})

    async def after_run(self, context: AgentRunHookContext) -> None:
        self.after_usage = dict(context.usage)
        context.usage["prompt_tokens"] = 999
        context.tools_used.append("MUTATED")

    async def on_finally(self, context: AgentRunHookContext) -> None:
        self.finally_usage = dict(context.usage)


class UsageProvider(LLMProvider):
    def __init__(self, usage: dict[str, int] | None = None) -> None:
        super().__init__()
        self._usage = usage

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="done", usage=self._usage)

    def get_default_model(self) -> str:
        return "dummy"


async def test_before_run_messages_are_isolated_from_the_run_history() -> None:
    """Regression: ``before_run`` handed out the live history list, so an
    observer appending to it rewrote the transcript the caller persists."""
    hook = MutatingObserverHook()

    result = await AgentRunner(UsageProvider()).run(make_spec(hook=hook))

    assert all(
        not (isinstance(m, dict) and m.get("content") == "INJECTED BY OBSERVER")
        for m in result.messages
    )


async def test_run_context_usage_is_isolated_from_the_result_usage() -> None:
    """Regression: the end snapshot aliased the runner's live usage dict, so an
    observer's mutation showed up in ``AgentRunResult.usage``."""
    hook = MutatingObserverHook()

    result = await AgentRunner(
        UsageProvider({"prompt_tokens": 7, "completion_tokens": 3})
    ).run(make_spec(hook=hook))

    assert result.usage["prompt_tokens"] == 7
    # The observer did mutate the context it was handed; only the result is safe.
    assert hook.after_usage is not None
    assert hook.after_usage["prompt_tokens"] == 7


async def test_run_context_tools_used_is_isolated_from_the_result() -> None:
    hook = MutatingObserverHook()

    result = await AgentRunner(UsageProvider()).run(make_spec(hook=hook))

    assert "MUTATED" not in result.tools_used


async def test_after_run_observes_the_settled_usage() -> None:
    """Regression: ``after_run`` ran before compression settled and usage was
    merged, so the run-level result hook saw a partial sum while the caller got
    the full one.
    """
    hook = MutatingObserverHook()
    runner = AgentRunner(UsageProvider({"prompt_tokens": 40, "completion_tokens": 20}))
    original_close = runner._close_compression

    async def billed_compression(spec: Any, state: Any, usage: dict[str, int]) -> None:
        # Stand in for compression tokens billed during the run.
        await original_close(spec, state, usage)
        usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + 100
        usage["completion_tokens"] = usage.get("completion_tokens", 0) + 50

    runner._close_compression = billed_compression  # type: ignore[method-assign]

    result = await runner.run(make_spec(hook=hook))

    assert result.usage == {"prompt_tokens": 140, "completion_tokens": 70}
    # The run-level result hook and the caller agree.
    assert hook.after_usage == result.usage
    assert hook.after_usage is not None
    assert hook.after_usage["prompt_tokens"] == 140


async def test_settled_usage_is_merged_exactly_once() -> None:
    """Accounting is settled both before ``after_run`` and in ``finally``; the
    second call must not double-count compression usage."""
    runner = AgentRunner(UsageProvider({"prompt_tokens": 40, "completion_tokens": 20}))
    original_close = runner._close_compression
    calls = 0

    async def counted(spec: Any, state: Any, usage: dict[str, int]) -> None:
        nonlocal calls
        calls += 1
        await original_close(spec, state, usage)
        usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + 100

    runner._close_compression = counted  # type: ignore[method-assign]

    result = await runner.run(make_spec())

    # ``_close_compression`` itself is guarded by ``state.closed``; the merge is
    # guarded separately, so the total is exactly one compression contribution.
    assert result.usage["prompt_tokens"] == 140


async def test_after_run_on_a_model_error_sees_the_final_error_state() -> None:
    contexts: list[AgentRunHookContext] = []

    class CapturingHook(AgentHook):
        async def on_error(self, context: AgentRunHookContext) -> None:
            contexts.append(context)

    provider = ScriptedProvider([
        LLMResponse(content="Error: upstream unavailable", finish_reason="error")
    ])

    result = await AgentRunner(provider).run(make_spec(hook=CapturingHook()))

    assert result.stop_reason == "error"
    assert contexts and contexts[0].error == "Error: upstream unavailable"
    assert contexts[0].usage == result.usage


async def test_run_context_carries_the_final_state_on_success() -> None:
    calls: list[str] = []
    hook = RecordingHook("h", calls)
    provider = ScriptedProvider([LLMResponse(content="all set")])

    await AgentRunner(provider).run(make_spec(hook=hook))

    context = hook.run_contexts[0]
    assert context.final_content == "all set"
    assert context.stop_reason == "completed"
    assert context.error is None
    assert context.exception is None
    assert context.usage == {"prompt_tokens": 0, "completion_tokens": 0}
    assert context.messages  # snapshotted, not the live runner list


async def test_run_context_reports_the_error_text_on_a_model_error() -> None:
    calls: list[str] = []
    hook = RecordingHook("h", calls)
    provider = ScriptedProvider([
        LLMResponse(content="Error: upstream unavailable", finish_reason="error")
    ])

    await AgentRunner(provider).run(make_spec(hook=hook))

    context = hook.run_contexts[0]
    assert context.stop_reason == "error"
    assert context.error == "Error: upstream unavailable"
    assert context.exception is None


async def test_run_context_reports_usage_when_finally_runs() -> None:
    """``on_finally`` runs after compression settles, so the observer sees the
    run's real spend rather than a pre-settlement partial sum."""
    calls: list[str] = []
    seen: list[dict[str, int]] = []

    class UsageCapturingHook(RecordingHook):
        async def on_finally(self, context: AgentRunHookContext) -> None:
            seen.append(dict(context.usage))
            await super().on_finally(context)

    provider = ScriptedProvider([LLMResponse(content="ok", usage={"prompt_tokens": 7, "completion_tokens": 3})])

    await AgentRunner(provider).run(make_spec(hook=UsageCapturingHook("h", calls)))

    assert seen == [{"prompt_tokens": 7, "completion_tokens": 3}]


async def test_finally_failure_does_not_mask_a_raised_exception() -> None:
    """A broken cleanup must not turn one failure into two: the original
    exception is what the caller sees."""
    calls: list[str] = []

    class FailingBeforeRunHook(RecordingHook):
        async def before_run(self, context: AgentRunHookContext) -> None:
            await super().before_run(context)
            raise RuntimeError("boom")

        async def on_finally(self, context: AgentRunHookContext) -> None:
            self.calls.append("h.on_finally")
            raise RuntimeError("h.on_finally failed")

    with pytest.raises(RuntimeError, match="boom"):
        await AgentRunner(ExplodingProvider()).run(
            make_spec(hook=FailingBeforeRunHook("h", calls))
        )

    assert calls == ["h.before_run", "h.on_error", "h.on_finally"]


async def test_finally_failure_does_not_mask_cancellation() -> None:
    calls: list[str] = []

    class FailingFinallyHook(RecordingHook):
        async def on_finally(self, context: AgentRunHookContext) -> None:
            self.calls.append("h.on_finally")
            raise RuntimeError("h.on_finally failed")

    hook = FailingFinallyHook("h", calls)

    with pytest.raises(asyncio.CancelledError):
        await AgentRunner(CancellingProvider()).run(make_spec(hook=hook))

    assert calls == ["h.before_run", "h.before_iteration", "h.on_finally"]


# ---------------------------------------------------------------------------
# CompositeHook fan-out
# ---------------------------------------------------------------------------


def run_context() -> AgentRunHookContext:
    return AgentRunHookContext(messages=[])


def hook_context() -> AgentHookContext:
    return AgentHookContext(iteration=0, messages=[])


async def test_composite_fans_out_run_methods_in_order() -> None:
    calls: list[str] = []
    composite = CompositeHook([RecordingHook("a", calls), RecordingHook("b", calls)])

    await composite.before_run(run_context())
    await composite.after_run(run_context())
    await composite.on_finally(run_context())

    assert calls == [
        "a.before_run",
        "b.before_run",
        "a.after_run",
        "b.after_run",
        "a.on_finally",
        "b.on_finally",
    ]


async def test_composite_isolates_an_ordinary_hook_failure() -> None:
    calls: list[str] = []
    composite = CompositeHook(
        [
            RecordingHook("bad", calls, fail="before_run"),
            RecordingHook("good", calls),
        ]
    )

    await composite.before_run(run_context())

    # The faulty observer neither stops the chain nor hides the next hook.
    assert calls == ["bad.before_run", "good.before_run"]


async def test_composite_propagates_a_delivery_hook_failure() -> None:
    """``reraise=True`` marks a delivery hook: its failures are the run's."""
    calls: list[str] = []
    delivery = RecordingHook("delivery", calls, fail="before_run")
    delivery._reraise = True
    composite = CompositeHook([delivery, RecordingHook("good", calls)])

    with pytest.raises(RuntimeError, match="delivery.before_run failed"):
        await composite.before_run(run_context())

    assert calls == ["delivery.before_run"]


async def test_composite_fans_out_the_per_tool_hooks() -> None:
    calls: list[str] = []
    composite = CompositeHook([RecordingHook("a", calls), RecordingHook("b", calls)])
    call = ToolCallRequest(id="c1", name="read_file", arguments={})

    await composite.before_execute_tool(hook_context(), call, None, {})
    await composite.after_execute_tool(hook_context(), call, None, {}, "ok")
    await composite.on_execute_tool_error(hook_context(), call, None, {}, ValueError("x"))

    assert calls == [
        "a.before_execute_tool",
        "b.before_execute_tool",
        "a.after_execute_tool",
        "b.after_execute_tool",
        "a.on_execute_tool_error",
        "b.on_execute_tool_error",
    ]


def test_composite_finalize_content_is_a_pipeline() -> None:
    """Cleaning composes in order; a buggy cleaner surfaces instead of being
    swallowed, because a silently dropped cleanup ships DSML to the user."""

    class SuffixHook(AgentHook):
        def __init__(self, suffix: str) -> None:
            super().__init__()
            self.suffix = suffix

        def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
            return f"{content}{self.suffix}"

    class BrokenHook(AgentHook):
        def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
            raise ValueError("broken cleaner")

    pipeline = CompositeHook([SuffixHook(" a"), SuffixHook(" b")])
    assert pipeline.finalize_content(hook_context(), "x") == "x a b"

    with pytest.raises(ValueError, match="broken cleaner"):
        CompositeHook([BrokenHook()]).finalize_content(hook_context(), "x")


def test_composite_resolve_final_content_takes_the_first_replacement() -> None:
    class ReplacingHook(AgentHook):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

        def resolve_final_content(
            self,
            context: AgentHookContext,
            content: str | None,
        ) -> FinalizeContentResult | None:
            return FinalizeContentResult(self.text, is_replaced=True)

    class ExplodingHook(AgentHook):
        def resolve_final_content(
            self,
            context: AgentHookContext,
            content: str | None,
        ) -> FinalizeContentResult | None:
            raise ValueError("bad replacement")

    # An ordinary hook's replacement failure is isolated; the next one wins.
    chain = CompositeHook([ReplacingHook("first"), ReplacingHook("second")])
    assert chain.resolve_final_content(hook_context(), "x").content == "first"

    tolerated = CompositeHook([ExplodingHook(), ReplacingHook("second")])
    assert tolerated.resolve_final_content(hook_context(), "x").content == "second"


def test_composite_propagates_a_delivery_hooks_replacement_failure() -> None:
    class ExplodingDeliveryHook(AgentHook):
        def __init__(self) -> None:
            super().__init__(reraise=True)

        def resolve_final_content(
            self,
            context: AgentHookContext,
            content: str | None,
        ) -> FinalizeContentResult | None:
            raise ValueError("delivery replacement failed")

    with pytest.raises(ValueError, match="delivery replacement failed"):
        CompositeHook([ExplodingDeliveryHook()]).resolve_final_content(hook_context(), "x")


def test_finalize_content_result_reports_cleaning_without_replacement() -> None:
    """DSML cleaning yields content; it is not an explicit replacement, so
    pending injections and terminal-tool retries still see a model answer."""
    result = finalize_content_result(AgentProgressHook(), hook_context(), "<think>x</think>hi")

    assert result.content == "hi"
    assert result.is_replaced is False


def test_finalize_content_result_prefers_an_explicit_replacement() -> None:
    class ReplacingHook(AgentHook):
        def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
            return "cleaned"

        def resolve_final_content(
            self,
            context: AgentHookContext,
            content: str | None,
        ) -> FinalizeContentResult | None:
            return FinalizeContentResult("report", is_replaced=True)

    result = finalize_content_result(ReplacingHook(), hook_context(), "raw")

    assert (result.content, result.is_replaced) == ("report", True)


# ---------------------------------------------------------------------------
# Turn hook builder
# ---------------------------------------------------------------------------


async def test_builder_assembles_in_the_fixed_order() -> None:
    order: list[str] = []
    context_holder: dict[str, AgentTurnHookContext] = {}

    def make_factory(name: str):
        def factory(context: AgentTurnHookContext) -> AgentHook:
            context_holder[name] = context
            return RecordingHook(name, order)

        return factory

    hook = build_agent_turn_hook(
        AgentTurnHookSpec(
            registered_hook_factories=[make_factory("registered_factory")],
            registered_hooks=[RecordingHook("registered_hook", order)],
            turn_hook_factories=[make_factory("turn_factory")],
            turn_hooks=[RecordingHook("turn_hook", order)],
        )
    )

    context = hook_context()
    await hook.before_run(run_context())
    await hook.before_iteration(context)

    # The progress hook is always first, then factories and hooks in spec order.
    assert order == [
        "registered_factory.before_run",
        "registered_hook.before_run",
        "turn_factory.before_run",
        "turn_hook.before_run",
        "registered_factory.before_iteration",
        "registered_hook.before_iteration",
        "turn_factory.before_iteration",
        "turn_hook.before_iteration",
    ]
    # Every factory sees the same turn context.
    assert context_holder["registered_factory"] is context_holder["turn_factory"]


async def test_builder_skips_a_failing_factory_and_keeps_the_rest() -> None:
    order: list[str] = []

    def broken_factory(context: AgentTurnHookContext) -> AgentHook:
        raise RuntimeError("factory exploded")

    hook = build_agent_turn_hook(
        AgentTurnHookSpec(
            registered_hook_factories=[broken_factory],
            turn_hooks=[RecordingHook("survivor", order)],
        )
    )

    await hook.before_iteration(hook_context())

    assert order == ["survivor.before_iteration"]


def test_builder_short_circuits_ephemeral_turns_to_the_progress_hook() -> None:
    """An ephemeral turn with no extra hooks is exactly the progress hook:
    there is nothing to compose, so no composite is allocated."""
    hook = build_agent_turn_hook(AgentTurnHookSpec(ephemeral=True))

    assert isinstance(hook, AgentProgressHook)


def test_builder_keeps_ephemeral_extra_hooks_when_asked() -> None:
    calls: list[str] = []
    hook = build_agent_turn_hook(
        AgentTurnHookSpec(
            ephemeral=True,
            run_extra_hooks_for_ephemeral=True,
            turn_hooks=[RecordingHook("observer", calls)],
        )
    )

    assert isinstance(hook, CompositeHook)


def test_builder_creates_independent_hook_instances_per_turn() -> None:
    """Stream buffers and file-edit trackers are turn-local: two turns in the
    same session must not share hook state."""
    first = build_agent_turn_hook(AgentTurnHookSpec())
    second = build_agent_turn_hook(AgentTurnHookSpec())

    assert first is not second


def test_every_runner_call_site_assembles_its_hook_through_the_builder() -> None:
    """A hand-rolled ``AgentHook`` at a call site would reintroduce the shared
    per-turn state this migration removed, so every spec that reaches
    ``AgentRunner.run`` must be built by the turn hook builder.
    """
    package = Path(hooks_module.__file__).resolve().parents[2]
    call_sites = (
        package / "agent" / "conversation_loop.py",
        package / "agent" / "review_loop.py",
        package / "agent" / "subagent.py",
        package / "review" / "output" / "judge.py",
    )
    for site in call_sites:
        text = site.read_text(encoding="utf-8")
        assert "build_agent_turn_hook(" in text, f"{site.name} bypasses the turn hook builder"
        assert "AgentTurnHookSpec(" in text, f"{site.name} bypasses the turn hook builder"


def test_builder_progress_hook_asks_for_streaming_only_with_a_consumer() -> None:
    without_consumer = build_agent_turn_hook(
        AgentTurnHookSpec(events=NO_EVENTS, streaming=True)
    )
    assert without_consumer.wants_streaming() is False

    published: list[Any] = []

    async def publish(event: Any) -> None:
        published.append(event)

    from nanoreview.events import EventSink

    with_consumer = build_agent_turn_hook(
        AgentTurnHookSpec(
            events=EventSink(publish=publish, accepts_type=lambda t: t is StreamDeltaEvent),
            streaming=True,
        )
    )
    assert with_consumer.wants_streaming() is True

    from nanoreview.events import EventSink as _EventSink

    progress_only = build_agent_turn_hook(
        AgentTurnHookSpec(
            events=_EventSink(publish=publish, accepts_type=lambda t: t is ProgressEvent),
            streaming=True,
        )
    )
    assert progress_only.wants_streaming() is False
