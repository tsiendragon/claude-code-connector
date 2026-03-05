"""
demo/server.py
--------------
FastAPI Web UI demo for claude-cli-connector.

Exposes a REST + SSE API on top of ClaudeSession / SessionManager,
and serves a single-page Web UI at GET /.

Usage
-----
    # Install deps first:
    pip install -e ..            # installs claude-cli-connector
    pip install -r requirements.txt

    # Start server:
    python server.py             # → http://localhost:8765

Architecture
------------
  Browser  ──SSE──►  GET /api/sessions/{name}/stream
           ◄──POST─  /api/sessions/{name}/send
           ◄──POST─  /api/sessions (create)
           ◄──DELETE  /api/sessions/{name}

  SSE stream polls the tmux pane every POLL_INTERVAL seconds and pushes
  incremental updates as JSON events.  This is the "real-time output" path.

  All session state lives in the SessionManager (in-process) + the
  SessionStore (JSON on disk).  The server is stateless between restarts
  (sessions survive because they live in tmux).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from claude_cli_connector import ClaudeSession, SessionManager
from claude_cli_connector.exceptions import ConnectorError
from claude_cli_connector.parser import strip_ansi, strip_ansi_lines
from claude_cli_connector.store import get_default_store

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLL_INTERVAL = 0.2        # seconds between pane captures (SSE stream)
DEFAULT_PORT  = int(os.environ.get("CCC_DEMO_PORT", "8765"))
LOG_LEVEL     = os.environ.get("CCC_LOG_LEVEL", "INFO")

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
logger = logging.getLogger("ccc.demo")

# ---------------------------------------------------------------------------
# Choice menu detection helpers
# ---------------------------------------------------------------------------

# Patterns that look like an interactive selection list:
#   "1. some option"  or  "❯ some option"  or  "> some option"
_CHOICE_NUMBER_RE = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")
_CHOICE_ARROW_RE  = re.compile(r"^\s*[❯>►▶]\s+(.+)$")
_CHOICE_BULLET_RE = re.compile(r"^\s*[○●◉◎✓✗]\s+(.+)$")


def detect_choice_menu(lines: list[str]) -> list[dict] | None:
    """
    If the last visible pane lines look like a selection menu, return
    a list of choice dicts: [{"key": "1", "label": "claude-opus-4-5"}, ...]
    Otherwise return None.
    """
    clean = strip_ansi_lines(lines)
    tail = clean[-20:]   # check last 20 lines

    # Numbered list (consecutive matching lines)
    numbered: list[dict] = []
    for line in tail:
        m = _CHOICE_NUMBER_RE.match(line)
        if m:
            numbered.append({"key": m.group(1), "label": m.group(2).strip()})

    if len(numbered) >= 2:
        return numbered

    # Arrow/bullet style — only accept a CONTIGUOUS block.
    # Claude Code uses ❯ as its input prompt too, so scattered ❯ lines
    # throughout the conversation must NOT be treated as a choice menu.
    last_block: list[dict] = []
    current_block: list[dict] = []

    for line in tail:
        m = _CHOICE_ARROW_RE.match(line)
        if m:
            current_block.append({"key": str(len(current_block) + 1),
                                   "label": m.group(1).strip(), "selected": True})
            continue
        mb = _CHOICE_BULLET_RE.match(line)
        if mb:
            current_block.append({"key": str(len(current_block) + 1),
                                   "label": mb.group(1).strip()})
            continue
        if current_block:
            last_block = current_block
            current_block = []

    if current_block:
        last_block = current_block

    return last_block if len(last_block) >= 2 else None


def detect_claude_status(lines: list[str]) -> str:
    """Return 'thinking' | 'ready' | 'choosing' based on pane tail."""
    choices = detect_choice_menu(lines)
    if choices:
        return "choosing"
    clean = strip_ansi_lines(lines)
    tail_text = "\n".join(clean[-6:])
    # Spinner chars
    if re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●]", tail_text):
        return "thinking"
    if re.search(r"Thinking\.\.\.|Generating|Working\.\.\.", tail_text):
        return "thinking"
    if re.search(r"\besc\b.*to interrupt", tail_text, re.IGNORECASE):
        return "thinking"
    # Prompt markers
    if re.search(r"^\s*[╰>─]+\s*>?\s*$", tail_text, re.MULTILINE):
        return "ready"
    if re.search(r"^\s*>\s*$", tail_text, re.MULTILINE):
        return "ready"
    return "thinking"

# ---------------------------------------------------------------------------
# Global session manager (process-level singleton)
# ---------------------------------------------------------------------------

manager = SessionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Re-attach to any sessions that are already alive on startup."""
    store = get_default_store()
    for record in store.list_all():
        try:
            session = ClaudeSession.attach(name=record.name)
            if session.is_alive():
                manager._sessions[record.name] = session
                logger.info("Restored session '%s' on startup", record.name)
        except Exception as e:
            logger.debug("Could not restore session '%s': %s", record.name, e)
    yield
    # cleanup on shutdown (don't kill sessions – they should persist in tmux)
    logger.info("Server shutting down. tmux sessions remain alive.")


app = FastAPI(
    title="claude-cli-connector demo",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    name: str
    cwd: str = "."
    command: str = "claude"

class SendMessageRequest(BaseModel):
    text: str

class SessionInfo(BaseModel):
    name: str
    cwd: str
    alive: bool
    tmux_session_name: str
    created_at: float

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _get_session(name: str) -> ClaudeSession:
    if name not in manager:
        # Try attaching (session might be in store but not in-process cache)
        try:
            session = ClaudeSession.attach(name=name)
            manager._sessions[name] = session
            return session
        except ConnectorError:
            raise HTTPException(404, detail=f"Session '{name}' not found.")
    return manager.get(name)


def _session_info(name: str) -> dict:
    store = get_default_store()
    record = store.get(name)
    session = manager._sessions.get(name)
    alive = session.is_alive() if session else False
    return {
        "name": name,
        "cwd": record.cwd if record else "",
        "alive": alive,
        "tmux_session_name": record.tmux_session_name if record else "",
        "created_at": record.created_at if record else 0,
    }

# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
def list_sessions():
    """List all known sessions (from store + in-process)."""
    store = get_default_store()
    records = store.list_all()
    return [_session_info(r.name) for r in records]


@app.post("/api/sessions", status_code=201)
def create_session(req: CreateSessionRequest):
    """Create a new Claude CLI session."""
    if req.name in manager:
        raise HTTPException(409, detail=f"Session '{req.name}' already exists.")
    try:
        session = manager.create(
            name=req.name,
            cwd=req.cwd,
            command=req.command,
            startup_wait=2.5,
        )
        logger.info("Created session '%s' (cwd=%s)", req.name, req.cwd)
        return _session_info(req.name)
    except ConnectorError as e:
        raise HTTPException(400, detail=str(e))


@app.delete("/api/sessions/{name}")
def kill_session(name: str):
    """Kill a session and remove it."""
    session = _get_session(name)
    session.kill()
    if name in manager._sessions:
        del manager._sessions[name]
    return {"status": "killed", "name": name}


@app.post("/api/sessions/{name}/send")
def send_message(name: str, req: SendMessageRequest):
    """
    Send a message to a Claude CLI session (fire-and-forget).
    The browser gets the response via the SSE stream.
    """
    session = _get_session(name)
    try:
        session.send(req.text)
        return {"status": "sent"}
    except ConnectorError as e:
        raise HTTPException(500, detail=str(e))


@app.post("/api/sessions/{name}/interrupt")
def interrupt_session(name: str):
    """Send Ctrl-C to interrupt the current Claude operation."""
    session = _get_session(name)
    session.interrupt()
    return {"status": "interrupted"}


# ---------------------------------------------------------------------------
# SSE stream  – the core real-time output path
# ---------------------------------------------------------------------------

@app.get("/api/sessions/{name}/stream")
async def stream_session(name: str, request: Request):
    """
    Server-Sent Events stream for real-time pane output.

    Events emitted (as JSON):
      {"type": "output",   "lines": [...], "full_text": "...", "status": "thinking|ready|choosing"}
      {"type": "choices",  "choices": [{"key": "1", "label": "..."}]}
      {"type": "error",    "message": "..."}
      {"type": "dead"}     – session died
    """
    session = _get_session(name)

    async def event_generator() -> AsyncGenerator[dict, None]:
        prev_lines: list[str] = []
        last_choices: list[dict] | None = None

        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                logger.debug("SSE client disconnected for session '%s'", name)
                break

            if not session.is_alive():
                yield {"event": "message", "data": json.dumps({"type": "dead"})}
                break

            try:
                snapshot = session.transport.capture()
                current_lines = strip_ansi_lines(snapshot.lines)
                status = detect_claude_status(snapshot.lines)

                # Detect choice menus
                choices = detect_choice_menu(snapshot.lines)
                if choices != last_choices:
                    last_choices = choices
                    if choices:
                        yield {
                            "event": "message",
                            "data": json.dumps({"type": "choices", "choices": choices}),
                        }

                # Always push the current pane state to keep UI in sync.
                # We send the full text + status every poll so the UI
                # can do a simple replace (avoids diff complexity in JS).
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "type": "output",
                        "lines": current_lines[-200:],   # cap at 200 lines
                        "full_text": "\n".join(current_lines[-200:]),
                        "status": status,
                        "choices": choices,
                    }),
                }

                prev_lines = current_lines

            except ConnectorError as e:
                yield {
                    "event": "message",
                    "data": json.dumps({"type": "error", "message": str(e)}),
                }
                break

            await asyncio.sleep(POLL_INTERVAL)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Web UI  (single-page, inline HTML + JS)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def web_ui():
    return _HTML


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>claude-cli-connector · Web UI</title>
<style>
  :root {
    --bg: #0d1117;
    --bg2: #161b22;
    --bg3: #21262d;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --green: #3fb950;
    --blue: #58a6ff;
    --yellow: #d29922;
    --red: #f85149;
    --purple: #bc8cff;
    --accent: #238636;
    --accent-hover: #2ea043;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--bg2);
    flex-shrink: 0;
  }
  header h1 { font-size: 15px; font-weight: 600; color: var(--blue); }
  .header-sub { font-size: 12px; color: var(--text-muted); }
  header a { color: var(--text-muted); font-size: 12px; text-decoration: none; margin-left: auto; }
  header a:hover { color: var(--blue); }

  /* ── Layout ── */
  .layout {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  /* ── Sidebar ── */
  .sidebar {
    width: 220px;
    border-right: 1px solid var(--border);
    background: var(--bg2);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
  }
  .sidebar-title {
    padding: 10px 12px;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: .5px;
    border-bottom: 1px solid var(--border);
  }
  .session-list {
    flex: 1;
    overflow-y: auto;
  }
  .session-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    cursor: pointer;
    font-size: 13px;
    border-left: 3px solid transparent;
    transition: background .1s;
  }
  .session-item:hover { background: var(--bg3); }
  .session-item.active {
    border-left-color: var(--blue);
    background: var(--bg3);
    color: var(--blue);
  }
  .session-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--text-muted);
    flex-shrink: 0;
  }
  .session-dot.alive { background: var(--green); }
  .session-dot.dead  { background: var(--red); }

  .sidebar-footer {
    padding: 10px 12px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .sidebar-footer input {
    background: var(--bg3);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 4px;
    padding: 5px 8px;
    font-size: 12px;
    width: 100%;
  }
  .sidebar-footer input:focus { outline: none; border-color: var(--blue); }
  .sidebar-footer input::placeholder { color: var(--text-muted); }
  .btn {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 4px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
    width: 100%;
    transition: background .1s;
  }
  .btn:hover { background: var(--accent-hover); }
  .btn.danger { background: #6e2b2b; }
  .btn.danger:hover { background: var(--red); }
  .btn.warn { background: #4a3b00; }
  .btn.warn:hover { background: var(--yellow); color: #000; }

  /* ── Main panel ── */
  .main {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── Toolbar ── */
  .toolbar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--bg2);
    flex-shrink: 0;
    min-height: 44px;
  }
  .toolbar .session-name {
    font-weight: 600;
    font-size: 13px;
    color: var(--blue);
  }
  .status-badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 100px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .status-badge.ready    { background: #1a3826; color: var(--green); }
  .status-badge.thinking { background: #2a2000; color: var(--yellow); }
  .status-badge.choosing { background: #1f1060; color: var(--purple); }
  .spinner {
    display: inline-block;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { from { opacity: 1; } to { opacity: 0.2; } }
  .toolbar-actions { margin-left: auto; display: flex; gap: 8px; }
  .icon-btn {
    background: var(--bg3);
    border: 1px solid var(--border);
    color: var(--text-muted);
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 12px;
    cursor: pointer;
    transition: all .1s;
  }
  .icon-btn:hover { color: var(--text); border-color: var(--text-muted); }
  .icon-btn.red-btn:hover { color: var(--red); border-color: var(--red); }

  /* ── Terminal output area ── */
  .output-wrap {
    flex: 1;
    overflow-y: auto;
    padding: 0;
    background: var(--bg);
  }
  #output {
    font-family: "JetBrains Mono", "Fira Code", "Cascadia Code", "SFMono-Regular",
                 Consolas, monospace;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-all;
    padding: 14px 16px;
    min-height: 100%;
    color: var(--text);
  }
  #output .line-new { color: var(--text); }
  .no-session-msg {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    gap: 12px;
    color: var(--text-muted);
    font-size: 14px;
  }
  .no-session-msg span { font-size: 32px; }

  /* ── Choice buttons ── */
  #choice-bar {
    padding: 10px 14px;
    border-top: 1px solid var(--border);
    background: var(--bg2);
    display: none;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  #choice-bar.visible { display: flex; }
  #choice-bar .choice-label {
    font-size: 12px;
    color: var(--text-muted);
    flex-basis: 100%;
    margin-bottom: 2px;
  }
  .choice-btn {
    background: var(--bg3);
    border: 1px solid var(--purple);
    color: var(--purple);
    border-radius: 6px;
    padding: 5px 14px;
    font-size: 12px;
    cursor: pointer;
    transition: all .1s;
  }
  .choice-btn:hover { background: #2a1a60; border-color: #d0a8ff; color: #d0a8ff; }
  .choice-btn.selected { background: #2a1a60; }

  /* ── Input bar ── */
  .input-bar {
    display: flex;
    gap: 8px;
    padding: 10px 14px;
    border-top: 1px solid var(--border);
    background: var(--bg2);
    flex-shrink: 0;
  }
  #msg-input {
    flex: 1;
    background: var(--bg3);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 14px;
    font-family: inherit;
    resize: none;
    min-height: 42px;
    max-height: 200px;
    transition: border-color .1s;
  }
  #msg-input:focus { outline: none; border-color: var(--blue); }
  #msg-input::placeholder { color: var(--text-muted); }
  #send-btn {
    background: var(--blue);
    color: #000;
    border: none;
    border-radius: 6px;
    padding: 8px 18px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    align-self: flex-end;
    transition: opacity .1s;
    white-space: nowrap;
  }
  #send-btn:hover { opacity: 0.85; }
  #send-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* ── Helpers ── */
  .hint {
    font-size: 11px;
    color: var(--text-muted);
    padding: 0 14px 6px;
    background: var(--bg2);
  }
</style>
</head>
<body>

<header>
  <h1>⚡ claude-cli-connector</h1>
  <span class="header-sub">Web UI Demo</span>
  <a href="https://github.com/your-org/claude-cli-connector" target="_blank">GitHub ↗</a>
</header>

<div class="layout">

  <!-- ── Sidebar ── -->
  <aside class="sidebar">
    <div class="sidebar-title">Sessions</div>
    <div class="session-list" id="session-list">
      <div style="padding:12px;font-size:12px;color:var(--text-muted)">Loading…</div>
    </div>
    <div class="sidebar-footer">
      <input id="new-name" placeholder="session name" maxlength="40">
      <input id="new-cwd"  placeholder="working directory (default: .)" >
      <button class="btn" onclick="createSession()">＋ New Session</button>
    </div>
  </aside>

  <!-- ── Main ── -->
  <div class="main">
    <!-- Toolbar -->
    <div class="toolbar" id="toolbar">
      <div class="no-session-msg" style="font-size:13px;flex-direction:row;height:auto;color:var(--text-muted)">
        ← Select or create a session
      </div>
    </div>

    <!-- Output -->
    <div class="output-wrap" id="output-wrap">
      <div class="no-session-msg">
        <span>🖥️</span>
        <div>Select a session on the left to start</div>
      </div>
    </div>

    <!-- Choice bar -->
    <div id="choice-bar">
      <div class="choice-label">Claude is asking you to choose:</div>
    </div>

    <!-- Input -->
    <div class="input-bar">
      <textarea id="msg-input" placeholder="Ask Claude… (Enter to send, Shift+Enter for newline)" rows="1"
        oninput="autoResize(this)" onkeydown="handleKey(event)"></textarea>
      <button id="send-btn" onclick="sendMessage()" disabled>Send</button>
    </div>
    <div class="hint">Tip: type /model opus · /model sonnet · /model haiku to switch models</div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let activeSession = null;
let sse = null;
let currentStatus = 'ready';
let sessions = {};   // name → {alive, cwd, ...}

// ── Session list ───────────────────────────────────────────────────────────

async function loadSessions() {
  const r = await fetch('/api/sessions');
  const list = await r.json();
  sessions = {};
  list.forEach(s => sessions[s.name] = s);
  renderSessionList();
}

function renderSessionList() {
  const el = document.getElementById('session-list');
  const names = Object.keys(sessions);
  if (!names.length) {
    el.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text-muted)">No sessions yet.</div>';
    return;
  }
  el.innerHTML = names.map(n => {
    const s = sessions[n];
    const active = n === activeSession ? 'active' : '';
    const dot = s.alive ? 'alive' : 'dead';
    return `<div class="session-item ${active}" onclick="switchSession('${n}')">
      <div class="session-dot ${dot}"></div>
      <span>${n}</span>
    </div>`;
  }).join('');
}

async function createSession() {
  const name = document.getElementById('new-name').value.trim();
  const cwd  = document.getElementById('new-cwd').value.trim() || '.';
  if (!name) { alert('Please enter a session name.'); return; }
  const r = await fetch('/api/sessions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, cwd}),
  });
  if (r.ok) {
    document.getElementById('new-name').value = '';
    document.getElementById('new-cwd').value  = '';
    await loadSessions();
    switchSession(name);
  } else {
    const err = await r.json();
    alert('Error: ' + (err.detail || JSON.stringify(err)));
  }
}

async function killSession(name) {
  if (!confirm(`Kill session '${name}'?`)) return;
  await fetch(`/api/sessions/${name}`, {method: 'DELETE'});
  if (activeSession === name) {
    activeSession = null;
    closeSSE();
    renderNoSession();
  }
  await loadSessions();
}

// ── Session switch ─────────────────────────────────────────────────────────

function switchSession(name) {
  if (activeSession === name) return;
  closeSSE();
  activeSession = name;
  renderSessionList();
  renderToolbar();
  clearOutput();
  document.getElementById('send-btn').disabled = false;
  openSSE(name);
}

function renderToolbar() {
  const tb = document.getElementById('toolbar');
  tb.innerHTML = `
    <span class="session-name">${activeSession}</span>
    <span class="status-badge ready" id="status-badge">
      <span id="status-icon">●</span>
      <span id="status-text">Ready</span>
    </span>
    <div class="toolbar-actions">
      <button class="icon-btn warn" onclick="interrupt()">⚡ Interrupt</button>
      <button class="icon-btn red-btn" onclick="killSession('${activeSession}')">✕ Kill</button>
    </div>`;
}

function renderNoSession() {
  const tb = document.getElementById('toolbar');
  tb.innerHTML = `<div class="no-session-msg" style="font-size:13px;flex-direction:row;height:auto;color:var(--text-muted)">← Select or create a session</div>`;
  document.getElementById('send-btn').disabled = true;
  document.getElementById('output-wrap').innerHTML = `
    <div class="no-session-msg"><span>🖥️</span><div>Select a session on the left to start</div></div>`;
  hideChoiceBar();
}

// ── Output rendering ───────────────────────────────────────────────────────

let outputEl = null;

function clearOutput() {
  const wrap = document.getElementById('output-wrap');
  wrap.innerHTML = '<pre id="output"></pre>';
  outputEl = document.getElementById('output');
}

function setOutput(text) {
  if (!outputEl) return;
  outputEl.textContent = text;
  // Auto-scroll to bottom
  const wrap = document.getElementById('output-wrap');
  wrap.scrollTop = wrap.scrollHeight;
}

function updateStatus(status) {
  currentStatus = status;
  const badge = document.getElementById('status-badge');
  const icon  = document.getElementById('status-icon');
  const label = document.getElementById('status-text');
  if (!badge) return;

  badge.className = `status-badge ${status}`;
  if (status === 'thinking') {
    icon.textContent = '⠋';
    icon.className = 'spinner';
    label.textContent = 'Thinking…';
  } else if (status === 'choosing') {
    icon.className = '';
    icon.textContent = '?';
    label.textContent = 'Choose';
  } else {
    icon.className = '';
    icon.textContent = '●';
    label.textContent = 'Ready';
  }
  // Disable send while thinking
  document.getElementById('send-btn').disabled = (status === 'thinking');
}

// ── Choice bar ─────────────────────────────────────────────────────────────

function showChoiceBar(choices) {
  const bar = document.getElementById('choice-bar');
  bar.className = 'visible';
  // keep the label, replace rest
  bar.innerHTML = `<div class="choice-label">Claude is asking — choose one:</div>`;
  choices.forEach(c => {
    const btn = document.createElement('button');
    btn.className = 'choice-btn' + (c.selected ? ' selected' : '');
    btn.textContent = `${c.key}. ${c.label}`;
    btn.onclick = () => sendRaw(c.key);
    bar.appendChild(btn);
  });
}

function hideChoiceBar() {
  const bar = document.getElementById('choice-bar');
  bar.className = '';
  bar.innerHTML = '';
}

// ── SSE ────────────────────────────────────────────────────────────────────

function openSSE(name) {
  sse = new EventSource(`/api/sessions/${name}/stream`);
  sse.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      handleSSEEvent(data);
    } catch(ex) {
      console.error('SSE parse error', ex);
    }
  };
  sse.onerror = () => {
    console.warn('SSE connection error');
  };
}

function closeSSE() {
  if (sse) { sse.close(); sse = null; }
}

function handleSSEEvent(data) {
  if (data.type === 'output') {
    setOutput(data.full_text || '');
    updateStatus(data.status || 'ready');
    if (data.choices && data.choices.length >= 2) {
      showChoiceBar(data.choices);
    } else {
      hideChoiceBar();
    }
  } else if (data.type === 'choices') {
    showChoiceBar(data.choices);
  } else if (data.type === 'dead') {
    updateStatus('ready');
    if (outputEl) {
      outputEl.textContent += '\n\n[Session has ended]';
    }
    closeSSE();
    loadSessions();
  } else if (data.type === 'error') {
    console.error('Session error:', data.message);
  }
}

// ── Sending ────────────────────────────────────────────────────────────────

async function sendMessage() {
  if (!activeSession) return;
  const input = document.getElementById('msg-input');
  let text = input.value.trim();
  if (!text) return;

  // Handle slash commands
  text = expandSlashCommand(text);

  input.value = '';
  autoResize(input);
  await sendRaw(text);
}

function expandSlashCommand(text) {
  // /model opus|sonnet|haiku → send the number key that selects it
  const modelMap = {
    '/model opus':    '/model opus',
    '/model sonnet':  '/model sonnet',
    '/model haiku':   '/model haiku',
  };
  // Just pass through for now – Claude CLI handles /model natively
  return text;
}

async function sendRaw(text) {
  if (!activeSession) return;
  updateStatus('thinking');
  await fetch(`/api/sessions/${activeSession}/send`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text}),
  });
  hideChoiceBar();
}

async function interrupt() {
  if (!activeSession) return;
  await fetch(`/api/sessions/${activeSession}/interrupt`, {method: 'POST'});
}

// ── Input helpers ──────────────────────────────────────────────────────────

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}

// ── Init ───────────────────────────────────────────────────────────────────

(async () => {
  await loadSessions();
  // Auto-select first alive session if any
  const alive = Object.values(sessions).find(s => s.alive);
  if (alive) switchSession(alive.name);
  // Refresh session list every 5s
  setInterval(loadSessions, 5000);
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    print(f"\n  🚀  claude-cli-connector Web UI")
    print(f"  →  http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level=LOG_LEVEL.lower())
