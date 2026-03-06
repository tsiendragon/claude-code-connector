"""
transport_base.py
-----------------
Abstract base class for all transport implementations.

Three concrete transports exist:
  - TmuxTransport      (interactive tmux pane, screen-scraping)
  - StreamJsonTransport (subprocess with --output-format stream-json)
  - SdkTransport        (claude-agent-sdk Python package, async)
  - AcpTransport        (ACP JSON-RPC over stdio, async)

Each transport must provide the same minimal interface so that
ClaudeSession can work with any of them.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class TransportMode(str, Enum):
    """Supported transport backends."""

    TMUX = "tmux"
    STREAM_JSON = "stream-json"
    SDK = "sdk"
    ACP = "acp"


@dataclass
class Message:
    """
    A structured message from the Claude CLI / SDK.

    For tmux mode, ``role`` and ``content`` are best-effort heuristics.
    For stream-json / SDK / ACP modes, they are exact.
    """

    role: str                           # "assistant", "user", "system", "tool"
    content: str                        # text content
    raw: dict[str, Any] | None = None   # original JSON event (non-tmux modes)
    message_type: str = ""              # e.g. "text", "tool_use", "result"
    tool_name: str = ""                 # when message_type == "tool_use"
    tool_input: dict[str, Any] = field(default_factory=dict)
    is_partial: bool = False            # streaming partial update
    is_error: bool = False
    session_id: str = ""
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class TransportEvent:
    """
    A raw event from the transport layer.

    The ``type`` field matches stream-json event types (init, message, result, etc).
    For tmux mode, events are synthesised from pane captures.
    """

    type: str                           # "init", "message", "tool_use", "tool_result", "result", etc.
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class BaseTransport(ABC):
    """
    Abstract base for all Claude transport implementations.

    Concrete subclasses implement either the synchronous or asynchronous
    methods depending on the underlying mechanism.
    """

    @abstractmethod
    def send(self, text: str) -> None:
        """Send a user message / text to Claude."""
        ...

    @abstractmethod
    def is_alive(self) -> bool:
        """Return True if the underlying process / session is running."""
        ...

    @abstractmethod
    def kill(self) -> None:
        """Terminate the underlying process / session."""
        ...

    @property
    @abstractmethod
    def mode(self) -> TransportMode:
        """Return the transport mode enum."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the logical session name."""
        ...

    # --- Optional async methods (override in async transports) ---

    async def async_send(self, text: str) -> None:
        """Async version of send(). Default delegates to sync."""
        self.send(text)

    async def async_kill(self) -> None:
        """Async version of kill(). Default delegates to sync."""
        self.kill()
