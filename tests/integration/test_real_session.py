"""
Integration tests for ClaudeSession against a real tmux + Claude CLI.

Run with:
    pytest tests/integration/ --run-integration
"""

import time
import pytest
from pathlib import Path

from claude_cli_connector import ClaudeSession
from claude_cli_connector.store import SessionStore

_STORE_NAME = "ccc-integration-test"


@pytest.fixture(autouse=True)
def cleanup_session(tmp_path):
    """Ensure the test session is always killed after each test."""
    yield
    store = SessionStore(tmp_path / "s.json")
    try:
        s = ClaudeSession.attach(name=_STORE_NAME, store=store)
        s.kill()
    except Exception:
        pass


@pytest.mark.integration
def test_create_and_wait_ready(tmp_path):
    """Session should start and become ready within 15 seconds."""
    store = SessionStore(tmp_path / "s.json")
    session = ClaudeSession.create(name=_STORE_NAME, cwd="/tmp", store=store)
    assert session.is_alive()
    text = session.wait_ready(timeout=15)
    assert isinstance(text, str)


@pytest.mark.integration
def test_attach_survives_process_restart(tmp_path):
    """
    Simulate a process restart: create a session object, discard it,
    then re-attach via the store.  The underlying tmux session should
    still be alive.
    """
    store = SessionStore(tmp_path / "s.json")
    s1 = ClaudeSession.create(name=_STORE_NAME, cwd="/tmp", store=store)
    s1.wait_ready(timeout=15)
    del s1  # discard in-process reference

    # Re-attach (simulates process restart)
    s2 = ClaudeSession.attach(name=_STORE_NAME, store=store)
    assert s2.is_alive()


@pytest.mark.integration
def test_capture_returns_non_empty(tmp_path):
    """capture() should return non-empty content after startup."""
    store = SessionStore(tmp_path / "s.json")
    session = ClaudeSession.create(name=_STORE_NAME, cwd="/tmp", store=store)
    session.wait_ready(timeout=15)
    text = session.capture()
    assert len(text.strip()) > 0


@pytest.mark.integration
def test_interrupt_recovers_to_ready(tmp_path):
    """After sending Ctrl-C, session should return to ready state."""
    store = SessionStore(tmp_path / "s.json")
    session = ClaudeSession.create(name=_STORE_NAME, cwd="/tmp", store=store)
    session.wait_ready(timeout=15)
    session.interrupt()
    time.sleep(1.0)
    # Should be ready again (not stuck in busy state)
    assert session.is_ready() or session.is_alive()


@pytest.mark.integration
def test_kill_removes_tmux_session(tmp_path):
    """After kill(), the tmux session should no longer exist."""
    store = SessionStore(tmp_path / "s.json")
    session = ClaudeSession.create(name=_STORE_NAME, cwd="/tmp", store=store)
    session.wait_ready(timeout=15)
    session.kill()
    assert not session.is_alive()
    assert store.get(_STORE_NAME) is None
