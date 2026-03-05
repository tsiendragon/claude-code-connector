"""
transport.py
------------
Low-level tmux transport layer.

Wraps libtmux primitives (Server, Session, Pane) and exposes a minimal,
typed interface used by the higher-level ClaudeSession.

Design notes:
  - One ClaudeSession <-> One tmux session <-> One tmux pane (window 0, pane 0).
  - The tmux session name is namespaced as "ccc-<session_name>" to avoid
    collisions with the user's own tmux sessions.
  - capture_pane() always returns stripped text (ANSI codes removed by tmux's
    `-e` flag being intentionally *omitted*, or via our parser).
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from typing import Optional

import libtmux

from claude_cli_connector.exceptions import TransportError

logger = logging.getLogger(__name__)

# Prefix for all tmux sessions managed by this package.
SESSION_PREFIX = "ccc-"

# Default terminal dimensions for the tmux pane.
DEFAULT_WIDTH = 220
DEFAULT_HEIGHT = 50


@dataclass
class PaneSnapshot:
    """A point-in-time snapshot of a pane's content."""

    lines: list[str]
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def __len__(self) -> int:
        return len(self.lines)


class TmuxTransport:
    """
    Wraps a single tmux session/pane for a Claude CLI process.

    Parameters
    ----------
    tmux_session_name:
        The *full* tmux session name (including the ``ccc-`` prefix).
    server:
        An existing libtmux.Server instance.  If None, a default server is
        created (connects to the running tmux daemon via $TMUX_TMPDIR or the
        default socket).
    """

    def __init__(
        self,
        tmux_session_name: str,
        server: Optional[libtmux.Server] = None,
    ) -> None:
        self._tmux_session_name = tmux_session_name
        self._server: libtmux.Server = server or libtmux.Server()
        self._session: Optional[libtmux.Session] = None
        self._pane: Optional[libtmux.Pane] = None

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        cwd: str,
        command: str = "claude",
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        server: Optional[libtmux.Server] = None,
    ) -> "TmuxTransport":
        """
        Create a new tmux session and start *command* inside it.

        Parameters
        ----------
        name:
            Logical session name (without prefix).
        cwd:
            Working directory for the Claude CLI process.
        command:
            The executable to run (default: ``"claude"``).
        width / height:
            Terminal dimensions.  Claude CLI renders differently depending on
            the terminal width, so use a wide value to minimise wrapping.
        """
        if not shutil.which(command):
            raise TransportError(
                f"Command '{command}' not found in PATH. "
                "Please install Claude Code CLI (npm install -g @anthropic-ai/claude-code)."
            )

        full_name = SESSION_PREFIX + name
        srv = server or libtmux.Server()

        # Fail fast if a session with this name already exists.
        existing = srv.sessions.get(session_name=full_name, default=None)
        if existing is not None:
            raise TransportError(
                f"tmux session '{full_name}' already exists. "
                "Use TmuxTransport.attach() to reconnect."
            )

        logger.debug("Creating tmux session '%s' (cwd=%s, cmd=%s)", full_name, cwd, command)
        session = srv.new_session(
            session_name=full_name,
            start_directory=cwd,
            window_name="claude",
            window_command=command,
            x=width,
            y=height,
        )

        transport = cls(full_name, server=srv)
        transport._session = session
        transport._pane = session.active_window.active_pane
        return transport

    @classmethod
    def attach(
        cls,
        name: str,
        server: Optional[libtmux.Server] = None,
    ) -> "TmuxTransport":
        """
        Attach to an *existing* tmux session by logical name.

        Raises
        ------
        TransportError
            If no matching tmux session is found.
        """
        full_name = SESSION_PREFIX + name
        srv = server or libtmux.Server()
        session = srv.sessions.get(session_name=full_name, default=None)
        if session is None:
            raise TransportError(
                f"No tmux session named '{full_name}' found. "
                "Use TmuxTransport.create() to start a new session."
            )

        transport = cls(full_name, server=srv)
        transport._session = session
        transport._pane = session.active_window.active_pane
        logger.debug("Attached to existing tmux session '%s'", full_name)
        return transport

    # ------------------------------------------------------------------
    # Core transport operations
    # ------------------------------------------------------------------

    @property
    def pane(self) -> libtmux.Pane:
        if self._pane is None:
            raise TransportError("Transport is not connected to a tmux pane.")
        return self._pane

    def send_keys(self, text: str, enter: bool = True, literal: bool = True) -> None:
        """
        Send *text* to the Claude CLI pane.

        Parameters
        ----------
        text:
            The string to send.
        enter:
            If True (default), append an ``Enter`` keystroke.
        literal:
            If True (default), use tmux ``send-keys -l`` to avoid special
            key interpretation (e.g. ``$`` signs in the text).
        """
        logger.debug("send_keys -> %r (enter=%s)", text[:80], enter)
        try:
            self.pane.send_keys(text, enter=enter, suppress_history=False)
        except Exception as exc:
            raise TransportError(f"send_keys failed: {exc}") from exc

    def send_ctrl(self, key: str) -> None:
        """
        Send a control character, e.g. ``send_ctrl('c')`` sends Ctrl-C.
        """
        logger.debug("send_ctrl -> C-%s", key)
        try:
            self.pane.send_keys(f"C-{key}", enter=False)
        except Exception as exc:
            raise TransportError(f"send_ctrl failed: {exc}") from exc

    def capture(
        self,
        start: int = 0,
        end: int = -1,
        strip_ansi: bool = True,
    ) -> PaneSnapshot:
        """
        Capture the current pane content.

        Parameters
        ----------
        start / end:
            Line range within the scrollback buffer (0 = oldest visible line,
            -1 = bottom of pane).  Default captures the full visible area.
        strip_ansi:
            Passed through to libtmux's capture_pane.  When True, tmux is
            asked to strip colour/attribute escape sequences.
        """
        try:
            # libtmux's capture_pane returns a list[str] of lines.
            lines: list[str] = self.pane.capture_pane(start=start, end=end)
        except Exception as exc:
            raise TransportError(f"capture_pane failed: {exc}") from exc

        return PaneSnapshot(lines=lines)

    def is_alive(self) -> bool:
        """Return True if the tmux session still exists."""
        if self._session is None:
            return False
        try:
            name = self._tmux_session_name
            found = self._server.sessions.get(session_name=name, default=None)
            return found is not None
        except Exception:
            return False

    def kill(self) -> None:
        """Kill the tmux session (and the Claude CLI process inside it)."""
        if self._session is not None:
            try:
                self._session.kill_session()
                logger.debug("Killed tmux session '%s'", self._tmux_session_name)
            except Exception as exc:
                raise TransportError(f"kill_session failed: {exc}") from exc
            finally:
                self._session = None
                self._pane = None

    def resize(self, width: int, height: int) -> None:
        """Resize the pane (useful before capturing wide output)."""
        try:
            self.pane.set_width(width)
            self.pane.set_height(height)
        except Exception as exc:
            raise TransportError(f"resize failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def tmux_session_name(self) -> str:
        return self._tmux_session_name

    @property
    def logical_name(self) -> str:
        return self._tmux_session_name.removeprefix(SESSION_PREFIX)

    def __repr__(self) -> str:
        alive = self.is_alive()
        return (
            f"TmuxTransport(name={self.logical_name!r}, "
            f"tmux={self._tmux_session_name!r}, alive={alive})"
        )
