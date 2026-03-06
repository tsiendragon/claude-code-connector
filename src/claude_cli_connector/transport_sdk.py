"""
transport_sdk.py
----------------
SDK transport: uses the official ``claude-agent-sdk`` Python package.

This transport wraps ``claude_agent_sdk.ClaudeSDKClient`` and provides
the same interface as the other transports.

.. warning::

    **NOT TESTED** – requires ``ANTHROPIC_API_KEY`` and
    ``pip install claude-agent-sdk``.  This module is provided as a
    reference implementation; use at your own risk.

Requirements
~~~~~~~~~~~~
  pip install claude-agent-sdk

References
~~~~~~~~~~
  - https://platform.claude.com/docs/en/agent-sdk/python
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Optional

from claude_cli_connector.exceptions import TransportError
from claude_cli_connector.transport_base import (
    BaseTransport,
    Message,
    TransportEvent,
    TransportMode,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports: claude-agent-sdk is optional
# ---------------------------------------------------------------------------

def _import_sdk():
    """Import claude_agent_sdk lazily; raise clear error if missing."""
    try:
        import claude_agent_sdk
        return claude_agent_sdk
    except ImportError:
        raise TransportError(
            "claude-agent-sdk is not installed. "
            "Install it with: pip install claude-agent-sdk"
        )


@dataclass
class SdkTransport(BaseTransport):
    """
    Transport using the official ``claude-agent-sdk`` Python package.

    .. warning::
        NOT TESTED – requires ANTHROPIC_API_KEY.
        Provided as a reference implementation.

    Parameters
    ----------
    name:
        Logical session name.
    cwd:
        Working directory for Claude.
    allowed_tools:
        Tools to auto-approve.
    system_prompt:
        Optional system prompt.
    model:
        Optional model override.
    permission_mode:
        Permission mode (e.g. "acceptEdits", "bypassPermissions").
    max_turns:
        Maximum conversation turns.
    include_partial:
        Whether to include partial message streaming events.
    """

    _name: str
    _cwd: str = "."
    _allowed_tools: list[str] = field(default_factory=list)
    _system_prompt: str = ""
    _model: str = ""
    _permission_mode: str = ""
    _max_turns: int = 0
    _include_partial: bool = False

    # Internal
    _client: Any = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False)
    _events: list[TransportEvent] = field(default_factory=list, init=False, repr=False)
    _session_id: str = field(default="", init=False)

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    async def connect(self, initial_prompt: str = "") -> None:
        """
        Connect to Claude via the SDK client.

        Parameters
        ----------
        initial_prompt:
            Optional prompt to send on connect.
        """
        sdk = _import_sdk()

        options_kwargs: dict[str, Any] = {}
        if self._allowed_tools:
            options_kwargs["allowed_tools"] = self._allowed_tools
        if self._system_prompt:
            options_kwargs["system_prompt"] = self._system_prompt
        if self._model:
            options_kwargs["model"] = self._model
        if self._permission_mode:
            options_kwargs["permission_mode"] = self._permission_mode
        if self._max_turns:
            options_kwargs["max_turns"] = self._max_turns
        if self._include_partial:
            options_kwargs["include_partial_messages"] = True
        if self._cwd and self._cwd != ".":
            options_kwargs["cwd"] = self._cwd

        options = sdk.ClaudeAgentOptions(**options_kwargs)
        self._client = sdk.ClaudeSDKClient(options=options)

        if initial_prompt:
            await self._client.connect(prompt=initial_prompt)
        else:
            await self._client.connect()

        self._connected = True
        logger.info("SdkTransport: connected (name=%s)", self._name)

    async def disconnect(self) -> None:
        """Disconnect from the SDK client."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:
                logger.warning("SdkTransport disconnect error: %s", exc)
            finally:
                self._client = None
                self._connected = False

    # -------------------------------------------------------------------
    # BaseTransport interface
    # -------------------------------------------------------------------

    def send(self, text: str) -> None:
        """Sync send — runs async_send in an event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # If we're already in an async context, create a task
            asyncio.ensure_future(self.async_send(text))
        else:
            asyncio.run(self.async_send(text))

    async def async_send(self, text: str) -> None:
        """Send a query via the SDK client."""
        if not self._client:
            raise TransportError("SdkTransport not connected. Call connect() first.")
        await self._client.query(text)

    def is_alive(self) -> bool:
        return self._connected and self._client is not None

    def kill(self) -> None:
        """Sync kill — runs async disconnect."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            asyncio.ensure_future(self.async_kill())
        else:
            asyncio.run(self.async_kill())

    async def async_kill(self) -> None:
        await self.disconnect()

    @property
    def mode(self) -> TransportMode:
        return TransportMode.SDK

    @property
    def name(self) -> str:
        return self._name

    # -------------------------------------------------------------------
    # Message iteration
    # -------------------------------------------------------------------

    async def receive_messages(self) -> AsyncIterator[Message]:
        """
        Yield structured Messages from the SDK client's response stream.

        This wraps ``client.receive_response()`` and converts SDK message
        types into our unified :class:`Message` format.
        """
        if not self._client:
            raise TransportError("SdkTransport not connected.")

        sdk = _import_sdk()

        async for msg in self._client.receive_response():
            evt = TransportEvent(
                type=type(msg).__name__,
                data={"raw": str(msg)},
            )
            self._events.append(evt)

            # Convert SDK message types to our Message
            if hasattr(sdk, "AssistantMessage") and isinstance(msg, sdk.AssistantMessage):
                text_parts = []
                if hasattr(msg, "content"):
                    for block in msg.content:
                        if hasattr(sdk, "TextBlock") and isinstance(block, sdk.TextBlock):
                            text_parts.append(block.text)
                yield Message(
                    role="assistant",
                    content="\n".join(text_parts),
                    raw={"type": "AssistantMessage"},
                    message_type="text",
                )

            elif hasattr(sdk, "ResultMessage") and isinstance(msg, sdk.ResultMessage):
                result_text = ""
                if hasattr(msg, "result"):
                    result_text = str(msg.result) if msg.result else ""
                yield Message(
                    role="assistant",
                    content=result_text,
                    raw={"type": "ResultMessage"},
                    message_type="result",
                    session_id=getattr(msg, "session_id", ""),
                    cost_usd=getattr(msg, "cost_usd", 0.0),
                    duration_ms=getattr(msg, "duration_ms", 0.0),
                )
                return  # ResultMessage is terminal

            else:
                # Unknown message type — emit as-is
                yield Message(
                    role="system",
                    content=str(msg),
                    raw={"type": type(msg).__name__},
                    message_type="unknown",
                )

    async def send_and_collect(self, text: str, timeout: float = 300.0) -> Message:
        """
        Send a query and collect the full response.
        """
        await self.async_send(text)

        text_parts: list[str] = []
        result_msg: Optional[Message] = None

        async for msg in self.receive_messages():
            if msg.message_type == "text":
                text_parts.append(msg.content)
            elif msg.message_type == "result":
                result_msg = msg
                break

        content = "\n".join(text_parts).strip()
        if result_msg and result_msg.content:
            content = result_msg.content

        return Message(
            role="assistant",
            content=content,
            raw=result_msg.raw if result_msg else None,
            message_type="result",
            session_id=result_msg.session_id if result_msg else "",
            cost_usd=result_msg.cost_usd if result_msg else 0.0,
            duration_ms=result_msg.duration_ms if result_msg else 0.0,
        )

    # -------------------------------------------------------------------
    # Convenience
    # -------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def all_events(self) -> list[TransportEvent]:
        return list(self._events)

    def __repr__(self) -> str:
        return (
            f"SdkTransport(name={self._name!r}, "
            f"connected={self._connected}, mode=sdk)"
        )
