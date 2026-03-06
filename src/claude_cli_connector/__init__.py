"""
claude-cli-connector
====================
A low-level Python package for interacting with a running Claude Code CLI
session via multiple transport backends.

Transport modes
~~~~~~~~~~~~~~~
  - **tmux** (default):  Interactive tmux pane, screen-scraping via libtmux.
  - **stream-json**:     Subprocess with ``--output-format stream-json`` for
                         structured bidirectional communication.
  - **sdk**:             Official ``claude-agent-sdk`` Python package (async).
                         *Not tested — requires ANTHROPIC_API_KEY.*
  - **acp**:             ACP (Agent Client Protocol) JSON-RPC over stdio.
                         *Not tested — requires ANTHROPIC_API_KEY + claude-agent-acp.*

Quick start (tmux mode)::

    from claude_cli_connector import ClaudeSession

    session = ClaudeSession.create(name="demo", cwd="/my/project")
    response = session.send_and_wait("Explain this codebase")
    print(response)
    session.kill()

Quick start (stream-json mode)::

    from claude_cli_connector import StreamJsonTransport

    t = StreamJsonTransport(_name="demo", _cwd="/my/project")
    t.start()
    msg = t.send_and_collect("Explain this codebase")
    print(msg.content)
    t.kill()
"""

from claude_cli_connector.session import ClaudeSession
from claude_cli_connector.manager import SessionManager
from claude_cli_connector.parser import ChoiceItem, detect_choices, detect_ready

# Transport base types
from claude_cli_connector.transport_base import (
    BaseTransport,
    Message,
    TransportEvent,
    TransportMode,
)

# Concrete transports
from claude_cli_connector.transport_stream import StreamJsonTransport
from claude_cli_connector.transport_sdk import SdkTransport
from claude_cli_connector.transport_acp import AcpTransport

# History
from claude_cli_connector.history import ConversationLogger, HistoryEntry

# Exceptions
from claude_cli_connector.exceptions import (
    ConnectorError,
    SessionNotFoundError,
    SessionAlreadyExistsError,
    SessionTimeoutError,
    TransportError,
)

__all__ = [
    # High-level session API (tmux mode)
    "ClaudeSession",
    "SessionManager",
    # Transport base types
    "BaseTransport",
    "Message",
    "TransportEvent",
    "TransportMode",
    # Concrete transports
    "StreamJsonTransport",
    "SdkTransport",
    "AcpTransport",
    # History
    "ConversationLogger",
    "HistoryEntry",
    # Parser types (re-exported for convenience)
    "ChoiceItem",
    "detect_choices",
    "detect_ready",
    # Exceptions
    "ConnectorError",
    "SessionNotFoundError",
    "SessionAlreadyExistsError",
    "SessionTimeoutError",
    "TransportError",
]

__version__ = "0.2.0"
