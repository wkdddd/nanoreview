"""Transcript summarization and session checkpoints.

Two collaborators live here:

* :class:`TranscriptSummarizer` turns a bounded chunk of transcript messages
  into one replacement checkpoint. It has no session, no tool and no journal
  knowledge, so the review roles, Judge and Dream can reuse it unchanged.
* :class:`Consolidator` owns the *session* side: it folds the part of the
  history hidden by the replay window into the same checkpoint and persists
  ``_last_summary`` + ``last_consolidated``.

There is no turn-level token consolidation any more: summarization is triggered
by request-level context pressure inside ``AgentRunner`` (see
``agent/context_governance.py``), and this module only produces the summary and
stores the checkpoint it yields. That keeps one summary machinery instead of two
competing ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

import tiktoken
from loguru import logger

from nanoreview.session.manager import Session
from nanoreview.utils.helpers import (
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    truncate_text,
)

if TYPE_CHECKING:
    from nanoreview.providers.base import LLMProvider
    from nanoreview.session.manager import SessionManager


class MemoryStore:
    """Pure file I/O for user-editable personalization files.

    The shared rules live in ``COMMON_RULES.md`` and are loaded through
    ``ContextBuilder.BOOTSTRAP_FILES``; this store stays a thin, extensible
    workspace file abstraction.
    """

    def __init__(self, workspace):
        self.workspace = workspace

    @staticmethod
    def read_file(path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""


_RAW_ARCHIVE_MAX_CHARS = 16_000
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000

_PREVIOUS_SUMMARY_HEADER = "Earlier summary of already-archived turns:"
_NEW_TURNS_HEADER = "Newly archived conversation turns:"
_NOTHING_SUMMARY = "(nothing)"
_ELISION = "\n... (truncated) ...\n"

_SUMMARY_PROMPT = (
    "Summarize the following older conversation turns for continuing the same session. "
    "Preserve user requests, decisions, unresolved work, tool outcomes, and code review findings. "
    "Do not create long-term facts, preferences, or cross-session memory. "
    "Keep the summary concise and useful for the next turn. "
    f"Return exactly {_NOTHING_SUMMARY!r} only when neither the previous checkpoint "
    "nor the newly archived turns contain anything useful to preserve. "
    f"Otherwise, never return {_NOTHING_SUMMARY!r}; output a concise replacement checkpoint."
)


def _truncate_keeping_ends(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters, eliding the middle.

    Trimming only the tail drops the newest turns, and trimming only the head
    drops the original task; a chunk that does not fit keeps both ends and marks
    the gap. Callers use this for the newly archived turns, never for a stored
    summary (whose opening facts are the ones worth keeping).
    """
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(0, (limit - len(_ELISION)) // 2)
    tail = max(0, limit - len(_ELISION) - head)
    return text[:head] + _ELISION + text[len(text) - tail:]


class TranscriptSummarizer:
    """Summarize a transcript chunk into one replacement checkpoint.

    ``archive`` asks the model; a failed or empty answer degrades to
    :meth:`raw_archive`, a bounded verbatim checkpoint. Because NanoReview keeps
    no raw-history sidecar, both ends of a truncated chunk are preserved.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        model: str,
        *,
        context_window_tokens: int = 0,
        max_completion_tokens: int = 4096,
        max_summary_chars: int = _ARCHIVE_SUMMARY_MAX_CHARS,
        prompt: str | None = None,
    ):
        self.provider = provider
        self.model = model
        self.context_window_tokens = int(context_window_tokens or 0)
        self.max_completion_tokens = int(max_completion_tokens or 0)
        self.max_summary_chars = max_summary_chars
        self.prompt = prompt or _SUMMARY_PROMPT

    def set_runtime(self, provider: "LLMProvider", model: str, context_window_tokens: int) -> None:
        self.provider = provider
        self.model = model
        self.context_window_tokens = int(context_window_tokens or 0)
        self.max_completion_tokens = int(
            getattr(getattr(provider, "generation", None), "max_tokens", 0)
            or self.max_completion_tokens
        )

    @property
    def _input_token_budget(self) -> int:
        if self.context_window_tokens <= 0:
            return 0
        return (
            self.context_window_tokens
            - self.max_completion_tokens
            - _SAFETY_BUFFER
        )

    def _truncate_to_token_budget(self, text: str, budget: int | None = None) -> str:
        if budget is None:
            budget = self._input_token_budget
        if budget <= 0:
            return truncate_text(text, _RAW_ARCHIVE_MAX_CHARS)
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            if len(tokens) <= budget:
                return text
            return enc.decode(tokens[:budget]) + "\n... (truncated)"
        except Exception:
            return truncate_text(text, budget * 4)

    @staticmethod
    def _count_tokens(text: str) -> int:
        if not text:
            return 0
        try:
            return len(tiktoken.get_encoding("cl100k_base").encode(text))
        except Exception:
            return max(1, len(text) // 4)

    def _truncate_to_token_budget_keeping_ends(self, text: str, budget: int) -> str:
        """Trim ``text`` to ``budget`` tokens, eliding the middle."""
        if budget <= 0:
            return truncate_text(text, _RAW_ARCHIVE_MAX_CHARS)
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            if len(tokens) <= budget:
                return text
            room = max(0, budget - len(enc.encode(_ELISION)))
            head = room // 2
            tail = room - head
            return (
                enc.decode(tokens[:head])
                + _ELISION
                + enc.decode(tokens[len(tokens) - tail:])
            )
        except Exception:
            return _truncate_keeping_ends(text, budget * 4)

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            content = message.get("content")
            if not content:
                continue
            if not isinstance(content, str):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{str(message.get('timestamp', '?'))[:16]}] {message.get('role', '?').upper()}{tools}: {content}"
            )
        return "\n".join(lines)

    def raw_archive(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_summary: str | None = None,
        max_chars: int | None = None,
    ) -> str | None:
        """Bounded verbatim checkpoint used when the summarizer is unavailable."""
        if not messages:
            return None
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        turns = self._format_messages(messages)
        prefix = f"[RAW] {len(messages)} messages\n"
        if previous_summary:
            notes = truncate_text(previous_summary, max(1, limit // 2))
            header = f"{prefix}{_PREVIOUS_SUMMARY_HEADER}\n{notes}\n\n{_NEW_TURNS_HEADER}\n"
            body = _truncate_keeping_ends(turns, max(1, limit - len(header)))
            result = header + body
        else:
            body = _truncate_keeping_ends(turns, max(1, limit - len(prefix)))
            result = prefix + body
        logger.warning(
            "Context summary degraded: raw checkpoint over {} messages", len(messages)
        )
        return result

    def _merge_archive_prompt(self, body: str, previous_summary: str) -> str:
        """Fit the running summary and the newly archived turns into the budget.

        The running summary keeps its head; the new turns keep both ends. Both
        matter: the new turns are about to be hidden behind the checkpoint (so
        losing them here loses them for good) and the newest of them sit right at
        the replay-window boundary, where a silent gap breaks continuity.
        """
        budget = self._input_token_budget
        if budget <= 0:
            notes = truncate_text(previous_summary, max(1, _RAW_ARCHIVE_MAX_CHARS // 2))
            header = f"{_PREVIOUS_SUMMARY_HEADER}\n{notes}\n\n{_NEW_TURNS_HEADER}\n"
            return header + _truncate_keeping_ends(
                body, max(1, _RAW_ARCHIVE_MAX_CHARS - len(header))
            )
        notes = self._truncate_to_token_budget(
            f"{_PREVIOUS_SUMMARY_HEADER}\n{previous_summary}",
            max(1, budget // 2),
        )
        remaining = max(1, budget - self._count_tokens(notes))
        turns = self._truncate_to_token_budget_keeping_ends(
            f"{_NEW_TURNS_HEADER}\n{body}", remaining
        )
        return f"{notes}\n\n{turns}"

    async def archive(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_summary: str | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> str | None:
        """Summarize messages into a replacement checkpoint.

        ``previous_summary`` is folded in rather than overwritten, so repeated
        compaction never drops decisions that earlier rounds already removed from
        the visible transcript.
        """
        if not messages:
            return None
        body = self._format_messages(messages)
        if not body:
            # Nothing textual to summarize; keep what the session already carried.
            return previous_summary
        if previous_summary:
            formatted = self._merge_archive_prompt(body, previous_summary)
        else:
            formatted = self._truncate_to_token_budget_keeping_ends(
                body, self._input_token_budget
            )
        if not formatted:
            return None
        prompt = self.prompt
        if previous_summary:
            prompt += (
                " An earlier summary of even older turns is included; merge it with the new"
                " turns so nothing already recorded is dropped."
            )
        try:
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            if on_usage is not None:
                usage = _usage_dict(getattr(response, "usage", None))
                if usage:
                    on_usage(usage)
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            answer = (response.content or "").strip()
            if not answer:
                # An empty answer is not a checkpoint: keeping it would hide the
                # archived turns behind nothing at all.
                return self.raw_archive(messages, previous_summary=previous_summary)
            return truncate_text(answer, self.max_summary_chars)
        except Exception:
            logger.warning("Context summary LLM call failed, using a raw checkpoint")
            return self.raw_archive(messages, previous_summary=previous_summary)


def _usage_dict(raw: Any) -> dict[str, int]:
    if raw is None:
        return {}
    prompt_tokens = getattr(raw, "prompt_tokens", 0) or 0
    completion_tokens = getattr(raw, "completion_tokens", 0) or 0
    total = getattr(raw, "total_tokens", 0) or 0
    if not (prompt_tokens or completion_tokens or total):
        return {}
    return {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total or (prompt_tokens + completion_tokens)),
    }


_SAFETY_BUFFER = 1024


class Consolidator:
    """Session-level checkpoint owner.

    It no longer scans for a token boundary or runs a multi-round consolidation
    loop: request-level compaction already decided which turns are hidden, and
    this class only persists the resulting checkpoint and folds the turns hidden
    by the replay window into it.
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: "LLMProvider",
        model: str,
        sessions: "SessionManager",
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
    ):
        self.store = store
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self.summarizer = TranscriptSummarizer(
            provider,
            model,
            context_window_tokens=context_window_tokens,
            max_completion_tokens=max_completion_tokens,
        )

    # -- runtime ------------------------------------------------------------

    @property
    def provider(self) -> "LLMProvider":
        return self.summarizer.provider

    @property
    def model(self) -> str:
        return self.summarizer.model

    @property
    def max_completion_tokens(self) -> int:
        return self.summarizer.max_completion_tokens

    def set_provider(
        self,
        provider: "LLMProvider",
        model: str,
        context_window_tokens: int,
    ) -> None:
        self.context_window_tokens = context_window_tokens
        self.summarizer.set_runtime(provider, model, context_window_tokens)
        self.summarizer.max_completion_tokens = getattr(
            getattr(provider, "generation", None), "max_tokens", 4096
        )

    # -- checkpoint ---------------------------------------------------------

    @staticmethod
    def last_summary_text(session: Session) -> str | None:
        """Read the persisted session summary, if any.

        Single source of truth for ``_last_summary``. Compaction hides old turns
        behind ``last_consolidated``, and this text is the only carrier of them
        back into later turns — including after a restart, when it is reloaded
        from session metadata. Also accepts the legacy bare-string shape.
        """
        meta = session.metadata.get("_last_summary")
        if isinstance(meta, dict):
            text = meta.get("text")
            return text if isinstance(text, str) and text else None
        if isinstance(meta, str) and meta:
            return meta
        return None

    def persist_checkpoint(
        self,
        session: Session,
        summary: str | None,
        *,
        advance: int = 0,
    ) -> None:
        """Advance the replay boundary, replace the checkpoint, persist.

        ``last_consolidated`` has already moved by the time this runs, so the
        save must happen even when the summary is missing or the model sent the
        ``"(nothing)"`` sentinel: skipping it leaves the progress only in memory
        and a restart re-reads turns that were already hidden.

        ``"(nothing)"`` is a *replacement* checkpoint, not a missing one: the
        model asserted that neither the previous checkpoint nor the newly
        archived turns hold anything worth carrying forward, so the stored
        summary is dropped. NanoReview represents "no checkpoint" by the absence
        of the key, so no reader has to know the sentinel. ``None`` means the
        opposite — nothing could be produced — and keeps the old checkpoint.
        """
        if advance:
            session.last_consolidated = min(
                len(session.messages), session.last_consolidated + max(0, advance)
            )
        if summary == _NOTHING_SUMMARY:
            session.metadata.pop("_last_summary", None)
        elif summary:
            session.metadata["_last_summary"] = {
                "text": summary,
                "last_active": session.updated_at.isoformat(),
            }
        self.sessions.save(session)

    # -- replay window ------------------------------------------------------

    @staticmethod
    def _replay_overflow_boundary(
        session: Session,
        replay_max_messages: int | None,
    ) -> int | None:
        if not replay_max_messages or replay_max_messages <= 0:
            return None
        tail = list(
            enumerate(
                session.messages[session.last_consolidated:],
                session.last_consolidated,
            )
        )
        if len(tail) <= replay_max_messages:
            return None

        sliced = tail[-replay_max_messages:]
        for i, (_idx, message) in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1][1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        legal_start = find_legal_message_start([message for _idx, message in sliced])
        if legal_start:
            sliced = sliced[legal_start:]
        if not sliced:
            return len(session.messages)

        first_visible_idx = sliced[0][0]
        if first_visible_idx <= session.last_consolidated:
            return None
        return first_visible_idx

    async def fold_replay_overflow(
        self,
        session: Session,
        replay_max_messages: int | None,
        *,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> str | None:
        """Fold turns hidden by the replay window into the checkpoint.

        Without this, a narrow replay window would silently drop old turns: the
        transcript no longer replays them and no summary ever describes them.
        Returns the new checkpoint text when one was produced.
        """
        end_idx = self._replay_overflow_boundary(session, replay_max_messages)
        if end_idx is None:
            return None
        chunk = session.messages[session.last_consolidated:end_idx]
        if not chunk:
            return None
        logger.info(
            "Replay-window fold for {}: chunk={} msgs, replay_max={}",
            session.key,
            len(chunk),
            replay_max_messages,
        )
        summary = await self.summarizer.archive(
            chunk,
            previous_summary=self.last_summary_text(session),
            on_usage=on_usage,
        )
        self.persist_checkpoint(session, summary, advance=end_idx - session.last_consolidated)
        return summary

    # -- status -------------------------------------------------------------

    def estimate_session_prompt_tokens(
        self,
        session: Session,
    ) -> tuple[int, str]:
        """Estimate the next request's prompt size.

        The next request carries the unconsolidated tail plus whatever the
        persisted checkpoint still describes, so both are counted. Reading the
        checkpoint explicitly is what keeps ``/status`` honest after request-level
        compaction hid part of the history: the visible tail alone understates
        the prompt.
        """
        unconsolidated_count = len(session.messages) - session.last_consolidated
        if unconsolidated_count <= 0:
            return 0, "empty"
        history = session.get_history(
            max_messages=unconsolidated_count,
            include_timestamps=True,
        )
        channel, chat_id = (
            session.key.split(":", 1) if ":" in session.key else (None, None)
        )
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            sender_id=None,
            session_metadata=session.metadata,
        )
        checkpoint = self.last_summary_text(session)
        if checkpoint:
            from nanoreview.agent.context_governance import build_summary_message

            probe_messages = [*probe_messages, build_summary_message(checkpoint)]
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )
