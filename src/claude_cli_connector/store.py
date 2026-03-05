"""
store.py
--------
Lightweight JSON-based session metadata store.

Persists session information so that Python processes can reconnect to
existing Claude CLI sessions after restart.

Storage location (in order of preference):
  1. $CCC_STORE_PATH   (env var override)
  2. ~/.local/share/claude-cli-connector/sessions.json
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DEFAULT_STORE_DIR = Path.home() / ".local" / "share" / "claude-cli-connector"
_DEFAULT_STORE_FILE = _DEFAULT_STORE_DIR / "sessions.json"


def _store_path() -> Path:
    env = os.environ.get("CCC_STORE_PATH")
    if env:
        return Path(env)
    return _DEFAULT_STORE_FILE


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class SessionRecord(BaseModel):
    """Persisted metadata for a single Claude CLI session."""

    name: str
    """Logical session name (without the ccc- prefix)."""

    tmux_session_name: str
    """Full tmux session name (with the ccc- prefix)."""

    cwd: str
    """Working directory where Claude was started."""

    command: str = "claude"
    """Command used to start Claude CLI."""

    created_at: float = Field(default_factory=time.time)
    """Unix timestamp of session creation."""

    last_seen_at: float = Field(default_factory=time.time)
    """Unix timestamp of the last successful interaction."""

    extra: dict = Field(default_factory=dict)
    """Arbitrary extra metadata (e.g. project name, task description)."""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SessionStore:
    """
    Simple flat JSON store for session metadata.

    All methods are synchronous and not thread-safe (single-process use).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _store_path()

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load store from %s: %s", self._path, exc)
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            tmp.replace(self._path)
        except OSError as exc:
            logger.error("Failed to save store to %s: %s", self._path, exc)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self, record: SessionRecord) -> None:
        """Upsert *record* into the store."""
        data = self._load()
        data[record.name] = record.model_dump()
        self._save(data)
        logger.debug("Saved session record: %s", record.name)

    def get(self, name: str) -> Optional[SessionRecord]:
        """Return the :class:`SessionRecord` for *name*, or None."""
        data = self._load()
        raw = data.get(name)
        if raw is None:
            return None
        return SessionRecord(**raw)

    def delete(self, name: str) -> bool:
        """Remove *name* from the store.  Returns True if it existed."""
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._save(data)
        logger.debug("Deleted session record: %s", name)
        return True

    def list_all(self) -> list[SessionRecord]:
        """Return all stored session records."""
        data = self._load()
        return [SessionRecord(**v) for v in data.values()]

    def touch(self, name: str) -> None:
        """Update the last_seen_at timestamp for *name*."""
        record = self.get(name)
        if record is not None:
            record.last_seen_at = time.time()
            self.save(record)


# ---------------------------------------------------------------------------
# Module-level default store instance
# ---------------------------------------------------------------------------

_default_store: Optional[SessionStore] = None


def get_default_store() -> SessionStore:
    """Return the process-level default :class:`SessionStore`."""
    global _default_store
    if _default_store is None:
        _default_store = SessionStore()
    return _default_store
