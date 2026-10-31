"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import inspect
import os
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.agent.context_governance import (
    ContextCompactionState,
    ContextGovernanceConfig,
    ContextGovernor,
    ContextWindowExceededError,
)
from nanoreview.agent.hooks.lifecycle import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
    finalize_content_result,
)
from nanoreview.agent.llm_runtime import LLMRuntime
from nanoreview.agent.memory import TranscriptSummarizer
from nanoreview.agent.output_contract import OutputContract
from nanoreview.agent.tools.base import tool_result_is_error
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.agent.tools.safety_boundary import classify_violation
from nanoreview.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanoreview.utils.helpers import (
    build_assistant_message,
    extract_reasoning,
    maybe_persist_tool_result,
    merge_token_usage,
    truncate_text,
)
from nanoreview.utils.prompt_templates import render_template
from nanoreview.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_final_structured_answer_message,
    build_finalization_retry_message,
    build_length_recovery_message,
    build_structured_length_recovery_message,
    ensure_nonempty_tool_result,
    is_blank_text,
    repeated_external_lookup_error,
)

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_INJECTIONS_PER_TURN = 5
_MAX_INJECTION_CYCLES = 10
_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 500
_COMPACTABLE_TOOLS = frozenset(
    {
        "read_file",
        "exec",
        "grep",
        "web_search",
        "web_fetch",
        "list_dir",
    }
)
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"
_STREAM_OUTER_TIMEOUT_MULTIPLIER = 3.0
_TOOL_ERROR_PREFIXES = ("Error:", "Error executing ")
# Stop reason of a run whose request still does not fit the model input budget
# after compaction and the unsent-read replacement. It always carries a
# non-empty ``error`` and ends the run before the next model request.
_STOP_CONTEXT_LIMIT = "context_limit"
# Max characters of a failure reason recorded/logged (never the full context).
_BOUNDED_REASON_CHARS = 200
#: Stop reason of a run whose final answer never satisfied its output contract.
_STOP_INVALID_OUTPUT = "invalid_output"


def _bounded(reason: Any) -> str:
    """Truncate a stop reason so logs never carry a full context dump."""
    text = str(reason or "").strip().replace("\n", " ")
    if len(text) > _BOUNDED_REASON_CHARS:
        return text[: _BOUNDED_REASON_CHARS - 3] + "..."
    return text


def _is_tool_error_result(result: Any) -> bool:
    # Prefer the explicit status a tool may report (e.g. MCP) so a successful
    # result whose text starts with "Error" is not misread as a failure; plain
    # string results keep the historical prefix convention.
    explicit_error = tool_result_is_error(result)
    if explicit_error is not None:
        return explicit_error
    return isinstance(result, str) and result.startswith(_TOOL_ERROR_PREFIXES)


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution.

    ``messages`` is the run's whole starting transcript. Its first entries must
    be the instruction header: a leading run of ``system``/``developer``
    messages carrying the role rules, the task objective and the review
    boundary. Everything after it is transcript data — session history, the
    current user message, the evidence/report/candidate payload and later tool
    rounds — and is what request-level governance may summarize.

    ``accepted_message_count`` marks where the pre-existing conversation context
    ends inside ``messages``: that region may be folded into a summary before the
    very first request. Reviewers and the Judge pass ``0`` because their task
    envelope is fixed authorized input, not history: when it does not fit, the
    run must fail explicitly instead of silently dropping evidence.
    """

    messages: list[dict[str, Any]]
    tools: ToolRegistry
    #: Frozen provider/model/generation selection for this whole run.
    runtime: LLMRuntime
    max_iterations: int
    max_tool_result_chars: int
    #: Raw index where the pre-existing conversation context ends (see above).
    accepted_message_count: int = 0
    #: Checkpoint carried from the session, replayed as a marked data message.
    initial_summary: str | None = None
    #: Summarizer used when a request must be compacted. ``None`` builds one from
    #: ``runtime`` (provider/model window, no journal).
    summarizer: TranscriptSummarizer | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    soft_tool_error_tools: frozenset[str] = field(default_factory=frozenset)
    #: Tools whose untruncated result is kept in ``tool_events[*].raw_result``.
    #: The runner only preserves what the caller explicitly declares, so an
    #: ordinary tool result is never persisted or propagated untruncated by
    #: default.
    preserve_tool_result_tools: frozenset[str] = field(default_factory=frozenset)
    #: Structured-output contract for the run's final answer. When set, the run
    #: only completes on a payload the contract accepts: an invalid answer is
    #: fed back with its concrete error and the model may correct itself inside
    #: the same run, bounded by ``output_retry_limit``. Without a contract the
    #: run's final text is whatever the model said (conversation behaviour).
    output_contract: OutputContract | None = None
    #: Correction attempts allowed after an invalid final answer. The value
    #: counts rejections, so the model always gets at least one retry.
    output_retry_limit: int = 3


@dataclass(frozen=True, slots=True)
class RunCompaction:
    """Request-level compaction that happened during a run.

    ``compacted_session_messages`` counts how many messages of the run's
    pre-existing context (``messages[header:accepted_message_count]``) the
    replacement summary now covers, so a session owner can advance its replay
    boundary by exactly that amount.
    """

    summary: str
    compacted_session_messages: int
    compaction_count: int


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    had_injections: bool = False
    content_replaced: bool = False
    #: Payload accepted by ``spec.output_contract``. Only a run that satisfied
    #: its contract has one; a truncated, prose or failed answer never does.
    structured_output: Any | None = None
    #: How many final answers the contract rejected before the run settled.
    output_attempts: int = 0
    output_error: str | None = None
    #: Replacement checkpoint produced by request-level compaction, if any.
    compaction: RunCompaction | None = None


@dataclass(slots=True)
class _IterationOutcome:
    """Counters and terminal state produced by one business-model iteration.

    A non-``None`` ``stop_reason`` ends the run; otherwise the caller loops and
    carries the mutated counters forward.
    """

    injection_cycles: int
    empty_content_retries: int
    length_recovery_count: int
    #: Rejections of the final answer by ``spec.output_contract`` so far.
    output_attempts: int = 0
    output_error: str | None = None
    had_injections: bool = False
    #: True once any iteration produced an explicit hook replacement of the
    #: final content. Carried forward so ``AgentRunResult`` can report it
    #: without re-deriving it from the public hook context.
    content_replaced: bool = False
    structured_output: Any | None = None
    stop_reason: str | None = None
    final_content: str | None = None
    error: str | None = None


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns.

    The runner is shared: the coordinator owns one instance for conversation,
    reviewer and Judge runs. Requests therefore always go to
    ``spec.runtime.provider`` — the provider frozen for that run's role — never
    to ``self.provider``, otherwise a role that selects its own provider would
    silently be sent to the conversation agent's.
    """

    def __init__(self, provider: LLMProvider):
        #: Provider of the *next* run created without a runtime snapshot
        #: (``SubagentManager`` and tests build specs by hand). Every run with a
        #: runtime ignores it.
        self.provider = provider

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected user messages while preserving role alternation."""
        for injection in injections:
            if messages and injection.get("role") == "user" and messages[-1].get("role") == "user":
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (plus a checkpoint if *assistant_message* and
        *iteration* are both provided) and return (True, cycles+1) so the caller
        continues the iteration loop. Otherwise return (False, cycles).
        """
        if injection_cycles >= _MAX_INJECTION_CYCLES:
            return False, injection_cycles
        injections = await self._drain_injections(spec)
        if not injections:
            return False, injection_cycles
        injection_cycles += 1
        if assistant_message is not None:
            self._append_raw(messages, assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.runtime.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        # Injected follow-up turns are ordinary transcript entries: the governor
        # sees them as not-yet-sent input on the next request.
        self._append_injected_messages(messages, deepcopy(injections))
        logger.info(
            "Injected {} follow-up message(s) {} ({}/{})",
            len(injections),
            phase,
            injection_cycles,
            _MAX_INJECTION_CYCLES,
        )
        return True, injection_cycles


    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost.
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = None
            try:
                signature = inspect.signature(spec.injection_callback)
                accepts_limit = "limit" in signature.parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            except (TypeError, ValueError):
                accepts_limit = True
            if accepts_limit:
                try:
                    items = await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
                except TypeError:
                    if signature is not None:
                        raise
                    items = await spec.injection_callback()
            else:
                items = await spec.injection_callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                injected_messages.append(item)
                continue
            text = getattr(item, "content", str(item))
            if text.strip():
                injected_messages.append({"role": "user", "content": text})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages),
                _MAX_INJECTIONS_PER_TURN,
                dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    ##核心方法
    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        # ``messages`` is the raw run history: it is append-only and is what the
        # ``messages`` is the raw transcript: it is append-only and is what the
        # caller persists. Governance only rebuilds the *request* payload, never
        # this list, so a provider that mutates its request in place cannot
        # corrupt the history either.
        messages = deepcopy(spec.messages)
        # Run-local governance state. Never stored on the runner, so concurrent
        # reviewer/judge runs cannot apply or cancel each other's compaction.
        state = ContextCompactionState.from_messages(
            messages,
            initial_summary=spec.initial_summary,
            accepted_message_count=spec.accepted_message_count,
        )
        governor = self._build_governor(spec, state)
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, Any]] = []
        external_lookup_counts: dict[str, int] = {}
        # Per-turn throttle for repeated attempts against the same outside target.
        workspace_violation_counts: dict[str, int] = {}
        empty_content_retries = 0
        length_recovery_count = 0
        had_injections = False
        injection_cycles = 0
        # Structured-output tracking: how many final answers the contract
        # rejected, the last concrete rejection reason, and the payload it
        # finally accepted. A run without a contract never sets any of them.
        output_attempts = 0
        output_error: str | None = None
        structured_output: Any | None = None
        content_replaced = False

        # The run hook gets its own transcript: an observer appending to
        # ``context.messages`` must not reach the history this run persists.
        run_context = AgentRunHookContext(messages=deepcopy(messages))
        try:
            await hook.before_run(run_context)
            for iteration in range(spec.max_iterations):
                # The last permitted request of a structured run is reserved for
                # the answer itself: no tools are exposed and the model is told
                # so, otherwise the budget can be spent on a tool call the run
                # has no room to come back from and the whole run ends at
                # ``max_iterations`` with nothing to hand over.
                final_structured = self._is_final_structured_turn(spec, iteration)
                if final_structured:
                    self._append_injected_messages(
                        messages, [build_final_structured_answer_message()]
                    )
                # Every request — the first one included — is measured against
                # the input budget. Under pressure the accepted history is
                # folded into one replacement summary; if the rebuilt request
                # still does not fit even after replacing unsent read results,
                # the run stops with an explicit ``context_limit``.
                try:
                    messages_for_model = await governor.prepare_request(state)
                except ContextWindowExceededError as exc:
                    stop_reason = _STOP_CONTEXT_LIMIT
                    error = _bounded(exc)
                    final_content = (
                        spec.error_message if spec.error_message is not None else error
                    )
                    logger.warning(
                        "context.limit scope={} trace={} session={} reason={}",
                        spec.session_key or spec.runtime.role,
                        state.trace_id,
                        spec.session_key or "default",
                        error,
                    )
                    break
                # Everything carried by this request is now "already sent".
                state.accept_request(len(messages))
                # The provider gets its own deep copy: in-place cleanup by the
                # provider chain must never reach the raw transcript.
                messages_for_model = deepcopy(messages_for_model)
                context = AgentHookContext(iteration=iteration, messages=messages)
                context.session_key = spec.session_key
                await hook.before_iteration(context)
                ##请求模型
                response = await self._request_model(
                    spec,
                    messages_for_model,
                    hook,
                    context,
                    final_structured=final_structured,
                )
                raw_usage = self._usage_dict(response.usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                self._accumulate_usage(usage, raw_usage)

                iteration_outcome = await self._run_iteration(
                    spec,
                    messages,
                    messages_for_model,
                    response,
                    raw_usage,
                    context,
                    hook,
                    iteration,
                    tools_used,
                    tool_events,
                    external_lookup_counts,
                    workspace_violation_counts,
                    usage,
                    injection_cycles,
                    empty_content_retries,
                    length_recovery_count,
                    output_attempts,
                    output_error,
                    had_injections,
                )
                injection_cycles = iteration_outcome.injection_cycles
                empty_content_retries = iteration_outcome.empty_content_retries
                length_recovery_count = iteration_outcome.length_recovery_count
                output_attempts = iteration_outcome.output_attempts
                output_error = iteration_outcome.output_error
                had_injections = had_injections or iteration_outcome.had_injections
                content_replaced = content_replaced or iteration_outcome.content_replaced
                if iteration_outcome.stop_reason is not None:
                    stop_reason = iteration_outcome.stop_reason
                    error = iteration_outcome.error
                    final_content = iteration_outcome.final_content
                    structured_output = iteration_outcome.structured_output
                    break
            else:
                stop_reason = "max_iterations"
                if spec.max_iterations_message:
                    final_content = spec.max_iterations_message.format(
                        max_iterations=spec.max_iterations,
                    )
                else:
                    final_content = render_template(
                        "agent/max_iterations_message.md",
                        strip=True,
                        max_iterations=spec.max_iterations,
                    )
                self._append_raw(messages, build_assistant_message(final_content))
                # Drain any remaining injections so they are appended to the
                # conversation history instead of being re-published as
                # independent inbound messages by _dispatch's finally block.
                # We ignore should_continue here because the for-loop has already
                # exhausted all iterations.
                drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                    spec,
                    messages,
                    None,
                    injection_cycles,
                    phase="after max_iterations",
                )
                if drained_after_max_iterations:
                    had_injections = True
        except asyncio.CancelledError as exc:
            # A cancelled run is not a run failure: ``on_error`` stays silent so
            # error reporting never races the caller's own cancellation.
            self._settle_run_accounting(state, usage)
            self._fill_run_context(
                run_context,
                messages=messages,
                final_content=final_content,
                tools_used=tools_used,
                usage=usage,
                stop_reason="cancelled",
                error=None,
                tool_events=tool_events,
                had_injections=had_injections,
                exception=exc,
            )
            run_context.usage = dict(usage)
            raise
        except Exception as exc:
            self._settle_run_accounting(state, usage)
            self._fill_run_context(
                run_context,
                messages=messages,
                final_content=final_content,
                tools_used=tools_used,
                usage=usage,
                stop_reason="error",
                error=f"Error: {type(exc).__name__}: {exc}",
                tool_events=tool_events,
                had_injections=had_injections,
                exception=exc,
            )
            run_context.usage = dict(usage)
            await hook.on_error(run_context)
            raise
        else:
            # Settle compaction usage *before* snapshotting: ``after_run`` is
            # the run-level result hook, so it must observe the same usage (and
            # the same finalized transcript) the caller receives from
            # ``AgentRunResult``, not a pre-settlement partial sum.
            self._settle_run_accounting(state, usage)
            self._fill_run_context(
                run_context,
                messages=messages,
                final_content=final_content,
                tools_used=tools_used,
                usage=usage,
                stop_reason=stop_reason,
                error=error,
                tool_events=tool_events,
                had_injections=had_injections,
                exception=None,
            )
            run_context.usage = dict(usage)
            if error is not None:
                await hook.on_error(run_context)
            await hook.after_run(run_context)
        finally:
            # Exit-path safety net. On the normal path accounting already ran
            # above; on the error/cancel paths it ran before the snapshot. This
            # call is idempotent (a settled state is a no-op), so it only does
            # work if a future exit path forgets to settle early.
            self._settle_run_accounting(state, usage)
            if run_context.exception is None:
                await hook.on_finally(run_context)
            else:
                # A failing finally hook must never mask the in-flight
                # exception or cancellation.
                try:
                    await hook.on_finally(run_context)
                except Exception:
                    logger.exception(
                        "AgentHook.on_finally error after {}",
                        run_context.stop_reason or "run exception",
                    )

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
            content_replaced=content_replaced,
            structured_output=structured_output,
            output_attempts=output_attempts,
            output_error=output_error,
            compaction=(
                RunCompaction(
                    summary=state.summary,
                    compacted_session_messages=state.compacted_session_messages,
                    compaction_count=state.compaction_count,
                )
                if state.summary and state.compaction_count
                else None
            ),
        )

    @staticmethod
    def _fill_run_context(
        run_context: AgentRunHookContext,
        *,
        messages: list[dict[str, Any]],
        final_content: str | None,
        tools_used: list[str],
        usage: dict[str, int],
        stop_reason: str,
        error: str | None,
        tool_events: list[dict[str, Any]],
        had_injections: bool,
        exception: BaseException | None,
    ) -> None:
        """Snapshot the run's end state for run-level hooks.

        Every collection is copied: the context is what hooks observe, never
        the runner's live state, so a misbehaving observer cannot rewrite the
        result the caller receives (or the history it persists).
        """
        run_context.messages = deepcopy(messages)
        run_context.final_content = final_content
        run_context.tools_used = list(tools_used)
        run_context.usage = dict(usage)
        run_context.stop_reason = stop_reason
        run_context.error = error
        run_context.tool_events = deepcopy(tool_events)
        run_context.had_injections = had_injections
        run_context.exception = exception


    async def _run_iteration(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        messages_for_model: list[dict[str, Any]],
        response: LLMResponse,
        raw_usage: dict[str, int],
        context: AgentHookContext,
        hook: AgentHook,
        iteration: int,
        tools_used: list[str],
        tool_events: list[dict[str, Any]],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        usage: dict[str, int],
        injection_cycles: int,
        empty_content_retries: int,
        length_recovery_count: int,
        output_attempts: int,
        output_error: str | None,
        had_injections: bool,
    ) -> "_IterationOutcome":
        """Handle one business model response and advance the run.

        Returns an :class:`_IterationOutcome`. A non-``None`` ``stop_reason``
        means the run must end with that reason; otherwise the caller loops.
        """
        reasoning_text, cleaned_content = extract_reasoning(
            response.reasoning_content,
            response.thinking_blocks,
            response.content,
        )
        response.content = cleaned_content
        if reasoning_text and not context.streamed_reasoning:
            await hook.emit_reasoning(reasoning_text)
            await hook.emit_reasoning_end()
            context.streamed_reasoning = True

        outcome = _IterationOutcome(
            injection_cycles=injection_cycles,
            empty_content_retries=empty_content_retries,
            length_recovery_count=length_recovery_count,
            output_attempts=output_attempts,
            output_error=output_error,
            had_injections=had_injections,
        )
        #: Set once ``finalize_content_result`` reports an explicit replacement.
        is_replaced = False

        if response.should_execute_tools:
            context.tool_calls = list(response.tool_calls)
            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=True)

            assistant_message = build_assistant_message(
                response.content or "",
                tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )
            self._append_raw(messages, assistant_message)
            tools_used.extend(tc.name for tc in response.tool_calls)
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "awaiting_tools",
                    "iteration": iteration,
                    "model": spec.runtime.model,
                    "assistant_message": assistant_message,
                    "completed_tool_results": [],
                    "pending_tool_calls": [
                        tc.to_openai_tool_call() for tc in response.tool_calls
                    ],
                },
            )

            await hook.before_execute_tools(context)

            results, new_events, fatal_error = await self._execute_tools(
                spec,
                response.tool_calls,
                external_lookup_counts,
                workspace_violation_counts,
                hook,
                context,
            )
            tool_events.extend(new_events)
            context.tool_results = list(results)
            context.tool_events = list(new_events)
            completed_tool_results: list[dict[str, Any]] = []
            for tool_call, result in zip(response.tool_calls, results):
                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.name,
                    "content": self._normalize_tool_result(
                        spec,
                        tool_call.id,
                        tool_call.name,
                        result,
                    ),
                }
                self._append_raw(messages, tool_message)
                completed_tool_results.append(tool_message)
            if fatal_error is not None:
                error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                final_content = error
                stop_reason = "tool_error"
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec,
                    messages,
                    None,
                    outcome.injection_cycles,
                    phase="after tool error",
                )
                outcome.injection_cycles = injection_cycles
                if should_continue:
                    outcome.had_injections = True
                    return outcome
                outcome.stop_reason = stop_reason
                outcome.error = error
                outcome.final_content = final_content
                return outcome
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "tools_completed",
                    "iteration": iteration,
                    "model": spec.runtime.model,
                    "assistant_message": assistant_message,
                    "completed_tool_results": completed_tool_results,
                    "pending_tool_calls": [],
                },
            )
            outcome.empty_content_retries = 0
            outcome.length_recovery_count = 0
            # Checkpoint 1: drain injections after tools, before next LLM call
            _drained, injection_cycles = await self._try_drain_injections(
                spec,
                messages,
                None,
                outcome.injection_cycles,
                phase="after tool execution",
            )
            outcome.injection_cycles = injection_cycles
            if _drained:
                outcome.had_injections = True
            await hook.after_iteration(context)
            return outcome

        if response.has_tool_calls:
            logger.warning(
                "Ignoring tool calls under finish_reason='{}' for {}",
                response.finish_reason,
                spec.session_key or "default",
            )

        finalized = finalize_content_result(hook, context, response.content)
        clean = finalized.content
        is_replaced = finalized.is_replaced
        outcome.content_replaced = is_replaced
        if response.finish_reason != "error" and is_blank_text(clean):
            outcome.empty_content_retries += 1
            if outcome.empty_content_retries < _MAX_EMPTY_RETRIES:
                logger.warning(
                    "Empty response on turn {} for {} ({}/{}); retrying",
                    iteration,
                    spec.session_key or "default",
                    outcome.empty_content_retries,
                    _MAX_EMPTY_RETRIES,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                await hook.after_iteration(context)
                return outcome
            logger.warning(
                "Empty response on turn {} for {} after {} retries; attempting finalization",
                iteration,
                spec.session_key or "default",
                outcome.empty_content_retries,
            )
            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=False)
            response = await self._request_finalization_retry(spec, messages_for_model)
            retry_usage = self._usage_dict(response.usage)
            self._accumulate_usage(usage, retry_usage)
            raw_usage = self._merge_usage(raw_usage, retry_usage)
            context.response = response
            context.usage = dict(raw_usage)
            context.tool_calls = list(response.tool_calls)
            finalized = finalize_content_result(hook, context, response.content)
            clean = finalized.content
            is_replaced = finalized.is_replaced
            outcome.content_replaced = is_replaced
        _ = raw_usage

        if response.finish_reason == "length" and not is_blank_text(clean):
            outcome.length_recovery_count += 1
            if outcome.length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                logger.info(
                    "Output truncated on turn {} for {} ({}/{}); continuing",
                    iteration,
                    spec.session_key or "default",
                    outcome.length_recovery_count,
                    _MAX_LENGTH_RECOVERIES,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)
                self._append_raw(
                    messages,
                    build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ),
                )
                # A truncated structured answer is a fragment, never a value the
                # contract could accept: ask for the whole object again instead
                # of appending the rest of a broken JSON document.
                self._append_raw(
                    messages,
                    build_structured_length_recovery_message()
                    if spec.output_contract is not None
                    else build_length_recovery_message(),
                )
                await hook.after_iteration(context)
                return outcome

        assistant_message: dict[str, Any] | None = None
        if response.finish_reason != "error" and not is_blank_text(clean):
            assistant_message = build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )

        # Check for mid-turn injections BEFORE signaling stream end.
        # If a hook explicitly replaced the content, that replacement is
        # authoritative for the turn and must not be overwritten by follow-up
        # prose from injected messages. Plain cleaning (DSML stripping) is not
        # a replacement and still drains injections.
        if is_replaced:
            should_continue = False
        else:
            should_continue, injection_cycles = await self._try_drain_injections(
                spec,
                messages,
                assistant_message,
                outcome.injection_cycles,
                phase="after final response",
                iteration=iteration,
            )
            outcome.injection_cycles = injection_cycles
            if should_continue:
                outcome.had_injections = True

        if hook.wants_streaming():
            await hook.on_stream_end(context, resuming=should_continue)

        if should_continue:
            await hook.after_iteration(context)
            return outcome

        if response.finish_reason == "error":
            final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
            stop_reason = "error"
            error = final_content
            self._append_model_error_placeholder(messages)
            context.final_content = final_content
            context.error = error
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            should_continue, injection_cycles = await self._try_drain_injections(
                spec,
                messages,
                None,
                outcome.injection_cycles,
                phase="after LLM error",
            )
            outcome.injection_cycles = injection_cycles
            if should_continue:
                outcome.had_injections = True
                return outcome
            outcome.stop_reason = stop_reason
            outcome.error = error
            outcome.final_content = final_content
            return outcome
        if is_blank_text(clean):
            final_content = EMPTY_FINAL_RESPONSE_MESSAGE
            stop_reason = "empty_final_response"
            error = final_content
            self._append_final_message(messages, final_content)
            context.final_content = final_content
            context.error = error
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            should_continue, injection_cycles = await self._try_drain_injections(
                spec,
                messages,
                None,
                outcome.injection_cycles,
                phase="after empty response",
            )
            outcome.injection_cycles = injection_cycles
            if should_continue:
                outcome.had_injections = True
                return outcome
            outcome.stop_reason = stop_reason
            outcome.error = error
            outcome.final_content = final_content
            return outcome

        # A run with an output contract only completes on a payload the contract
        # accepts. A rejected answer keeps the same AgentRun — the rejected text
        # and the concrete reason stay in context — so the model can correct
        # itself with full context, up to ``output_retry_limit`` rejections.
        if spec.output_contract is not None and not is_replaced:
            self._append_raw(
                messages,
                assistant_message
                or build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                ),
            )
            payload, rejection = spec.output_contract.validate(clean)
            if payload is not None:
                final_content = clean
                context.final_content = final_content
                context.stop_reason = "completed"
                await hook.after_iteration(context)
                outcome.stop_reason = "completed"
                outcome.final_content = final_content
                outcome.structured_output = payload
                logger.info(
                    "output_contract.accepted name={} attempts={}",
                    spec.output_contract.name,
                    outcome.output_attempts,
                )
                return outcome
            outcome.output_attempts += 1
            outcome.output_error = rejection
            # The last permitted request cannot be followed by a correction
            # round: asking for one more answer would append a prompt the loop
            # has no room to send, and the exhausted loop would then report
            # ``max_iterations`` instead of the real reason.
            if (
                outcome.output_attempts >= spec.output_retry_limit
                or self._is_final_structured_turn(spec, iteration)
            ):
                error = rejection or "the final answer was rejected"
                final_content = error
                stop_reason = _STOP_INVALID_OUTPUT
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                logger.info(
                    "output_contract.rejected name={} attempts={} reason={}",
                    spec.output_contract.name,
                    outcome.output_attempts,
                    _bounded(rejection),
                )
                await hook.after_iteration(context)
                outcome.stop_reason = stop_reason
                outcome.error = error
                outcome.final_content = final_content
                return outcome
            logger.info(
                "output_contract.retry name={} attempt={}/{} reason={}",
                spec.output_contract.name,
                outcome.output_attempts,
                spec.output_retry_limit,
                _bounded(rejection),
            )
            repair_prompt = spec.output_contract.repair_prompt(
                rejection or "the final answer was rejected"
            )
            # The correction prompt is new input for the next request. It is
            # appended to the raw transcript only: the request payload is rebuilt
            # from it, so there is no separate "working" copy to keep in sync.
            self._append_injected_messages(
                messages, [{"role": "user", "content": repair_prompt}]
            )
            await hook.after_iteration(context)
            return outcome

        final_message = (
            assistant_message
            or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )
        )
        self._append_raw(messages, final_message)
        await self._emit_checkpoint(
            spec,
            {
                "phase": "final_response",
                "iteration": iteration,
                "model": spec.runtime.model,
                "assistant_message": messages[-1],
                "completed_tool_results": [],
                "pending_tool_calls": [],
            },
        )
        final_content = clean
        context.final_content = final_content
        context.stop_reason = "completed"
        await hook.after_iteration(context)
        outcome.stop_reason = "completed"
        outcome.final_content = final_content
        return outcome

    # ------------------------------------------------------------------
    # Request governance
    # ------------------------------------------------------------------
    @staticmethod
    def _append_raw(messages: list[dict[str, Any]], message: dict[str, Any]) -> None:
        """Append an independent copy of *message* to the raw transcript."""
        messages.append(deepcopy(message))

    def _build_governor(
        self,
        spec: AgentRunSpec,
        state: ContextCompactionState,
    ) -> ContextGovernor:
        """Wire request-level governance for this run.

        The summarizer is built from the run's own runtime unless the caller
        supplies one (the conversation agent shares its session summarizer so the
        checkpoint it stores is produced by the same prompt). Compaction usage is
        folded into the run's usage as it is billed, so a run that later fails
        still reports what it spent.
        """
        runtime = spec.runtime
        summarizer = spec.summarizer or TranscriptSummarizer(
            runtime.provider,
            runtime.model,
            context_window_tokens=runtime.context_window_tokens,
            max_completion_tokens=runtime.max_tokens or 4096,
        )
        if isinstance(summarizer, TranscriptSummarizer):
            summarizer = summarizer.with_runtime(runtime)

        def _clean(body: list[dict[str, Any]]) -> list[dict[str, Any]]:
            try:
                return self._govern(spec, body)
            except Exception:
                logger.exception(
                    "Context governance failed for {}; applying minimal repair",
                    spec.session_key or "default",
                )
                repaired = self._drop_orphan_tool_results(list(body))
                return self._backfill_missing_tool_results(repaired)

        return ContextGovernor(
            ContextGovernanceConfig(
                provider=runtime.provider,
                model=runtime.model,
                context_window_tokens=runtime.context_window_tokens,
                max_tokens=runtime.max_tokens,
                context_block_limit=spec.context_block_limit,
                tool_definitions=lambda: self._tool_definitions(spec),
            ),
            summarizer=summarizer,
            clean=_clean,
            on_usage=lambda usage: merge_token_usage(state.usage, usage),
            log_scope=spec.session_key or runtime.role,
        )

    def _settle_run_accounting(
        self,
        state: ContextCompactionState,
        usage: dict[str, int],
    ) -> None:
        """Merge summarization usage into the run's usage exactly once.

        Idempotent: the normal path settles before the run-level result hook so
        the hook sees final usage, and ``finally`` calls it again purely as an
        exit-path safety net. Usage observed by a summarization request is part
        of the run and survives every exit path (normal, error, max iterations,
        cancellation).
        """
        if state.usage_banked:
            return
        merge_token_usage(usage, state.usage)
        state.usage_banked = True

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Request kwargs taken from the run's frozen runtime, never the provider.

        Reading ``provider.generation`` here would let a concurrent ``/model``
        switch or another role's mutation change an admitted run mid-flight.
        """
        runtime = spec.runtime
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": runtime.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if runtime.temperature is not None:
            kwargs["temperature"] = runtime.temperature
        if runtime.max_tokens > 0:
            kwargs["max_tokens"] = runtime.max_tokens
        if runtime.reasoning_effort is not None:
            kwargs["reasoning_effort"] = runtime.reasoning_effort
        if spec.tool_choice is not None:
            kwargs["tool_choice"] = spec.tool_choice
        if spec.response_format is not None:
            kwargs["response_format"] = spec.response_format
        return kwargs

    @staticmethod
    def _tool_definitions(spec: AgentRunSpec) -> list[dict[str, Any]]:
        """The run's full tool surface (a run may legitimately have none)."""
        return spec.tools.get_definitions()

    @staticmethod
    def _is_final_structured_turn(spec: AgentRunSpec, iteration: int) -> bool:
        """Whether *iteration* is the last chance to answer a structured run.

        A run with an output contract must answer with its payload, not with
        another tool call: reserving the final permitted request for the answer
        turns the would-be ``max_iterations`` exit into a guaranteed answer
        turn. A run without a contract (conversation) keeps its full budget.
        """
        return (
            spec.output_contract is not None
            and iteration == spec.max_iterations - 1
        )

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
        *,
        final_structured: bool = False,
    ):
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            # Default to a finite timeout to avoid per-session lock starvation when an LLM
            # request hangs indefinitely (e.g. gateway/network stall).
            # Set NANOBOT_LLM_TIMEOUT_S=0 to disable.
            raw = os.environ.get("NANOBOT_LLM_TIMEOUT_S", "300").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 300.0
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None

        # The reserved answer turn exposes no tools at all, so the model can
        # only reply with text; every other turn gets the run's full surface.
        tools = [] if final_structured else self._tool_definitions(spec)
        kwargs = self._build_request_kwargs(spec, messages, tools=tools)
        wants_streaming = hook.wants_streaming()

        async def _provider_tool_event(event: dict[str, Any]) -> None:
            if event.get("kind") != "hosted_tool":
                return
            await hook.on_provider_tool_event(context, event)

        if wants_streaming:

            async def _stream(delta: str) -> None:
                if delta:
                    context.streamed_content = True
                await hook.on_stream(context, delta)

            async def _thinking(delta: str) -> None:
                if not delta:
                    return
                context.streamed_reasoning = True
                await hook.emit_reasoning(delta)

            coro = spec.runtime.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_event=_provider_tool_event,
            )
        else:
            coro = spec.runtime.provider.chat_with_retry(**kwargs)

        # Streaming requests have provider-level idle timeouts
        # (NANOBOT_STREAM_IDLE_TIMEOUT_S), plus a longer wall-clock cap here as
        # a last-resort guard for providers that fail to enforce idle timeouts.
        outer_timeout_s = timeout_s
        if wants_streaming and timeout_s is not None:
            outer_timeout_s = timeout_s * _STREAM_OUTER_TIMEOUT_MULTIPLIER
        try:
            response = (
                await coro
                if outer_timeout_s is None
                else await asyncio.wait_for(coro, timeout=outer_timeout_s)
            )
        except asyncio.TimeoutError:
            if outer_timeout_s is None:
                return LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            return LLMResponse(
                content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
            )
        return response

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        kwargs = self._build_request_kwargs(spec, retry_messages, tools=None)
        return await spec.runtime.provider.chat_with_retry(**kwargs)

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        """清洗字典的value为int类型"""
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        hook: AgentHook | None = None,
        context: AgentHookContext | None = None,
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        batches = self._partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                batch_results = await asyncio.gather(
                    *(
                        self._run_tool(
                            spec,
                            tool_call,
                            external_lookup_counts,
                            workspace_violation_counts,
                            hook,
                            context,
                        )
                        for tool_call in batch
                    )
                )
                tool_results.extend(batch_results)
            else:
                batch_results = []
                for tool_call in batch:
                    result = await self._run_tool(
                        spec,
                        tool_call,
                        external_lookup_counts,
                        workspace_violation_counts,
                        hook,
                        context,
                    )
                    tool_results.append(result)
                    batch_results.append(result)

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    @staticmethod
    def _is_fatal_tool_error(spec: AgentRunSpec, tool_name: str) -> bool:
        """Whether a failed tool call should abort the run."""
        return (
            spec.fail_on_tool_error
            and tool_name not in spec.soft_tool_error_tools
        )

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        hook: AgentHook | None = None,
        context: AgentHookContext | None = None,
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        hint = "\n\n[Analyze the error above and try a different approach.]"
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated external lookup blocked",
            }
            if self._is_fatal_tool_error(spec, tool_call.name):
                return lookup_error + hint, event, RuntimeError(lookup_error)
            return lookup_error + hint, event, None
        prepare_call = getattr(spec.tools, "prepare_call", None)
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            with suppress(Exception):
                prepared = prepare_call(tool_call.name, tool_call.arguments)
                if isinstance(prepared, tuple) and len(prepared) == 3:
                    tool, params, prep_error = prepared
        if prep_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
            }
            handled = classify_violation(
                raw_text=prep_error,
                soft_payload=prep_error + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            error = None
            if self._is_fatal_tool_error(spec, tool_call.name):
                error = RuntimeError(prep_error)
            return prep_error + hint, event, error
        if hook is not None and context is not None:
            await hook.before_execute_tool(context, tool_call, tool, params)
        try:
            if tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(tool_call.name, params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Tool '{}' execution failed for call_id={}",
                tool_call.name,
                tool_call.id,
            )
            exc_text = f"{type(exc).__name__}: {exc}"
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": exc_text.replace("\n", " ").strip()[:160],
            }
            payload = f"Error: {exc_text}"
            if hook is not None and context is not None:
                await hook.on_execute_tool_error(context, tool_call, tool, params, exc)
            handled = classify_violation(
                raw_text=str(exc),
                soft_payload=payload,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if self._is_fatal_tool_error(spec, tool_call.name):
                return payload, event, exc
            return payload, event, None

        if _is_tool_error_result(result):
            if hook is not None and context is not None:
                await hook.on_execute_tool_error(context, tool_call, tool, params, result)
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            }
            handled = classify_violation(
                raw_text=result,
                soft_payload=result + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if self._is_fatal_tool_error(spec, tool_call.name):
                return result + hint, event, RuntimeError(result)
            return result + hint, event, None

        if hook is not None and context is not None:
            await hook.after_execute_tool(context, tool_call, tool, params, result)

        detail = "" if result is None else str(result)
        raw_result = (
            detail if tool_call.name in spec.preserve_tool_result_tools else None
        )
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        event: dict[str, Any] = {"name": tool_call.name, "status": "ok", "detail": detail}
        if raw_result is not None:
            event["raw_result"] = raw_result
        return result, event, None

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def _normalize_tool_result(
        self,
        spec: AgentRunSpec,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        result = ensure_nonempty_tool_result(tool_name, result)
        try:
            content = maybe_persist_tool_result(
                spec.workspace,
                spec.session_key,
                tool_call_id,
                result,
                max_chars=spec.max_tool_result_chars,
            )
        except Exception:
            logger.exception(
                "Tool result persist failed for {} in {}; using raw result",
                tool_call_id,
                spec.session_key or "default",
            )
            content = result
        if isinstance(content, str) and len(content) > spec.max_tool_result_chars:
            return truncate_text(content, spec.max_tool_result_chars)
        return content

    def _govern(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Run the repair/compact/budget pipeline over a request body.

        The pipeline never drops interactive units: capacity problems are handled
        by request-level compaction and, as a last resort, by the governor's
        explicit failure, so a request is either complete or rejected.
        """
        result = self._drop_orphan_tool_results(messages)
        result = self._backfill_missing_tool_results(result)
        result = self._microcompact(result)
        result = self._apply_tool_result_budget(spec, result)
        # Dropping/backfilling tool results can create new orphans.
        result = self._drop_orphan_tool_results(result)
        result = self._backfill_missing_tool_results(result)
        return result


    @staticmethod
    def _drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop tool results that have no matching assistant tool_call earlier in the history."""
        declared: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
            if updated is not None:
                updated.append(dict(msg))

        if updated is None:
            return messages
        return updated

    @staticmethod
    def _backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert synthetic error results for orphaned tool_use blocks."""
        declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared.append((idx, str(tc["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            updated.insert(
                insert_at,
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": _BACKFILL_CONTENT,
                },
            )
            offset += 1
        return updated

    @staticmethod
    def _microcompact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace old compactable tool results with one-line summaries."""
        compactable_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "tool" and msg.get("name") in _COMPACTABLE_TOOLS:
                compactable_indices.append(idx)

        if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
            return messages

        stale = compactable_indices[: len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
        updated: list[dict[str, Any]] | None = None
        for idx in stale:
            msg = messages[idx]
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
                continue
            name = msg.get("name", "tool")
            summary = f"[{name} result omitted from context]"
            if updated is None:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = summary

        return updated if updated is not None else messages

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self._normalize_tool_result(
                spec,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches
