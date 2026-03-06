"""
Tests for transport_stream.py — StreamJsonTransport.

These tests mock subprocess.Popen so they run without an actual Claude CLI.
"""

import json
import subprocess
from io import StringIO
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from claude_cli_connector.transport_stream import (
    StreamJsonTransport,
    EVT_RESULT,
    EVT_STREAM_EVENT,
    EVT_CONTENT_BLOCK_DELTA,
)
from claude_cli_connector.transport_base import TransportMode, Message
from claude_cli_connector.exceptions import TransportError


class TestStreamJsonTransport:

    def test_mode_is_stream_json(self):
        t = StreamJsonTransport(_name="test")
        assert t.mode == TransportMode.STREAM_JSON

    def test_name_property(self):
        t = StreamJsonTransport(_name="my-session")
        assert t.name == "my-session"

    def test_is_alive_before_start(self):
        t = StreamJsonTransport(_name="test")
        assert t.is_alive() is False

    def test_repr(self):
        t = StreamJsonTransport(_name="test")
        r = repr(t)
        assert "StreamJsonTransport" in r
        assert "test" in r

    def test_send_without_start_raises(self):
        t = StreamJsonTransport(_name="test")
        with pytest.raises(TransportError, match="not started"):
            t.send("hello")

    def test_start_command_not_found(self):
        t = StreamJsonTransport(_name="test", _command="nonexistent-claude-xxx")
        with pytest.raises(TransportError, match="not found in PATH"):
            t.start()

    def test_build_command_basic(self):
        t = StreamJsonTransport(_name="test")
        cmd = t._build_command()
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--input-format" in cmd
        assert "--verbose" in cmd

    def test_build_command_with_tools(self):
        t = StreamJsonTransport(
            _name="test",
            _allowed_tools=["Bash", "Read"],
            _model="claude-sonnet-4-5-20250929",
        )
        cmd = t._build_command()
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == "Bash,Read"
        assert "--model" in cmd

    def test_build_command_with_system_prompt(self):
        t = StreamJsonTransport(
            _name="test",
            _system_prompt="You are helpful",
        )
        cmd = t._build_command()
        assert "--append-system-prompt" in cmd

    def test_build_command_with_include_partial(self):
        t = StreamJsonTransport(
            _name="test",
            _include_partial=True,
        )
        cmd = t._build_command()
        assert "--include-partial-messages" in cmd


class TestStreamJsonSend:
    """Test send() with a mocked process."""

    def _make_transport_with_mock_process(self):
        t = StreamJsonTransport(_name="test")
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = StringIO("")
        proc.stderr = StringIO("")
        proc.poll.return_value = None  # alive
        t._process = proc
        return t, proc

    def test_send_writes_json_to_stdin(self):
        t, proc = self._make_transport_with_mock_process()
        t.send("Hello Claude")

        written = proc.stdin.write.call_args[0][0]
        obj = json.loads(written.strip())
        assert obj["type"] == "user"
        assert obj["message"]["role"] == "user"
        assert obj["message"]["content"] == "Hello Claude"

    def test_send_flushes_stdin(self):
        t, proc = self._make_transport_with_mock_process()
        t.send("test")
        proc.stdin.flush.assert_called_once()

    def test_send_broken_pipe_raises(self):
        t, proc = self._make_transport_with_mock_process()
        proc.stdin.write.side_effect = BrokenPipeError("pipe broken")

        with pytest.raises(TransportError, match="Failed to write"):
            t.send("test")


class TestStreamJsonKill:
    def test_kill_terminates_process(self):
        t = StreamJsonTransport(_name="test")
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        t._process = proc

        t.kill()
        proc.terminate.assert_called_once()
        assert t._process is None

    def test_kill_when_no_process(self):
        t = StreamJsonTransport(_name="test")
        t.kill()  # should not raise


class TestStreamJsonEvents:
    def test_read_events_empty(self):
        t = StreamJsonTransport(_name="test")
        events = t.read_events()
        assert events == []

    def test_read_events_from_queue(self):
        t = StreamJsonTransport(_name="test")
        # Manually enqueue some events
        t._stdout_queue.put({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        t._stdout_queue.put({"type": "result", "result": "done"})

        events = t.read_events()
        assert len(events) == 2
        assert events[0].type == "assistant"
        assert events[1].type == "result"

    def test_read_events_eof_sentinel(self):
        t = StreamJsonTransport(_name="test")
        t._stdout_queue.put(None)

        events = t.read_events()
        assert len(events) == 1
        assert events[0].type == "eof"


class TestStreamJsonCollect:
    def test_send_and_collect_with_result(self):
        t = StreamJsonTransport(_name="test")
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.return_value = None
        t._process = proc

        # Pre-fill queue with response events
        t._stdout_queue.put({"type": "result", "result": "The answer is 42", "session_id": "s1"})

        msg = t.send_and_collect("test", timeout=5.0)
        assert msg.role == "assistant"
        assert "42" in msg.content
        assert msg.message_type == "result"

    def test_send_and_collect_with_content_deltas(self):
        t = StreamJsonTransport(_name="test")
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.return_value = None
        t._process = proc

        t._stdout_queue.put({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello "},
        })
        t._stdout_queue.put({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "world"},
        })
        t._stdout_queue.put({"type": "result", "result": ""})

        msg = t.send_and_collect("test", timeout=5.0)
        assert msg.content == "Hello world"

    def test_send_and_collect_with_stream_events(self):
        t = StreamJsonTransport(_name="test")
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.return_value = None
        t._process = proc

        t._stdout_queue.put({
            "type": "stream_event",
            "event": {"delta": {"type": "text_delta", "text": "Hi"}},
        })
        t._stdout_queue.put({"type": "result", "result": ""})

        msg = t.send_and_collect("test", timeout=5.0)
        assert msg.content == "Hi"
