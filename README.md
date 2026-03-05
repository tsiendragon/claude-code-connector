# claude-cli-connector

A low-level Python package for interacting with a running [Claude Code CLI](https://docs.anthropic.com/claude-code) session from Python.

## Architecture: tmux-first

```
┌─────────────────────────────────────────────────┐
│  Your Python code                               │
│                                                 │
│  session = ClaudeSession.create("task1", ...)   │
│  response = session.send_and_wait("...")        │
└────────────────────┬────────────────────────────┘
                     │  libtmux
                     ▼
┌─────────────────────────────────────────────────┐
│  tmux session: ccc-task1                        │
│  ┌─────────────────────────────────────────┐   │
│  │  pane 0: claude (Claude Code CLI)       │   │
│  │  > █                                    │   │
│  └─────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

Claude CLI always runs as the foreground process inside a tmux pane.
Python interacts with it exclusively via `tmux send-keys` (input) and
`tmux capture-pane` (output).  This gives us:

- **Persistence** – the tmux session survives Python process restarts.
- **Reconnection** – attach to any running session by name.
- **Multi-session** – run many Claude CLI instances in parallel.
- **No PTY hacks** – tmux already manages the pseudo-terminal.

## Installation

```bash
pip install claude-cli-connector
```

**Prerequisites:**
- `tmux` ≥ 3.0 installed and available in PATH
- [Claude Code CLI](https://docs.anthropic.com/claude-code) installed: `npm install -g @anthropic-ai/claude-code`
- Authenticated: `claude auth login`

## Quick start

```python
from claude_cli_connector import ClaudeSession

# Create a new session (starts claude in a background tmux pane)
session = ClaudeSession.create(name="demo", cwd="/my/project")

# Send a message and wait for the full response
response = session.send_and_wait("Explain the main entry point of this project")
print(response)

# Continue the conversation
response2 = session.send_and_wait("Now write unit tests for it")
print(response2)

# Clean up
session.kill()
```

Using the context manager (auto-kills on exit):

```python
with ClaudeSession.create(name="demo", cwd=".") as session:
    print(session.send_and_wait("Hello!"))
```

Reconnecting after a restart:

```python
# In another process / after a crash
session = ClaudeSession.attach("demo")
print(session.send_and_wait("Continue where we left off"))
```

## Multi-session with SessionManager

```python
from claude_cli_connector import SessionManager

mgr = SessionManager()
mgr.create("frontend", cwd="/repo/frontend")
mgr.create("backend",  cwd="/repo/backend")

mgr.send_all("git pull and summarise what changed")
responses = mgr.collect_responses(timeout=120)

for name, text in responses.items():
    print(f"[{name}]\n{text}\n")

mgr.kill_all()
```

## CLI (`ccc`)

```
ccc run    <name> [--cwd DIR]        Start a new Claude session
ccc attach <name>                    Attach to an existing session
ccc send   <name> "message"          Send a message and print response
ccc tail   <name> [--lines 40]       Print last N lines of pane
ccc ps                               List all known sessions
ccc kill   <name>                    Kill a session
ccc interrupt <name>                 Send Ctrl-C to a session
```

Example:
```bash
ccc run myproject --cwd /repo/myproject
ccc send myproject "What files should I look at first?"
ccc ps
ccc kill myproject
```

## Package structure

```
src/claude_cli_connector/
├── __init__.py        Public API exports
├── exceptions.py      Custom exceptions
├── transport.py       Low-level tmux wrapper (libtmux)
├── parser.py          ANSI stripping + ready-state detection
├── session.py         ClaudeSession – main public class
├── manager.py         SessionManager – multi-session orchestration
├── store.py           JSON session metadata persistence
└── cli.py             Typer CLI entry point (ccc command)
```

## Ready detection

The hardest part of the tmux-first approach is knowing when Claude has
finished generating and is ready for the next input.  We use a layered
strategy:

1. **Prompt pattern** (high confidence) – detect Claude CLI's input prompt
   (e.g. `> `, `╰─>`) in the last few lines of the pane.
2. **Busy indicators** – look for spinner characters (`⠋⠙⠹…`) or
   "Thinking…" text.  If found, we know Claude is still working.
3. **Output stability** (fallback) – capture the pane twice with a short
   gap.  If the content is identical and enough time has passed, assume
   generation is done.

## Verification guide

A step-by-step checklist for confirming the package works end-to-end on a
real machine.

### Prerequisites

```bash
tmux -V          # tmux 3.0+
claude --version # Claude Code CLI installed
```

If tmux is missing: `brew install tmux` (macOS) or `apt install tmux` (Linux).

### Step 1 — Unit tests (no tmux required)

```bash
pip install -e ".[dev]"
make test
```

Expected output:

```
87 passed in 0.2s
```

### Step 2 — Create a session

```bash
ccc run smoke-test
```

Expected output:

```
✓ Session smoke-test started (tmux: ccc-smoke-test)
  cwd: .
  To send a message: ccc send smoke-test "your message"
```

> **Note:** Claude Code shows a one-time trust prompt on first launch in a
> directory. If the session appears stuck, send `ccc send smoke-test "1"` to
> confirm "Yes, I trust this folder", then continue.

### Step 3 — Send a message and read the response

```bash
ccc send smoke-test "say hello in one sentence"
# wait 3-5 seconds for Claude to respond
ccc tail smoke-test -n 20
```

Expected output (actual wording will vary):

```
❯ say hello in one sentence

⏺ Hello there, nice to meet you!

❯
```

Verify:
- Output is clean text — no ANSI escape codes or box-drawing garbage
- Claude's reply appears between the two `❯` prompt lines
- The idle `❯` prompt at the bottom confirms Claude is ready for the next message

### Step 4 — Clean up

```bash
ccc kill smoke-test
ccc ps    # list should be empty
```

### Step 5 — Demo Web UI (optional, end-to-end visual check)

```bash
cd demo
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

Open `http://localhost:8000`, type a session name, click **Connect**, and chat.
Verify that:
- Output streams in real time (not dumped all at once)
- Status bar toggles between `thinking` and `ready`
- Numbered choice menus (e.g. model selection) render as clickable buttons

### Known compatibility notes

| Issue | Fix applied |
|-------|-------------|
| `libtmux.Server.find_where()` removed in 0.17 | Replaced with `srv.sessions.get(session_name=..., default=None)` |
| `capture_pane(start=0, end=-1)` returns `[]` in libtmux ≥ 0.17 | Removed `start`/`end` kwargs; `capture_pane()` called with no arguments |

## Development

```bash
git clone https://github.com/your-org/claude-cli-connector
cd claude-cli-connector
pip install -e ".[dev]"

# Run tests (no tmux required – libtmux is mocked)
pytest

# Lint
ruff check src tests

# Type check
mypy src
```

## License

MIT
