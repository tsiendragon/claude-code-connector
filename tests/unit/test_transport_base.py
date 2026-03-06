"""Tests for transport_base.py — Message, TransportEvent, TransportMode."""

from claude_cli_connector.transport_base import Message, TransportEvent, TransportMode


class TestTransportMode:
    def test_enum_values(self):
        assert TransportMode.TMUX == "tmux"
        assert TransportMode.STREAM_JSON == "stream-json"
        assert TransportMode.SDK == "sdk"
        assert TransportMode.ACP == "acp"

    def test_all_modes(self):
        assert len(TransportMode) == 4


class TestMessage:
    def test_basic_message(self):
        msg = Message(role="assistant", content="hello")
        assert msg.role == "assistant"
        assert msg.content == "hello"
        assert msg.raw is None
        assert msg.message_type == ""
        assert msg.is_partial is False
        assert msg.is_error is False

    def test_message_with_metadata(self):
        msg = Message(
            role="assistant",
            content="done",
            message_type="result",
            session_id="ses-123",
            cost_usd=0.0025,
            duration_ms=1500.0,
        )
        assert msg.session_id == "ses-123"
        assert msg.cost_usd == 0.0025
        assert msg.duration_ms == 1500.0

    def test_message_with_tool(self):
        msg = Message(
            role="assistant",
            content="",
            message_type="tool_use",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        assert msg.tool_name == "Bash"
        assert msg.tool_input == {"command": "ls"}


class TestTransportEvent:
    def test_basic_event(self):
        evt = TransportEvent(type="result", data={"text": "ok"})
        assert evt.type == "result"
        assert evt.data["text"] == "ok"
        assert evt.timestamp > 0

    def test_empty_event(self):
        evt = TransportEvent(type="eof")
        assert evt.data == {}
