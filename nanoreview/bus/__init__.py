"""Message bus module for decoupled channel-agent communication."""

from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
