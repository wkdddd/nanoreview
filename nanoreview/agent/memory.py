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
    """Pure file I/O for user-editable personalization files."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.soul_file = workspace / "SOUL.md"

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")


_RAW_ARCHIVE_MAX_CHARS = 16_000
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000


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
    ) -> str | None:
        """Summarize messages that would be hidden by the replay window."""
        end_idx = self._replay_overflow_boundary(session, replay_max_messages)
        if end_idx is None:
            return None
        chunk = session.messages[session.last_consolidated:end_idx]
        if not chunk:
            return None
        logger.info(
            "Replay-window consolidation for {}: chunk={} msgs, replay_max={}",
            session.key,
            len(chunk),
            replay_max_messages,
        )
        summary = await self.archive(chunk)
        session.last_consolidated = end_idx
        self.sessions.save(session)
        return summary

    def _persist_last_summary(self, session: Session, summary: str | None) -> None:
        if summary and summary != "(nothing)":
            session.metadata["_last_summary"] = {
                "text": summary,
                "last_active": session.updated_at.isoformat(),
            }
            self.sessions.save(session)

    def estimate_session_prompt_tokens(
        self,
        session: Session,
    ) -> tuple[int, str]:
        """Estimate prompt size from the full unconsolidated session tail."""
        history = self._full_unconsolidated_history(session, include_timestamps=True)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        meta = session.metadata.get("_last_summary")
        summary = meta.get("text") if isinstance(meta, dict) else (meta if isinstance(meta, str) else None)
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

    def _truncate_to_token_budget(self, text: str) -> str:
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

    def raw_archive(self, messages: list[dict[str, Any]], *, max_chars: int | None = None) -> str | None:
        """Fallback summary for session metadata when the LLM is unavailable."""
        if not messages:
            return None
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        formatted = truncate_text(self._format_messages(messages), limit)
        logger.warning(
            "Session consolidation degraded: raw-summarized {} messages", len(messages)
        )
        return f"[RAW] {len(messages)} messages\n{formatted}"

    async def archive(self, messages: list[dict[str, Any]]) -> str | None:
        """Summarize messages and return text for current-session metadata."""
        if not messages:
            return None
        formatted = self._truncate_to_token_budget(self._format_messages(messages))
        if not formatted:
            return None
        prompt = (
            "Summarize the following older conversation turns for continuing the same session. "
            "Preserve user requests, decisions, unresolved work, tool outcomes, and code review findings. "
            "Do not create long-term facts, preferences, or cross-session memory. "
            "Keep the summary concise and useful for the next turn."
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
            return self.raw_archive(messages)

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
            last_summary = await self._consolidate_replay_overflow(
                session,
                replay_max_messages,
            )
            try:
                estimated, source = self.estimate_session_prompt_tokens(session)
            except Exception:
                logger.exception("Token estimation failed for {}", session.key)
                estimated, source = 0, "error"
            if estimated <= 0:
                self._persist_last_summary(session, last_summary)
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
                self._persist_last_summary(session, last_summary)
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
                summary = await self.archive(chunk)
                if summary:
                    last_summary = summary
                session.last_consolidated = end_idx
                self.sessions.save(session)
                if not summary:
                    break

                with suppress(Exception):
                    estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    break

            self._persist_last_summary(session, last_summary)
