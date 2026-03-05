"""Tests for session.py — ClaudeSession state machine and flow logic."""

import pytest
from unittest.mock import MagicMock, patch

from claude_cli_connector.session import ClaudeSession
from claude_cli_connector.transport import TmuxTransport, PaneSnapshot
from claude_cli_connector.store import SessionStore
from claude_cli_connector.exceptions import SessionTimeoutError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_transport():
    t = MagicMock(spec=TmuxTransport)
    t.logical_name = "test"
    t.tmux_session_name = "ccc-test"
    t.is_alive.return_value = True
    return t


@pytest.fixture
def session(mock_transport, tmp_path):
    store = SessionStore(tmp_path / "s.json")
    return ClaudeSession(
        transport=mock_transport,
        store=store,
        poll_interval=0.0,   # no sleep in unit tests
        stable_secs=0.0,
    )


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------

class TestSend:
    def test_send_calls_transport_send_keys(self, session, mock_transport):
        session.send("hello")
        mock_transport.send_keys.assert_called_once_with("hello", enter=True)

    def test_send_without_enter(self, session, mock_transport):
        session.send("x", enter=False)
        mock_transport.send_keys.assert_called_once_with("x", enter=False)


# ---------------------------------------------------------------------------
# wait_ready()
# ---------------------------------------------------------------------------

class TestWaitReady:
    def test_returns_when_prompt_detected(self, session, mock_transport):
        mock_transport.capture.side_effect = [
            PaneSnapshot(["⠋ Thinking..."]),      # busy
            PaneSnapshot(["Response text", ">"]), # ready (prompt)
        ]
        with patch("time.sleep"):
            text = session.wait_ready(timeout=5, initial_delay=0)
        assert "Response text" in text

    def test_raises_on_timeout(self, session, mock_transport):
        mock_transport.capture.return_value = PaneSnapshot(["⠋ Thinking..."])
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 0, 999]):
            with pytest.raises(SessionTimeoutError):
                session.wait_ready(timeout=1, initial_delay=0)

    def test_stability_fallback(self, session, mock_transport):
        # Same content twice → stability → ready
        stable = PaneSnapshot(["No prompt here but stable"])
        mock_transport.capture.return_value = stable
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1.0, 1.0]):
            text = session.wait_ready(timeout=5, initial_delay=0)
        assert "stable" in text


# ---------------------------------------------------------------------------
# send_and_wait()
# ---------------------------------------------------------------------------

class TestSendAndWait:
    def test_normal_flow(self, session, mock_transport):
        mock_transport.capture.side_effect = [
            PaneSnapshot(["> "]),                   # before snapshot
            PaneSnapshot(["⠋ Thinking..."]),        # busy
            PaneSnapshot(["Claude: hi there", ">"]),# ready
        ]
        with patch("time.sleep"):
            resp = session.send_and_wait("Hello", initial_delay=0)
        assert "hi there" in resp

    def test_sends_text_to_transport(self, session, mock_transport):
        mock_transport.capture.side_effect = [
            PaneSnapshot([">"]),
            PaneSnapshot(["answer", ">"]),
        ]
        with patch("time.sleep"):
            session.send_and_wait("my question", initial_delay=0)
        mock_transport.send_keys.assert_called_with("my question", enter=True)


# ---------------------------------------------------------------------------
# capture() and tail()
# ---------------------------------------------------------------------------

class TestCapture:
    def test_capture_strips_ansi(self, session, mock_transport):
        mock_transport.capture.return_value = PaneSnapshot(["\x1b[32mGreen\x1b[0m"])
        text = session.capture()
        assert "\x1b" not in text
        assert "Green" in text

    def test_tail_returns_last_n_lines(self, session, mock_transport):
        mock_transport.capture.return_value = PaneSnapshot(
            ["l1", "l2", "l3", "l4", "l5"]
        )
        assert session.tail(lines=3) == "l3\nl4\nl5"

    def test_new_output_since_last_capture(self, session, mock_transport):
        mock_transport.capture.side_effect = [
            PaneSnapshot(["a", "b"]),        # first call sets cursor
            PaneSnapshot(["a", "b", "c"]),   # second call → diff = ["c"]
        ]
        session.capture()   # sets _last_line_count = 2
        new = session.new_output_since_last_capture()
        assert "c" in new
        assert "a" not in new


# ---------------------------------------------------------------------------
# Control operations
# ---------------------------------------------------------------------------

class TestControl:
    def test_interrupt_sends_ctrl_c(self, session, mock_transport):
        session.interrupt()
        mock_transport.send_ctrl.assert_called_once_with("c")

    def test_is_alive_delegates_to_transport(self, session, mock_transport):
        mock_transport.is_alive.return_value = True
        assert session.is_alive() is True
        mock_transport.is_alive.return_value = False
        assert session.is_alive() is False

    def test_kill_calls_transport_kill(self, session, mock_transport, tmp_path):
        session.kill()
        mock_transport.kill.assert_called_once()

    def test_context_manager_kills_on_exit(self, mock_transport, tmp_path):
        store = SessionStore(tmp_path / "s.json")
        with ClaudeSession(mock_transport, store=store) as s:
            pass
        mock_transport.kill.assert_called_once()


# ---------------------------------------------------------------------------
# is_ready()
# ---------------------------------------------------------------------------

class TestIsReady:
    def test_ready_when_prompt_visible(self, session, mock_transport):
        mock_transport.capture.return_value = PaneSnapshot(["Response", ">"])
        assert session.is_ready() is True

    def test_not_ready_when_spinner_visible(self, session, mock_transport):
        mock_transport.capture.return_value = PaneSnapshot(["⠋ Thinking..."])
        assert session.is_ready() is False
