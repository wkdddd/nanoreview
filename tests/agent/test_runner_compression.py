"""Run-level compression tests for :class:`AgentRunner`.

Covers the plan's §12 verification matrix:

- §12.1 thresholds and the state machine (async at 60%, sync at 80%, stop
  reasons);
- §12.2 the frozen/working three-zone contract and that the *raw* run history is
  never rewritten;
- §12.3 compression usage accounting, including the usage callback used by the
  judge's outer timeout;
- §12.4 the first business request keeping today's behaviour.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nanoreview.agent.compression import (
    COMPRESSED_METADATA_KEY,
    AsyncSnapshot,
    RunCompressionState,
    has_usable_content,
    parse_and_validate,
    partition_working,
    soft_limit,
    split_units,
    sync_limit,
)
from nanoreview.agent.hooks.lifecycle import AgentHook, AgentHookContext
from nanoreview.agent.runner import AgentRunner, AgentRunSpec
from nanoreview.agent.tools.base import Tool
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanoreview.utils.helpers import estimate_message_tokens

# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

def summary_payload(**overrides: Any) -> dict[str, Any]:
    """A minimal valid summary object."""
    payload: dict[str, Any] = {
        "task_context": {
            "task": "review repository",
            "objective": "find defects",
            "focus": "auth",
        },
        "confirmed_conclusions": ["login flow is untested"],
        "evidence": [],
        "findings": [],
        "pending_tasks": ["review session handling"],
        "constraints_and_availability": {
            "constraints": [],
            "evidence_availability": [],
        },
    }
    payload.update(overrides)
    return payload


def make_spec(**overrides: Any) -> AgentRunSpec:
    values: dict[str, Any] = {
        "frozen_messages": [],
        "working_messages": [],
        "tools": ToolRegistry(),
        "model": "dummy",
        "max_iterations": 3,
        "max_tool_result_chars": 4000,
    }
    values.update(overrides)
    return AgentRunSpec(**values)


def filler(count: int, chars: int = 2000, *, prefix: str = "filler") -> list[dict[str, Any]]:
    """Build *count* messages whose text is large enough to trip thresholds."""
    body = "x" * chars
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"{prefix}-{index} {body}"}
        for index in range(count)
    ]


def tokens_for(messages: list[dict[str, Any]]) -> int:
    """Mirror ScriptedProvider.estimate_prompt_tokens for per-message totals."""
    return sum(estimate_message_tokens(message) for message in messages)


def done_future(value: Any) -> asyncio.Future[Any]:
    """An already-resolved future standing in for a finished compression task."""
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


def failed_future(exc: BaseException) -> asyncio.Future[Any]:
    """An already-failed future standing in for a failed compression task."""
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(exc)
    return future


def compression_state(
    frozen: list[dict[str, Any]],
    working: list[dict[str, Any]],
    *,
    context_window_tokens: int | None = None,
) -> RunCompressionState:
    """A run-local compression state mid-run.

    There is no longer a "first request" flag: ``_model_context`` always builds
    ``frozen + governed working``, and ``_apply_run_compression`` is only ever
    called from the second request on. ``working_revision`` starts at the number
    of messages already appended so a snapshot taken here can still be matched.
    """
    state = RunCompressionState(
        frozen=[dict(message) for message in frozen],
        working=[dict(message) for message in working],
        context_window_tokens=context_window_tokens,
    )
    state.working_revision = len(working)
    return state


def summary_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every synthetic summary currently present in *messages*."""
    return [
        message
        for message in messages
        if message.get("_metadata", {}).get(COMPRESSED_METADATA_KEY)
    ]


class NoopTool(Tool):
    """A tool that succeeds immediately, so a run can span several iterations."""

    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "No-op tool used to keep a run going."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return "noop result"


def tool_call(call_id: str = "call_1", name: str = "noop") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments={})


def tool_turn(
    call_id: str = "call_1",
    name: str = "noop",
    *,
    usage: dict[str, int] | None = None,
) -> LLMResponse:
    """A response that executes one tool round, keeping the run alive."""
    return LLMResponse(content="", tool_calls=[tool_call(call_id, name)], usage=usage)


class BigTool(Tool):
    """A tool returning a large, poorly-compressible payload.

    A run of repeated characters tokenizes very cheaply, so the payload is
    varied to really grow the working zone by a realistic amount per round.
    """

    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "Returns a large payload to grow the working zone."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return " ".join(f"line-{index:05d}" for index in range(600))


def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(NoopTool())
    return registry


def big_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(BigTool())
    return registry


def multi_iteration_spec(**overrides: Any) -> AgentRunSpec:
    """A spec whose registry contains a working tool, for >1 iteration runs."""
    overrides.setdefault("tools", tool_registry())
    return make_spec(**overrides)


class ScriptedProvider(LLMProvider):
    """Provider that answers business and compression requests from scripts.

    Business requests (``tools`` is not ``None``) pop from ``business``; the
    compression request is identified by ``tools=None`` and answered from
    ``compression``. When the compression script is exhausted the last entry is
    reused so tests do not depend on exact request counts.
    """

    def __init__(
        self,
        *,
        business: list[LLMResponse] | None = None,
        compression: list[LLMResponse] | None = None,
    ) -> None:
        super().__init__()
        self.business = list(business or [LLMResponse(content="done")])
        self.compression = list(compression or [])
        self.business_requests: list[list[dict[str, Any]]] = []
        self.compression_requests: list[list[dict[str, Any]]] = []
        self.compression_kwargs: list[dict[str, Any]] = []

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
        if tools is None:
            self.compression_requests.append([dict(message) for message in messages])
            self.compression_kwargs.append(
                {
                    "response_format": response_format,
                    "max_tokens": max_tokens,
                    "model": model,
                }
            )
            if self.compression:
                if len(self.compression) == 1:
                    return self.compression[0]
                return self.compression.pop(0)
            return LLMResponse(content=json.dumps(summary_payload()))
        self.business_requests.append([dict(message) for message in messages])
        if self.business:
            if len(self.business) == 1:
                return self.business[0]
            return self.business.pop(0)
        return LLMResponse(content="done")

    def get_default_model(self) -> str:
        return "dummy"

    def estimate_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> tuple[int, str]:
        """Deterministic counter aligned with ``estimate_message_tokens``.

        ``estimate_message_tokens`` is the estimator the partitioner uses, so
        matching it keeps the soft/sync thresholds and the compress-zone budget
        on the same scale in tests.
        """
        return sum(estimate_message_tokens(m) for m in messages), "test_counter"


class BlockingCompressionProvider(ScriptedProvider):
    """Provider whose compression request blocks until cancelled."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        #: Set when the blocked compression request was cancelled.
        self.cancelled = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if tools is None:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return await super().chat(messages, tools, **kwargs)


class TaggedProvider(ScriptedProvider):
    """Keeps each run alive until *its own* history was summarized.

    Business answers are derived from the request content instead of a shared
    script cursor, so two interleaved ``run()`` calls on one runner can be
    driven concurrently without stealing each other's scripted responses.
    """

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if tools is None:
            return await super().chat(messages, tools, **kwargs)
        self.business_requests.append([dict(message) for message in messages])
        blob = json.dumps(messages, ensure_ascii=False)
        if "<compressed_context>" in blob:
            return LLMResponse(content="done")
        return tool_turn(f"call_{len(self.business_requests)}")


class MutatingProvider(ScriptedProvider):
    """Provider that rewrites the request it is handed, in place.

    Real provider/proxy chains do mutate the message list they receive (image
    stripping, argument normalization). The runner must hand out a private deep
    copy so a mutation can never reach the persisted raw history or the
    compression snapshot.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.mutations = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self._mutate(messages)
        return await super().chat(messages, tools, **kwargs)

    def _mutate(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        block["text"] = "MUTATED"
                        self.mutations += 1
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    call["id"] = "MUTATED"
                    function = call.get("function")
                    if isinstance(function, dict):
                        function["arguments"] = "MUTATED"
                    self.mutations += 1
            meta = message.get("_metadata")
            if isinstance(meta, dict):
                meta["mutated"] = True
                self.mutations += 1


# --------------------------------------------------------------------------- #
# §12.1 Thresholds and state machine
# --------------------------------------------------------------------------- #

def test_soft_and_sync_limits_are_floor_of_ratios() -> None:
    assert soft_limit(65536) == 39321
    assert sync_limit(65536) == 52428
    assert soft_limit(131072) == 78643
    assert sync_limit(131072) == 104857
    assert soft_limit(200000) == 120000
    assert sync_limit(200000) == 160000


def test_limits_are_none_for_unusable_window() -> None:
    for window in (None, 0, -1):
        assert soft_limit(window) is None
        assert sync_limit(window) is None


@pytest.mark.asyncio
async def test_first_request_skips_run_level_compression() -> None:
    """§12.4: the very first business request is never compressed."""
    provider = ScriptedProvider(business=[LLMResponse(content="done")])
    runner = AgentRunner(provider)
    spec = make_spec(
        frozen_messages=[{"role": "system", "content": "sys"}],
        working_messages=filler(20, chars=4000),
        context_window_tokens=1000,  # tiny window: limits far below the request
        max_iterations=1,
    )

    result = await runner.run(spec)

    assert result.stop_reason == "completed"
    assert provider.compression_requests == []
    assert len(provider.business_requests) == 1
    # Frozen envelope stayed verbatim at the head of the request.
    assert provider.business_requests[0][0] == {"role": "system", "content": "sys"}


class YieldingHook(AgentHook):
    """Hook that yields to the event loop, letting async compression run.

    ``AgentRunner`` starts run-level compression as a background task; nothing
    in the loop awaits it, so in a test the task only progresses when the loop
    is given control. Real runs spend that time in provider I/O.
    """

    def __init__(self, cycles: int = 8) -> None:
        self.cycles = cycles

    async def before_iteration(self, context: AgentHookContext) -> None:
        for _ in range(self.cycles):
            await asyncio.sleep(0)


class YieldingBlockingProvider(BlockingCompressionProvider):
    """Blocks inside compression while still yielding once so ``entered`` fires."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if tools is None:
            self.entered.set()
            # Yield so the test coroutine observes ``entered`` before blocking.
            for _ in range(4):
                await asyncio.sleep(0)
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return await ScriptedProvider.chat(self, messages, tools, **kwargs)


@pytest.mark.asyncio
async def test_async_compression_starts_above_soft_limit() -> None:
    """60% zone: a background job is started and applied on a later request."""
    provider = ScriptedProvider(
        business=[
            tool_turn("c1"),
            tool_turn("c2"),
            tool_turn("c3"),
            LLMResponse(content="finished"),
        ],
        compression=[
            LLMResponse(
                content=json.dumps(summary_payload(confirmed_conclusions=["summarized"]))
            )
        ],
    )
    runner = AgentRunner(provider)
    # Window chosen so the rebuilt request lands in the 60%..80% soft zone with
    # a legal compress zone.
    working = filler(20, chars=4000)
    spec = multi_iteration_spec(
        frozen_messages=[{"role": "system", "content": "sys"}],
        working_messages=working,
        context_window_tokens=15000,
        context_block_limit=20000,
        max_iterations=4,
        hook=YieldingHook(),
    )

    result = await runner.run(spec)

    assert result.stop_reason == "completed"
    assert len(provider.compression_requests) == 1
    # A later business request carries the synthetic summary instead of the
    # full original history, and the frozen envelope stays at its head.
    compressed_requests = [
        request
        for request in provider.business_requests
        if any("<compressed_context>" in str(m.get("content")) for m in request)
    ]
    assert compressed_requests, "no business request received the summary"
    assert compressed_requests[-1][0] == {"role": "system", "content": "sys"}
    # The rebuild is frozen + summary + active + suffix: it must be cheaper in
    # tokens than the uncompressed request while still ending with the tool
    # rounds appended after the snapshot.
    assert tokens_for(compressed_requests[-1]) < tokens_for(
        provider.business_requests[0]
    )
    assert compressed_requests[-1][-1]["role"] == "tool"
    # Compression request is JSON-mode without business tools.
    assert provider.compression_kwargs[0]["response_format"] == {"type": "json_object"}
    assert provider.compression_kwargs[0]["max_tokens"] > 0


@pytest.mark.asyncio
async def test_sync_compression_failure_stops_run() -> None:
    """§12.1: a failed sync compression stops the run with compression_failed."""
    provider = ScriptedProvider(
        business=[tool_turn("c1"), tool_turn("c2"), LLMResponse(content="finished")],
        compression=[LLMResponse(content="not json at all")],
    )
    runner = AgentRunner(provider)
    working = filler(20, chars=4000)
    spec = multi_iteration_spec(
        frozen_messages=[{"role": "system", "content": "sys"}],
        working_messages=working,
        context_window_tokens=11000,
        # Keep the hard-trim ceiling high so the request stays oversized and the
        # sync path is exercised instead of being silently snipped down.
        context_block_limit=20000,
        max_iterations=3,
        hook=YieldingHook(),
    )

    result = await runner.run(spec)

    assert result.stop_reason == "compression_failed"
    assert result.error  # non-empty by contract
    assert "compression" in result.error.lower()
    # Only the first business request went out; the run stopped before the second.
    assert len(provider.business_requests) == 1
    # Both compression attempts were made (first attempt + one retry).
    assert len(provider.compression_requests) == 2


@pytest.mark.asyncio
async def test_compression_limit_when_the_newest_unit_alone_is_oversized() -> None:
    """§12.1: sync succeeded but the rebuilt request is still >= sync limit.

    Driven through the state machine directly: context governance (microcompact
    + hard trim) would otherwise shrink the oversized tail before the count, and
    the point of this test is the compression contract, not governance.
    """
    provider = ScriptedProvider(
        compression=[LLMResponse(content=json.dumps(summary_payload()))]
    )
    runner = AgentRunner(provider)
    working = [
        {"role": "user", "content": "small " + "a" * 400},
        {"role": "assistant", "content": "small reply"},
        # Newest unit alone is over the sync limit, so it is kept whole and the
        # compressible prefix cannot bring the request back under the limit.
        {"role": "user", "content": "huge " + "z" * 120000},
    ]
    spec = make_spec(
        frozen_messages=[{"role": "system", "content": "sys"}],
        working_messages=working,
        context_window_tokens=12000,
        max_iterations=3,
    )
    state = RunCompressionState(
        frozen=[dict(m) for m in spec.frozen_messages],
        working=[dict(m) for m in working],
        context_window_tokens=spec.context_window_tokens,
    )
    state.working_revision = len(working)

    outcome = await runner._apply_run_compression(spec, state)

    assert outcome == "stop"
    assert state.stopped_reason == "compression_limit"
    assert state.stopped_error  # non-empty by contract
    # The summary was produced and applied, then rejected for being too small.
    assert len(provider.compression_requests) == 1


@pytest.mark.asyncio
async def test_compression_stop_is_reported_through_the_run_result() -> None:
    """A compression stop ends the run and carries a non-empty error."""
    provider = ScriptedProvider(
        business=[tool_turn("c1"), LLMResponse(content="finished")],
        compression=[LLMResponse(content="not json at all")],
    )
    runner = AgentRunner(provider)
    working = filler(20, chars=4000)
    spec = multi_iteration_spec(
        frozen_messages=[{"role": "system", "content": "sys"}],
        working_messages=working,
        context_window_tokens=11000,
        context_block_limit=20000,
        max_iterations=3,
        hook=YieldingHook(),
    )

    result = await runner.run(spec)

    assert result.stop_reason == "compression_failed"
    assert result.error
    # The run stopped before issuing another business request.
    assert len(provider.business_requests) == 1


# --------------------------------------------------------------------------- #
# §12.2 Three-zone contract and raw-history immutability
# --------------------------------------------------------------------------- #

def test_partition_working_keeps_newest_units_whole() -> None:
    working = [
        {"role": "user", "content": "u0 " + "x" * 4000},
        {"role": "assistant", "content": "a0 " + "x" * 4000},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    partition = partition_working(
        working, keep_budget_tokens=200, estimate=lambda m: len(str(m.get("content"))) // 4
    )
    # Only the newest units (u1, a1, ~3 tokens) fit in the 200-token budget, so
    # the two large older messages form the compress zone.
    assert partition.compress is True
    assert partition.active_prefix == working[2:]
    assert partition.compress_prefix == working[:2]
    # The compress zone is a legal whole-unit prefix: newest-first, never a
    # half of a tool round.
    assert all(message in working for message in partition.compress_prefix)
    # The two zones tile the input exactly, with no gap and no overlap.
    assert partition.compress_prefix + partition.active_prefix == working


def test_partition_marks_no_compress_zone_for_leading_open_unit() -> None:
    """A leading assistant/tool run is compressible, but a tail-only list is not."""
    working = [
        {"role": "user", "content": "only " + "x" * 4000},
        {"role": "assistant", "content": "reply " + "x" * 4000},
    ]
    partition = partition_working(
        working, keep_budget_tokens=1, estimate=lambda m: len(str(m.get("content"))) // 4
    )
    # The newest (and only) unit always stays active, so nothing is compressible.
    assert partition.compress is False
    assert partition.compress_prefix == []
    assert partition.active_prefix == working


def test_partition_keeps_oversized_newest_unit_whole() -> None:
    """The newest unit is never split, even when it alone blows the budget."""
    working = [
        {"role": "user", "content": "small " + "a" * 400},
        {"role": "assistant", "content": "small reply"},
        {"role": "user", "content": "huge " + "z" * 40000},
        {"role": "assistant", "content": "huge reply " + "z" * 40000},
    ]
    partition = partition_working(
        working,
        keep_budget_tokens=10,
        estimate=lambda m: len(str(m.get("content"))) // 4,
    )
    assert partition.compress is True
    # The oversized newest unit is kept in full instead of being split.
    assert partition.active_prefix == working[2:]
    assert partition.compress_prefix == working[:2]


def test_split_units_groups_user_with_direct_assistant_only() -> None:
    """A user turn groups with the *one* assistant response directly after it."""
    messages = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "assistant", "content": "a0b"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    units = split_units(messages)
    # u0 + its direct reply is one unit; a second assistant response that does
    # not directly follow a user is a plain unit of its own; u1 + a1 closes.
    assert [len(unit) for unit in units] == [2, 1, 2]
    assert units[0] == messages[:2]
    assert units[1] == messages[2:3]
    assert units[2] == messages[3:]


def test_split_units_keeps_injection_and_tool_round_together() -> None:
    """An injected user turn and the tool round it triggers stay one unit."""
    messages = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "injection"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "data"},
        {"role": "assistant", "content": "summary of the tool round"},
    ]
    units = split_units(messages)
    assert [len(unit) for unit in units] == [2, 3, 1]
    # The injection, the tool call it triggered and the tool result are never
    # separated from one another; the assistant wrap-up is its own unit.
    assert units[1] == messages[2:5]
    assert units[2] == messages[5:]


def test_split_units_keeps_terminal_retry_prompt_with_its_round() -> None:
    """A terminal retry prompt is an injection; the round it triggers stays whole."""
    prompt = {"role": "user", "content": "You did not call submit_verdicts. Call it now."}
    messages = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "prose instead of a tool call"},
        prompt,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c9",
                    "type": "function",
                    "function": {"name": "submit_verdicts", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c9", "name": "submit_verdicts", "content": "ok"},
    ]
    units = split_units(messages)
    assert [len(unit) for unit in units] == [2, 3]
    assert units[1][0] is prompt
    assert prompt["content"] in str(units[1])


def test_split_units_keeps_dangling_user_and_open_tool_round() -> None:
    """Dangling units are kept whole instead of being trimmed into shape."""
    dangling_tool_round = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
    ]
    dangling_user = {"role": "user", "content": "still waiting for an answer"}
    units = split_units([*dangling_tool_round, {"role": "user", "content": "u0"}, dangling_user])
    assert len(units) == 3
    # Open tool round with no preceding user: its own unit.
    assert units[0] == dangling_tool_round
    # Trailing unanswered user turn: its own unit, kept whole.
    assert units[-1] == [dangling_user]


def test_split_units_keeps_tool_round_together() -> None:
    call = ToolCallRequest(id="c1", name="read_file", arguments={})
    messages = [
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "data"},
    ]
    units = split_units(messages)
    assert len(units) == 1
    assert len(units[0]) == 3  # user turn + assistant tool call + its result
    assert call.name == "read_file"


@pytest.mark.asyncio
async def test_raw_run_history_is_never_rewritten_by_compression() -> None:
    """The frozen envelope and the original working history stay in the result."""
    frozen = [{"role": "system", "content": "sys"}]
    working = filler(20, chars=4000)
    provider = ScriptedProvider(
        business=[
            tool_turn("c1"),
            tool_turn("c2"),
            tool_turn("c3"),
            LLMResponse(content="finished"),
        ],
        compression=[LLMResponse(content=json.dumps(summary_payload()))],
    )
    runner = AgentRunner(provider)

    result = await runner.run(
        multi_iteration_spec(
            frozen_messages=frozen,
            working_messages=working,
            context_window_tokens=15000,
            context_block_limit=20000,
            max_iterations=4,
            hook=YieldingHook(),
        )
    )

    # Compression really happened: a later business request carried a summary.
    assert provider.compression_requests, "compression never ran"
    # Raw history: frozen + every original working message, with no synthetic
    # summary injected into the persisted list.
    assert result.messages[: len(frozen)] == frozen
    assert result.messages[len(frozen) : len(frozen) + len(working)] == working
    assert not any(
        m.get("_metadata", {}).get(COMPRESSED_METADATA_KEY) for m in result.messages
    )
    # The model context, in contrast, did carry a summary.
    assert any(
        "<compressed_context>" in str(m.get("content"))
        for request in provider.business_requests
        for m in request
    )


# --------------------------------------------------------------------------- #
# §12.2b Async apply contract: active zone, suffix, mismatch, no accumulation
# --------------------------------------------------------------------------- #

def _snapshot(
    frozen: list[dict[str, Any]],
    working: list[dict[str, Any]],
    *,
    compress_count: int,
    revision: int = 0,
) -> AsyncSnapshot:
    """A snapshot whose active zone is everything after *compress_count*."""
    return AsyncSnapshot(
        frozen_prefix=[dict(message) for message in frozen],
        working_prefix=[dict(message) for message in working],
        compress_prefix=[dict(message) for message in working[:compress_count]],
        active_prefix=[dict(message) for message in working[compress_count:]],
        working_revision=revision,
    )


@pytest.mark.asyncio
async def test_async_apply_keeps_active_zone_and_appends_suffix() -> None:
    """The rebuild is summary + snapshot active zone + suffix, never suffix only."""
    runner = AgentRunner(ScriptedProvider())
    frozen = [{"role": "system", "content": "sys"}]
    working = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "active question"},
        {"role": "assistant", "content": "active answer"},
    ]
    state = compression_state(frozen, working)
    state.snapshot = _snapshot(frozen, working, compress_count=2)
    state.pending = done_future(summary_payload())
    # Appended while the background job was in flight.
    suffix = [
        {"role": "user", "content": "later question"},
        {"role": "assistant", "content": "later answer"},
    ]
    state.working = [*state.working, *suffix]

    applied = await runner._collect_async_result(make_spec(), state)

    assert applied is True
    summaries = summary_messages(state.working)
    assert len(summaries) == 1
    # Active zone intact, suffix still last: no active message was lost.
    assert state.working[0] is summaries[0]
    assert state.working[1:] == [*working[2:], *suffix]


@pytest.mark.asyncio
async def test_async_apply_is_discarded_on_frozen_mismatch() -> None:
    """A rewritten frozen prefix invalidates the job; nothing is written back."""
    runner = AgentRunner(ScriptedProvider())
    frozen = [{"role": "system", "content": "sys"}]
    working = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
    ]
    state = compression_state(frozen, working)
    state.snapshot = _snapshot(frozen, working, compress_count=1)
    state.pending = done_future(summary_payload())
    original = [dict(message) for message in state.working]
    state.frozen = [{"role": "system", "content": "sys rewritten"}]

    applied = await runner._collect_async_result(make_spec(), state)

    assert applied is False
    assert state.working == original
    assert summary_messages(state.working) == []


@pytest.mark.asyncio
async def test_async_apply_is_discarded_on_working_mismatch() -> None:
    """A rewritten working prefix also invalidates the job."""
    runner = AgentRunner(ScriptedProvider())
    frozen = [{"role": "system", "content": "sys"}]
    working = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
    ]
    state = compression_state(frozen, working)
    state.snapshot = _snapshot(frozen, working, compress_count=1)
    state.pending = done_future(summary_payload())
    state.working[0] = {"role": "user", "content": "u0 rewritten"}

    applied = await runner._collect_async_result(make_spec(), state)

    assert applied is False
    assert summary_messages(state.working) == []


@pytest.mark.asyncio
async def test_async_apply_replaces_the_previous_summary() -> None:
    """A second summary replaces the first instead of stacking up."""
    runner = AgentRunner(ScriptedProvider())
    frozen = [{"role": "system", "content": "sys"}]
    state = compression_state(frozen, [{"role": "user", "content": "u0"}])

    for conclusion in ("first pass", "second pass"):
        working_prefix = [dict(message) for message in state.working]
        state.snapshot = AsyncSnapshot(
            frozen_prefix=[dict(message) for message in state.frozen],
            working_prefix=working_prefix,
            compress_prefix=working_prefix,
            active_prefix=[],
        )
        state.pending = done_future(
            summary_payload(confirmed_conclusions=[conclusion])
        )
        assert await runner._collect_async_result(make_spec(), state) is True

        summaries = summary_messages(state.working)
        assert len(summaries) == 1
        assert conclusion in summaries[0]["content"]

    # Exactly one synthetic summary remains after two compression rounds.
    assert len(state.working) == 1


@pytest.mark.asyncio
async def test_async_apply_does_not_reenter_sync_in_the_same_step() -> None:
    """Recounting is deferred to the next request, so sync is not re-entered."""
    provider = ScriptedProvider(
        compression=[LLMResponse(content=json.dumps(summary_payload()))]
    )
    runner = AgentRunner(provider)
    frozen = [{"role": "system", "content": "sys"}]
    # Well above the sync limit even before the summary is applied.
    working = [
        {"role": "user", "content": "u0 " + "x" * 40000},
        {"role": "assistant", "content": "a0 " + "x" * 40000},
    ]
    spec = make_spec(
        frozen_messages=frozen,
        working_messages=working,
        context_window_tokens=12000,
        context_block_limit=20000,
        max_iterations=3,
    )
    state = compression_state(frozen, working, context_window_tokens=12000)
    state.snapshot = _snapshot(frozen, working, compress_count=1)
    state.pending = done_future(summary_payload())

    outcome = await runner._apply_run_compression(spec, state)

    # The summary landed ...
    assert summary_messages(state.working)
    # ... and the same step did not fall through into sync compression.
    assert outcome == "continue"
    assert state.stopped is False
    assert provider.compression_requests == []


# --------------------------------------------------------------------------- #
# §12.2c Compression request generation defaults
# --------------------------------------------------------------------------- #

def test_compression_kwargs_omit_max_tokens_to_use_provider_default() -> None:
    """No hard-coded cap: the provider generation default applies when unset."""
    runner = AgentRunner(ScriptedProvider())
    kwargs = runner._build_compression_kwargs(make_spec(max_tokens=None), [])
    assert "max_tokens" not in kwargs
    assert kwargs["tools"] is None
    assert kwargs["response_format"] == {"type": "json_object"}


def test_compression_kwargs_reuse_spec_max_tokens_when_set() -> None:
    runner = AgentRunner(ScriptedProvider())
    kwargs = runner._build_compression_kwargs(make_spec(max_tokens=512), [])
    assert kwargs["max_tokens"] == 512


# --------------------------------------------------------------------------- #
# §12.3 Usage accounting
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_compression_usage_counts_into_run() -> None:
    provider = ScriptedProvider(
        business=[
            tool_turn("c1", usage={"prompt_tokens": 10, "completion_tokens": 5}),
            tool_turn("c2", usage={"prompt_tokens": 10, "completion_tokens": 5}),
            tool_turn("c3", usage={"prompt_tokens": 10, "completion_tokens": 5}),
            LLMResponse(
                content="finished",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            ),
        ],
        compression=[
            LLMResponse(
                content=json.dumps(summary_payload()),
                usage={"prompt_tokens": 100, "completion_tokens": 50},
            )
        ],
    )
    runner = AgentRunner(provider)
    working = filler(20, chars=4000)

    result = await runner.run(
        multi_iteration_spec(
            frozen_messages=[{"role": "system", "content": "sys"}],
            working_messages=working,
            context_window_tokens=15000,
            context_block_limit=20000,
            max_iterations=4,
            hook=YieldingHook(),
        )
    )

    # 4 business requests x 10 prompt tokens + 1 compression request x 100.
    assert result.usage["prompt_tokens"] == 140
    assert result.usage["completion_tokens"] == 70


@pytest.mark.asyncio
async def test_compression_usage_callback_receives_each_attempt() -> None:
    """The judge relies on this to keep usage when its own timeout fires."""
    recorded: list[dict[str, int]] = []
    provider = ScriptedProvider(
        business=[
            tool_turn("c1"),
            tool_turn("c2"),
            tool_turn("c3"),
            LLMResponse(content="finished"),
        ],
        compression=[
            LLMResponse(
                content=json.dumps(summary_payload()),
                usage={"prompt_tokens": 7, "completion_tokens": 3},
            )
        ],
    )
    runner = AgentRunner(provider)
    working = filler(20, chars=4000)

    await runner.run(
        multi_iteration_spec(
            frozen_messages=[{"role": "system", "content": "sys"}],
            working_messages=working,
            context_window_tokens=15000,
            context_block_limit=20000,
            max_iterations=4,
            hook=YieldingHook(),
            compression_usage_callback=recorded.append,
        )
    )

    assert recorded == [{"prompt_tokens": 7, "completion_tokens": 3}]


@pytest.mark.asyncio
async def test_compression_usage_callback_exception_is_not_fatal() -> None:
    def boom(_usage: dict[str, int]) -> None:
        raise RuntimeError("callback exploded")

    provider = ScriptedProvider(
        business=[
            tool_turn("c1"),
            tool_turn("c2"),
            tool_turn("c3"),
            LLMResponse(content="finished"),
        ],
        compression=[LLMResponse(content=json.dumps(summary_payload()))],
    )
    runner = AgentRunner(provider)
    working = filler(20, chars=4000)

    result = await runner.run(
        multi_iteration_spec(
            frozen_messages=[{"role": "system", "content": "sys"}],
            working_messages=working,
            context_window_tokens=15000,
            context_block_limit=20000,
            max_iterations=4,
            hook=YieldingHook(),
            compression_usage_callback=boom,
        )
    )

    assert result.stop_reason == "completed"


# --------------------------------------------------------------------------- #
# §12.4 Protocol validation
# --------------------------------------------------------------------------- #

def test_parse_and_validate_accepts_canonical_summary() -> None:
    payload = parse_and_validate(json.dumps(summary_payload()))
    assert payload["task_context"]["task"] == "review repository"


def test_parse_and_validate_drops_extra_fields() -> None:
    raw = json.dumps(summary_payload(invented_field="kept out"))
    payload = parse_and_validate(raw)
    assert "invented_field" not in payload


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[1, 2, 3]",
        json.dumps({"task_context": {"task": "only"}}),
        json.dumps(
            {
                "task_context": {"task": "", "objective": "", "focus": ""},
                "confirmed_conclusions": [],
                "evidence": [],
                "findings": [],
                "pending_tasks": [],
                "constraints_and_availability": {
                    "constraints": [],
                    "evidence_availability": [],
                },
            }
        ),
    ],
)
def test_parse_and_validate_rejects_bad_summaries(raw: str) -> None:
    from nanoreview.agent.compression import CompressionError

    with pytest.raises(CompressionError):
        parse_and_validate(raw)


def test_has_usable_content_reports_canonical_availability() -> None:
    """Only canonical fields decide whether a summary is usable."""
    assert has_usable_content(summary_payload()) is True
    assert has_usable_content({"task_context": {"task": "x"}}) is True


# --------------------------------------------------------------------------- #
# §12.5 Lifecycle safety
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_pending_async_compression_is_settled_when_the_run_ends() -> None:
    """Run cleanup cancels and awaits an in-flight compression task."""
    provider = YieldingBlockingProvider(
        business=[
            tool_turn("c1"),
            tool_turn("c2"),
            LLMResponse(content="finished"),
        ]
    )
    runner = AgentRunner(provider)
    working = filler(20, chars=4000)
    spec = multi_iteration_spec(
        frozen_messages=[{"role": "system", "content": "sys"}],
        working_messages=working,
        context_window_tokens=15000,
        context_block_limit=20000,
        max_iterations=2,
        hook=YieldingHook(),
    )

    run_task = asyncio.ensure_future(runner.run(spec))
    # The background compression request is now blocked inside the provider.
    await asyncio.wait_for(provider.entered.wait(), timeout=5)
    # The run reaches max_iterations and ends while compression is still in
    # flight; cleanup must cancel and await it rather than leaving it dangling.
    result = await asyncio.wait_for(run_task, timeout=5)

    assert result.stop_reason == "max_iterations"
    assert provider.cancelled is True


@pytest.mark.asyncio
async def test_concurrent_runs_keep_compression_state_isolated() -> None:
    """Two runs on one runner compress their own history and nobody else's."""
    provider = TaggedProvider()
    runner = AgentRunner(provider)
    specs = []
    for tag in ("alpha", "beta"):
        specs.append(
            multi_iteration_spec(
                frozen_messages=[{"role": "system", "content": f"sys-{tag}"}],
                working_messages=[
                    {
                        "role": "user" if index % 2 == 0 else "assistant",
                        "content": f"{tag}-{index} " + "x" * 4000,
                    }
                    for index in range(20)
                ],
                context_window_tokens=15000,
                context_block_limit=20000,
                max_iterations=6,
                hook=YieldingHook(),
            )
        )

    results = await asyncio.gather(*(runner.run(spec) for spec in specs))

    assert [result.stop_reason for result in results] == ["completed", "completed"]
    for tag in ("alpha", "beta"):
        other = "beta" if tag == "alpha" else "alpha"
        own = [
            request
            for request in provider.business_requests
            if request[0].get("content") == f"sys-{tag}"
        ]
        assert own, f"run {tag} issued no business request"
        # Each run compressed on its own schedule ...
        assert any(
            "<compressed_context>" in str(message.get("content"))
            for message in own[-1]
        ), f"run {tag} never received its own summary"
        # ... and never replayed the other run's history.
        assert not any(
            f"{other}-" in str(message.get("content")) for message in own[-1]
        )


# --------------------------------------------------------------------------- #
# §12.6 Frozen ownership and governance boundaries
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_first_request_protects_frozen_and_trims_only_working() -> None:
    """§12.4: frozen enters verbatim; governance only ever shrinks working."""
    frozen = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "current task: review auth"},
        {"role": "user", "content": "evidence: app.py:1 dead code"},
    ]
    working = filler(30, chars=4000)
    provider = ScriptedProvider(business=[LLMResponse(content="done")])
    runner = AgentRunner(provider)
    spec = make_spec(
        frozen_messages=frozen,
        working_messages=working,
        context_window_tokens=8000,
        # Tiny explicit block ceiling so the working zone must be snipped.
        context_block_limit=3000,
        max_iterations=1,
    )

    result = await runner.run(spec)

    assert result.stop_reason == "completed"
    request = provider.business_requests[0]
    # Frozen envelope is intact, in order and verbatim at the head of the
    # request: system, current task and evidence are never summarized or cut.
    assert request[: len(frozen)] == frozen
    # Only the working zone shrank; the original raw history is untouched.
    working_in_request = request[len(frozen) :]
    assert len(working_in_request) < len(working)
    assert result.messages[: len(frozen)] == frozen


@pytest.mark.asyncio
async def test_governance_never_pairs_a_tool_round_across_the_frozen_boundary() -> None:
    """Governance runs over working only; it cannot repair a cross-boundary pair.

    Frozen is the task envelope and never holds a live tool round in practice,
    so a tool result that only matches a tool call inside frozen is *not* re-paired
    by the working-only governance pipeline. This pins the boundary: the frozen
    messages stay byte-identical and no synthetic message is injected into them.
    """
    frozen = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
    ]
    working = [
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "data"},
        {"role": "assistant", "content": "plain answer"},
    ]
    provider = ScriptedProvider(business=[LLMResponse(content="done")])
    runner = AgentRunner(provider)
    spec = make_spec(
        frozen_messages=frozen,
        working_messages=working,
        max_iterations=1,
    )

    await runner.run(spec)

    request = provider.business_requests[0]
    # Frozen is byte-identical, including the still-open tool call.
    assert request[: len(frozen)] == frozen
    # The working-only governance dropped the orphan result (it was not paired
    # with frozen's call) rather than reaching across the boundary.
    assert request[len(frozen) :] == [{"role": "assistant", "content": "plain answer"}]


# --------------------------------------------------------------------------- #
# §12.7 Deep-copy isolation between raw history, working zone and snapshot
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_provider_mutation_never_reaches_raw_history() -> None:
    """A provider that rewrites its request cannot corrupt the persisted run."""
    frozen = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "review this image"}],
            "_metadata": {"marker": "frozen"},
        },
    ]
    working = [
        {"role": "user", "content": [{"type": "text", "text": "old task"}]},
        {
            "role": "assistant",
            "content": "old answer",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "noop", "arguments": "{}"},
                }
            ],
            "_metadata": {"marker": "working"},
        },
    ]
    provider = MutatingProvider(business=[LLMResponse(content="done")])
    runner = AgentRunner(provider)
    spec = make_spec(
        frozen_messages=frozen,
        working_messages=working,
        max_iterations=1,
    )

    result = await runner.run(spec)

    # The provider really did rewrite the request it received.
    assert provider.mutations > 0
    # ... yet the raw history is byte-identical to the caller's input.
    assert result.messages[: len(frozen)] == frozen
    assert result.messages[len(frozen) : len(frozen) + len(working)] == working
    # Nested multimodal content, tool calls and metadata all survived intact.
    assert result.messages[1]["content"][0]["text"] == "review this image"
    assert result.messages[len(frozen) + 1]["tool_calls"][0]["id"] == "c1"
    assert result.messages[len(frozen) + 1]["_metadata"]["marker"] == "working"


def test_append_raw_shares_no_nested_references() -> None:
    """``_append_raw`` writes independent deep copies into raw and working."""
    runner = AgentRunner(ScriptedProvider())
    state = compression_state([{"role": "system", "content": "sys"}], [])
    raw: list[dict[str, Any]] = []
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "original"}],
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "noop", "arguments": "{}"}}
        ],
        "_metadata": {"marker": "x"},
    }

    runner._append_raw(raw, state, message)

    assert raw[0] == message
    assert state.working[0] == message
    # Mutating nested structures in the raw copy leaves the working copy alone.
    raw[0]["content"][0]["text"] = "MUTATED"
    raw[0]["tool_calls"][0]["id"] = "MUTATED"
    raw[0]["_metadata"]["marker"] = "MUTATED"

    assert state.working[0]["content"][0]["text"] == "original"
    assert state.working[0]["tool_calls"][0]["id"] == "c1"
    assert state.working[0]["_metadata"]["marker"] == "x"


@pytest.mark.asyncio
async def test_async_snapshot_deep_copies_live_zones() -> None:
    """The in-flight snapshot owns copies of frozen/working, not references."""
    provider = ScriptedProvider(
        compression=[LLMResponse(content=json.dumps(summary_payload()))]
    )
    runner = AgentRunner(provider)
    frozen = [{"role": "system", "content": "sys", "_metadata": {"m": 1}}]
    working = [
        {"role": "user", "content": [{"type": "text", "text": "old " + "y" * 8000}]},
        {"role": "assistant", "content": "reply " + "y" * 8000},
        {"role": "user", "content": "active"},
        {"role": "assistant", "content": "active reply"},
    ]
    spec = make_spec(
        frozen_messages=frozen,
        working_messages=working,
        context_window_tokens=1000,
    )
    state = compression_state(frozen, working, context_window_tokens=1000)
    state.working[0]["_metadata"] = {"m": 2}

    await runner._maybe_start_async_compression(
        spec, state, before_tokens=900, source="test"
    )

    snapshot = state.snapshot
    assert snapshot is not None
    # Rewrite the live zones; the snapshot must not follow.
    state.working[0]["content"][0]["text"] = "MUTATED"
    state.working[0]["_metadata"]["m"] = 999
    state.frozen[0]["_metadata"]["m"] = 999

    assert snapshot.working_prefix[0]["content"][0]["text"] == "old " + "y" * 8000
    assert snapshot.working_prefix[0]["_metadata"]["m"] == 2
    assert snapshot.frozen_prefix[0]["_metadata"]["m"] == 1

    await runner._close_compression(spec, state, {})


# --------------------------------------------------------------------------- #
# §12.8 Empty-working tool runs (coordinator / reviewer / judge shape)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_empty_working_tool_run_produces_a_legal_compress_prefix() -> None:
    """A run that starts with empty working still compresses instead of failing.

    Coordinator/reviewer/judge runs begin with only a frozen envelope, then
    append consecutive assistant/tool rounds. Those rounds must form legal whole
    units so a compress prefix exists once the request crosses the soft limit;
    the run must never fall straight through to ``compression_limit``.
    """
    provider = ScriptedProvider(
        business=[tool_turn(f"c{index}") for index in range(9)]
        + [LLMResponse(content="finished")],
        compression=[LLMResponse(content=json.dumps(summary_payload()))],
    )
    runner = AgentRunner(provider)
    spec = multi_iteration_spec(
        frozen_messages=[
            {"role": "system", "content": "reviewer system prompt"},
            {"role": "user", "content": "task " + "t" * 200},
        ],
        working_messages=[],
        tools=big_tool_registry(),
        context_window_tokens=16000,
        context_block_limit=200000,
        max_iterations=10,
        hook=YieldingHook(),
    )

    result = await runner.run(spec)

    assert provider.compression_requests, "empty-working run never compressed"
    assert result.stop_reason != "compression_limit"
    # The rebuild carried the summary into a later business request.
    assert any(
        "<compressed_context>" in str(message.get("content"))
        for request in provider.business_requests
        for message in request
    )


# --------------------------------------------------------------------------- #
# §12.9 Async retry gated on the working revision
# --------------------------------------------------------------------------- #

async def _start_async(
    runner: AgentRunner,
    spec: AgentRunSpec,
    state: RunCompressionState,
) -> None:
    await runner._maybe_start_async_compression(
        spec, state, before_tokens=900, source="test"
    )


@pytest.mark.asyncio
async def test_retry_gate_uses_working_revision_not_prefix_length() -> None:
    """Same compress prefix, but a moved revision, still permits a retry."""
    runner = AgentRunner(ScriptedProvider())
    frozen = [{"role": "system", "content": "sys"}]
    working = [
        {"role": "user", "content": "old " + "y" * 8000},
        {"role": "assistant", "content": "reply " + "y" * 8000},
        {"role": "user", "content": "active"},
        {"role": "assistant", "content": "active reply"},
    ]
    spec = make_spec(
        frozen_messages=frozen,
        working_messages=working,
        context_window_tokens=1000,
    )

    # A failure recorded against an *older* revision: a complete round landed
    # after it, so the retry is allowed even though the prefix is identical.
    stale = compression_state(frozen, working, context_window_tokens=1000)
    stale.async_failed = True
    stale.async_failed_revision = stale.working_revision - 1
    await _start_async(runner, spec, stale)
    assert stale.pending is not None
    await runner._close_compression(spec, stale, {})

    # A failure recorded at the current revision: nothing new to compress, so
    # no request is issued.
    current = compression_state(frozen, working, context_window_tokens=1000)
    current.async_failed = True
    current.async_failed_revision = current.working_revision
    await _start_async(runner, spec, current)
    assert current.pending is None


@pytest.mark.asyncio
async def test_failed_async_records_the_revision_it_started_from() -> None:
    """Appends made while a job was in flight still count as a retry trigger."""
    runner = AgentRunner(ScriptedProvider())
    frozen = [{"role": "system", "content": "sys"}]
    working = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
    ]
    state = compression_state(frozen, working)
    started_revision = state.working_revision
    state.snapshot = _snapshot(frozen, working, compress_count=1, revision=started_revision)
    state.pending = failed_future(RuntimeError("provider exploded"))
    # A new round was appended while the job was still in flight.
    state.working.append({"role": "user", "content": "new round"})
    state.working_revision += 1

    applied = await runner._collect_async_result(make_spec(), state)

    assert applied is False
    assert state.async_failed is True
    # The revision the failed attempt began at is banked, so the append made
    # during the run still unlocks a retry.
    assert state.async_failed_revision == started_revision
    assert state.async_failed_revision < state.working_revision
    assert summary_messages(state.working) == []


# --------------------------------------------------------------------------- #
# §12.10 Usage visibility on failed / discarded compression
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_compression_error_response_usage_is_still_counted() -> None:
    """A provider error response is billed; its visible usage must be recorded."""
    recorded: list[dict[str, int]] = []
    provider = ScriptedProvider(
        business=[
            tool_turn("c1", usage={"prompt_tokens": 10, "completion_tokens": 5}),
            tool_turn("c2"),
            LLMResponse(content="finished"),
        ],
        compression=[
            LLMResponse(
                content=None,
                finish_reason="error",
                usage={"prompt_tokens": 11, "completion_tokens": 0},
            )
        ],
    )
    runner = AgentRunner(provider)
    working = filler(20, chars=4000)
    spec = multi_iteration_spec(
        frozen_messages=[{"role": "system", "content": "sys"}],
        working_messages=working,
        context_window_tokens=11000,
        context_block_limit=20000,
        max_iterations=3,
        hook=YieldingHook(),
        compression_usage_callback=recorded.append,
    )

    result = await runner.run(spec)

    assert result.stop_reason == "compression_failed"
    # Both attempts returned the same unusable error response; each visible
    # usage was banked even though the content was rejected.
    assert recorded == [
        {"prompt_tokens": 11, "completion_tokens": 0},
        {"prompt_tokens": 11, "completion_tokens": 0},
    ]
    # One business request (10) + two compression attempts (11 + 11).
    assert result.usage["prompt_tokens"] == 32
    assert result.usage["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_compression_usage_counts_first_failure_then_success() -> None:
    """A retried attempt's usage is recorded for both the failure and success."""
    recorded: list[dict[str, int]] = []
    provider = ScriptedProvider(
        business=[
            tool_turn("c1"),
            tool_turn("c2"),
            tool_turn("c3"),
            LLMResponse(content="finished"),
        ],
        compression=[
            LLMResponse(
                content="not json",
                usage={"prompt_tokens": 3, "completion_tokens": 1},
            ),
            LLMResponse(
                content=json.dumps(summary_payload()),
                usage={"prompt_tokens": 5, "completion_tokens": 2},
            ),
        ],
    )
    runner = AgentRunner(provider)
    working = filler(20, chars=4000)

    await runner.run(
        multi_iteration_spec(
            frozen_messages=[{"role": "system", "content": "sys"}],
            working_messages=working,
            context_window_tokens=15000,
            context_block_limit=20000,
            max_iterations=4,
            hook=YieldingHook(),
            compression_usage_callback=recorded.append,
        )
    )

    assert recorded == [
        {"prompt_tokens": 3, "completion_tokens": 1},
        {"prompt_tokens": 5, "completion_tokens": 2},
    ]


@pytest.mark.asyncio
async def test_discarded_async_job_still_banks_its_usage() -> None:
    """A finished job whose snapshot no longer matches is dropped, tokens kept."""
    recorded: list[dict[str, int]] = []
    provider = ScriptedProvider(
        compression=[
            LLMResponse(
                content=json.dumps(summary_payload()),
                usage={"prompt_tokens": 9, "completion_tokens": 4},
            )
        ]
    )
    runner = AgentRunner(provider)
    frozen = [{"role": "system", "content": "sys"}]
    working = [
        {"role": "user", "content": "old " + "y" * 8000},
        {"role": "assistant", "content": "reply " + "y" * 8000},
        {"role": "user", "content": "active"},
        {"role": "assistant", "content": "active reply"},
    ]
    spec = make_spec(
        frozen_messages=frozen,
        working_messages=working,
        context_window_tokens=1000,
        compression_usage_callback=recorded.append,
    )
    state = compression_state(frozen, working, context_window_tokens=1000)
    state.snapshot = _snapshot(
        frozen, working, compress_count=2, revision=state.working_revision
    )
    state.pending = asyncio.create_task(
        runner._async_compress(spec, state, [dict(m) for m in working[:2]])
    )
    # Let the in-flight job finish (it banks its usage on completion).
    await asyncio.wait_for(state.pending, timeout=5)
    # The context moved on, so the finished job is discarded.
    state.working[0] = {"role": "user", "content": "rewritten"}

    applied = await runner._collect_async_result(spec, state)

    assert applied is False
    assert summary_messages(state.working) == []
    assert recorded == [{"prompt_tokens": 9, "completion_tokens": 4}]


# --------------------------------------------------------------------------- #
# §12.11 Bounded, traceable compression logging
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_compression_logs_carry_trace_and_boundaries_without_leaking() -> None:
    """Logs expose mode/trace/attempt/token boundaries but never content."""
    from loguru import logger as loguru_logger

    lines: list[str] = []
    sink = loguru_logger.add(
        lambda message: lines.append(str(message)), level="INFO", format="{message}"
    )
    try:
        provider = ScriptedProvider(
            business=[
                tool_turn("c1"),
                tool_turn("c2"),
                tool_turn("c3"),
                LLMResponse(content="finished"),
            ],
            compression=[
                LLMResponse(content="not json"),
                LLMResponse(
                    content=json.dumps(
                        summary_payload(confirmed_conclusions=["SECRET-CONCLUSION"])
                    )
                ),
            ],
        )
        runner = AgentRunner(provider)
        working = filler(20, chars=4000, prefix="SECRET-WORKING")
        await runner.run(
            multi_iteration_spec(
                frozen_messages=[{"role": "system", "content": "SECRET-FROZEN"}],
                working_messages=working,
                context_window_tokens=15000,
                context_block_limit=20000,
                max_iterations=4,
                hook=YieldingHook(),
            )
        )
    finally:
        loguru_logger.remove(sink)

    joined = "\n".join(lines)
    assert "compression.started mode=async" in joined
    assert "compression.applied mode=async" in joined
    assert "compression.retry mode=async" in joined
    assert "attempt=1/2" in joined
    assert "trace=" in joined
    assert "before_tokens=" in joined
    assert "after_tokens=" in joined
    # No message body, summary content or frozen text may leak into the log.
    assert "SECRET-CONCLUSION" not in joined
    assert "SECRET-FROZEN" not in joined
    assert "SECRET-WORKING" not in joined
