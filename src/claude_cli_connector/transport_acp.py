"""
transport_acp.py
----------------
ACP (Agent Client Protocol) transport: spawns ``claude-agent-acp`` as a
subprocess and communicates via JSON-RPC over stdin/stdout.

ACP is the protocol used by Zed editor to communicate with Claude agents.
The ``@zed-industries/claude-agent-acp`` package implements an ACP server
that wraps the Claude Agent SDK.

.. warning::

    **NOT TESTED** – requires ``ANTHROPIC_API_KEY`` and the
    ``claude-agent-acp`` binary.  This module is provided as a
    reference implementation; use at your own risk.

Requirements
~~~~~~~~~~~~
  npm install -g @zed-industries/claude-agent-acp
  export ANTHROPIC_API_KEY=sk-...

Protocol
~~~~~~~~
  ACP uses JSON-RPC 2.0 over stdio:

  Request  (Python → stdin):
    {"jsonrpc": "2.0", "id": 1, "method": "agent/prompt", "params": {...}}

  Response (stdout → Python):
    {"jsonrpc": "2.0", "id": 1, "result": {...}}

  Notification (stdout → Python):
    {"jsonrpc": "2.0", "method": "agent/toolCall", "params": {...}}

References
~~~~~~~~~~
  - https://github.com/zed-industries/claude-agent-acp
  - ACP spec: https://agentclientprotocol.org
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Optional

from claude_cli_connector.exceptions import TransportError
from claude_cli_connector.transport_base import (
    BaseTransport,
    Message,
    TransportEvent,
    TransportMode,
)

logger = logging.getLogger(__name__)

# JSON-RPC 2.0 version string
JSONRPC_VERSION = "2.0"

# ACP method names
ACP_NEW_SESSION = "agent/newSession"
ACP_PROMPT = "agent/prompt"
ACP_CANCEL = "agent/cancel"

# ACP notification methods (server → client)
ACP_NOTIFY_TEXT = "agent/text"
ACP_NOTIFY_TOOL_CALL = "agent/toolCall"
ACP_NOTIFY_TOOL_RESULT = "agent/toolResult"
ACP_NOTIFY_STATUS = "agent/status"
ACP_NOTIFY_FINISH = "agent/finish"


@dataclass
class AcpTransport(BaseTransport):
    """
    Transport using the ACP (Agent Client Protocol) over stdio.

    Spawns ``claude-agent-acp`` as a subprocess and communicates using
    JSON-RPC 2.0 messages over stdin/stdout.

    .. warning::
        NOT TESTED – requires ANTHROPIC_API_KEY and claude-agent-acp binary.
        Provided as a reference implementation based on the
        zed-industries/claude-agent-acp source.

    Parameters
    ----------
    name:
        Logical session name.
    command:
        Path to the ACP binary (default: "claude-agent-acp").
    cwd:
        Working directory for the agent.
    env:
        Additional environment variables (e.g. ANTHROPIC_API_KEY).
    """

    _name: str
    _command: str = "claude-agent-acp"
    _cwd: str = "."
    _env: dict[str, str] = field(default_factory=dict)

    # Internal state
    _process: Optional[subprocess.Popen] = field(default=None, init=False, repr=False)
    _stdout_queue: Queue = field(default_factory=Queue, init=False, repr=False)
    _reader_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _request_id: int = field(default=0, init=False)
    _session_id: str = field(default="", init=False)
    _events: list[TransportEvent] = field(default_factory=list, init=False, repr=False)
    _pending_responses: dict[int, Any] = field(default_factory=dict, init=False, repr=False)

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the claude-agent-acp subprocess."""
        if not shutil.which(self._command):
            raise TransportError(
                f"Command '{self._command}' not found in PATH. "
                "Install: npm install -g @zed-industries/claude-agent-acp"
            )

        env = {**os.environ, **self._env}

        if "ANTHROPIC_API_KEY" not in env:
            logger.warning(
                "AcpTransport: ANTHROPIC_API_KEY not set. "
                "The ACP agent will likely fail to authenticate."
            )

        self._process = subprocess.Popen(
            [self._command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
            text=True,
            bufsize=1,
            env=env,
        )

        self._reader_thread = threading.Thread(
            target=self._read_stdout_loop,
            daemon=True,
            name=f"ccc-acp-{self._name}",
        )
        self._reader_thread.start()
        logger.info("AcpTransport: started %s", self._command)

    def _read_stdout_loop(self) -> None:
        """Background thread: read JSON-RPC messages from stdout."""
        assert self._process and self._process.stdout
        try:
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    self._stdout_queue.put(obj)
                except json.JSONDecodeError:
                    logger.warning("AcpTransport: non-JSON line: %r", line[:200])
        except Exception as exc:
            logger.debug("AcpTransport reader exited: %s", exc)
        finally:
            self._stdout_queue.put(None)  # EOF sentinel

    # -------------------------------------------------------------------
    # JSON-RPC helpers
    # -------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _send_rpc(self, method: str, params: dict[str, Any] | None = None) -> int:
        """Send a JSON-RPC 2.0 request and return the request ID."""
        if not self._process or not self._process.stdin:
            raise TransportError("AcpTransport not started.")

        req_id = self._next_id()
        msg: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": req_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        raw = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(raw)
            self._process.stdin.flush()
            logger.debug("ACP → %s (id=%d)", method, req_id)
        except (BrokenPipeError, OSError) as exc:
            raise TransportError(f"ACP stdin write failed: {exc}") from exc

        return req_id

    def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC 2.0 notification (no id, no response expected)."""
        if not self._process or not self._process.stdin:
            raise TransportError("AcpTransport not started.")

        msg: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        raw = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(raw)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TransportError(f"ACP notification failed: {exc}") from exc

    # -------------------------------------------------------------------
    # ACP session management
    # -------------------------------------------------------------------

    def new_session(self) -> str:
        """
        Create a new ACP session.

        Returns the session ID.
        """
        req_id = self._send_rpc(ACP_NEW_SESSION, {
            "context": [],
        })
        # Wait for response
        response = self._wait_for_response(req_id, timeout=30.0)
        self._session_id = response.get("result", {}).get("session_id", "")
        return self._session_id

    def prompt(self, text: str, session_id: str = "") -> int:
        """
        Send a prompt to the ACP agent.

        Returns the request ID for tracking the response.
        """
        sid = session_id or self._session_id
        if not sid:
            # Auto-create session if needed
            sid = self.new_session()

        return self._send_rpc(ACP_PROMPT, {
            "session_id": sid,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        })

    def cancel(self, session_id: str = "") -> None:
        """Cancel the current operation in a session."""
        sid = session_id or self._session_id
        if sid:
            self._send_rpc(ACP_CANCEL, {"session_id": sid})

    # -------------------------------------------------------------------
    # BaseTransport interface
    # -------------------------------------------------------------------

    def send(self, text: str) -> None:
        """Send a prompt via ACP."""
        self.prompt(text)

    def is_alive(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    def kill(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception as exc:
                logger.warning("AcpTransport kill error: %s", exc)
            finally:
                self._process = None

    @property
    def mode(self) -> TransportMode:
        return TransportMode.ACP

    @property
    def name(self) -> str:
        return self._name

    # -------------------------------------------------------------------
    # Event / response handling
    # -------------------------------------------------------------------

    def _wait_for_response(self, req_id: int, timeout: float = 30.0) -> dict:
        """Block until a JSON-RPC response with the given id arrives."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                obj = self._stdout_queue.get(timeout=min(1.0, deadline - time.monotonic()))
            except Empty:
                continue

            if obj is None:
                raise TransportError("ACP process exited while waiting for response")

            # Check if it's a response (has "id")
            if "id" in obj and obj["id"] == req_id:
                if "error" in obj:
                    raise TransportError(
                        f"ACP error (code={obj['error'].get('code')}): "
                        f"{obj['error'].get('message', 'unknown')}"
                    )
                return obj

            # It's a notification — record as event
            evt = TransportEvent(
                type=obj.get("method", "unknown"),
                data=obj.get("params", {}),
            )
            self._events.append(evt)

        raise TransportError(f"ACP response timeout ({timeout}s) for request {req_id}")

    def read_events(self) -> list[TransportEvent]:
        """Non-blocking: drain available notifications from the queue."""
        events: list[TransportEvent] = []
        while True:
            try:
                obj = self._stdout_queue.get_nowait()
            except Empty:
                break
            if obj is None:
                events.append(TransportEvent(type="eof"))
                break
            if "id" in obj:
                # It's a response, stash it
                self._pending_responses[obj["id"]] = obj
            else:
                evt = TransportEvent(
                    type=obj.get("method", "unknown"),
                    data=obj.get("params", {}),
                )
                events.append(evt)
                self._events.append(evt)
        return events

    def iter_events(self, timeout: float = 300.0) -> Iterator[TransportEvent]:
        """Blocking iterator: yield ACP notifications until finish or EOF."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                obj = self._stdout_queue.get(timeout=min(1.0, deadline - time.monotonic()))
            except Empty:
                continue

            if obj is None:
                yield TransportEvent(type="eof")
                return

            if "id" in obj:
                self._pending_responses[obj["id"]] = obj
                continue

            method = obj.get("method", "unknown")
            evt = TransportEvent(type=method, data=obj.get("params", {}))
            self._events.append(evt)
            yield evt

            if method == ACP_NOTIFY_FINISH:
                return

    def send_and_collect(self, text: str, timeout: float = 300.0) -> Message:
        """Send a prompt and collect the full response."""
        self.prompt(text)

        text_parts: list[str] = []
        tool_calls: list[dict] = []

        for evt in self.iter_events(timeout=timeout):
            if evt.type == ACP_NOTIFY_TEXT:
                text_parts.append(evt.data.get("text", ""))
            elif evt.type == ACP_NOTIFY_TOOL_CALL:
                tool_calls.append(evt.data)
            elif evt.type == ACP_NOTIFY_FINISH:
                break
            elif evt.type == "eof":
                break

        content = "".join(text_parts).strip()
        return Message(
            role="assistant",
            content=content,
            raw={"tool_calls": tool_calls} if tool_calls else None,
            message_type="result",
            session_id=self._session_id,
        )

    # -------------------------------------------------------------------
    # Async interface
    # -------------------------------------------------------------------

    async def async_send(self, text: str) -> None:
        self.prompt(text)

    async def async_iter_events(self, timeout: float = 300.0) -> AsyncIterator[TransportEvent]:
        """Async notification iterator."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                obj = await asyncio.to_thread(
                    self._stdout_queue.get, timeout=min(1.0, remaining)
                )
            except Empty:
                continue

            if obj is None:
                yield TransportEvent(type="eof")
                return

            if "id" in obj:
                self._pending_responses[obj["id"]] = obj
                continue

            method = obj.get("method", "unknown")
            evt = TransportEvent(type=method, data=obj.get("params", {}))
            self._events.append(evt)
            yield evt

            if method == ACP_NOTIFY_FINISH:
                return

    async def async_send_and_collect(self, text: str, timeout: float = 300.0) -> Message:
        """Async send and collect."""
        self.prompt(text)

        text_parts: list[str] = []
        tool_calls: list[dict] = []

        async for evt in self.async_iter_events(timeout=timeout):
            if evt.type == ACP_NOTIFY_TEXT:
                text_parts.append(evt.data.get("text", ""))
            elif evt.type == ACP_NOTIFY_TOOL_CALL:
                tool_calls.append(evt.data)
            elif evt.type in (ACP_NOTIFY_FINISH, "eof"):
                break

        content = "".join(text_parts).strip()
        return Message(
            role="assistant",
            content=content,
            raw={"tool_calls": tool_calls} if tool_calls else None,
            message_type="result",
            session_id=self._session_id,
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
        alive = self.is_alive()
        return (
            f"AcpTransport(name={self._name!r}, "
            f"alive={alive}, session_id={self._session_id!r})"
        )
