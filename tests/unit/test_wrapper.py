"""Basic smoke tests for the ccc Python wrapper."""

import json
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest

import ccc
from ccc import ClaudeSession, SessionManager, CccError


# ---------------------------------------------------------------------------
# _run helper
# ---------------------------------------------------------------------------

def make_result(stdout="", returncode=0, stderr=""):
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


@patch("ccc.subprocess.run")
def test_run_returns_stdout(mock_run):
    mock_run.return_value = make_result(stdout="hello\n")
    result = ccc._run("ps")
    assert result == "hello"
    mock_run.assert_called_once_with(
        ["ccc", "ps"], capture_output=True, text=True, input=None
    )


@patch("ccc.subprocess.run")
def test_run_raises_on_nonzero(mock_run):
    mock_run.return_value = make_result(returncode=1, stderr="Error: session not found")
    with pytest.raises(CccError, match="session not found"):
        ccc._run("send", "noexist", "hello")


# ---------------------------------------------------------------------------
# ClaudeSession
# ---------------------------------------------------------------------------

@patch("ccc.subprocess.run")
def test_create_calls_run(mock_run):
    mock_run.return_value = make_result()
    s = ClaudeSession.create("test", cwd="/tmp")
    assert s.name == "test"
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["ccc", "run", "test"]
    assert "--cwd" in cmd
    assert "/tmp" in cmd


@patch("ccc.subprocess.run")
def test_send_and_wait(mock_run):
    mock_run.return_value = make_result(stdout="The answer is 42")
    s = ClaudeSession.attach("demo")
    result = s.send("what is 6*7")
    assert result == "The answer is 42"
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["ccc", "send", "demo"]


@patch("ccc.subprocess.run")
def test_status_porcelain(mock_run):
    mock_run.return_value = make_result(stdout="ready")
    s = ClaudeSession.attach("demo")
    assert s.status() == "ready"
    cmd = mock_run.call_args[0][0]
    assert "--porcelain" in cmd


@patch("ccc.subprocess.run")
def test_read_parses_json(mock_run):
    payload = {"state": "ready", "lastResponse": "hello"}
    mock_run.return_value = make_result(stdout=json.dumps(payload))
    s = ClaudeSession.attach("demo")
    result = s.read()
    assert result["state"] == "ready"
    cmd = mock_run.call_args[0][0]
    assert "--json" in cmd


@patch("ccc.subprocess.run")
def test_context_manager_kills(mock_run):
    mock_run.return_value = make_result()
    with ClaudeSession.attach("demo") as s:
        pass
    kill_call = mock_run.call_args[0][0]
    assert kill_call == ["ccc", "kill", "demo"]


@patch("ccc.subprocess.run")
def test_context_manager_ignores_kill_error(mock_run):
    mock_run.side_effect = [
        make_result(returncode=1, stderr="dead"),
    ]
    with ClaudeSession.attach("demo"):
        pass  # should not raise


# ---------------------------------------------------------------------------
# relay / stream helpers
# ---------------------------------------------------------------------------

@patch("ccc.subprocess.run")
def test_relay_debate(mock_run):
    mock_run.return_value = make_result(stdout="transcript here")
    result = ccc.relay_debate("Python vs Rust", rounds=2)
    assert result == "transcript here"
    cmd = mock_run.call_args[0][0]
    assert "debate" in cmd
    assert "--rounds" in cmd
    assert "2" in cmd


@patch("ccc.subprocess.run")
def test_stream(mock_run):
    mock_run.return_value = make_result(stdout="response text")
    result = ccc.stream("hello", cwd="/tmp", tools="Bash")
    assert result == "response text"
    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["ccc", "stream"]
    assert "--cwd" in cmd
    assert "--tools" in cmd
