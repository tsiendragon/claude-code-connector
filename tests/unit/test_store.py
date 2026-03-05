"""Tests for store.py — JSON session metadata persistence."""

import json
import pytest
from pathlib import Path

from claude_cli_connector.store import SessionRecord, SessionStore


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.json")


def _rec(name: str, cwd: str = "/tmp") -> SessionRecord:
    return SessionRecord(name=name, tmux_session_name=f"ccc-{name}", cwd=cwd)


class TestSaveAndGet:
    def test_save_and_get_roundtrip(self, store):
        store.save(_rec("foo", "/repo"))
        got = store.get("foo")
        assert got is not None
        assert got.name == "foo"
        assert got.cwd == "/repo"
        assert got.tmux_session_name == "ccc-foo"

    def test_get_nonexistent_returns_none(self, store):
        assert store.get("does-not-exist") is None

    def test_upsert_overwrites(self, store):
        store.save(_rec("foo", "/old"))
        store.save(_rec("foo", "/new"))
        assert store.get("foo").cwd == "/new"

    def test_multiple_records(self, store):
        store.save(_rec("a"))
        store.save(_rec("b"))
        store.save(_rec("c"))
        assert store.get("b") is not None
        assert len(store.list_all()) == 3


class TestDelete:
    def test_delete_existing(self, store):
        store.save(_rec("bar"))
        assert store.delete("bar") is True
        assert store.get("bar") is None

    def test_delete_nonexistent_returns_false(self, store):
        assert store.delete("ghost") is False

    def test_delete_leaves_others_intact(self, store):
        store.save(_rec("keep"))
        store.save(_rec("remove"))
        store.delete("remove")
        assert store.get("keep") is not None


class TestListAll:
    def test_empty_store(self, store):
        assert store.list_all() == []

    def test_list_all_returns_all_records(self, store):
        store.save(_rec("x"))
        store.save(_rec("y"))
        names = {r.name for r in store.list_all()}
        assert names == {"x", "y"}


class TestAtomicWrite:
    def test_file_is_valid_json_after_writes(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        for i in range(30):
            store.save(_rec(f"s{i}"))
        with open(path) as f:
            data = json.load(f)
        assert len(data) == 30

    def test_no_tmp_file_left_behind(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.save(_rec("cleanup"))
        assert not (tmp_path / "sessions.tmp").exists()


class TestTouch:
    def test_touch_updates_last_seen_at(self, store):
        store.save(_rec("t"))
        before = store.get("t").last_seen_at
        import time; time.sleep(0.01)
        store.touch("t")
        after = store.get("t").last_seen_at
        assert after >= before

    def test_touch_nonexistent_does_not_raise(self, store):
        store.touch("nonexistent")   # should be a no-op
