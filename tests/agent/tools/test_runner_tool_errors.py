from __future__ import annotations

import json
from typing import Any

import pytest

from nanoreview.agent.hooks.lifecycle import (
    AgentHook,
    AgentHookContext,
    FinalizeContentResult,
)
from nanoreview.agent.output_contract import OutputContract
from nanoreview.agent.runner import AgentRunner, AgentRunSpec
from nanoreview.agent.tools.base import Tool
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanoreview.review.contracts import StructuredOutputError, parse_json_object


class DummyProvider(LLMProvider):
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
        _ = response_format
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


class ReplacingHook(AgentHook):
    def resolve_final_content(
        self,
        context: AgentHookContext,
        content: str | None,
    ) -> "FinalizeContentResult | None":
        return FinalizeContentResult("REPORT", is_replaced=True)


class FailingTool(Tool):
    @property
    def name(self) -> str:
        return "fail_tool"

    @property
    def description(self) -> str:
        return "Always fails."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        raise ValueError("boom")


class SystemExitTool(Tool):
    @property
    def name(self) -> str:
        return "system_exit_tool"

    @property
    def description(self) -> str:
        return "Raises SystemExit."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        raise SystemExit("stop")


class ErrorTextTool(Tool):
    @property
    def name(self) -> str:
        return "error_text_tool"

    @property
    def description(self) -> str:
        return "Returns a legitimate result that begins with Error."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return "Error handling best practices: keep the real result intact."


class LongResultTool(Tool):
    @property
    def name(self) -> str:
        return "long_result_tool"

    @property
    def description(self) -> str:
        return "Returns a result longer than the event detail cap."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return "X" * 300


class ScriptedProvider(LLMProvider):
    """Returns scripted responses; the last reply repeats once exhausted."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls = 0
        self.requests: list[list[dict[str, Any]]] = []

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
        index = self.calls
        self.calls += 1
        self.requests.append([dict(message) for message in messages])
        return self.responses[min(index, len(self.responses) - 1)]

    def get_default_model(self) -> str:
        return "dummy"


def _prose_response(text: str = "all done") -> LLMResponse:
    return LLMResponse(content=text)


def _payload_response(payload: dict[str, Any] | None = None) -> LLMResponse:
    return LLMResponse(content=json.dumps(payload if payload is not None else {"ok": True}))


class ResponseFormatRejectingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any] | None] = []

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
        self.calls.append(response_format)
        if response_format is not None:
            return LLMResponse(
                content="Error: unsupported parameter: response_format",
                finish_reason="error",
                error_status_code=400,
            )
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


def make_spec(tools: ToolRegistry | None = None, **overrides: Any) -> AgentRunSpec:
    values: dict[str, Any] = {
        "frozen_messages": [],
        "working_messages": [],
        "tools": tools or ToolRegistry(),
        "model": "dummy",
        "max_iterations": 1,
        "max_tool_result_chars": 1000,
    }
    values.update(overrides)
    return AgentRunSpec(**values)


def test_build_request_kwargs_includes_tool_choice() -> None:
    runner = AgentRunner(DummyProvider())
    choice = {"function": {"name": "review_submit"}}

    kwargs = runner._build_request_kwargs(
        make_spec(tool_choice=choice),
        [{"role": "user", "content": "submit"}],
        tools=[],
    )

    assert kwargs["tool_choice"] == choice


def test_build_request_kwargs_includes_response_format() -> None:
    runner = AgentRunner(DummyProvider())
    response_format = {"type": "json_object"}

    kwargs = runner._build_request_kwargs(
        make_spec(response_format=response_format),
        [{"role": "user", "content": "submit JSON"}],
        tools=[],
    )

    assert kwargs["response_format"] == response_format


@pytest.mark.asyncio
async def test_chat_with_retry_falls_back_when_response_format_is_unsupported() -> None:
    provider = ResponseFormatRejectingProvider()

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "Return JSON"}],
        response_format={"type": "json_object"},
    )

    assert response.content == "ok"
    assert provider.calls == [{"type": "json_object"}, None]


@pytest.mark.asyncio
async def test_run_tool_logs_exception_and_preserves_model_error_payload(monkeypatch) -> None:
    log_calls: list[tuple[str, str]] = []

    def capture_exception(message: str, tool_name: str, call_id: str) -> None:
        log_calls.append((message.format(tool_name, call_id), call_id))

    monkeypatch.setattr("nanoreview.agent.runner.logger.exception", capture_exception)

    tools = ToolRegistry()
    tools.register(FailingTool())
    runner = AgentRunner(DummyProvider())
    spec = make_spec(tools)

    result, event, error = await runner._run_tool(
        spec,
        ToolCallRequest(id="call_1", name="fail_tool", arguments={}),
        external_lookup_counts={},
        workspace_violation_counts={},
    )

    assert result == "Error: ValueError: boom"
    assert event == {
        "name": "fail_tool",
        "status": "error",
        "detail": "ValueError: boom",
    }
    assert error is None
    assert log_calls == [("Tool 'fail_tool' execution failed for call_id=call_1", "call_1")]


@pytest.mark.asyncio
async def test_run_tool_soft_error_tool_overrides_fail_on_tool_error(monkeypatch) -> None:
    monkeypatch.setattr("nanoreview.agent.runner.logger.exception", lambda *args, **kwargs: None)

    tools = ToolRegistry()
    tools.register(FailingTool())
    runner = AgentRunner(DummyProvider())
    spec = make_spec(
        tools,
        fail_on_tool_error=True,
        soft_tool_error_tools=frozenset({"fail_tool"}),
    )

    result, event, error = await runner._run_tool(
        spec,
        ToolCallRequest(id="call_1", name="fail_tool", arguments={}),
        external_lookup_counts={},
        workspace_violation_counts={},
    )

    assert result == "Error: ValueError: boom"
    assert event["status"] == "error"
    assert error is None


@pytest.mark.asyncio
async def test_run_tool_fail_on_tool_error_remains_strict_by_default(monkeypatch) -> None:
    monkeypatch.setattr("nanoreview.agent.runner.logger.exception", lambda *args, **kwargs: None)

    tools = ToolRegistry()
    tools.register(FailingTool())
    runner = AgentRunner(DummyProvider())
    spec = make_spec(tools, fail_on_tool_error=True)

    result, event, error = await runner._run_tool(
        spec,
        ToolCallRequest(id="call_1", name="fail_tool", arguments={}),
        external_lookup_counts={},
        workspace_violation_counts={},
    )

    assert result == "Error: ValueError: boom"
    assert event["status"] == "error"
    assert isinstance(error, ValueError)


@pytest.mark.asyncio
async def test_replaced_final_content_does_not_drain_injections() -> None:
    calls = 0

    async def injection_callback(**kwargs: Any) -> list[dict[str, str]]:
        nonlocal calls
        calls += 1
        return [{"role": "user", "content": "late system summary request"}]

    runner = AgentRunner(DummyProvider())
    result = await runner.run(make_spec(
        hook=ReplacingHook(),
        injection_callback=injection_callback,
        max_iterations=2,
    ))

    assert result.final_content == "REPORT"
    assert result.content_replaced is True
    assert calls == 0


@pytest.mark.asyncio
async def test_run_tool_does_not_catch_system_exit() -> None:
    tools = ToolRegistry()
    tools.register(SystemExitTool())
    runner = AgentRunner(DummyProvider())

    with pytest.raises(SystemExit):
        await runner._run_tool(
            make_spec(tools),
            ToolCallRequest(id="call_1", name="system_exit_tool", arguments={}),
            external_lookup_counts={},
            workspace_violation_counts={},
        )


@pytest.mark.asyncio
async def test_run_tool_does_not_treat_error_word_as_error_status() -> None:
    tools = ToolRegistry()
    tools.register(ErrorTextTool())
    runner = AgentRunner(DummyProvider())

    result, event, error = await runner._run_tool(
        make_spec(tools),
        ToolCallRequest(id="call_1", name="error_text_tool", arguments={}),
        external_lookup_counts={},
        workspace_violation_counts={},
    )

    assert result == "Error handling best practices: keep the real result intact."
    assert event["status"] == "ok"
    assert error is None


@pytest.mark.asyncio
async def test_drain_injections_falls_back_when_signature_is_unavailable(monkeypatch) -> None:
    class OpaqueInjectionCallback:
        async def __call__(self, *, limit: int) -> list[dict[str, str]]:
            return [{"role": "user", "content": f"limit={limit}"}]

    monkeypatch.setattr(
        "nanoreview.agent.runner.inspect.signature",
        lambda _callback: (_ for _ in ()).throw(ValueError("opaque")),
    )

    runner = AgentRunner(DummyProvider())
    injected = await runner._drain_injections(
        make_spec(injection_callback=OpaqueInjectionCallback())
    )

    assert injected == [{"role": "user", "content": "limit=5"}]


@pytest.mark.asyncio
async def test_run_tool_omits_raw_result_unless_declared() -> None:
    """An ordinary tool result is never kept untruncated by default."""
    tools = ToolRegistry()
    tools.register(LongResultTool())
    runner = AgentRunner(DummyProvider())

    result, event, error = await runner._run_tool(
        make_spec(tools),
        ToolCallRequest(id="call_1", name="long_result_tool", arguments={}),
        external_lookup_counts={},
        workspace_violation_counts={},
    )

    assert result == "X" * 300
    assert event["status"] == "ok"
    assert "raw_result" not in event
    assert error is None


@pytest.mark.asyncio
async def test_run_tool_preserves_declared_tool_result_untruncated() -> None:
    """A declared tool keeps the full result alongside the bounded detail."""
    tools = ToolRegistry()
    tools.register(LongResultTool())
    runner = AgentRunner(DummyProvider())

    result, event, error = await runner._run_tool(
        make_spec(tools, preserve_tool_result_tools=frozenset({"long_result_tool"})),
        ToolCallRequest(id="call_1", name="long_result_tool", arguments={}),
        external_lookup_counts={},
        workspace_violation_counts={},
    )

    assert event["status"] == "ok"
    assert event["raw_result"] == "X" * 300
    assert event["detail"].endswith("...")
    assert error is None


# ---------------------------------------------------------------------------
# Structured output contract
#
# A profile whose result is machine-consumed declares an ``OutputContract``
# instead of a terminal submission tool. Only a final answer the contract
# accepts completes the run; a rejected answer is fed back so the model can
# correct itself inside the same run, bounded by ``output_retry_limit``.
# ---------------------------------------------------------------------------


def _payload_contract() -> OutputContract:
    def _parse(text: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            payload = parse_json_object(text)
        except StructuredOutputError as exc:
            return None, str(exc)
        if "ok" not in payload:
            return None, "the payload must carry an 'ok' field"
        return payload, None

    return OutputContract(name="test_payload", parse=_parse, shape='{"ok":true}')


@pytest.mark.asyncio
async def test_accepted_output_completes_the_run() -> None:
    provider = ScriptedProvider([_payload_response()])
    runner = AgentRunner(provider)

    result = await runner.run(
        make_spec(output_contract=_payload_contract(), max_iterations=3)
    )

    assert result.stop_reason == "completed"
    assert result.structured_output == {"ok": True}
    assert result.output_attempts == 0
    assert result.output_error is None
    # Success ends immediately instead of giving the model another turn.
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_rejected_output_retries_then_fails_at_limit() -> None:
    provider = ScriptedProvider([_prose_response()])
    runner = AgentRunner(provider)

    result = await runner.run(
        make_spec(
            output_contract=_payload_contract(),
            output_retry_limit=2,
            max_iterations=5,
        )
    )

    assert result.stop_reason == "invalid_output"
    assert result.output_attempts == 2
    assert result.output_error is not None
    assert "one JSON object" in result.output_error
    # A rejected answer never yields a structured result.
    assert result.structured_output is None
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_rejected_output_then_accepted_answer_completes_the_run() -> None:
    """A rejected answer is retried inside the same run and can still succeed."""
    provider = ScriptedProvider(
        [_prose_response("I reviewed the change."), _payload_response()]
    )
    runner = AgentRunner(provider)

    result = await runner.run(
        make_spec(
            output_contract=_payload_contract(),
            output_retry_limit=3,
            max_iterations=5,
        )
    )

    assert result.stop_reason == "completed"
    assert result.output_attempts == 1
    assert result.structured_output == {"ok": True}
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_the_rejection_reason_is_fed_back_to_the_model() -> None:
    provider = ScriptedProvider([_prose_response(), _payload_response()])
    runner = AgentRunner(provider)

    await runner.run(
        make_spec(output_contract=_payload_contract(), max_iterations=3)
    )

    # The second request carries a correction prompt naming the concrete problem,
    # so the model can fix the answer instead of guessing.
    repair = provider.requests[1][-1]
    assert repair["role"] == "user"
    assert "one JSON object" in repair["content"]


@pytest.mark.asyncio
async def test_the_final_turn_tells_the_model_to_answer_without_tools() -> None:
    """The reserved answer turn is announced in the request the model receives."""
    provider = ScriptedProvider([_payload_response()])
    runner = AgentRunner(provider)

    result = await runner.run(
        make_spec(output_contract=_payload_contract(), max_iterations=1)
    )

    assert result.stop_reason == "completed"
    # The model finds out its tools are gone only from what is actually sent.
    assert "final available turn" in provider.requests[0][-1]["content"]


@pytest.mark.asyncio
async def test_a_rejection_on_the_final_turn_reports_the_real_reason() -> None:
    """The reserved turn has no correction round, so it must not read as a cap."""
    provider = ScriptedProvider([_prose_response(), _prose_response()])
    runner = AgentRunner(provider)

    result = await runner.run(
        make_spec(
            output_contract=_payload_contract(),
            output_retry_limit=5,
            max_iterations=2,
        )
    )

    assert result.stop_reason == "invalid_output"
    assert result.structured_output is None
    assert result.output_error is not None
    assert result.output_attempts == 2
    # No prompt is appended for a round the budget cannot afford.
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_a_run_without_a_contract_completes_on_free_text() -> None:
    """Conversation keeps a free-form final answer; only contracted runs parse."""
    provider = ScriptedProvider([_prose_response("anything goes")])
    runner = AgentRunner(provider)

    result = await runner.run(make_spec(max_iterations=2))

    assert result.stop_reason == "completed"
    assert result.final_content == "anything goes"
    assert result.structured_output is None


@pytest.mark.asyncio
async def test_a_blank_answer_is_reported_before_the_contract() -> None:
    """An empty answer is its own failure, not a contract rejection."""
    provider = ScriptedProvider([LLMResponse(content="   ")])
    runner = AgentRunner(provider)

    result = await runner.run(
        make_spec(output_contract=_payload_contract(), max_iterations=3)
    )

    assert result.stop_reason == "empty_final_response"
    assert result.output_attempts == 0


def test_the_terminal_submission_protocol_is_gone() -> None:
    """No run may declare a terminal tool or a reserved terminal iteration."""
    from dataclasses import fields

    names = {field.name for field in fields(AgentRunSpec)}
    assert "terminal_tools" not in names
    assert "terminal_retry_limit" not in names
    assert "prose_retry_limit" not in names
    assert "reserve_terminal_iteration" not in names
    assert "output_contract" in names
    assert "output_retry_limit" in names
