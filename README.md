# claude-cli-connector

A Python package for interacting with [Claude Code CLI](https://docs.anthropic.com/claude-code) from Python — supporting multiple transport modes, conversation logging, and both library and CLI usage.

## Transport modes

`claude-cli-connector` offers four transport backends.  Pick the one that fits your use case:

| Mode | How it works | Multi-turn | Best for |
|------|-------------|------------|----------|
| **tmux** (default) | Screen-scrapes a tmux pane running `claude` | ✅ persistent | Interactive sessions, long-running agents |
| **stream-json** | Subprocess with `--output-format stream-json` | single-shot | Scripts, CI/CD, automation |
| **sdk** ⚠ | Wraps `claude-agent-sdk` Python package | ✅ async | Native Python integration |
| **acp** ⚠ | JSON-RPC 2.0 over stdio (`claude-agent-acp`) | ✅ | IDE integrations (Zed, etc.) |

> ⚠ SDK and ACP transports are **not tested** — they require `ANTHROPIC_API_KEY` and their respective packages.

## Installation

```bash
pip install claude-cli-connector
```

**Prerequisites (tmux mode):**
- `tmux` ≥ 3.0 installed and available in PATH
- [Claude Code CLI](https://docs.anthropic.com/claude-code) installed: `npm install -g @anthropic-ai/claude-code`
- Authenticated: `claude auth login`

**Optional extras:**
```bash
pip install claude-cli-connector[sdk]   # adds claude-agent-sdk
```

## Quick start — tmux mode

```python
from claude_cli_connector import ClaudeSession

# Create a new session (starts claude in a background tmux pane)
session = ClaudeSession.create(name="demo", cwd="/my/project")

# Send a message and wait for the full response
response = session.send_and_wait("Explain the main entry point of this project")
print(response)

# Continue the conversation (multi-turn)
response2 = session.send_and_wait("Now write unit tests for it")
print(response2)

# Clean up
session.kill()
```

Context manager (auto-kills on exit):

```python
with ClaudeSession.create(name="demo", cwd=".") as session:
    print(session.send_and_wait("Hello!"))
```

Reconnecting after a restart:

```python
session = ClaudeSession.attach("demo")
print(session.send_and_wait("Continue where we left off"))
```

## Quick start — stream-json mode

```python
from claude_cli_connector import StreamJsonTransport

t = StreamJsonTransport(_name="demo", _cwd="/my/project")
t.start()

msg = t.send_and_collect("Explain this codebase")
print(msg.content)
print(f"Cost: ${msg.cost_usd:.4f}")

t.kill()
```

Streaming events one by one:

```python
t.start()
t.send("What files are in this directory?")
for event in t.iter_events(timeout=60):
    print(event.type, event.data)
    if event.type in ("result", "eof"):
        break
t.kill()
```

Async support:

```python
import asyncio
from claude_cli_connector import StreamJsonTransport

async def main():
    t = StreamJsonTransport(_name="demo", _cwd=".")
    t.start()
    msg = await t.async_send_and_collect("Hello!")
    print(msg.content)
    t.kill()

asyncio.run(main())
```

## Quick start — SDK mode (untested)

```python
from claude_cli_connector import SdkTransport

t = SdkTransport(_name="demo")
await t.connect(api_key="sk-ant-...", model="claude-sonnet-4-20250514")

async for msg in t.receive_messages("Explain this repo"):
    print(msg.content)

await t.disconnect()
```

Requires: `pip install claude-cli-connector[sdk]` and `ANTHROPIC_API_KEY`.

## Quick start — ACP mode (untested)

```python
from claude_cli_connector import AcpTransport

t = AcpTransport(_name="demo", _cwd="/my/project")
t.start()

session_id = t.new_session()
t.prompt(session_id, "What does this project do?")

for event in t.iter_events(timeout=60):
    print(event.type, event.data)
    if event.type == "agent/finish":
        break

t.kill()
```

Requires: `claude-agent-acp` binary (see [zed-industries/claude-agent-acp](https://github.com/zed-industries/claude-agent-acp)).

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

## Conversation history

All user↔assistant messages are automatically logged as JSONL files.

```python
from claude_cli_connector import ConversationLogger, HistoryEntry

# Logger is created automatically inside ClaudeSession and StreamJsonTransport.
# You can also create one manually:
logger = ConversationLogger(session_name="demo", transport="tmux")
logger.log_user("What is 2+2?")
logger.log_assistant("2+2 = 4")

# Read back
for entry in logger.read():
    print(f"[{entry.role}] {entry.content}")
```

Storage location: `~/.local/share/claude-cli-connector/history/{session}/{run_id}.jsonl`
Override with: `$CCC_HISTORY_DIR` environment variable.

## CLI (`ccc`)

### Session lifecycle (tmux mode)

```
ccc run    <name> [--cwd DIR]          Start a new Claude session
ccc attach <name>                      Attach to an existing session
ccc send   <name> "message"            Send a message and print response
ccc tail   <name> [--lines 40]         Print last N lines of pane
ccc ps                                 List all known sessions
ccc status <name> [--porcelain]        Show state: thinking / ready / choosing / dead
ccc kill   <name>                      Kill a session
ccc interrupt <name>                   Send Ctrl-C to a session
```

### Stream-json mode (one-shot)

```
ccc stream "prompt" [--cwd DIR] [--tools Bash,Read] [--model MODEL] [--raw]
```

Runs a single prompt through `claude -p --output-format stream-json` and prints the result. Use `--raw` to see each JSON event. Ideal for scripts and CI/CD.

### Conversation history

```
ccc history                            List all sessions with history
ccc history <name>                     Show formatted conversation log
ccc history <name> -n 10              Last 10 entries
ccc history <name> --run <run_id>     Specific run only
ccc history <name> --json             Raw JSONL output
```

### Examples

```bash
# tmux mode: interactive multi-turn session
ccc run myproject --cwd /repo/myproject
ccc send myproject "What files should I look at first?"
ccc status myproject
ccc send myproject "Now write tests for the main module"
ccc history myproject
ccc kill myproject

# stream-json mode: one-shot for scripts
ccc stream "List all TODO comments in this repo" --cwd /repo --tools Bash,Read
ccc stream "Summarize README.md" --cwd . --raw | jq '.type'
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Your Python code / ccc CLI                             │
│                                                         │
│  ClaudeSession ─────────► TmuxTransport (libtmux)       │
│  StreamJsonTransport ───► subprocess (stream-json)       │
│  SdkTransport ──────────► claude-agent-sdk               │
│  AcpTransport ──────────► claude-agent-acp (JSON-RPC)    │
│                                                         │
│  ConversationLogger ────► JSONL files (~/.local/share/)  │
│  SessionManager ────────► multi-session orchestration    │
└─────────────────────────────────────────────────────────┘
```

## Package structure

```
src/claude_cli_connector/
├── __init__.py            Public API exports
├── exceptions.py          Custom exceptions
├── transport.py           TmuxTransport (libtmux wrapper)
├── transport_base.py      BaseTransport ABC, Message, TransportEvent, TransportMode
├── transport_stream.py    StreamJsonTransport (subprocess + stream-json)
├── transport_sdk.py       SdkTransport (claude-agent-sdk wrapper) ⚠ untested
├── transport_acp.py       AcpTransport (JSON-RPC 2.0 over stdio) ⚠ untested
├── parser.py              ANSI stripping + ready-state detection
├── session.py             ClaudeSession — high-level tmux session API
├── manager.py             SessionManager — multi-session orchestration
├── store.py               JSON session metadata persistence
├── history.py             ConversationLogger — JSONL conversation logging
└── cli.py                 Typer CLI entry point (ccc command)
```

## Ready detection (tmux mode)

The hardest part of the tmux-first approach is knowing when Claude has
finished generating and is ready for the next input.  We use a layered
strategy:

1. **Prompt pattern** (high confidence) — detect Claude CLI's input prompt
   (e.g. `> `, `╰─>`) in the last few lines of the pane.
2. **Busy indicators** — look for spinner characters (`⠋⠙⠹…`) or
   "Thinking…" text.  If found, we know Claude is still working.
3. **Output stability** (fallback) — capture the pane twice with a short
   gap.  If the content is identical and enough time has passed, assume
   generation is done.

## Verification guide

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

Expected: `160 passed` (87 core + 50 transport + 23 history).

### Step 2 — Create a session

```bash
ccc run smoke-test
```

> **Note:** Claude Code shows a one-time trust prompt on first launch in a
> directory. If the session appears stuck, send `ccc send smoke-test "1"` to
> confirm "Yes, I trust this folder", then continue.

### Step 3 — Send a message and read the response

```bash
ccc send smoke-test "say hello in one sentence"
ccc tail smoke-test -n 20
```

Verify: output is clean text with no ANSI escape codes, Claude's reply appears between prompt lines.

### Step 4 — Check status and history

```bash
ccc status smoke-test           # should show "ready"
ccc history smoke-test          # should show user/assistant messages
```

### Step 5 — Stream-json mode

```bash
ccc stream "say hello in one sentence" --cwd .
```

### Step 6 — Clean up

```bash
ccc kill smoke-test
ccc ps    # list should be empty
```

### Known compatibility notes

| Issue | Fix applied |
|-------|-------------|
| `libtmux.Server.find_where()` removed in 0.17 | Replaced with `srv.sessions.get(session_name=..., default=None)` |
| `capture_pane(start=0, end=-1)` returns `[]` in libtmux ≥ 0.17 | Removed `start`/`end` kwargs; `capture_pane()` called with no arguments |

## Development

```bash
git clone https://github.com/anthropics/claude-cli-connector
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
