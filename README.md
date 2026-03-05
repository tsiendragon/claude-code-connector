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
