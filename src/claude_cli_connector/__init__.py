"""
claude-cli-connector
====================
A low-level Python package for interacting with a running Claude Code CLI
session via tmux.

Architecture: tmux-first
  - Claude CLI always runs as the foreground process inside a tmux pane.
  - Python manages the tmux session (send_keys / capture_pane) via libtmux.
  - This package provides the high-level abstractions on top.

Quick start::

    from claude_cli_connector import ClaudeSession

    session = ClaudeSession.create(name="demo", cwd="/my/project")
    response = session.send_and_wait("Explain this codebase")
    print(response)
    session.kill()
"""

from claude_cli_connector.session import ClaudeSession
from claude_cli_connector.manager import SessionManager
from claude_cli_connector.exceptions import (
    ConnectorError,
    SessionNotFoundError,
    SessionTimeoutError,
    TransportError,
)

__all__ = [
    "ClaudeSession",
    "SessionManager",
    "ConnectorError",
    "SessionNotFoundError",
    "SessionTimeoutError",
    "TransportError",
]

__version__ = "0.1.0"
