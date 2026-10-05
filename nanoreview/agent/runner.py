"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import inspect
import os
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanoreview.agent.compression import (
    AsyncSnapshot,
    CompressionError,
    RunCompressionState,
    build_summary_message,
    canonical_json,
    is_compression_summary,
    parse_and_validate,
    partition_working,
    serialize_transcript,
    split_units,
)
from nanoreview.agent.hooks.lifecycle import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
    finalize_content_result,
)
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.agent.tools.safety_boundary import classify_violation
from nanoreview.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanoreview.utils.helpers import (
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    extract_reasoning,
    find_legal_message_start,
    maybe_persist_tool_result,
    merge_token_usage,
    truncate_text,
)
from nanoreview.utils.prompt_templates import render_template
from nanoreview.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_finalization_retry_message,
    build_length_recovery_message,
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
_SNIP_SAFETY_BUFFER = 1024
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
# Run-level compression stop reasons. Both carry a non-empty ``error`` and end
# the run before the next business model request.
_STOP_COMPRESSION_FAILED = "compression_failed"
_STOP_COMPRESSION_LIMIT = "compression_limit"
# Each compression task runs at most this many runner-level logical requests
# (first attempt + one retry). Provider-internal retries are not counted here.
_COMPRESSION_ATTEMPTS = 2
# Max characters of a failure reason recorded/logged (never the full context).
_COMPRESSION_RETRY_REASON_CHARS = 200
_COMPRESSION_TEMPLATE_PATH = "agent/memory_compression.md"
# Reason recorded when the model answers with prose instead of submitting
# through its required terminal tool.
_TERMINAL_PROSE_MISS_REASON = (
    "model returned a prose response without calling the required terminal tool"
)


def _bounded(reason: Any) -> str:
    """Truncate a compression failure reason so logs never carry full context."""
    text = str(reason or "").strip().replace("\n", " ")
    if len(text) > _COMPRESSION_RETRY_REASON_CHARS:
        return text[: _COMPRESSION_RETRY_REASON_CHARS - 3] + "..."
    return text


def _is_tool_error_result(result: Any) -> bool:
    return isinstance(result, str) and result.startswith(_TOOL_ERROR_PREFIXES)


def _build_terminal_submission_prompt(terminal_tools: frozenset[str]) -> str:
    """Fixed prompt injected when the model skips its terminal tool."""
    names = ", ".join(f"`{name}`" for name in sorted(terminal_tools))
    return (
        f"You did not call the required terminal tool ({names}). "
        "Call it now with JSON-compatible structured arguments and no prose."
    )


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution.

    ``frozen_messages`` and ``working_messages`` are an explicit partition of
    the run's starting context, chosen by the caller (the runner never guesses
    a boundary by role or position):

    - ``frozen_messages`` is a fixed task/evidence envelope copied verbatim into
      every request. Context governance never repairs, compacts, or trims it.
    - ``working_messages`` is the mutable history. It may be summarized by
      run-level compression; the original messages stay in the run result.
    """

    frozen_messages: list[dict[str, Any]]
    working_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    permission_policy: Any | None = None
    permission_request_callback: Any | None = None
    soft_tool_error_tools: frozenset[str] = field(default_factory=frozenset)
    terminal_tools: frozenset[str] = field(default_factory=frozenset)
    #: Tools whose untruncated result is kept in ``tool_events[*].raw_result``.
    #: The runner only preserves what the caller explicitly declares, so an
    #: ordinary tool result is never persisted or propagated untruncated by
    #: default.
    preserve_tool_result_tools: frozenset[str] = field(default_factory=frozenset)
    # Max terminal-tool submission attempts (failed submissions and prose
    # answers both count) before the run fails with terminal_tool_failed.
    terminal_retry_limit: int = 5
    #: Overrides the default ``agent/memory_compression.md`` prompt template.
    compression_prompt: str | None = None
    #: Wall-clock timeout applied to each compression-layer logical request.
    compression_timeout_s: float = 180.0
    #: Receives each completed compression request's usage for outer-timeout
    #: snapshots (judge). Exceptions are logged, never fatal.
    compression_usage_callback: Callable[[dict[str, int]], None] | None = None



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
    # Terminal-tool diagnostics: how many submission attempts were made and
    # the last concrete failure reason when the run ended terminal_tool_failed.
    terminal_attempts: int = 0
    terminal_error: str | None = None


@dataclass(slots=True)
class _IterationOutcome:
    """Counters and terminal state produced by one business-model iteration.

    A non-``None`` ``stop_reason`` ends the run; otherwise the caller loops and
    carries the mutated counters forward.
    """

    injection_cycles: int
    empty_content_retries: int
    length_recovery_count: int
    terminal_attempts: int
    terminal_error: str | None
    had_injections: bool
    #: True once any iteration produced an explicit hook replacement of the
    #: final content. Carried forward so ``AgentRunResult`` can report it
    #: without re-deriving it from the public hook context.
    content_replaced: bool = False
    stop_reason: str | None = None
    final_content: str | None = None
    error: str | None = None


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
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
        state: RunCompressionState | None,
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (and to the model working context when *state*
        is given, plus a checkpoint if *assistant_message* and *iteration* are
        both provided) and return (True, cycles+1) so the caller continues the
        iteration loop. Otherwise return (False, cycles).
        """
        if injection_cycles >= _MAX_INJECTION_CYCLES:
            return False, injection_cycles
        injections = await self._drain_injections(spec)
        if not injections:
            return False, injection_cycles
        injection_cycles += 1
        if assistant_message is not None:
            if state is not None:
                self._append_raw(messages, state, assistant_message)
            else:
                messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        # Injections are written separately (independent copies) into the raw
        # history and the model working context; they also count as new
        # interactive content for the working revision, which re-enables a
        # previously failed async compression retry.
        self._append_injected_messages(messages, deepcopy(injections))
        if state is not None:
            self._append_injected_messages(state.working, deepcopy(injections))
            state.working_revision += 1
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
        # caller persists. Run-level compression only ever rewrites the separate
        # model working context, never this list. Raw history, model state and
        # provider requests all hold independent deep copies, so a provider
        # that mutates its request in place can never corrupt either.
        messages = deepcopy([*spec.frozen_messages, *spec.working_messages])
        # Run-local compression state: never stored on the runner, so concurrent
        # reviewer/judge runs cannot apply or cancel each other's jobs.
        state = RunCompressionState(
            frozen=deepcopy(spec.frozen_messages),
            working=deepcopy(spec.working_messages),
            context_window_tokens=spec.context_window_tokens,
        )
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
        # Terminal-tool submission tracking: attempts count every failed or
        # successful terminal submission (and prose answers while the terminal
        # tool is still pending); terminal_error keeps the last concrete error.
        terminal_attempts = 0
        terminal_error: str | None = None
        content_replaced = False

        # The run hook gets its own transcript: an observer appending to
        # ``context.messages`` must not reach the history this run persists.
        run_context = AgentRunHookContext(messages=deepcopy(messages))
        try:
            await hook.before_run(run_context)
            for iteration in range(spec.max_iterations):
                # The first business request skips run-level 60%/80%
                # compression, but its context is already built as
                # ``frozen + governed working``: governance (orphan repair,
                # backfill, microcompact, tool budget, hard trim) only ever
                # touches the working zone and never crosses the frozen
                # boundary. From the second request on, the compression state
                # machine additionally runs before every request.
                if iteration > 0:
                    outcome = await self._apply_run_compression(spec, state)
                    if outcome == "stop":
                        stop_reason = state.stopped_reason or _STOP_COMPRESSION_FAILED
                        error = state.stopped_error or stop_reason
                        # Normal agents surface the caller's user-facing error
                        # message; the bounded diagnostic stays in ``error``.
                        # Callers that pass ``error_message=None`` (coordinator,
                        # reviewer, judge) fall back to the diagnostic itself.
                        final_content = (
                            spec.error_message if spec.error_message is not None else error
                        )
                        break
                # The provider gets its own deep copy: in-place cleanup by the
                # provider chain must never reach the raw history or the state.
                messages_for_model = deepcopy(self._model_context(spec, state))
                context = AgentHookContext(iteration=iteration, messages=messages)
                context.session_key = spec.session_key
                await hook.before_iteration(context)
                ##请求模型
                response = await self._request_model(spec, messages_for_model, hook, context)
                raw_usage = self._usage_dict(response.usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                self._accumulate_usage(usage, raw_usage)

                iteration_outcome = await self._run_iteration(
                    spec,
                    state,
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
                    terminal_attempts,
                    terminal_error,
                    had_injections,
                )
                injection_cycles = iteration_outcome.injection_cycles
                empty_content_retries = iteration_outcome.empty_content_retries
                length_recovery_count = iteration_outcome.length_recovery_count
                terminal_attempts = iteration_outcome.terminal_attempts
                terminal_error = iteration_outcome.terminal_error
                had_injections = had_injections or iteration_outcome.had_injections
                content_replaced = content_replaced or iteration_outcome.content_replaced
                if iteration_outcome.stop_reason is not None:
                    stop_reason = iteration_outcome.stop_reason
                    error = iteration_outcome.error
                    final_content = iteration_outcome.final_content
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
                self._append_raw(messages, state, build_assistant_message(final_content))
                # Drain any remaining injections so they are appended to the
                # conversation history instead of being re-published as
                # independent inbound messages by _dispatch's finally block.
                # We ignore should_continue here because the for-loop has already
                # exhausted all iterations.
                drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                    spec,
                    messages,
                    state,
                    None,
                    injection_cycles,
                    phase="after max_iterations",
                )
                if drained_after_max_iterations:
                    had_injections = True
        except asyncio.CancelledError as exc:
            # A cancelled run is not a run failure: ``on_error`` stays silent so
            # error reporting never races the caller's own cancellation.
            await self._settle_run_accounting(spec, state, usage)
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
            await self._settle_run_accounting(spec, state, usage)
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
            # Settle compression *before* snapshotting: ``after_run`` is the
            # run-level result hook, so it must observe the same usage (and the
            # same finalized transcript) the caller receives from
            # ``AgentRunResult``, not a pre-settlement partial sum.
            await self._settle_run_accounting(spec, state, usage)
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
            await self._settle_run_accounting(spec, state, usage)
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
            terminal_attempts=terminal_attempts,
            terminal_error=terminal_error,
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
        state: RunCompressionState,
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
        terminal_attempts: int,
        terminal_error: str | None,
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
            terminal_attempts=terminal_attempts,
            terminal_error=terminal_error,
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
            self._append_raw(messages, state, assistant_message)
            tools_used.extend(tc.name for tc in response.tool_calls)
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "awaiting_tools",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": assistant_message,
                    "completed_tool_results": [],
                    "pending_tool_calls": [
                        tc.to_openai_tool_call() for tc in response.tool_calls
                    ],
                },
            )

            await hook.before_execute_tools(context)

            if spec.terminal_tools:
                for tool_call in response.tool_calls:
                    if tool_call.name in spec.terminal_tools:
                        logger.info(
                            "terminal_tool.start tool={} attempt={}/{}",
                            tool_call.name,
                            outcome.terminal_attempts + 1,
                            spec.terminal_retry_limit,
                        )

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
                self._append_raw(messages, state, tool_message)
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
                    state,
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
                    "model": spec.model,
                    "assistant_message": assistant_message,
                    "completed_tool_results": completed_tool_results,
                    "pending_tool_calls": [],
                },
            )
            # A terminal tool signals completion: once executed successfully,
            # break immediately instead of giving the LLM another turn that
            # could re-invoke it in a loop. A failed terminal submission stays
            # inside this AgentRun: the assistant and tool messages (including
            # the error) are kept so the model can correct its submission with
            # full context, up to spec.terminal_retry_limit attempts.
            if spec.terminal_tools:
                terminal_success: str | None = None
                terminal_failure: tuple[str, str] | None = None
                for tool_call, result in zip(response.tool_calls, results):
                    if tool_call.name not in spec.terminal_tools:
                        continue
                    if _is_tool_error_result(result):
                        terminal_failure = (tool_call.name, str(result))
                    else:
                        terminal_success = tool_call.name
                        break
                if terminal_success is not None:
                    outcome.terminal_attempts += 1
                    logger.info(
                        "terminal_tool.completed tool={} attempts={}",
                        terminal_success,
                        outcome.terminal_attempts,
                    )
                    stop_reason = "completed"
                    final_content = ""
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    outcome.stop_reason = stop_reason
                    outcome.final_content = final_content
                    return outcome
                if terminal_failure is not None:
                    failure_name, failure_text = terminal_failure
                    outcome.terminal_attempts += 1
                    outcome.terminal_error = failure_text
                    if outcome.terminal_attempts >= spec.terminal_retry_limit:
                        error = outcome.terminal_error
                        final_content = error
                        stop_reason = "terminal_tool_failed"
                        self._append_final_message(messages, final_content)
                        context.final_content = final_content
                        context.error = error
                        context.stop_reason = stop_reason
                        logger.info(
                            "terminal_tool.failed tool={} attempts={} reason={}",
                            failure_name,
                            outcome.terminal_attempts,
                            _bounded(failure_text),
                        )
                        await hook.after_iteration(context)
                        outcome.stop_reason = stop_reason
                        outcome.error = error
                        outcome.final_content = final_content
                        return outcome
                    logger.info(
                        "terminal_tool.retry tool={} attempt={} reason={}",
                        failure_name,
                        outcome.terminal_attempts,
                        _bounded(failure_text),
                    )
                    # Continue the current AgentRun so the same context,
                    # plan, and tool definitions remain available.
            outcome.empty_content_retries = 0
            outcome.length_recovery_count = 0
            # Checkpoint 1: drain injections after tools, before next LLM call
            _drained, injection_cycles = await self._try_drain_injections(
                spec,
                messages,
                state,
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
                    state,
                    build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ),
                )
                self._append_raw(messages, state, build_length_recovery_message())
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
                state,
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
                state,
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
                state,
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

        # A terminal-tool run that answers with prose instead of submitting
        # keeps the same AgentRun: inject one fixed prompt asking for the
        # terminal tool and count this as a terminal submission attempt.
        if spec.terminal_tools and not is_replaced:
            self._append_raw(
                messages,
                state,
                assistant_message
                or build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                ),
            )
            outcome.terminal_attempts += 1
            outcome.terminal_error = _TERMINAL_PROSE_MISS_REASON
            terminal_names = ",".join(sorted(spec.terminal_tools))
            if outcome.terminal_attempts >= spec.terminal_retry_limit:
                error = outcome.terminal_error
                final_content = error
                stop_reason = "terminal_tool_failed"
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                logger.info(
                    "terminal_tool.failed tool={} attempts={} reason={}",
                    terminal_names,
                    outcome.terminal_attempts,
                    _TERMINAL_PROSE_MISS_REASON,
                )
                await hook.after_iteration(context)
                outcome.stop_reason = stop_reason
                outcome.error = error
                outcome.final_content = final_content
                return outcome
            logger.info(
                "terminal_tool.retry tool={} attempt={} reason={}",
                terminal_names,
                outcome.terminal_attempts,
                _TERMINAL_PROSE_MISS_REASON,
            )
            self._append_injected_messages(
                messages,
                [
                    {
                        "role": "user",
                        "content": _build_terminal_submission_prompt(
                            spec.terminal_tools
                        ),
                    }
                ],
            )
            # The synthetic prompt must enter the model working context too.
            self._append_injected_messages(
                state.working,
                [
                    {
                        "role": "user",
                        "content": _build_terminal_submission_prompt(
                            spec.terminal_tools
                        ),
                    }
                ],
            )
            # New interactive content: counts for the compression retry gate.
            state.working_revision += 1
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
        self._append_raw(messages, state, final_message)
        await self._emit_checkpoint(
            spec,
            {
                "phase": "final_response",
                "iteration": iteration,
                "model": spec.model,
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
    # Run-level compression
    # ------------------------------------------------------------------
    @staticmethod
    def _append_raw(
        messages: list[dict[str, Any]],
        state: RunCompressionState,
        message: dict[str, Any],
    ) -> None:
        """Append a message to the raw history and the model working context.

        Each list receives its own deep copy, so no nested content, tool call
        or metadata is ever shared between the persisted history and the
        model-visible context. Bumping ``working_revision`` records that new
        interactive content exists, which is what re-enables a previously
        failed async compression retry.
        """
        messages.append(deepcopy(message))
        state.working.append(deepcopy(message))
        state.working_revision += 1

    def _model_context(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
    ) -> list[dict[str, Any]]:
        """Build the message list for the next business model request.

        From the very first request on, the context is ``frozen verbatim +
        governed working``: orphan repair, backfill, microcompact, tool-result
        budget and hard trim only ever see the working zone, never the frozen
        envelope, and never pair a tool round across the boundary. If the
        frozen zone alone exceeds the window it is still sent whole and the
        Provider is the one that reports the context-length error.
        """
        try:
            return self._prepare_messages(spec, state.frozen, list(state.working))
        except Exception:
            logger.exception(
                "Context governance failed for {}; applying minimal repair",
                spec.session_key or "default",
            )
            try:
                repaired = self._drop_orphan_tool_results(list(state.working))
                repaired = self._backfill_missing_tool_results(repaired)
                return [*[dict(m) for m in state.frozen], *repaired]
            except Exception:
                return [*[dict(m) for m in state.frozen], *state.working]

    async def _apply_run_compression(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
    ) -> str:
        """Run the pre-request compression state machine.

        Returns ``"stop"`` when the run must end (``compression_failed`` /
        ``compression_limit``), otherwise ``"continue"``.
        """
        sync_limit = state.sync_limit
        soft_limit = state.soft_limit
        if sync_limit is None:
            # No usable window: nothing to protect, leave the run untouched.
            return "continue"

        # 1. Collect a completed async task. Its usage is always recorded, even
        #    when the result is discarded because the context moved on. When the
        #    summary is applied, the next business request is sent as rebuilt:
        #    sync compression is deliberately not re-entered in the same step,
        #    the recount happens before the following request.
        if await self._collect_async_result(spec, state):
            return "continue"

        # 2. Recount the full request that would be sent next.
        before_tokens, source = self._count_request_tokens(spec, state)
        if before_tokens <= 0:
            return "continue"

        if before_tokens >= sync_limit:
            # 3. Sync compression takes priority over any background job.
            await self._cancel_pending_compression(state, reason="sync_takeover")
            await self._compress_sync(
                spec, state, before_tokens=before_tokens, source=source
            )
            if state.stopped:
                return "stop"
            after_tokens, _ = self._count_request_tokens(spec, state)
            if after_tokens >= sync_limit:
                state.stopped = True
                state.stopped_reason = _STOP_COMPRESSION_LIMIT
                state.stopped_error = (
                    "compression succeeded but the request still reaches "
                    f"{after_tokens} tokens (>= {sync_limit} sync limit)"
                )
                logger.warning(
                    "compression.stopped trace={} reason={} tokens={} limit={}",
                    state.trace_id,
                    _STOP_COMPRESSION_LIMIT,
                    after_tokens,
                    sync_limit,
                )
            return "stop" if state.stopped else "continue"

        # 4. Below the hard limit: start async compression if we are in the
        #    soft zone, there is no pending job, and a retry is permitted.
        if before_tokens >= soft_limit:
            await self._maybe_start_async_compression(
                spec, state, before_tokens=before_tokens, source=source
            )
        return "continue"

    def _tool_definitions_for_estimate(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        try:
            return spec.tools.get_definitions()
        except Exception:
            logger.exception("Failed to build tool definitions for token estimate")
            return []

    def _count_request_tokens(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
    ) -> tuple[int, str]:
        """Estimate the full next request: frozen + working + tools."""
        messages = self._model_context(spec, state)
        try:
            tokens, source = estimate_prompt_tokens_chain(
                self.provider,
                spec.model,
                messages,
                self._tool_definitions_for_estimate(spec),
            )
        except Exception:
            logger.exception("Token estimate failed; treating request as under limit")
            return 0, "none"
        return int(tokens), source

    def _compression_template(self, spec: AgentRunSpec) -> str:
        if spec.compression_prompt:
            return spec.compression_prompt
        return render_template(_COMPRESSION_TEMPLATE_PATH, strip=True)

    async def _collect_async_result(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
    ) -> bool:
        """Apply a finished async compression job if its snapshot still matches.

        Returns ``True`` only when a summary was actually written back, so the
        caller can skip further compression decisions for this request. The
        job's usage was already banked when the compression request returned
        (see ``_compress_working``), so nothing is recorded here.
        """
        pending = state.pending
        if pending is None or not pending.done():
            return False
        state.pending = None
        snapshot = state.snapshot
        state.snapshot = None
        try:
            result = pending.result()
        except asyncio.CancelledError:
            return False
        except Exception as exc:
            state.async_failed = True
            # Retry is keyed on the working revision the failed attempt
            # started from: messages appended while the job was in flight
            # (even before this failure was collected) move the revision and
            # therefore count as a retryable condition; a failure with no new
            # content does not.
            state.async_failed_revision = (
                snapshot.working_revision if snapshot is not None else state.working_revision
            )
            logger.warning(
                "compression.failed mode=async trace={} attempts={} reason={}",
                state.trace_id,
                _COMPRESSION_ATTEMPTS,
                _bounded(exc),
            )
            return False
        if result is None or snapshot is None:
            return False
        if not self._snapshot_matches(state, snapshot):
            logger.info(
                "compression.discarded mode=async trace={} reason=snapshot_mismatch snapshot={}",
                state.trace_id,
                state.snapshot_context(),
            )
            return False
        summary_message = build_summary_message(canonical_json(result))
        # Rebuild: new summary + the snapshot's active zone + everything appended
        # since the snapshot. The compress zone (which held the previous summary,
        # if any) is dropped from the model context, so summaries are replaced,
        # never accumulated, and the active zone is never lost.
        suffix = state.working[len(snapshot.working_prefix) :]
        state.working = self._rebuild_working(
            summary_message, snapshot.active_prefix, suffix
        )
        state.working_revision += 1
        state.async_failed = False
        state.async_failed_revision = None
        after_tokens, _ = self._count_request_tokens(spec, state)
        logger.info(
            "compression.applied mode=async trace={} before_tokens={} after_tokens={} "
            "active={} suffix={} working_len={}",
            state.trace_id,
            snapshot.before_tokens,
            after_tokens,
            len(snapshot.active_prefix),
            len(suffix),
            len(state.working),
        )
        return True

    @staticmethod
    def _rebuild_working(
        summary_message: dict[str, Any],
        active_prefix: list[dict[str, Any]],
        suffix: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Rebuild the model working context after a successful compression.

        Shared by the async apply path and the sync path: ``frozen + summary +
        active + suffix``. The compress zone is replaced by the synthetic
        summary, the active zone is replayed verbatim, and anything appended
        after the compression boundary keeps its position at the tail.
        """
        return [
            summary_message,
            *[dict(message) for message in active_prefix],
            *[dict(message) for message in suffix],
        ]

    def _snapshot_matches(
        self,
        state: RunCompressionState,
        snapshot: AsyncSnapshot,
    ) -> bool:
        """Whether the snapshot's frozen/working prefixes are still intact.

        Comparison is by value, not object identity: the async job and the
        governance pipeline both deep-copy messages, so the live entries are
        never the same objects the snapshot captured. While a snapshot is live,
        ``state.working`` only ever grows at the tail (``_append_raw``); every
        path that rewrites it (sync compression, applying an async summary)
        clears ``state.snapshot`` first. A value-equal prefix therefore means the
        region the summary was built from has not been rewritten.
        """
        if len(state.frozen) < snapshot.frozen_len:
            return False
        if len(state.working) < snapshot.working_len:
            return False
        return all(
            state.frozen[index] == snapshot.frozen_prefix[index]
            for index in range(snapshot.frozen_len)
        ) and all(
            state.working[index] == snapshot.working_prefix[index]
            for index in range(snapshot.working_len)
        )

    async def _maybe_start_async_compression(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
        *,
        before_tokens: int,
        source: str,
    ) -> None:
        if state.pending is not None:
            return
        # Retry gate after a failed attempt: the working revision must have
        # moved on since that attempt started (a complete round or an
        # injection was appended). Without new content the failure is never
        # retried, so no request loop forms.
        if state.async_failed and state.working_revision == state.async_failed_revision:
            return
        working_prefix = deepcopy(state.working)
        frozen_prefix = deepcopy(state.frozen)
        # Only snapshot the compress zone: keep the newest complete units active.
        keep_budget = state.soft_limit or before_tokens
        partition = partition_working(
            working_prefix,
            keep_budget_tokens=keep_budget,
            estimate=estimate_message_tokens,
        )
        if not partition.compress:
            return
        if not self._compression_request_room_available(partition.compress_prefix):
            return
        # The snapshot records the compress zone, the active zone that must be
        # replayed verbatim on apply, the full working prefix used to locate
        # the suffix appended while the job was in flight, and the working
        # revision that governs any later retry decision.
        snapshot = AsyncSnapshot(
            frozen_prefix=frozen_prefix,
            working_prefix=working_prefix,
            compress_prefix=partition.compress_prefix,
            active_prefix=partition.active_prefix,
            working_revision=state.working_revision,
            before_tokens=before_tokens,
        )
        state.snapshot = snapshot
        logger.info(
            "compression.started mode=async trace={} session={} before_tokens={} "
            "soft_limit={} source={}",
            state.trace_id,
            spec.session_key or "default",
            before_tokens,
            state.soft_limit,
            source,
        )
        state.pending = asyncio.create_task(
            self._async_compress(spec, state, partition.compress_prefix)
        )

    @staticmethod
    def _compression_request_room_available(messages: list[dict[str, Any]]) -> bool:
        """Whether the compress zone carries anything worth summarizing.

        A zone made only of synthetic summaries has no new content to fold in;
        re-summarizing a summary would loop without shrinking the request, so
        such a zone is treated as not compressible (the caller then reports
        ``compression_limit`` on its recount).
        """
        return any(not is_compression_summary(message) for message in messages)

    async def _async_compress(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
        prefix: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Deep-copy the compress zone, summarize it, and return the payload.

        Nothing is written back here: the runner applies the summary only after
        re-validating the snapshot, so a cancelled or stale job can never
        rewrite the working context. Per-attempt logging (including retry and
        failure reasons) happens inside ``_compress_working``; this coroutine
        stays silent so a failure is logged exactly once when collected.
        """
        working_copy = deepcopy(prefix)
        return await self._compress_working(spec, state, working_copy, mode="async")

    async def _cancel_pending_compression(
        self,
        state: RunCompressionState,
        *,
        reason: str,
    ) -> None:
        pending = state.pending
        if pending is None:
            return
        state.pending = None
        state.snapshot = None
        if not pending.done():
            pending.cancel()
            logger.info(
                "compression.discarded mode=async trace={} reason={}",
                state.trace_id,
                reason,
            )
        with suppress(asyncio.CancelledError, Exception):
            await pending

    async def _settle_run_accounting(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
        usage: dict[str, int],
    ) -> None:
        """Settle compression and merge its billed usage into ``usage``.

        Idempotent: the normal path settles before the run-level result hook so
        the hook sees final usage, and ``finally`` calls it again purely as an
        exit-path safety net. ``_close_compression`` guards on ``state.closed``;
        the merge is guarded here so a second call cannot double-count.

        Compression usage recorded here is part of the run and must survive
        every exit path (normal completion, terminal tool, business error, max
        iterations, cancellation).
        """
        if state.usage_banked:
            return
        await self._close_compression(spec, state, usage)
        merge_token_usage(usage, state.usage)
        state.usage_banked = True

    async def _close_compression(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
        usage: dict[str, int],
    ) -> None:
        """Settle the in-flight compression task when the run ends."""
        if state.closed:
            return
        state.closed = True
        pending = state.pending
        if pending is None:
            return
        state.pending = None
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pending
            # A cancelled summary must never be applied.
            state.snapshot = None
            return
        # Already finished but not yet consumed: still bank its usage.
        try:
            pending.result()
        except (asyncio.CancelledError, Exception):
            # A failed async job contributed only its own (already recorded)
            # usage; nothing to apply.
            state.snapshot = None
            return
        state.snapshot = None

    async def _compress_sync(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
        *,
        before_tokens: int,
        source: str,
    ) -> None:
        """Synchronously compress the compress zone; stop the run on failure."""
        keep_budget = state.soft_limit or before_tokens
        partition = partition_working(
            [dict(message) for message in state.working],
            keep_budget_tokens=keep_budget,
            estimate=estimate_message_tokens,
        )
        if not partition.compress or not self._compression_request_room_available(
            partition.compress_prefix
        ):
            # No legal complete prefix (or only synthetic summaries, which
            # carry nothing new): nothing to summarize. The caller then
            # reports compression_limit because the request is still too big.
            return
        logger.info(
            "compression.started mode=sync trace={} session={} before_tokens={} "
            "sync_limit={} source={}",
            state.trace_id,
            spec.session_key or "default",
            before_tokens,
            state.sync_limit,
            source,
        )
        try:
            payload = await self._compress_working(
                spec, state, partition.compress_prefix, mode="sync"
            )
        except CompressionError as exc:
            state.stopped = True
            state.stopped_reason = _STOP_COMPRESSION_FAILED
            state.stopped_error = (
                f"sync compression failed after {_COMPRESSION_ATTEMPTS} attempts: "
                f"{_bounded(exc)}"
            )
            logger.warning(
                "compression.failed mode=sync trace={} attempts={} reason={}",
                state.trace_id,
                _COMPRESSION_ATTEMPTS,
                _bounded(exc),
            )
            logger.warning(
                "compression.stopped trace={} reason={} detail={}",
                state.trace_id,
                _STOP_COMPRESSION_FAILED,
                state.stopped_error,
            )
            return
        summary_message = build_summary_message(canonical_json(payload))
        # Same rebuild as the async path: summary + active zone, no suffix
        # because sync compression is applied at the compression boundary.
        state.working = self._rebuild_working(
            summary_message, partition.active_prefix, ()
        )
        state.working_revision += 1
        state.async_failed = False
        state.async_failed_revision = None
        after_tokens, _ = self._count_request_tokens(spec, state)
        logger.info(
            "compression.applied mode=sync trace={} before_tokens={} after_tokens={} "
            "active={} working_len={}",
            state.trace_id,
            before_tokens,
            after_tokens,
            len(partition.active_prefix),
            len(state.working),
        )

    async def _compress_working(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
        messages: list[dict[str, Any]],
        *,
        mode: str,
    ) -> dict[str, Any]:
        """Run the bounded compression requests for *messages*.

        At most ``_COMPRESSION_ATTEMPTS`` runner-level logical requests are made
        (first attempt + one retry). The Provider's visible usage is recorded
        for *every* attempt — error responses, empty responses and first-fail/
        second-success alike — before ``finish_reason``, content and JSON are
        validated, and each recording triggers ``compression_usage_callback``.
        Usage that the Provider never exposed (timeouts, raised exceptions) is
        never fabricated.
        """
        last_error: CompressionError | None = None
        for attempt in range(1, _COMPRESSION_ATTEMPTS + 1):
            try:
                content, attempt_usage = await self._run_compression_request(
                    spec, messages
                )
            except CompressionError as exc:
                last_error = exc
                if exc.visible_usage:
                    # The Provider returned (and billed) a response whose
                    # content was unusable; its usage still counts.
                    self._record_compression_usage(spec, state, exc.visible_usage)
                logger.warning(
                    "compression.retry mode={} trace={} attempt={}/{} reason={}",
                    mode,
                    state.trace_id,
                    attempt,
                    _COMPRESSION_ATTEMPTS,
                    _bounded(exc),
                )
                continue
            self._record_compression_usage(spec, state, attempt_usage)
            try:
                payload = parse_and_validate(content)
            except CompressionError as exc:
                last_error = exc
                logger.warning(
                    "compression.retry mode={} trace={} attempt={}/{} reason={}",
                    mode,
                    state.trace_id,
                    attempt,
                    _COMPRESSION_ATTEMPTS,
                    _bounded(exc),
                )
                continue
            return payload
        raise last_error or CompressionError("compression produced no usable summary")

    async def _run_compression_request(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> tuple[str, dict[str, int]]:
        """One compression-layer logical request. Raises on failure/timeout."""
        template = self._compression_template(spec)
        transcript = serialize_transcript(messages)
        request_messages = [
            {"role": "system", "content": template},
            {"role": "user", "content": transcript},
        ]
        kwargs = self._build_compression_kwargs(spec, request_messages)
        timeout_s = spec.compression_timeout_s
        try:
            coro = self.provider.chat_with_retry(**kwargs)
            if timeout_s is not None and timeout_s > 0:
                response = await asyncio.wait_for(coro, timeout=timeout_s)
            else:
                response = await coro
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise CompressionError(
                f"compression request timed out after {timeout_s:g}s"
            ) from exc
        except Exception as exc:
            raise CompressionError(f"compression request failed: {exc}") from exc
        usage = self._usage_dict(response.usage)
        if response.finish_reason == "error" or not (response.content or "").strip():
            # The Provider answered (and billed usage for) a response whose
            # content is unusable: carry the visible usage on the error so the
            # retry loop can still account for it before validating anything.
            raise CompressionError(
                f"compression request returned no content ({response.finish_reason})",
                visible_usage=usage,
            )
        return response.content, usage

    def _build_compression_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compression request kwargs: same provider chain, no business tools.

        ``max_tokens`` is only forwarded when the spec sets it; otherwise the
        parameter is omitted so ``Provider.chat_with_retry()`` falls back to the
        provider's own generation default instead of a hard-coded cap.
        """
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": None,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
            "response_format": {"type": "json_object"},
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if isinstance(spec.max_tokens, int):
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs

    def _record_compression_usage(
        self,
        spec: AgentRunSpec,
        state: RunCompressionState,
        attempt_usage: dict[str, int],
    ) -> None:
        if not attempt_usage:
            return
        merge_token_usage(state.usage, attempt_usage)
        callback = spec.compression_usage_callback
        if callback is None:
            return
        try:
            callback(dict(attempt_usage))
        except Exception:
            logger.exception("compression_usage_callback failed")

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:

        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        if spec.tool_choice is not None:
            kwargs["tool_choice"] = spec.tool_choice
        if spec.response_format is not None:
            kwargs["response_format"] = spec.response_format
        return kwargs

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
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

        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=spec.tools.get_definitions(),
        )
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

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_event=_provider_tool_event,
            )
        else:
            coro = self.provider.chat_with_retry(**kwargs)

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
        return await self.provider.chat_with_retry(**kwargs)

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
        """Whether a failed tool call should abort the run.

        Terminal-tool failures never abort: they are retried inside the same
        AgentRun by the terminal retry loop, keeping the error in context.
        """
        return (
            spec.fail_on_tool_error
            and tool_name not in spec.soft_tool_error_tools
            and tool_name not in spec.terminal_tools
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
        try:
            if spec.permission_policy and spec.permission_request_callback:
                from nanoreview.agent.tools.permissions import PermissionVerdict, check_permission

                verdict = check_permission(tool_call.name, tool, params, spec.permission_policy)
                if verdict == PermissionVerdict.CONFIRM:
                    import uuid as _uuid

                    request_id = str(_uuid.uuid4())
                    payload = {
                        "request_id": request_id,
                        "tool_name": tool_call.name,
                        "arguments": params,
                        "permission": "user_approval",
                    }
                    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                    approved = await spec.permission_request_callback(request_id, payload, future)
                    if not approved:
                        event = {
                            "name": tool_call.name,
                            "status": "denied",
                            "detail": "user denied",
                        }
                        return "Tool execution denied by user.", event, None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Permission check failed for tool {!r}, denying by default", tool_call.name
            )
            event = {"name": tool_call.name, "status": "denied", "detail": "permission check error"}
            return "Tool execution denied due to permission check error.", event, None
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

    def _prepare_messages(
        self,
        spec: AgentRunSpec,
        frozen: list[dict[str, Any]],
        working: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Prepare message history for the model in a fixed-order pipeline.

        ``frozen`` is copied verbatim and prepended; governance (orphan repair,
        backfill, microcompact, tool-result budget, hard trim) only ever touches
        the ``working`` zone and never pairs a tool round across the boundary.
        """
        governed = self._govern(spec, list(working))
        frozen_copy = [dict(message) for message in frozen]
        return [*frozen_copy, *governed]

    def _govern(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Run the repair/compact/budget/trim pipeline over *messages*."""
        result = self._drop_orphan_tool_results(messages)
        result = self._backfill_missing_tool_results(result)
        result = self._microcompact(result)
        result = self._apply_tool_result_budget(spec, result)
        result = self._snip_history(spec, result)
        # Snipping may create new orphans
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

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not messages or not spec.context_window_tokens:
            return messages

        provider_max_tokens = getattr(
            getattr(self.provider, "generation", None), "max_tokens", 4096
        )
        max_output = (
            spec.max_tokens
            if isinstance(spec.max_tokens, int)
            else (provider_max_tokens if isinstance(provider_max_tokens, int) else 4096)
        )
        budget = spec.context_block_limit or (
            spec.context_window_tokens - max_output - _SNIP_SAFETY_BUFFER
        )
        if budget <= 0:
            return messages

        estimate, _ = estimate_prompt_tokens_chain(
            self.provider,
            spec.model,
            messages,
            spec.tools.get_definitions(),
        )
        if estimate <= budget:
            return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        remaining_budget = max(128, budget - system_tokens)
        # Drop the oldest whole interactive units so a user/assistant/tool round
        # is never cut in half (which would orphan tool results or split a
        # tool-call from its results).
        units = split_units(non_system)
        kept_units: list[list[dict[str, Any]]] = []
        kept_tokens = 0
        for unit in reversed(units):
            unit_tokens = sum(estimate_message_tokens(message) for message in unit)
            if kept_units and kept_tokens + unit_tokens > remaining_budget:
                break
            kept_units.append(unit)
            kept_tokens += unit_tokens
        kept_units.reverse()
        kept: list[dict[str, Any]] = [message for unit in kept_units for message in unit]

        if kept:
            for i, message in enumerate(kept):
                if message.get("role") == "user":
                    kept = kept[i:]
                    break
            else:
                # Recover nearest user message from outside the kept window;
                # GLM rejects system→assistant (error 1214).  Budget is
                # intentionally exceeded — oversized beats invalid.
                for idx in range(len(non_system) - 1, -1, -1):
                    if non_system[idx].get("role") == "user":
                        kept = non_system[idx:]
                        break
                # If no user exists at all, _enforce_role_alternation
                # will insert a synthetic one as a safety net.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        if not kept:
            kept = non_system[-min(len(non_system), 4) :]
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        return system_messages + kept

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
