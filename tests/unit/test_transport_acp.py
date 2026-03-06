"""
Tests for transport_acp.py — AcpTransport.

These tests mock subprocess.Popen since we don't have the actual
claude-agent-acp binary or ANTHROPIC_API_KEY in dev.
"""

import json
from unittest.mock import MagicMock
from io import StringIO

import pytest

from claude_cli_connector.transport_acp import (
    AcpTransport,
    JSONRPC_VERSION,
    ACP_PROMPT,
    ACP_NEW_SESSION,
    ACP_NOTIFY_TEXT,
    ACP_NOTIFY_FINISH,
)
from claude_cli_connector.transport_base import TransportMode
from claude_cli_connector.exceptions import TransportError


class TestAcpTransport:

    def test_mode_is_acp(self):
        t = AcpTransport(_name="test")
        assert t.mode == TransportMode.ACP

    def test_name_property(self):
        t = AcpTransport(_name="my-acp")
        assert t.name == "my-acp"

    def test_is_alive_before_start(self):
        t = AcpTransport(_name="test")
        assert t.is_alive() is False

    def test_repr(self):
        t = AcpTransport(_name="test")
        r = repr(t)
        assert "AcpTransport" in r
        assert "test" in r

    def test_start_command_not_found(self):
        t = AcpTransport(_name="test", _command="nonexistent-acp-xxx")
        with pytest.raises(TransportError, match="not found in PATH"):
            t.start()


class TestAcpJsonRpc:
    """Test JSON-RPC message construction."""

    def _make_transport_with_mock(self):
        t = AcpTransport(_name="test")
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = StringIO("")
        proc.stderr = StringIO("")
        proc.poll.return_value = None
        t._process = proc
        return t, proc

    def test_send_rpc_format(self):
        t, proc = self._make_transport_with_mock()

        req_id = t._send_rpc("agent/newSession", {"context": []})

        written = proc.stdin.write.call_args[0][0]
        obj = json.loads(written.strip())
        assert obj["jsonrpc"] == JSONRPC_VERSION
        assert obj["id"] == req_id
        assert obj["method"] == "agent/newSession"
        assert obj["params"] == {"context": []}

    def test_send_rpc_increments_id(self):
        t, proc = self._make_transport_with_mock()

        id1 = t._send_rpc("method1")
        id2 = t._send_rpc("method2")
        assert id2 == id1 + 1

    def test_send_notification_no_id(self):
        t, proc = self._make_transport_with_mock()

        t._send_notification("agent/cancel", {"session_id": "s1"})

        written = proc.stdin.write.call_args[0][0]
        obj = json.loads(written.strip())
        assert "id" not in obj
        assert obj["method"] == "agent/cancel"

    def test_send_rpc_broken_pipe(self):
        t, proc = self._make_transport_with_mock()
        proc.stdin.write.side_effect = BrokenPipeError()

        with pytest.raises(TransportError, match="stdin write failed"):
            t._send_rpc("test")

    def test_send_without_start_raises(self):
        t = AcpTransport(_name="test")
        with pytest.raises(TransportError, match="not started"):
            t._send_rpc("test")


class TestAcpPrompt:
    def _make_transport_with_mock(self):
        t = AcpTransport(_name="test")
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = StringIO("")
        proc.stderr = StringIO("")
        proc.poll.return_value = None
        t._process = proc
        t._session_id = "session-123"
        return t, proc

    def test_prompt_sends_correct_method(self):
        t, proc = self._make_transport_with_mock()

        t.prompt("Hello world")

        written = proc.stdin.write.call_args[0][0]
        obj = json.loads(written.strip())
        assert obj["method"] == ACP_PROMPT
        assert obj["params"]["session_id"] == "session-123"
        messages = obj["params"]["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"][0]["text"] == "Hello world"


class TestAcpKill:
    def test_kill_terminates_process(self):
        t = AcpTransport(_name="test")
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        t._process = proc

        t.kill()
        proc.terminate.assert_called_once()
        assert t._process is None

    def test_kill_when_no_process(self):
        t = AcpTransport(_name="test")
        t.kill()  # should not raise


class TestAcpEvents:
    def test_read_events_empty(self):
        t = AcpTransport(_name="test")
        events = t.read_events()
        assert events == []

    def test_read_events_notifications(self):
        t = AcpTransport(_name="test")
        t._stdout_queue.put({
            "jsonrpc": "2.0",
            "method": ACP_NOTIFY_TEXT,
            "params": {"text": "Hello"},
        })
        t._stdout_queue.put({
            "jsonrpc": "2.0",
            "method": ACP_NOTIFY_FINISH,
            "params": {},
        })

        events = t.read_events()
        assert len(events) == 2
        assert events[0].type == ACP_NOTIFY_TEXT
        assert events[0].data["text"] == "Hello"
        assert events[1].type == ACP_NOTIFY_FINISH

    def test_read_events_response_stashed(self):
        t = AcpTransport(_name="test")
        t._stdout_queue.put({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"session_id": "s1"},
        })

        events = t.read_events()
        assert len(events) == 0  # responses are stashed, not yielded
        assert 1 in t._pending_responses
