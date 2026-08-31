"""Base channel interface for chat platforms."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.pairing import (
    PAIRING_CODE_META_KEY,
    format_pairing_reply,
    generate_code,
    is_approved,
)


class BaseChannel(ABC):
    """
    Base interface for the WebSocket transport and message bus.
    """

    name: str = "base"
    display_name: str = "Base"
    send_progress: bool = True
    send_tool_hints: bool = False
    show_reasoning: bool = True

    def __init__(self, config: Any, bus: MessageBus):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
        """
        self.config = config
        self.logger = logger.bind(channel=self.name)
        self.bus = bus
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        """
        Start the channel and begin listening for messages.

        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        Send a message through this channel.

        Args:
            msg: The message to send.

        Implementations should raise on delivery failure so the channel manager
        can apply any retry policy in one place.
        """
        pass

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """Deliver a streaming text chunk.

        Override in subclasses to enable streaming. Implementations should
        raise on delivery failure so the channel manager can retry.

        Streaming contract: ``_stream_delta`` is a chunk, ``_stream_end`` ends
        the current segment, and stateful implementations must key buffers by
        ``_stream_id`` rather than only by ``chat_id``.
        """
        pass

    async def send_reasoning_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Stream a chunk of model reasoning/thinking content.

        Default is no-op. The WebSocket UI overrides this to render reasoning
        as a subordinate trace that updates in place as the model thinks.

        Streaming contract mirrors :meth:`send_delta`: ``_reasoning_delta``
        is a chunk, ``_reasoning_end`` ends the current reasoning segment,
        and stateful implementations should key buffers by ``_stream_id``
        rather than only by ``chat_id``.
        """
        return

    async def send_reasoning_end(
        self, chat_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Mark the end of a reasoning stream segment.

        Default is no-op. Transports that buffer ``send_reasoning_delta``
        chunks for in-place updates use this signal to flush and freeze
        the rendered group; one-shot channels can ignore it entirely.
        """
        return

    async def send_reasoning(self, msg: OutboundMessage) -> None:
        """Deliver a complete reasoning block.

        Default implementation reuses the streaming pair so transports only
        need to override the delta/end methods. Equivalent to one delta
        with the full content followed immediately by an end marker —
        keeps a single rendering path for both streamed and one-shot
        reasoning (e.g. DeepSeek-R1's final-response ``reasoning_content``).
        """
        if not msg.content:
            return
        meta = dict(msg.metadata or {})
        meta.setdefault("_reasoning_delta", True)
        await self.send_reasoning_delta(msg.chat_id, msg.content, meta)
        end_meta = dict(meta)
        end_meta.pop("_reasoning_delta", None)
        end_meta["_reasoning_end"] = True
        await self.send_reasoning_end(msg.chat_id, end_meta)

    async def send_subagent_status(
        self,
        chat_id: str,
        subagent_id: str,
        label: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Notify the frontend that a subagent started or finished.

        Default is no-op. The WebSocket transport overrides
        to render per-subagent status cards. ``status`` is one of
        ``"running"``, ``"completed"``, or ``"error"``.
        """
        return

    @property
    def supports_streaming(self) -> bool:
        """True when config enables streaming AND this subclass implements send_delta."""
        cfg = self.config
        streaming = cfg.get("streaming", False) if isinstance(cfg, dict) else getattr(cfg, "streaming", False)
        return bool(streaming) and type(self).send_delta is not BaseChannel.send_delta

    def is_allowed(self, sender_id: str) -> bool:
        """Check sender permission: star > allowlist > pairing store > deny."""
        if isinstance(self.config, dict):
            allow_list = self.config.get("allow_from") or self.config.get("allowFrom") or []
        else:
            allow_list = getattr(self.config, "allow_from", None) or []
        if "*" in allow_list:
            return True
        # allowFrom entries are opaque tokens — must match exactly.
        if str(sender_id) in allow_list:
            return True
        if is_approved(self.name, str(sender_id)):
            return True
        return False

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        is_dm: bool = False,
    ) -> None:
        """Handle an incoming message: check permissions, issue pairing codes in DMs, or forward to bus."""
        if not self.is_allowed(sender_id):
            if is_dm:
                code = generate_code(self.name, str(sender_id))
                await self.send(
                    OutboundMessage(
                        channel=self.name,
                        chat_id=str(chat_id),
                        content=format_pairing_reply(code),
                        metadata={PAIRING_CODE_META_KEY: code},
                    )
                )
                self.logger.info(
                    "Sent pairing code {} to sender {} in chat {}",
                    code, sender_id, chat_id,
                )
            else:
                self.logger.warning(
                    "Access denied for sender {}. "
                    "Add them to allowFrom list in config to grant access.",
                    sender_id,
                )
            return

        meta = metadata or {}
        if self.supports_streaming:
            meta = {**meta, "_wants_stream": True}

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=meta,
            session_key_override=session_key,
        )

        await self.bus.publish_inbound(msg)

    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running
