"""Tests for manager.py — multi-session orchestration."""

from unittest.mock import MagicMock, patch, call
import pytest

from claude_cli_connector.manager import SessionManager
from claude_cli_connector.exceptions import SessionAlreadyExistsError, SessionNotFoundError
from claude_cli_connector.store import SessionStore, SessionRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(name: str, alive: bool = True) -> MagicMock:
    """Return a mock ClaudeSession with the given name and alive status."""
    s = MagicMock()
    s.name = name
    s.is_alive.return_value = alive
    return s


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.json")


@pytest.fixture
def manager(store) -> SessionManager:
    return SessionManager(store=store)


# ---------------------------------------------------------------------------
# TestCreate
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_registers_and_returns_session(self, manager):
        mock_session = _make_session("proj")
        with patch(
            "claude_cli_connector.manager.ClaudeSession.create",
            return_value=mock_session,
        ):
            result = manager.create("proj", cwd="/repo")

        assert result is mock_session
        assert "proj" in manager

    def test_create_passes_kwargs_to_session(self, manager):
        mock_session = _make_session("x")
        with patch(
            "claude_cli_connector.manager.ClaudeSession.create",
            return_value=mock_session,
        ) as mock_create:
            manager.create("x", cwd="/tmp", command="my-claude", startup_wait=0.0)

        mock_create.assert_called_once_with(
            name="x",
            cwd="/tmp",
            command="my-claude",
            startup_wait=0.0,
            store=manager._store,
        )

    def test_create_raises_if_duplicate(self, manager):
        mock_session = _make_session("dup")
        with patch(
            "claude_cli_connector.manager.ClaudeSession.create",
            return_value=mock_session,
        ):
            manager.create("dup")

        with pytest.raises(SessionAlreadyExistsError):
            manager.create("dup")

    def test_create_exist_ok_returns_existing(self, manager):
        mock_session = _make_session("dup")
        with patch(
            "claude_cli_connector.manager.ClaudeSession.create",
            return_value=mock_session,
        ):
            s1 = manager.create("dup")
            s2 = manager.create("dup", exist_ok=True)

        assert s1 is s2

    def test_create_increments_len(self, manager):
        assert len(manager) == 0
        s = _make_session("a")
        with patch("claude_cli_connector.manager.ClaudeSession.create", return_value=s):
            manager.create("a")
        assert len(manager) == 1


# ---------------------------------------------------------------------------
# TestAttach
# ---------------------------------------------------------------------------

class TestAttach:
    def test_attach_registers_session(self, manager):
        mock_session = _make_session("old")
        with patch(
            "claude_cli_connector.manager.ClaudeSession.attach",
            return_value=mock_session,
        ):
            result = manager.attach("old")

        assert result is mock_session
        assert "old" in manager

    def test_attach_returns_cached_if_already_registered(self, manager):
        mock_session = _make_session("cached")
        manager._sessions["cached"] = mock_session

        with patch(
            "claude_cli_connector.manager.ClaudeSession.attach",
        ) as mock_attach:
            result = manager.attach("cached")

        mock_attach.assert_not_called()
        assert result is mock_session


# ---------------------------------------------------------------------------
# TestGet
# ---------------------------------------------------------------------------

class TestGet:
    def test_get_returns_registered_session(self, manager):
        s = _make_session("s")
        manager._sessions["s"] = s
        assert manager.get("s") is s

    def test_get_raises_for_unknown(self, manager):
        with pytest.raises(SessionNotFoundError):
            manager.get("nonexistent")


# ---------------------------------------------------------------------------
# TestKill
# ---------------------------------------------------------------------------

class TestKill:
    def test_kill_calls_session_kill(self, manager):
        s = _make_session("k")
        manager._sessions["k"] = s
        manager.kill("k")
        s.kill.assert_called_once()

    def test_kill_removes_from_cache(self, manager):
        s = _make_session("k")
        manager._sessions["k"] = s
        manager.kill("k")
        assert "k" not in manager

    def test_kill_nonexistent_raises(self, manager):
        with pytest.raises(SessionNotFoundError):
            manager.kill("ghost")

    def test_kill_all_kills_all_sessions(self, manager):
        s1 = _make_session("a")
        s2 = _make_session("b")
        manager._sessions["a"] = s1
        manager._sessions["b"] = s2
        manager.kill_all()
        s1.kill.assert_called_once()
        s2.kill.assert_called_once()
        assert len(manager) == 0

    def test_kill_all_continues_after_error(self, manager):
        s1 = _make_session("good")
        s2 = _make_session("bad")
        s2.kill.side_effect = RuntimeError("tmux gone")
        manager._sessions["good"] = s1
        manager._sessions["bad"] = s2
        # Should not raise even though s2 fails
        manager.kill_all()
        s1.kill.assert_called_once()


# ---------------------------------------------------------------------------
# TestBulkOperations
# ---------------------------------------------------------------------------

class TestSendAll:
    def test_send_all_reaches_every_session(self, manager):
        s1 = _make_session("a")
        s2 = _make_session("b")
        manager._sessions["a"] = s1
        manager._sessions["b"] = s2
        manager.send_all("hello")
        s1.send.assert_called_once_with("hello")
        s2.send.assert_called_once_with("hello")

    def test_send_all_continues_after_error(self, manager):
        s1 = _make_session("ok")
        s2 = _make_session("err")
        s2.send.side_effect = OSError("broken pipe")
        manager._sessions["ok"] = s1
        manager._sessions["err"] = s2
        manager.send_all("ping")  # should not raise
        s1.send.assert_called_once_with("ping")


class TestCollectResponses:
    def test_collect_responses_maps_name_to_result(self, manager):
        s1 = _make_session("a")
        s1.wait_ready.return_value = "response A"
        s2 = _make_session("b")
        s2.wait_ready.return_value = "response B"
        manager._sessions["a"] = s1
        manager._sessions["b"] = s2

        results = manager.collect_responses()
        assert results["a"] == "response A"
        assert results["b"] == "response B"

    def test_collect_responses_captures_errors(self, manager):
        s = _make_session("bad")
        s.wait_ready.side_effect = TimeoutError("timed out")
        manager._sessions["bad"] = s

        results = manager.collect_responses()
        assert "bad" in results
        assert "<error:" in results["bad"]

    def test_collect_responses_passes_timeout(self, manager):
        s = _make_session("x")
        s.wait_ready.return_value = ""
        manager._sessions["x"] = s

        manager.collect_responses(timeout=42.0)
        s.wait_ready.assert_called_once_with(timeout=42.0)


# ---------------------------------------------------------------------------
# TestIntrospection
# ---------------------------------------------------------------------------

class TestListSessions:
    def test_list_sessions_empty(self, manager):
        assert manager.list_sessions() == []

    def test_list_sessions_returns_names(self, manager):
        manager._sessions["p"] = _make_session("p")
        manager._sessions["q"] = _make_session("q")
        names = set(manager.list_sessions())
        assert names == {"p", "q"}

    def test_list_stored_sessions(self, store, manager):
        store.save(SessionRecord(name="stored", tmux_session_name="ccc-stored", cwd="/tmp"))
        assert "stored" in manager.list_stored_sessions()


class TestPruneDead:
    def test_prune_removes_dead_sessions(self, manager):
        alive = _make_session("alive", alive=True)
        dead = _make_session("dead", alive=False)
        manager._sessions["alive"] = alive
        manager._sessions["dead"] = dead

        pruned = manager.prune_dead()
        assert pruned == ["dead"]
        assert "alive" in manager
        assert "dead" not in manager

    def test_prune_returns_empty_when_all_alive(self, manager):
        manager._sessions["s"] = _make_session("s", alive=True)
        assert manager.prune_dead() == []

    def test_prune_cleans_up_store(self, store, manager):
        store.save(SessionRecord(name="d", tmux_session_name="ccc-d", cwd="/tmp"))
        dead = _make_session("d", alive=False)
        manager._sessions["d"] = dead

        manager.prune_dead()
        assert store.get("d") is None


# ---------------------------------------------------------------------------
# TestDunderMethods
# ---------------------------------------------------------------------------

class TestDunderMethods:
    def test_len(self, manager):
        assert len(manager) == 0
        manager._sessions["x"] = _make_session("x")
        assert len(manager) == 1

    def test_contains(self, manager):
        assert "x" not in manager
        manager._sessions["x"] = _make_session("x")
        assert "x" in manager

    def test_repr(self, manager):
        manager._sessions["foo"] = _make_session("foo")
        r = repr(manager)
        assert "foo" in r
        assert "SessionManager" in r
