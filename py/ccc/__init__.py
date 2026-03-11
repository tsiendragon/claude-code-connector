"""
ccc — Python wrapper for the ccc CLI (TypeScript implementation).

All session management, parsing, and transport logic lives in the ccc binary.
This package provides a Pythonic API on top of subprocess calls to ccc.

Quick start::

    from ccc import ClaudeSession

    with ClaudeSession.create("demo", cwd="/my/project") as s:
        print(s.send("Explain this codebase"))

Relay::

    from ccc import relay_debate
    print(relay_debate("Python vs Rust for CLI tools", rounds=3))

Stream (one-shot)::

    from ccc import stream
    print(stream("List all TODO comments", cwd="/my/project"))
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CccError(Exception):
    """Raised when the ccc binary exits non-zero."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(*args: str, input: Optional[str] = None) -> str:
    """Run `ccc <args>` and return stdout. Raises CccError on failure."""
    r = subprocess.run(
        ["ccc", *args],
        capture_output=True,
        text=True,
        input=input,
    )
    if r.returncode != 0:
        msg = r.stderr.strip() or r.stdout.strip() or f"ccc {' '.join(args)} exited {r.returncode}"
        raise CccError(msg)
    return r.stdout.strip()


def _run_json(*args: str) -> dict:
    """Run `ccc <args> --json` and return parsed JSON."""
    return json.loads(_run(*args, "--json"))


# ---------------------------------------------------------------------------
# ClaudeSession
# ---------------------------------------------------------------------------

class ClaudeSession:
    """Pythonic handle for a ccc-managed Claude or Cursor tmux session."""

    def __init__(self, name: str) -> None:
        self.name = name

    # ── factory methods ───────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        name: str,
        cwd: Optional[str] = None,
        cursor: bool = False,
        model: Optional[str] = None,
    ) -> "ClaudeSession":
        """Start a new session (runs `ccc run`)."""
        args = ["run", name]
        if cwd:
            args += ["--cwd", cwd]
        if cursor:
            args.append("--cursor")
        if model:
            args += ["--model", model]
        _run(*args)
        return cls(name)

    @classmethod
    def attach(cls, name: str) -> "ClaudeSession":
        """Attach to an existing session (no subprocess call needed)."""
        return cls(name)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def kill(self) -> None:
        _run("kill", self.name)

    def interrupt(self) -> None:
        _run("interrupt", self.name)

    def is_alive(self) -> bool:
        try:
            state = self.status()
            return state != "dead"
        except CccError:
            return False

    # ── messaging ─────────────────────────────────────────────────────────────

    def send(
        self,
        message: str,
        *,
        no_wait: bool = False,
        auto_approve: bool = False,
        timeout: int = 300,
    ) -> str:
        """Send a message; by default waits for ready and returns the response."""
        args = ["send", self.name, message, "--timeout", str(timeout)]
        if no_wait:
            args.append("--no-wait")
        if auto_approve:
            args.append("--auto-approve")
        return _run(*args)

    # Alias for backwards compat with old Python API
    send_and_wait = send

    def last(self) -> str:
        """Extract the last Claude/Cursor response from the pane."""
        return _run("last", self.name)

    # ── inspection ────────────────────────────────────────────────────────────

    def status(self) -> str:
        """Returns: 'ready' | 'thinking' | 'approval' | 'choosing' | 'dead'"""
        return _run("status", self.name, "--porcelain")

    def read(self) -> dict:
        """Structured pane state as a dict (state, permission, choices, etc.)."""
        return _run_json("read", self.name)

    def wait(self, state: str = "ready", timeout: int = 300) -> None:
        """Block until the session reaches the target state."""
        _run("wait", self.name, state, "--timeout", str(timeout))

    def tail(self, lines: int = 40, full: bool = False) -> str:
        """Return last N lines of pane output."""
        args = ["tail", self.name, "--lines", str(lines)]
        if full:
            args.append("--full")
        return _run(*args)

    # ── permissions ───────────────────────────────────────────────────────────

    def approve(self, choice: str = "yes") -> None:
        """Respond to a permission/approval prompt."""
        _run("approve", self.name, choice)

    # ── model ─────────────────────────────────────────────────────────────────

    def switch_model(self, model: str) -> None:
        _run("model", self.name, model)

    def list_models(self) -> str:
        return _run("model", self.name)

    # ── low-level control ─────────────────────────────────────────────────────

    def input(self, text: str, enter: bool = True) -> None:
        args = ["input", self.name, text]
        if not enter:
            args.append("--no-enter")
        _run(*args)

    def key(self, keys: str, repeat: int = 1) -> None:
        args = ["key", self.name, keys]
        if repeat > 1:
            args += ["--repeat", str(repeat)]
        _run(*args)

    # ── context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "ClaudeSession":
        return self

    def __exit__(self, *_) -> None:
        try:
            self.kill()
        except CccError:
            pass

    def __repr__(self) -> str:
        return f"ClaudeSession(name={self.name!r})"


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """Orchestrate multiple ccc sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, ClaudeSession] = {}

    def create(self, name: str, **kwargs) -> ClaudeSession:
        s = ClaudeSession.create(name, **kwargs)
        self._sessions[name] = s
        return s

    def get(self, name: str) -> ClaudeSession:
        if name not in self._sessions:
            self._sessions[name] = ClaudeSession.attach(name)
        return self._sessions[name]

    def send_all(self, message: str, **kwargs) -> None:
        for s in self._sessions.values():
            s.send(message, no_wait=True, **kwargs)

    def collect_responses(self, timeout: int = 120) -> dict[str, str]:
        results: dict[str, str] = {}
        for name, s in self._sessions.items():
            try:
                s.wait(timeout=timeout)
                results[name] = s.last()
            except CccError as exc:
                results[name] = f"ERROR: {exc}"
        return results

    def kill_all(self) -> None:
        for s in self._sessions.values():
            try:
                s.kill()
            except CccError:
                pass
        self._sessions.clear()


# ---------------------------------------------------------------------------
# Relay
# ---------------------------------------------------------------------------

def relay_debate(
    topic: str,
    *,
    role_a: str = "Proponent",
    role_b: str = "Opponent",
    rounds: int = 3,
    model: Optional[str] = None,
) -> str:
    """Run a two-Claude debate via `ccc relay debate`. Returns transcript text."""
    args = [
        "relay", "debate", topic,
        "--role-a", role_a,
        "--role-b", role_b,
        "--rounds", str(rounds),
    ]
    if model:
        args += ["--model", model]
    return _run(*args)


def relay_collab(
    task: str,
    *,
    dev: str = "Developer",
    reviewer: str = "Reviewer",
    rounds: int = 3,
    tools: Optional[str] = None,
) -> str:
    """Run a two-Claude collaboration via `ccc relay collab`. Returns transcript text."""
    args = [
        "relay", "collab", task,
        "--dev", dev,
        "--reviewer", reviewer,
        "--rounds", str(rounds),
    ]
    if tools:
        args += ["--tools", tools]
    return _run(*args)


# ---------------------------------------------------------------------------
# Stream (one-shot)
# ---------------------------------------------------------------------------

def stream(
    prompt: str,
    *,
    cwd: Optional[str] = None,
    tools: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """One-shot stream-json query via `ccc stream`. Returns the final response."""
    args = ["stream", prompt]
    if cwd:
        args += ["--cwd", cwd]
    if tools:
        args += ["--tools", tools]
    if model:
        args += ["--model", model]
    return _run(*args)


# ---------------------------------------------------------------------------
# ps / clean helpers
# ---------------------------------------------------------------------------

def list_sessions() -> str:
    """List all known sessions (equivalent to `ccc ps`)."""
    return _run("ps")


def clean(yes: bool = False, dry_run: bool = False) -> str:
    """Remove dead session records."""
    args = ["clean"]
    if yes:
        args.append("--yes")
    if dry_run:
        args.append("--dry-run")
    return _run(*args)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__version__ = "0.3.0"

__all__ = [
    "ClaudeSession",
    "SessionManager",
    "relay_debate",
    "relay_collab",
    "stream",
    "list_sessions",
    "clean",
    "CccError",
]
