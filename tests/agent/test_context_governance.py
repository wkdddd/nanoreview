"""Request-level context governance: budget, compaction, degradation, failure.

These tests pin the contract of ``agent/context_governance.py`` and the runner
path that uses it:

- one budget formula (``window - reserved output - safety buffer``) is shared by
  every planner, so a planned request is never rejected at send time;
- the leading system/developer header is copied verbatim and never summarized;
- the checkpoint enters the request as a *marked data message*, not as an
  instruction and not as part of the header;
- the first request is checked too — an oversized one fails explicitly instead
  of being sent;
- a failed summarizer degrades to a bounded verbatim checkpoint, and only when
  that is impossible does the run end with ``context_limit``;
- the raw transcript is never rewritten; only the request payload is.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoreview.agent.context_governance import (
    ARCHIVED_SUMMARY_CLOSE,
    ARCHIVED_SUMMARY_OPEN,
    CONTEXT_SAFETY_BUFFER,
    UNSENT_READ_REPLACEMENT,
    ContextCompactionState,
    ContextGovernanceConfig,
    ContextGovernor,
    ContextWindowExceededError,
    build_summary_message,
    header_length,
    input_token_budget,
    is_summary_message,
)
from nanoreview.agent.llm_runtime import ROLE_CONVERSATION, LLMRuntime
from nanoreview.agent.runner import AgentRunner, AgentRunSpec
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.providers.base import LLMProvider, LLMResponse


class EchoProvider(LLMProvider):
    """Provider that echoes a fixed answer and records what it was sent."""

    def __init__(self, reply: str = "done") -> None:
        super().__init__()
        self.reply = reply
        self.calls: list[list[dict[str, Any]]] = []

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
        _ = (tools, model, max_tokens, temperature, reasoning_effort, tool_choice)
        _ = response_format
        self.calls.append([dict(message) for message in messages])
        return LLMResponse(content=self.reply)

    def get_default_model(self) -> str:
        return "echo"


class StubSummarizer:
    """Records each archive call and returns a marker checkpoint."""

    def __init__(self, summary: str | None = "CHECKPOINT", *, fail: bool = False) -> None:
        self.summary = summary
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def archive(self, messages, *, previous_summary=None, on_usage=None):
        self.calls.append(
            {"messages": list(messages), "previous_summary": previous_summary}
        )
        if self.fail:
            raise RuntimeError("summarizer down")
        return self.summary

    def raw_archive(self, messages, *, previous_summary=None):
        self.calls.append(
            {"messages": list(messages), "previous_summary": previous_summary, "raw": True}
        )
        return "[RAW] bounded checkpoint"


def _governor(
    *,
    window: int = 2_048,
    max_tokens: int = 128,
    summarizer: StubSummarizer | None = None,
    context_block_limit: int | None = None,
) -> ContextGovernor:
    return ContextGovernor(
        ContextGovernanceConfig(
            provider=EchoProvider(),
            model="echo",
            context_window_tokens=window,
            max_tokens=max_tokens,
            context_block_limit=context_block_limit,
            tool_definitions=lambda: [],
        ),
        summarizer=summarizer,
        log_scope="test",
    )


def _bulk(chars: int) -> str:
    """A single message body of roughly ``chars // 4`` tokens."""
    return "x" * chars


# --- budget ----------------------------------------------------------------


def test_input_budget_reserves_output_and_the_safety_buffer() -> None:
    assert input_token_budget(200_000, 8_192) == 200_000 - 8_192 - CONTEXT_SAFETY_BUFFER
    # Unknown window: no budget rather than a negative one.
    assert input_token_budget(0, 8_192) == 0
    assert input_token_budget(None, 8_192) == 0
    # Unknown output reservation still keeps the buffer.
    assert input_token_budget(4_000, None) == 4_000 - CONTEXT_SAFETY_BUFFER


def test_the_governor_budget_is_the_shared_formula() -> None:
    governor = _governor(window=2_048, max_tokens=128)

    assert governor.input_budget() == input_token_budget(2_048, 128)


def test_an_explicit_block_limit_overrides_the_derived_budget() -> None:
    governor = _governor(window=200_000, max_tokens=8_192, context_block_limit=12_000)

    assert governor.input_budget() == 12_000


def test_no_window_means_no_budget_and_no_pressure() -> None:
    governor = _governor(window=0)
    state = ContextCompactionState.from_messages([{"role": "user", "content": _bulk(80_000)}])

    assert governor.input_budget() is None
    assert governor._has_pressure(governor.request_messages(state)) is False  # noqa: SLF001


# --- header and summary message --------------------------------------------


def test_header_length_counts_only_leading_instruction_messages() -> None:
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "developer", "content": "runtime"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "late system note"},
    ]

    assert header_length(messages) == 2


def test_the_summary_is_a_marked_data_message() -> None:
    message = build_summary_message("earlier decisions")

    assert message["role"] == "user"
    assert message["content"].startswith(ARCHIVED_SUMMARY_OPEN)
    assert message["content"].endswith(ARCHIVED_SUMMARY_CLOSE)
    assert "earlier decisions" in message["content"]
    # The preamble marks it as background state, not as an instruction.
    assert "not an instruction" in message["content"]
    assert is_summary_message(message)


def test_an_ordinary_user_message_is_not_mistaken_for_a_summary() -> None:
    assert not is_summary_message({"role": "user", "content": "review app.py"})
    assert not is_summary_message({"role": "user", "content": None})


def test_the_request_keeps_the_header_verbatim_and_hides_summarized_turns() -> None:
    governor = _governor()
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new question"},
    ]
    state = ContextCompactionState.from_messages(messages)
    state.summary = "CHECKPOINT"
    state.compacted_through = 3  # the first two turns are behind the checkpoint

    request = governor.request_messages(state)

    assert request[0] == {"role": "system", "content": "identity"}
    # The summarized turns are gone; the newest turn is merged into the
    # checkpoint message so no two user messages end up adjacent.
    assert len(request) == 2
    assert is_summary_message(request[1])
    assert "CHECKPOINT" in request[1]["content"]
    assert "new question" in request[1]["content"]


def test_a_summary_merges_into_a_leading_user_message() -> None:
    """The checkpoint must not become a separate turn from the user's message."""
    governor = _governor()
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "new question"},
    ]
    state = ContextCompactionState.from_messages(messages)
    state.summary = "CHECKPOINT"
    state.compacted_through = 1

    request = governor.request_messages(state)

    assert len(request) == 2
    assert is_summary_message(request[1])
    assert "new question" in request[1]["content"]


# --- degradation and failure -----------------------------------------------


@pytest.mark.asyncio
async def test_a_first_request_is_also_checked_against_the_budget() -> None:
    """An oversized envelope has nothing to summarize, so it fails explicitly.

    Reviewer and Judge envelopes pass ``accepted_message_count=0``: their
    authorized evidence must never be summarized away, so the only honest
    outcome is a bounded ``context_limit`` failure.
    """
    governor = _governor(window=2_048, max_tokens=128, summarizer=StubSummarizer())
    state = ContextCompactionState.from_messages(
        [
            {"role": "system", "content": "identity"},
            {"role": "user", "content": _bulk(40_000)},
        ],
        accepted_message_count=0,
    )

    with pytest.raises(ContextWindowExceededError) as excinfo:
        await governor.prepare_request(state)

    assert excinfo.value.source == "no-compressible-history"
    assert excinfo.value.input_budget == governor.input_budget()


@pytest.mark.asyncio
async def test_accepted_history_is_folded_into_the_checkpoint() -> None:
    summarizer = StubSummarizer("CHECKPOINT")
    governor = _governor(window=2_048, max_tokens=128, summarizer=summarizer)
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": _bulk(20_000)},
        {"role": "assistant", "content": _bulk(20_000)},
        {"role": "user", "content": "and now the new question"},
    ]
    state = ContextCompactionState.from_messages(messages, accepted_message_count=3)

    request = await governor.prepare_request(state)

    assert len(summarizer.calls) == 1
    # Only the accepted history is summarized: the header and the current turn
    # are excluded.
    assert [m["content"] for m in summarizer.calls[0]["messages"]] == [
        _bulk(20_000),
        _bulk(20_000),
    ]
    assert state.compaction_count == 1
    assert state.compacted_through == state.accepted_boundary == 3
    assert state.compacted_session_messages == 2
    assert state.summary == "CHECKPOINT"
    assert is_summary_message(request[1])
    assert "and now the new question" in request[1]["content"]
    # The request now fits.
    measured, _source = governor.estimate(request)
    assert measured <= (governor.input_budget() or 0)


@pytest.mark.asyncio
async def test_a_failed_summary_degrades_to_a_bounded_raw_checkpoint() -> None:
    summarizer = StubSummarizer(fail=True)
    governor = _governor(window=2_048, max_tokens=128, summarizer=summarizer)
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": _bulk(20_000)},
        {"role": "user", "content": "next"},
    ]
    state = ContextCompactionState.from_messages(messages, accepted_message_count=2)

    request = await governor.prepare_request(state)

    assert [call.get("raw", False) for call in summarizer.calls] == [False, True]
    assert state.summary == "[RAW] bounded checkpoint"
    assert is_summary_message(request[1])


@pytest.mark.asyncio
async def test_compaction_without_a_summarizer_fails_explicitly() -> None:
    """No summarizer and no usable checkpoint is a failure, never a silent send."""
    governor = _governor(window=2_048, max_tokens=128, summarizer=None)
    state = ContextCompactionState.from_messages(
        [
            {"role": "system", "content": "identity"},
            {"role": "user", "content": _bulk(20_000)},
            {"role": "user", "content": "next"},
        ],
        accepted_message_count=2,
    )

    with pytest.raises(ContextWindowExceededError) as excinfo:
        await governor.prepare_request(state)

    assert excinfo.value.source == "compaction-failed"


# --- unsent read replacement ----------------------------------------------


def _tool_round(call_id: str, content: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "read_file",
            "content": content,
        },
    ]


@pytest.mark.asyncio
async def test_an_unsent_oversized_read_result_is_replaced_in_the_request_only() -> None:
    """The model gets a bounded notice; the transcript keeps the real result."""
    governor = _governor(window=2_048, max_tokens=128, summarizer=StubSummarizer())
    big = _bulk(30_000)
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "read the file"},
        *_tool_round("call-1", big),
    ]
    state = ContextCompactionState.from_messages(messages, accepted_message_count=2)
    # Nothing accepted left to summarize beyond the first turn: force the
    # replacement path by marking the read as not yet sent.
    state.accepted_boundary = 2

    request = await governor.prepare_request(state)

    tool_messages = [m for m in request if m.get("role") == "tool"]
    assert [m["content"] for m in tool_messages] == [UNSENT_READ_REPLACEMENT]
    # The raw transcript is untouched.
    assert messages[-1]["content"] == big
    assert state.replaced_tool_results == {"call-1"}


@pytest.mark.asyncio
async def test_an_already_sent_read_result_is_never_replaced() -> None:
    """A result the model has seen stays as it was: it is accepted history."""
    governor = _governor(window=2_048, max_tokens=128, summarizer=StubSummarizer())
    big = _bulk(30_000)
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "read the file"},
        *_tool_round("call-1", big),
    ]
    state = ContextCompactionState.from_messages(messages, accepted_message_count=len(messages))
    state.accepted_boundary = len(messages)

    # Compaction (not replacement) is the answer here; it summarizes the round.
    state.summary = "CHECKPOINT"
    state.compacted_through = len(messages)
    request = governor.request_messages(state)

    assert not any(m.get("role") == "tool" for m in request)
    assert state.replaced_tool_results == set()


# --- runner integration ----------------------------------------------------


def _runtime(provider: LLMProvider, *, window: int, max_tokens: int = 128) -> LLMRuntime:
    return LLMRuntime.capture(
        provider,
        model="echo",
        context_window_tokens=window,
        role=ROLE_CONVERSATION,
    ).with_generation(max_tokens=max_tokens)


@pytest.mark.asyncio
async def test_the_runner_keeps_the_raw_transcript_and_reports_coverage() -> None:
    """Compaction changes the request, never ``spec.messages``."""
    provider = EchoProvider()
    summarizer = StubSummarizer("CHECKPOINT")
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": _bulk(20_000)},
        {"role": "assistant", "content": _bulk(20_000)},
        {"role": "user", "content": "next"},
    ]
    original = [dict(message) for message in messages]
    spec = AgentRunSpec(
        messages=messages,
        tools=ToolRegistry(),
        runtime=_runtime(provider, window=2_048),
        max_iterations=1,
        max_tool_result_chars=10_000,
        accepted_message_count=3,
        summarizer=summarizer,
    )

    result = await AgentRunner(provider).run(spec)

    assert result.stop_reason == "completed"
    assert result.compaction is not None
    assert result.compaction.summary == "CHECKPOINT"
    assert result.compaction.compacted_session_messages == 2
    assert result.compaction.compaction_count == 1
    # The transcript the caller handed in is not rewritten.
    assert spec.messages == original
    # The request the model saw was compacted and carries the marked summary.
    sent = provider.calls[0]
    assert is_summary_message(sent[1])


@pytest.mark.asyncio
async def test_an_unfittable_first_request_ends_with_context_limit() -> None:
    provider = EchoProvider()
    spec = AgentRunSpec(
        messages=[
            {"role": "system", "content": "identity"},
            {"role": "user", "content": _bulk(40_000)},
        ],
        tools=ToolRegistry(),
        runtime=_runtime(provider, window=2_048),
        max_iterations=2,
        max_tool_result_chars=10_000,
        accepted_message_count=0,
        summarizer=StubSummarizer(),
    )

    result = await AgentRunner(provider).run(spec)

    assert result.stop_reason == "context_limit"
    assert result.error
    # Nothing was ever sent: failing before the request is the whole point.
    assert provider.calls == []
