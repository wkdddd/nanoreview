"""Session memory helpers and lightweight consolidation."""

from __future__ import annotations

import asyncio
import weakref
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import tiktoken
from loguru import logger

from nanoreview.session.manager import Session
from nanoreview.utils.helpers import (
    estimate_message_tokens,
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

    def __init__(self, workspace: Path):
        self.workspace = workspace

    @staticmethod
    def read_file(path: Path) -> str:
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


class Consolidator:
    """Summarize old session turns into session metadata only."""

    _MAX_CONSOLIDATION_ROUNDS = 5
    _SAFETY_BUFFER = 1024

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
        consolidation_ratio: float = 0.5,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self.consolidation_ratio = consolidation_ratio
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
    ) -> None:
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = provider.generation.max_tokens

    def get_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    @staticmethod
    def _full_unconsolidated_history(
        session: Session,
        *,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        unconsolidated_count = len(session.messages) - session.last_consolidated
        if unconsolidated_count <= 0:
            return []
        return session.get_history(
            max_messages=unconsolidated_count,
            include_timestamps=include_timestamps,
        )

    @staticmethod
    def _replay_overflow_boundary(
        session: Session,
        replay_max_messages: int | None,
    ) -> int | None:
        if not replay_max_messages or replay_max_messages <= 0:
            return None
        tail = list(enumerate(session.messages[session.last_consolidated:], session.last_consolidated))
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

    async def _consolidate_replay_overflow(
        self,
        session: Session,
        replay_max_messages: int | None,
    ) -> None:
        """Summarize messages hidden by the replay window and persist the result.

        The new summary folds in the session's current ``_last_summary``, so the
        turns hidden by the replay window join the ones hidden by earlier
        consolidation instead of replacing them.
        """
        end_idx = self._replay_overflow_boundary(session, replay_max_messages)
        if end_idx is None:
            return
        chunk = session.messages[session.last_consolidated:end_idx]
        if not chunk:
            return
        logger.info(
            "Replay-window consolidation for {}: chunk={} msgs, replay_max={}",
            session.key,
            len(chunk),
            replay_max_messages,
        )
        summary = await self.archive(
            chunk,
            previous_summary=self.last_summary_text(session),
        )
        session.last_consolidated = end_idx
        self._persist_last_summary(session, summary)

    def _persist_last_summary(self, session: Session, summary: str | None) -> None:
        """Replace the session checkpoint and persist the session either way.

        ``last_consolidated`` has usually already moved by the time this runs, so
        the save must happen even when the summary is missing or the model sent
        the ``"(nothing)"`` sentinel: skipping it leaves the progress only in
        memory and a restart re-reads turns that were already hidden.

        ``"(nothing)"`` is a *replacement* checkpoint, not a missing one: the
        model asserted that neither the previous checkpoint nor the newly
        archived turns hold anything worth carrying forward. The stored summary
        is therefore dropped — keeping it would leave a stale checkpoint that
        claims to describe turns it no longer covers. NanoReview represents "no
        checkpoint" by the absence of the key, so no reader has to know the
        sentinel.

        A ``None`` summary means the opposite — nothing could be produced — and
        keeps the existing checkpoint in place.
        """
        if summary == _NOTHING_SUMMARY:
            session.metadata.pop("_last_summary", None)
        elif summary:
            session.metadata["_last_summary"] = {
                "text": summary,
                "last_active": session.updated_at.isoformat(),
            }
        self.sessions.save(session)

    @staticmethod
    def last_summary_text(session: Session) -> str | None:
        """Read the persisted session summary, if any.

        Single source of truth for ``_last_summary``: the token probe and the
        context that reaches the model both read it here, so the budget and the
        real prompt always account for the same summary. Consolidation hides old
        turns from the replay window, and this text is the only carrier of them
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

    def estimate_session_prompt_tokens(
        self,
        session: Session,
    ) -> tuple[int, str]:
        """Estimate prompt size from the full unconsolidated session tail."""
        history = self._full_unconsolidated_history(session, include_timestamps=True)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        summary = self.last_summary_text(session)
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            sender_id=None,
            session_summary=summary,
            session_metadata=session.metadata,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @property
    def _input_token_budget(self) -> int:
        return self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER

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
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {content}"
            )
        return "\n".join(lines)

    def raw_archive(
        self,
        messages: list[dict[str, Any]],
        *,
        max_chars: int | None = None,
        previous_summary: str | None = None,
    ) -> str | None:
        """Fallback summary for session metadata when the LLM is unavailable.

        Bounded text is the only copy that survives here — NanoReview keeps no
        raw-history sidecar — so neither end of the archive may be dropped
        silently: the running summary keeps its head (its opening facts are the
        ones that matter) and the new turns keep both ends (the newest turns sit
        at the tail, the original task at the head).
        """
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
            "Session consolidation degraded: raw-summarized {} messages", len(messages)
        )
        return result

    def _merge_archive_prompt(self, body: str, previous_summary: str) -> str:
        """Fit the running summary and the newly archived turns into the budget.

        The running summary keeps its head; the new turns keep both ends. Both
        matter: the new turns are about to be hidden behind ``last_consolidated``
        (so losing them here loses them for good) and the newest of them sit
        right at the replay-window boundary, where a silent gap breaks
        continuity.
        """
        budget = self._input_token_budget
        if budget <= 0:
            # No token budget at all: fall back to the character cap, half/half.
            notes = truncate_text(previous_summary, max(1, _RAW_ARCHIVE_MAX_CHARS // 2))
            header = (
                f"{_PREVIOUS_SUMMARY_HEADER}\n{notes}\n\n{_NEW_TURNS_HEADER}\n"
            )
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
    ) -> str | None:
        """Summarize messages and return text for current-session metadata.

        ``previous_summary`` is the summary of turns consolidated earlier — by an
        earlier round of the same call or an earlier turn of the session. It is
        folded into the new summary rather than overwritten, so repeated
        consolidation never drops the older decisions and open work that earlier
        rounds already hid from the replay window.
        """
        if not messages:
            return None
        body = self._format_messages(messages)
        if not body:
            # Nothing new to summarize; keep what the session already carried.
            return previous_summary
        if previous_summary:
            formatted = self._merge_archive_prompt(body, previous_summary)
        else:
            formatted = self._truncate_to_token_budget_keeping_ends(
                body, self._input_token_budget
            )
        if not formatted:
            return None
        prompt = (
            "Summarize the following older conversation turns for continuing the same session. "
            "Preserve user requests, decisions, unresolved work, tool outcomes, and code review findings. "
            "Do not create long-term facts, preferences, or cross-session memory. "
            "Keep the summary concise and useful for the next turn."
        )
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
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            summary = truncate_text(response.content or "[no summary]", _ARCHIVE_SUMMARY_MAX_CHARS)
            return summary
        except Exception:
            logger.warning("Consolidation LLM call failed, using raw session summary")
            return self.raw_archive(messages, previous_summary=previous_summary)

    async def maybe_consolidate_by_tokens(
        self,
        session: Session,
        *,
        replay_max_messages: int | None = None,
    ) -> None:
        """Archive old session turns into session metadata until prompt fits."""
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            budget = self._input_token_budget
            target = int(budget * self.consolidation_ratio)
            await self._consolidate_replay_overflow(
                session,
                replay_max_messages,
            )
            try:
                estimated, source = self.estimate_session_prompt_tokens(session)
            except Exception:
                logger.exception("Token estimation failed for {}", session.key)
                estimated, source = 0, "error"
            if estimated <= 0:
                return
            if estimated < budget:
                unconsolidated_count = len(session.messages) - session.last_consolidated
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}, msgs={}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    unconsolidated_count,
                )
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    break

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    break

                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    break

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                # Feed the running summary back in so each round folds the new
                # chunk into what earlier rounds already hid, then persist it:
                # `_last_summary` stays the single source both the next round's
                # prompt and the token probe read.
                summary = await self.archive(
                    chunk,
                    previous_summary=self.last_summary_text(session),
                )
                session.last_consolidated = end_idx
                self._persist_last_summary(session, summary)
                if not summary:
                    break

                with suppress(Exception):
                    estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    break
