"""
manager.py
----------
``SessionManager``: multi-session orchestration.

Provides a registry-like interface for creating, listing, and retrieving
multiple concurrent ``ClaudeSession`` instances.  Useful when running
Claude CLI sessions for different projects or tasks in parallel.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_cli_connector.exceptions import SessionAlreadyExistsError, SessionNotFoundError
from claude_cli_connector.session import ClaudeSession
from claude_cli_connector.store import SessionStore, get_default_store

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages a pool of :class:`~session.ClaudeSession` instances.

    Example::

        manager = SessionManager()

        s1 = manager.create("frontend", cwd="/repo/frontend")
        s2 = manager.create("backend",  cwd="/repo/backend")

        manager.send_all("git pull and tell me what changed")

        for name, response in manager.collect_responses():
            print(f"[{name}] {response}")

        manager.kill_all()
    """

    def __init__(self, store: Optional[SessionStore] = None) -> None:
        self._store = store or get_default_store()
        # In-process cache: name -> ClaudeSession
        self._sessions: dict[str, ClaudeSession] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        cwd: str = ".",
        command: str = "claude",
        startup_wait: float = 2.0,
        exist_ok: bool = False,
    ) -> ClaudeSession:
        """
        Create a new :class:`~session.ClaudeSession` and register it.

        Parameters
        ----------
        name:
            Unique session name.
        cwd:
            Working directory.
        command:
            Claude CLI executable.
        startup_wait:
            Seconds to sleep after spawning.
        exist_ok:
            If True, return the existing session instead of raising.

        Raises
        ------
        SessionAlreadyExistsError
            If a session with *name* already exists and *exist_ok* is False.
        """
        if name in self._sessions:
            if exist_ok:
                logger.debug("Session '%s' already exists, returning existing.", name)
                return self._sessions[name]
            raise SessionAlreadyExistsError(
                f"Session '{name}' already exists in this manager."
            )

        session = ClaudeSession.create(
            name=name,
            cwd=cwd,
            command=command,
            startup_wait=startup_wait,
            store=self._store,
        )
        self._sessions[name] = session
        logger.info("Manager: created session '%s'", name)
        return session

    def attach(self, name: str) -> ClaudeSession:
        """
        Attach to an existing tmux session and register it in this manager.

        If the session is already in the in-process cache, returns it directly.
        """
        if name in self._sessions:
            return self._sessions[name]
        session = ClaudeSession.attach(name=name, store=self._store)
        self._sessions[name] = session
        logger.info("Manager: attached session '%s'", name)
        return session

    def get(self, name: str) -> ClaudeSession:
        """
        Return the session for *name*.

        Raises
        ------
        SessionNotFoundError
            If *name* is not registered in this manager.
        """
        if name not in self._sessions:
            raise SessionNotFoundError(
                f"Session '{name}' is not registered in this manager. "
                "Use create() or attach() first."
            )
        return self._sessions[name]

    def kill(self, name: str) -> None:
        """Kill a session by name and remove it from the manager."""
        session = self.get(name)
        session.kill()
        del self._sessions[name]
        logger.info("Manager: killed session '%s'", name)

    def kill_all(self) -> None:
        """Kill all registered sessions."""
        for name in list(self._sessions):
            try:
                self.kill(name)
            except Exception as exc:
                logger.warning("Failed to kill session '%s': %s", name, exc)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def send_all(self, text: str) -> None:
        """Send *text* to all registered sessions (fire-and-forget)."""
        for name, session in self._sessions.items():
            try:
                session.send(text)
            except Exception as exc:
                logger.warning("send_all: session '%s' failed: %s", name, exc)

    def collect_responses(
        self,
        timeout: Optional[float] = None,
    ) -> dict[str, str]:
        """
        Wait for all sessions to become ready and return their responses.

        Returns a mapping of session name -> response text.
        Sessions that time out or error are included with an error string.
        """
        results: dict[str, str] = {}
        for name, session in self._sessions.items():
            try:
                results[name] = session.wait_ready(timeout=timeout)
            except Exception as exc:
                logger.warning("collect_responses: session '%s' error: %s", name, exc)
                results[name] = f"<error: {exc}>"
        return results

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[str]:
        """Return the names of all registered sessions."""
        return list(self._sessions.keys())

    def list_stored_sessions(self) -> list[str]:
        """Return all session names persisted in the store (may include dead ones)."""
        return [r.name for r in self._store.list_all()]

    def prune_dead(self) -> list[str]:
        """
        Remove sessions that are no longer alive from the in-process cache
        *and* the store.

        Returns the names of the pruned sessions.
        """
        dead = [
            name for name, s in self._sessions.items()
            if not s.is_alive()
        ]
        for name in dead:
            del self._sessions[name]
            self._store.delete(name)
            logger.info("Manager: pruned dead session '%s'", name)
        return dead

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, name: str) -> bool:
        return name in self._sessions

    def __repr__(self) -> str:
        names = list(self._sessions.keys())
        return f"SessionManager(sessions={names})"
