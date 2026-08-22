"""Chat channels module with plugin architecture."""

from nanoreview.channels.base import BaseChannel
from nanoreview.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
