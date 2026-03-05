"""
Tests for transport.py.

These tests use pytest-mock to patch libtmux so they can run without an
actual tmux server.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from claude_cli_connector.transport import TmuxTransport, SESSION_PREFIX
from claude_cli_connector.exceptions import TransportError


@pytest.fixture
def mock_server():
    server = MagicMock()
    server.find_where.return_value = None   # no existing sessions by default
    return server


@pytest.fixture
def mock_pane():
    pane = MagicMock()
    pane.capture_pane.return_value = ["line1", "line2", "> "]
    return pane


@pytest.fixture
def mock_session(mock_pane):
    window = MagicMock()
    window.active_pane = mock_pane
    session = MagicMock()
    session.active_window = window
    return session


class TestTmuxTransportCreate:
    def test_raises_if_command_not_found(self, mock_server):
        with patch("shutil.which", return_value=None):
            with pytest.raises(TransportError, match="not found in PATH"):
                TmuxTransport.create(name="test", cwd=".", server=mock_server)

    def test_raises_if_session_already_exists(self, mock_server):
        mock_server.find_where.return_value = MagicMock()  # session exists
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with pytest.raises(TransportError, match="already exists"):
                TmuxTransport.create(name="test", cwd=".", server=mock_server)

    def test_creates_session_successfully(self, mock_server, mock_session):
        mock_server.new_session.return_value = mock_session
        with patch("shutil.which", return_value="/usr/bin/claude"):
            transport = TmuxTransport.create(name="mytest", cwd="/tmp", server=mock_server)

        assert transport.logical_name == "mytest"
        assert transport.tmux_session_name == f"{SESSION_PREFIX}mytest"
        mock_server.new_session.assert_called_once()


class TestTmuxTransportAttach:
    def test_raises_if_not_found(self, mock_server):
        mock_server.find_where.return_value = None
        with pytest.raises(TransportError, match="No tmux session"):
            TmuxTransport.attach(name="missing", server=mock_server)

    def test_attaches_successfully(self, mock_server, mock_session):
        mock_server.find_where.return_value = mock_session
        transport = TmuxTransport.attach(name="existing", server=mock_server)
        assert transport.logical_name == "existing"


class TestTmuxTransportCapture:
    def test_capture_returns_snapshot(self, mock_server, mock_session, mock_pane):
        mock_server.find_where.return_value = mock_session
        transport = TmuxTransport.attach(name="s", server=mock_server)
        snapshot = transport.capture()
        assert snapshot.lines == ["line1", "line2", "> "]
        assert "line1" in snapshot.text

    def test_capture_raises_on_error(self, mock_server, mock_session, mock_pane):
        mock_pane.capture_pane.side_effect = RuntimeError("tmux gone")
        mock_server.find_where.return_value = mock_session
        transport = TmuxTransport.attach(name="s", server=mock_server)
        with pytest.raises(TransportError, match="capture_pane failed"):
            transport.capture()


class TestTmuxTransportSendKeys:
    def test_send_keys_called(self, mock_server, mock_session, mock_pane):
        mock_server.find_where.return_value = mock_session
        transport = TmuxTransport.attach(name="s", server=mock_server)
        transport.send_keys("hello")
        mock_pane.send_keys.assert_called_once_with("hello", enter=True, suppress_history=False)

    def test_send_keys_raises_on_error(self, mock_server, mock_session, mock_pane):
        mock_pane.send_keys.side_effect = RuntimeError("oh no")
        mock_server.find_where.return_value = mock_session
        transport = TmuxTransport.attach(name="s", server=mock_server)
        with pytest.raises(TransportError, match="send_keys failed"):
            transport.send_keys("hi")


class TestTmuxTransportIsAlive:
    def test_alive_when_session_found(self, mock_server, mock_session):
        mock_server.find_where.return_value = mock_session
        transport = TmuxTransport.attach(name="s", server=mock_server)
        # After attach, is_alive queries the server again
        mock_server.find_where.return_value = mock_session
        assert transport.is_alive() is True

    def test_dead_when_session_gone(self, mock_server, mock_session):
        mock_server.find_where.return_value = mock_session
        transport = TmuxTransport.attach(name="s", server=mock_server)
        mock_server.find_where.return_value = None
        assert transport.is_alive() is False
