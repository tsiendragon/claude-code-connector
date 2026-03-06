"""
session.py
----------
High-level ``ClaudeSession`` – the main public API.

A ``ClaudeSession`` wraps a ``TmuxTransport`` and adds:
  - ``send_and_wait()``: send a message and block until Claude is done.
  - ``capture()``:       return the current pane text (ANSI stripped).
  - ``tail()``:          return only the *new* lines since last capture.
  - Ready-state polling via ``parser.detect_ready()``.
  - Session metadata persistence via ``store.SessionStore``.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from claude_cli_connector.exceptions import SessionTimeoutError, SessionNotFoundError
from claude_cli_connector.history import ConversationLogger
from claude_cli_connector.parser import (
    detect_ready,
    detect_choices,
    detect_permission,
    strip_ansi_lines,
    extract_last_response,
    ChoiceItem,
    PermissionPrompt,
)
from claude_cli_connector.store import SessionRecord, SessionStore, get_default_store
from claude_cli_connector.transport import TmuxTransport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_READY_TIMEOUT = 300.0    # seconds to wait for Claude to respond
DEFAULT_POLL_INTERVAL = 0.3      # seconds between pane captures while polling
DEFAULT_STABLE_SECS = 0.8        # seconds of output stability = done
DEFAULT_STARTUP_WAIT = 2.0       # seconds to wait for Claude to boot up


class ClaudeSession:
    """
    A managed Claude Code CLI session running inside a tmux pane.

    Typical usage::

        # Start a new session
        session = ClaudeSession.create(name="myproject", cwd="/path/to/repo")

        # Wait for Claude CLI to finish starting up
        session.wait_ready(timeout=10)

        # Send a message and wait for the full response
        response = session.send_and_wait("Explain the main entry point")
        print(response)

        # Continue the conversation
        response2 = session.send_and_wait("Now write unit tests for it")

        # When done
        session.kill()

    Reconnecting after a process restart::

        session = ClaudeSession.attach("myproject")
        response = session.send_and_wait("Continue where we left off")
    """

    def __init__(
        self,
        transport: TmuxTransport,
        store: Optional[SessionStore] = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        stable_secs: float = DEFAULT_STABLE_SECS,
        enable_history: bool = True,
    ) -> None:
        self._transport = transport
        self._store = store or get_default_store()
        self._ready_timeout = ready_timeout
        self._poll_interval = poll_interval
        self._stable_secs = stable_secs

        # Cursor: line count of the last captured snapshot (for tail()).
        self._last_line_count: int = 0
        self._last_lines: list[str] = []

        # Conversation history logger
        self._history: Optional[ConversationLogger] = None
        if enable_history:
            self._history = ConversationLogger(
                session_name=transport.logical_name,
                transport="tmux",
            )

    # ------------------------------------------------------------------
    # Factory class methods
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        cwd: str = ".",
        command: str = "claude",
        backend: str = "claude",
        startup_wait: float = DEFAULT_STARTUP_WAIT,
        store: Optional[SessionStore] = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        stable_secs: float = DEFAULT_STABLE_SECS,
    ) -> "ClaudeSession":
        """
        Create a new tmux session and start Claude CLI inside it.

        Parameters
        ----------
        name:
            Unique logical name for this session.
        cwd:
            Working directory (passed to tmux / Claude CLI).
        command:
            The CLI executable (default: ``"claude"``).
        backend:
            Backend type: ``"claude"`` or ``"cursor"`` (default: ``"claude"``).
        startup_wait:
            Seconds to sleep after spawning to let the CLI initialise.
        store:
            Custom :class:`~store.SessionStore`.  Defaults to the process-
            level store (~/.local/share/claude-cli-connector/sessions.json).
        """
        transport = TmuxTransport.create(name=name, cwd=cwd, command=command)

        _store = store or get_default_store()
        record = SessionRecord(
            name=name,
            tmux_session_name=transport.tmux_session_name,
            cwd=cwd,
            command=command,
            backend=backend,
        )
        _store.save(record)

        session = cls(
            transport=transport,
            store=_store,
            ready_timeout=ready_timeout,
            poll_interval=poll_interval,
            stable_secs=stable_secs,
        )

        logger.info("Created Claude session '%s' in '%s'", name, cwd)

        # Give Claude CLI time to boot before returning.
        if startup_wait > 0:
            time.sleep(startup_wait)

        return session

    @classmethod
    def attach(
        cls,
        name: str,
        store: Optional[SessionStore] = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        stable_secs: float = DEFAULT_STABLE_SECS,
    ) -> "ClaudeSession":
        """
        Attach to an existing Claude CLI session.

        Looks up the session by *name* in the store and reconnects to the
        corresponding tmux session.

        Raises
        ------
        SessionNotFoundError
            If no session with *name* exists in the store or in tmux.
        """
        _store = store or get_default_store()
        record = _store.get(name)
        if record is None:
            raise SessionNotFoundError(
                f"No session named '{name}' found in the store. "
                "Run ClaudeSession.create() first."
            )

        transport = TmuxTransport.attach(name=name)
        logger.info("Attached to existing Claude session '%s'", name)

        return cls(
            transport=transport,
            store=_store,
            ready_timeout=ready_timeout,
            poll_interval=poll_interval,
            stable_secs=stable_secs,
        )

    # ------------------------------------------------------------------
    # Core interaction API
    # ------------------------------------------------------------------

    def send(self, text: str, enter: bool = True) -> None:
        """
        Send *text* to the Claude CLI pane (non-blocking).

        Use :meth:`send_and_wait` for the common case of sending a message
        and waiting for the full response.
        """
        self._transport.send_keys(text, enter=enter)
        self._store.touch(self.name)
        if self._history:
            self._history.log_user(text)

    def wait_ready(
        self,
        timeout: Optional[float] = None,
        initial_delay: float = 0.5,
    ) -> str:
        """
        Block until Claude CLI appears to be ready for input.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait.  Defaults to ``self._ready_timeout``.
        initial_delay:
            Seconds to sleep before starting to poll (allows Claude to start
            producing output before we check).

        Returns
        -------
        str
            The cleaned pane text when Claude became ready.

        Raises
        ------
        SessionTimeoutError
            If Claude does not become ready within *timeout* seconds.
        """
        timeout = timeout or self._ready_timeout
        time.sleep(initial_delay)

        start = time.monotonic()
        prev_lines: Optional[list[str]] = None

        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise SessionTimeoutError(
                    f"Claude session '{self.name}' did not become ready "
                    f"within {timeout:.1f}s."
                )

            snapshot = self._transport.capture()
            current_lines = strip_ansi_lines(snapshot.lines)

            result = detect_ready(
                lines=current_lines,
                prev_lines=prev_lines,
                elapsed=elapsed,
                min_stable_secs=self._stable_secs,
            )

            logger.debug(
                "wait_ready: elapsed=%.2fs confidence=%s ready=%s",
                elapsed,
                result.confidence,
                result.is_ready,
            )

            if result.is_ready:
                self._last_lines = current_lines
                self._last_line_count = len(current_lines)
                return result.snapshot_text

            prev_lines = current_lines
            time.sleep(self._poll_interval)

    def send_and_wait(
        self,
        text: str,
        timeout: Optional[float] = None,
        initial_delay: float = 0.8,
    ) -> str:
        """
        Send *text* and block until Claude has finished responding.

        This is the primary method for interacting with Claude.

        If a **permission prompt** is detected instead of a completed response,
        ``send_and_wait`` returns a special string starting with
        ``"[PERMISSION_REQUIRED]"`` so the caller can decide how to handle it.

        Parameters
        ----------
        text:
            The message to send.
        timeout:
            Maximum seconds to wait for a response.
        initial_delay:
            Seconds to wait after sending before polling starts (gives
            Claude time to begin generating before we check for stability).

        Returns
        -------
        str
            The assistant's response (best-effort extraction from pane text),
            or a ``[PERMISSION_REQUIRED] ...`` string if a tool approval
            prompt was detected.
        """
        # Capture state before sending so we can diff later.
        before_snapshot = self._transport.capture()
        before_lines = strip_ansi_lines(before_snapshot.lines)

        self.send(text)

        # Wait for the pane content to actually change after sending.
        # This prevents wait_ready() from returning immediately if the
        # old idle ❯ prompt is still visible before Claude starts processing.
        _change_deadline = time.monotonic() + min(initial_delay + 5.0, timeout or self._ready_timeout)
        time.sleep(initial_delay)
        while time.monotonic() < _change_deadline:
            snap = self._transport.capture()
            current = strip_ansi_lines(snap.lines)
            if current != before_lines:
                break
            time.sleep(0.2)

        full_text = self.wait_ready(timeout=timeout, initial_delay=1.5)

        after_lines = strip_ansi_lines(self._last_lines)
        # Pass backend hint for accurate extraction.
        record = self._store.get(self.name)
        backend = record.backend if record else ""

        # Check for a permission prompt — if detected, the "ready" state
        # was actually a permission gate, not a completed response.
        perm = detect_permission(after_lines, backend=backend)
        if perm:
            opts_str = ", ".join(
                f"{o.key}={o.label}" for o in perm.options
            )
            return (
                f"[PERMISSION_REQUIRED] {perm.tool}: {perm.action}\n"
                f"Options: {opts_str}\n"
                f"Use 'ccc approve {self.name}' to respond."
            )

        response = extract_last_response(after_lines, backend=backend)

        if self._history and response:
            self._history.log_assistant(response)

        return response

    # ------------------------------------------------------------------
    # Capture / tail helpers
    # ------------------------------------------------------------------

    def capture(self) -> str:
        """
        Return the full current pane content (ANSI stripped).
        """
        snapshot = self._transport.capture()
        lines = strip_ansi_lines(snapshot.lines)
        self._last_lines = lines
        self._last_line_count = len(lines)
        return "\n".join(lines)

    def tail(self, lines: int = 40) -> str:
        """
        Return the last *lines* lines of the pane (ANSI stripped).
        """
        snapshot = self._transport.capture()
        clean = strip_ansi_lines(snapshot.lines)
        return "\n".join(clean[-lines:])

    def new_output_since_last_capture(self) -> str:
        """
        Return any new lines that appeared since the last ``capture()`` or
        ``send_and_wait()`` call.
        """
        snapshot = self._transport.capture()
        current = strip_ansi_lines(snapshot.lines)
        new_lines = current[self._last_line_count:]
        self._last_lines = current
        self._last_line_count = len(current)
        return "\n".join(new_lines)

    # ------------------------------------------------------------------
    # Control operations
    # ------------------------------------------------------------------

    def detect_choices(self) -> list[ChoiceItem] | None:
        """
        Check whether the current pane shows an interactive selection menu.

        Returns a list of :class:`~parser.ChoiceItem` when Claude is presenting
        choices (model selection, command confirmation, etc.), or ``None`` if
        there is no active menu.

        Example::

            choices = session.detect_choices()
            if choices:
                # Auto-select the first option
                session.send(choices[0].key)
        """
        snapshot = self._transport.capture()
        return detect_choices(snapshot.lines)

    def interrupt(self) -> None:
        """Send Ctrl-C to the Claude CLI (cancel current operation)."""
        self._transport.send_ctrl("c")
        logger.info("Sent interrupt (Ctrl-C) to session '%s'", self.name)

    def is_alive(self) -> bool:
        """Return True if the underlying tmux session is still running."""
        return self._transport.is_alive()

    def is_ready(self) -> bool:
        """
        Non-blocking check: is Claude currently ready for input?

        Returns True if a prompt pattern is detected; False otherwise.
        """
        snapshot = self._transport.capture()
        lines = strip_ansi_lines(snapshot.lines)
        result = detect_ready(lines=lines, elapsed=999.0, min_stable_secs=0.0)
        return result.is_ready

    def kill(self) -> None:
        """
        Kill the Claude CLI process and the tmux session.

        Also removes the session record from the store.
        """
        self._transport.kill()
        self._store.delete(self.name)
        logger.info("Killed Claude session '%s'", self.name)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Logical session name."""
        return self._transport.logical_name

    @property
    def transport(self) -> TmuxTransport:
        """Direct access to the underlying :class:`TmuxTransport`."""
        return self._transport

    @property
    def history(self) -> Optional[ConversationLogger]:
        """Conversation history logger (None if ``enable_history=False``)."""
        return self._history

    def __repr__(self) -> str:
        alive = self.is_alive()
        return f"ClaudeSession(name={self.name!r}, alive={alive})"

    def __enter__(self) -> "ClaudeSession":
        return self

    def __exit__(self, *_: object) -> None:
        if self.is_alive():
            self.kill()
