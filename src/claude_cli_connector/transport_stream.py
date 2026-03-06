"""
transport_stream.py
-------------------
Stream-JSON transport: spawns ``claude -p`` with ``--output-format stream-json``
and ``--input-format stream-json`` for bidirectional structured communication.

This is the recommended programmatic mode for Claude Code CLI.  Each line on
stdout is a self-contained JSON object (newline-delimited JSON / NDJSON).

Message flow
~~~~~~~~~~~~
  Python  →  stdin   →  {"type": "user", "message": {...}}
  Python  ←  stdout  ←  {"type": "assistant", ...}  (per-token or per-turn)

References
~~~~~~~~~~
  - https://code.claude.com/docs/en/headless
  - OpenCovibe src-tauri: bidirectional stream-JSON over stdin/stdout
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
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

# ---------------------------------------------------------------------------
# Stream event types emitted by ``claude -p --output-format stream-json``
# ---------------------------------------------------------------------------

# System-level
EVT_SYSTEM_INIT = "system"        # first message with session info
EVT_START = "start"               # conversation turn started
EVT_RESULT = "result"             # final aggregated result

# Streaming content
EVT_CONTENT_BLOCK_START = "content_block_start"
EVT_CONTENT_BLOCK_DELTA = "content_block_delta"
EVT_CONTENT_BLOCK_STOP = "content_block_stop"

# Message-level
EVT_MESSAGE_START = "message_start"
EVT_MESSAGE_DELTA = "message_delta"
EVT_MESSAGE_STOP = "message_stop"

# Agent SDK streaming events (wrapped)
EVT_STREAM_EVENT = "stream_event"


@dataclass
class StreamJsonTransport(BaseTransport):
    """
    Manages a ``claude -p --output-format stream-json`` subprocess.

    Parameters
    ----------
    name:
        Logical session name (used for logging / store).
    cwd:
        Working directory for the Claude CLI process.
    command:
        Path to the Claude CLI executable.
    allowed_tools:
        List of tool names to auto-approve (e.g. ["Bash", "Read", "Edit"]).
    system_prompt:
        Optional system prompt override or append.
    model:
        Optional model name (e.g. "claude-sonnet-4-5-20250929").
    extra_args:
        Additional CLI flags as key→value pairs.
    verbose:
        Whether to pass ``--verbose`` for full turn-by-turn output.
    include_partial:
        Whether to pass ``--include-partial-messages`` for token-level streaming.
    """

    _name: str
    _cwd: str = "."
    _command: str = "claude"
    _allowed_tools: list[str] = field(default_factory=list)
    _system_prompt: str = ""
    _model: str = ""
    _extra_args: dict[str, str] = field(default_factory=dict)
    _verbose: bool = True
    _include_partial: bool = False

    # Internal state
    _process: Optional[subprocess.Popen] = field(default=None, init=False, repr=False)
    _stdout_queue: Queue = field(default_factory=Queue, init=False, repr=False)
    _reader_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _session_id: str = field(default="", init=False)
    _events: list[TransportEvent] = field(default_factory=list, init=False, repr=False)
    _accumulated_text: str = field(default="", init=False, repr=False)

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the ``claude`` subprocess in stream-json mode."""
        cmd = self._build_command()
        logger.info("StreamJson: starting %s", " ".join(cmd))

        if not shutil.which(self._command):
            raise TransportError(
                f"Command '{self._command}' not found in PATH. "
                "Install Claude Code CLI: npm install -g @anthropic-ai/claude-code"
            )

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
            text=True,
            bufsize=1,  # line-buffered
            env={**os.environ},
        )

        # Background reader thread to avoid blocking on stdout
        self._reader_thread = threading.Thread(
            target=self._read_stdout_loop,
            daemon=True,
            name=f"ccc-stream-{self._name}",
        )
        self._reader_thread.start()

    def _build_command(self) -> list[str]:
        """Build the CLI argument list."""
        cmd = [
            self._command, "-p",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
        ]
        if self._verbose:
            cmd.append("--verbose")
        if self._include_partial:
            cmd.append("--include-partial-messages")
        if self._allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self._allowed_tools)])
        if self._model:
            cmd.extend(["--model", self._model])
        if self._system_prompt:
            cmd.extend(["--append-system-prompt", self._system_prompt])

        for key, val in self._extra_args.items():
            cmd.append(f"--{key}")
            if val:
                cmd.append(val)

        return cmd

    def _read_stdout_loop(self) -> None:
        """Background thread: read NDJSON lines from stdout and enqueue."""
        assert self._process and self._process.stdout
        try:
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    self._stdout_queue.put(obj)

                    # Track session ID from init message
                    if obj.get("type") == "system" and obj.get("session_id"):
                        self._session_id = obj["session_id"]
                except json.JSONDecodeError:
                    logger.warning("StreamJson: non-JSON line: %r", line[:200])
        except Exception as exc:
            logger.debug("StreamJson reader exited: %s", exc)
        finally:
            # Sentinel to signal EOF
            self._stdout_queue.put(None)

    # -------------------------------------------------------------------
    # BaseTransport interface
    # -------------------------------------------------------------------

    def send(self, text: str) -> None:
        """Send a user message via stdin (stream-json format)."""
        if not self._process or not self._process.stdin:
            raise TransportError("StreamJson process not started. Call start() first.")

        # For stream-json input format, wrap as a user message
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": text,
            },
        }
        raw = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(raw)
            self._process.stdin.flush()
            logger.debug("StreamJson: sent %d bytes", len(raw))
        except (BrokenPipeError, OSError) as exc:
            raise TransportError(f"Failed to write to stdin: {exc}") from exc

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
                logger.warning("StreamJson kill error: %s", exc)
            finally:
                self._process = None

    @property
    def mode(self) -> TransportMode:
        return TransportMode.STREAM_JSON

    @property
    def name(self) -> str:
        return self._name

    # -------------------------------------------------------------------
    # Event iteration (synchronous)
    # -------------------------------------------------------------------

    def read_events(self, timeout: float = 0.1) -> list[TransportEvent]:
        """
        Non-blocking: drain all available events from the stdout queue.

        Returns an empty list if no events are available.
        """
        events: list[TransportEvent] = []
        while True:
            try:
                obj = self._stdout_queue.get_nowait()
            except Empty:
                break
            if obj is None:
                # EOF sentinel
                events.append(TransportEvent(type="eof"))
                break
            evt = TransportEvent(type=obj.get("type", "unknown"), data=obj)
            events.append(evt)
            self._events.append(evt)
        return events

    def iter_events(self, timeout: float = 300.0) -> Iterator[TransportEvent]:
        """
        Blocking iterator: yield events until a ``result`` or ``eof`` event.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for events.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                obj = self._stdout_queue.get(timeout=min(1.0, deadline - time.monotonic()))
            except Empty:
                continue
            if obj is None:
                yield TransportEvent(type="eof")
                return
            evt = TransportEvent(type=obj.get("type", "unknown"), data=obj)
            self._events.append(evt)
            yield evt
            if evt.type in ("result", "eof"):
                return
        raise TransportError(f"StreamJson: no result within {timeout}s")

    def send_and_collect(self, text: str, timeout: float = 300.0) -> Message:
        """
        Send a message and block until the full response is collected.

        Returns a :class:`Message` with the aggregated assistant text.
        """
        self.send(text)

        full_text_parts: list[str] = []
        tool_uses: list[dict] = []
        result_data: dict[str, Any] = {}

        for evt in self.iter_events(timeout=timeout):
            d = evt.data

            if evt.type == EVT_STREAM_EVENT:
                # Unwrap agent SDK streaming events
                inner = d.get("event", {})
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    full_text_parts.append(delta.get("text", ""))

            elif evt.type == EVT_CONTENT_BLOCK_DELTA:
                delta = d.get("delta", {})
                if delta.get("type") == "text_delta":
                    full_text_parts.append(delta.get("text", ""))

            elif evt.type == "assistant":
                # Some modes emit complete assistant messages
                msg = d.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        full_text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_uses.append(block)

            elif evt.type == EVT_RESULT:
                result_data = d
                # result may contain the final text
                if d.get("result"):
                    full_text_parts.append(d["result"])
                break

            elif evt.type == "eof":
                break

        content = "".join(full_text_parts).strip()
        self._accumulated_text = content

        return Message(
            role="assistant",
            content=content,
            raw=result_data or None,
            message_type="result",
            session_id=result_data.get("session_id", self._session_id),
            cost_usd=result_data.get("cost_usd", 0.0),
            duration_ms=result_data.get("duration_ms", 0.0),
        )

    # -------------------------------------------------------------------
    # Async event iteration
    # -------------------------------------------------------------------

    async def async_send(self, text: str) -> None:
        """Async wrapper — delegates to sync send (stdin write is fast)."""
        self.send(text)

    async def async_iter_events(self, timeout: float = 300.0) -> AsyncIterator[TransportEvent]:
        """
        Async iterator: yield events using asyncio.to_thread for queue reads.
        """
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
            evt = TransportEvent(type=obj.get("type", "unknown"), data=obj)
            self._events.append(evt)
            yield evt
            if evt.type in ("result", "eof"):
                return

    async def async_send_and_collect(self, text: str, timeout: float = 300.0) -> Message:
        """Async version of send_and_collect."""
        await self.async_send(text)

        full_text_parts: list[str] = []
        result_data: dict[str, Any] = {}

        async for evt in self.async_iter_events(timeout=timeout):
            d = evt.data
            if evt.type == EVT_STREAM_EVENT:
                inner = d.get("event", {})
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    full_text_parts.append(delta.get("text", ""))
            elif evt.type == EVT_CONTENT_BLOCK_DELTA:
                delta = d.get("delta", {})
                if delta.get("type") == "text_delta":
                    full_text_parts.append(delta.get("text", ""))
            elif evt.type == "assistant":
                msg = d.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        full_text_parts.append(block.get("text", ""))
            elif evt.type == EVT_RESULT:
                result_data = d
                if d.get("result"):
                    full_text_parts.append(d["result"])
                break
            elif evt.type == "eof":
                break

        content = "".join(full_text_parts).strip()

        return Message(
            role="assistant",
            content=content,
            raw=result_data or None,
            message_type="result",
            session_id=result_data.get("session_id", self._session_id),
            cost_usd=result_data.get("cost_usd", 0.0),
            duration_ms=result_data.get("duration_ms", 0.0),
        )

    # -------------------------------------------------------------------
    # Convenience properties
    # -------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def process(self) -> Optional[subprocess.Popen]:
        return self._process

    @property
    def all_events(self) -> list[TransportEvent]:
        """Return all events received so far (for debugging)."""
        return list(self._events)

    def __repr__(self) -> str:
        alive = self.is_alive()
        return (
            f"StreamJsonTransport(name={self._name!r}, "
            f"alive={alive}, session_id={self._session_id!r})"
        )
