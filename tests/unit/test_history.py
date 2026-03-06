"""
Tests for history.py — ConversationLogger, HistoryEntry, and helpers.
"""

import json
import time
from pathlib import Path

import pytest

from claude_cli_connector.history import (
    ConversationLogger,
    HistoryEntry,
    list_session_runs,
    list_sessions_with_history,
    read_full_session_history,
    read_history_file,
)


class TestHistoryEntry:
    def test_basic_creation(self):
        e = HistoryEntry(role="user", content="hello")
        assert e.role == "user"
        assert e.content == "hello"
        assert e.ts > 0
        assert e.transport == ""
        assert e.metadata == {}

    def test_to_json(self):
        e = HistoryEntry(role="assistant", content="hi", transport="tmux")
        line = e.to_json()
        obj = json.loads(line)
        assert obj["role"] == "assistant"
        assert obj["content"] == "hi"
        assert obj["transport"] == "tmux"
        assert "ts" in obj

    def test_to_json_omits_empty_fields(self):
        e = HistoryEntry(role="user", content="test")
        obj = json.loads(e.to_json())
        assert "metadata" not in obj
        assert "tool_name" not in obj
        assert "event_type" not in obj

    def test_from_json_roundtrip(self):
        original = HistoryEntry(
            role="assistant",
            content="The answer is 42",
            transport="stream-json",
            event_type="response",
            session_name="demo",
            metadata={"cost_usd": 0.005},
        )
        line = original.to_json()
        restored = HistoryEntry.from_json(line)
        assert restored.role == original.role
        assert restored.content == original.content
        assert restored.transport == original.transport
        assert restored.event_type == original.event_type
        assert restored.metadata["cost_usd"] == 0.005


class TestConversationLogger:
    def test_creates_session_dir(self, tmp_path):
        log = ConversationLogger(
            session_name="test-session",
            transport="tmux",
            history_dir=tmp_path,
        )
        assert (tmp_path / "test-session").is_dir()

    def test_creates_jsonl_file(self, tmp_path):
        log = ConversationLogger(
            session_name="test",
            history_dir=tmp_path,
            run_id="2026-03-06T12-00-00",
        )
        log.log_user("hello")
        assert log.file_path.exists()
        assert log.file_path.name == "2026-03-06T12-00-00.jsonl"

    def test_log_user(self, tmp_path):
        log = ConversationLogger(session_name="s", history_dir=tmp_path)
        log.log_user("hello world")

        entries = log.read()
        assert len(entries) == 1
        assert entries[0].role == "user"
        assert entries[0].content == "hello world"
        assert entries[0].event_type == "send"
        assert entries[0].session_name == "s"

    def test_log_assistant(self, tmp_path):
        log = ConversationLogger(session_name="s", history_dir=tmp_path)
        log.log_assistant("The answer is 42", cost_usd=0.001)

        entries = log.read()
        assert len(entries) == 1
        assert entries[0].role == "assistant"
        assert entries[0].content == "The answer is 42"
        assert entries[0].event_type == "response"
        assert entries[0].metadata["cost_usd"] == 0.001

    def test_log_tool(self, tmp_path):
        log = ConversationLogger(session_name="s", history_dir=tmp_path)
        log.log_tool("Bash", "ls -la", exit_code=0)

        entries = log.read()
        assert len(entries) == 1
        assert entries[0].role == "tool"
        assert entries[0].metadata["tool_name"] == "Bash"

    def test_log_state(self, tmp_path):
        log = ConversationLogger(session_name="s", history_dir=tmp_path)
        log.log_state("ready", confidence="prompt")

        entries = log.read()
        assert entries[0].content == "state=ready"
        assert entries[0].event_type == "state"

    def test_log_error(self, tmp_path):
        log = ConversationLogger(session_name="s", history_dir=tmp_path)
        log.log_error("timeout after 300s")

        entries = log.read()
        assert entries[0].event_type == "error"
        assert "timeout" in entries[0].content

    def test_multi_turn_conversation(self, tmp_path):
        log = ConversationLogger(session_name="s", history_dir=tmp_path)
        log.log_user("what is 2+2?")
        log.log_assistant("4")
        log.log_user("and 3+3?")
        log.log_assistant("6")

        entries = log.read()
        assert len(entries) == 4
        assert entries[0].role == "user"
        assert entries[1].role == "assistant"
        assert entries[2].role == "user"
        assert entries[3].role == "assistant"
        assert entries[1].content == "4"
        assert entries[3].content == "6"

    def test_read_last(self, tmp_path):
        log = ConversationLogger(session_name="s", history_dir=tmp_path)
        for i in range(10):
            log.log_user(f"msg {i}")

        last_3 = log.read_last(3)
        assert len(last_3) == 3
        assert last_3[0].content == "msg 7"
        assert last_3[2].content == "msg 9"

    def test_properties(self, tmp_path):
        log = ConversationLogger(
            session_name="myproj",
            transport="stream-json",
            history_dir=tmp_path,
            run_id="run-1",
        )
        assert log.session_name == "myproj"
        assert log.run_id == "run-1"
        assert "myproj" in repr(log)

    def test_transport_field_set(self, tmp_path):
        log = ConversationLogger(
            session_name="s",
            transport="stream-json",
            history_dir=tmp_path,
        )
        log.log_user("test")
        entries = log.read()
        assert entries[0].transport == "stream-json"


class TestReadHistoryFile:
    def test_nonexistent_file(self, tmp_path):
        entries = read_history_file(tmp_path / "nonexistent.jsonl")
        assert entries == []

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        entries = read_history_file(f)
        assert entries == []

    def test_malformed_lines_skipped(self, tmp_path):
        f = tmp_path / "bad.jsonl"
        f.write_text('{"ts": 1.0, "role": "user", "content": "ok"}\nnot-json\n')
        entries = read_history_file(f)
        assert len(entries) == 1
        assert entries[0].content == "ok"


class TestListHelpers:
    def test_list_session_runs(self, tmp_path):
        session_dir = tmp_path / "proj"
        session_dir.mkdir()
        (session_dir / "run-a.jsonl").write_text("")
        (session_dir / "run-b.jsonl").write_text("")
        (session_dir / "not-jsonl.txt").write_text("")

        runs = list_session_runs("proj", history_dir=tmp_path)
        assert len(runs) == 2
        names = [r.stem for r in runs]
        assert "run-a" in names
        assert "run-b" in names

    def test_list_session_runs_no_dir(self, tmp_path):
        runs = list_session_runs("nonexistent", history_dir=tmp_path)
        assert runs == []

    def test_list_sessions_with_history(self, tmp_path):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "run.jsonl").write_text("")
        (tmp_path / "beta").mkdir()
        (tmp_path / "beta" / "run.jsonl").write_text("")
        (tmp_path / "empty").mkdir()  # no jsonl files

        sessions = list_sessions_with_history(history_dir=tmp_path)
        assert sessions == ["alpha", "beta"]

    def test_list_sessions_no_dir(self, tmp_path):
        sessions = list_sessions_with_history(history_dir=tmp_path / "nope")
        assert sessions == []

    def test_read_full_session_history(self, tmp_path):
        session_dir = tmp_path / "proj"
        session_dir.mkdir()

        # Two run files
        e1 = HistoryEntry(ts=100.0, role="user", content="first")
        e2 = HistoryEntry(ts=200.0, role="assistant", content="second")
        (session_dir / "run-a.jsonl").write_text(e1.to_json() + "\n")
        (session_dir / "run-b.jsonl").write_text(e2.to_json() + "\n")

        entries = read_full_session_history("proj", history_dir=tmp_path)
        assert len(entries) == 2
        assert entries[0].content == "first"
        assert entries[1].content == "second"
        # Should be sorted by timestamp
        assert entries[0].ts < entries[1].ts
