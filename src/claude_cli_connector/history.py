"""
history.py
----------
Conversation logger: persists user↔assistant messages as JSONL files.

Storage layout
~~~~~~~~~~~~~~
  ~/.local/share/claude-cli-connector/history/
    └── {session_name}/
        ├── 2026-03-06T10-30-00.jsonl   ← one file per "run" (create / attach)
        └── 2026-03-06T14-15-22.jsonl

Each line in the JSONL file is a self-contained JSON object::

    {"ts": 1741234567.89, "role": "user",      "content": "explain auth.py", "transport": "tmux"}
    {"ts": 1741234570.12, "role": "assistant",  "content": "The auth module ...", "transport": "tmux"}
    {"ts": 1741234575.00, "role": "system",     "content": "state=ready confidence=prompt", "transport": "tmux"}

Override the storage root with ``$CCC_HISTORY_DIR``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_HISTORY_DIR = Path.home() / ".local" / "share" / "claude-cli-connector" / "history"


def _history_dir() -> Path:
    env = os.environ.get("CCC_HISTORY_DIR")
    if env:
        return Path(env)
    return _DEFAULT_HISTORY_DIR


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class HistoryEntry:
    """A single conversation turn or event."""

    ts: float = field(default_factory=time.time)
    """Unix timestamp."""

    role: str = ""
    """'user', 'assistant', 'system', 'tool'."""

    content: str = ""
    """Text content of the message."""

    transport: str = ""
    """Transport mode: 'tmux', 'stream-json', 'sdk', 'acp'."""

    event_type: str = ""
    """Optional event subtype: 'send', 'response', 'state', 'tool_use', 'error', etc."""

    session_name: str = ""
    """Logical session name."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extra metadata (session_id, cost_usd, duration_ms, tool_name, etc.)."""

    def to_json(self) -> str:
        d = {k: v for k, v in asdict(self).items() if v or k == "ts"}
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "HistoryEntry":
        d = json.loads(line)
        return cls(**d)


# ---------------------------------------------------------------------------
# Conversation Logger
# ---------------------------------------------------------------------------

class ConversationLogger:
    """
    Append-only JSONL logger for one session's conversation history.

    Usage::

        log = ConversationLogger(session_name="myproject", transport="tmux")
        log.log_user("explain auth.py")
        log.log_assistant("The auth module handles JWT tokens...")
        log.log_state("ready", confidence="prompt")

        # Read back
        for entry in log.read():
            print(f"[{entry.role}] {entry.content[:80]}")
    """

    def __init__(
        self,
        session_name: str,
        transport: str = "tmux",
        history_dir: Optional[Path] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self._session_name = session_name
        self._transport = transport

        base = history_dir or _history_dir()
        self._session_dir = base / session_name
        self._session_dir.mkdir(parents=True, exist_ok=True)

        # One file per run
        if run_id is None:
            run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        self._file_path = self._session_dir / f"{run_id}.jsonl"
        self._run_id = run_id

        logger.debug("ConversationLogger: %s → %s", session_name, self._file_path)

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _append(self, entry: HistoryEntry) -> None:
        """Append a single entry to the JSONL file."""
        entry.session_name = self._session_name
        entry.transport = self._transport
        try:
            with self._file_path.open("a", encoding="utf-8") as fh:
                fh.write(entry.to_json() + "\n")
        except OSError as exc:
            logger.warning("Failed to write history: %s", exc)

    def log_user(self, content: str, **meta: Any) -> None:
        """Log a user message (outgoing to Claude)."""
        self._append(HistoryEntry(
            role="user",
            content=content,
            event_type="send",
            metadata=meta,
        ))

    def log_assistant(self, content: str, **meta: Any) -> None:
        """Log an assistant response (incoming from Claude)."""
        self._append(HistoryEntry(
            role="assistant",
            content=content,
            event_type="response",
            metadata=meta,
        ))

    def log_tool(self, tool_name: str, content: str, **meta: Any) -> None:
        """Log a tool use or tool result event."""
        self._append(HistoryEntry(
            role="tool",
            content=content,
            event_type="tool_use",
            metadata={"tool_name": tool_name, **meta},
        ))

    def log_state(self, state: str, **meta: Any) -> None:
        """Log a state change (ready, thinking, dead, etc.)."""
        self._append(HistoryEntry(
            role="system",
            content=f"state={state}",
            event_type="state",
            metadata=meta,
        ))

    def log_error(self, error: str, **meta: Any) -> None:
        """Log an error event."""
        self._append(HistoryEntry(
            role="system",
            content=error,
            event_type="error",
            metadata=meta,
        ))

    def log_event(self, role: str, content: str, event_type: str = "", **meta: Any) -> None:
        """Log a generic event."""
        self._append(HistoryEntry(
            role=role,
            content=content,
            event_type=event_type,
            metadata=meta,
        ))

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read(self) -> list[HistoryEntry]:
        """Read all entries from the current run's JSONL file."""
        return read_history_file(self._file_path)

    def read_last(self, n: int = 20) -> list[HistoryEntry]:
        """Read the last *n* entries from the current run."""
        entries = self.read()
        return entries[-n:]

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def file_path(self) -> Path:
        return self._file_path

    @property
    def session_name(self) -> str:
        return self._session_name

    @property
    def run_id(self) -> str:
        return self._run_id

    def __repr__(self) -> str:
        return (
            f"ConversationLogger(session={self._session_name!r}, "
            f"run={self._run_id!r}, file={self._file_path})"
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def read_history_file(path: Path) -> list[HistoryEntry]:
    """Read all entries from a single JSONL history file."""
    if not path.exists():
        return []
    entries: list[HistoryEntry] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(HistoryEntry.from_json(line))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("Skipping malformed history line: %s", exc)
    return entries


def list_session_runs(session_name: str, history_dir: Optional[Path] = None) -> list[Path]:
    """
    List all JSONL run files for a given session, sorted by name (oldest first).
    """
    base = history_dir or _history_dir()
    session_dir = base / session_name
    if not session_dir.exists():
        return []
    return sorted(session_dir.glob("*.jsonl"))


def list_sessions_with_history(history_dir: Optional[Path] = None) -> list[str]:
    """List all session names that have at least one history file."""
    base = history_dir or _history_dir()
    if not base.exists():
        return []
    return sorted(
        d.name for d in base.iterdir()
        if d.is_dir() and any(d.glob("*.jsonl"))
    )


def read_full_session_history(
    session_name: str,
    history_dir: Optional[Path] = None,
) -> list[HistoryEntry]:
    """Read all entries across all runs for a session, in chronological order."""
    runs = list_session_runs(session_name, history_dir)
    entries: list[HistoryEntry] = []
    for path in runs:
        entries.extend(read_history_file(path))
    entries.sort(key=lambda e: e.ts)
    return entries
