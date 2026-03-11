# ccc — Claude CLI Connector

A CLI tool that manages [Claude Code](https://docs.anthropic.com/claude-code), Cursor Agent, Codex, and OpenCode sessions running inside tmux panes. Provides session lifecycle management, message sending, response extraction, and conversation history.

## Installation

### From source (npm)

```bash
git clone https://github.com/anthropics/claude-cli-connector
cd claude-cli-connector
npm install
npm run build
npm install -g .     # installs `ccc` binary globally
```

### From source (bun — single binary)

```bash
./install.sh         # or: make install-bun
```

Installs a self-contained binary to `~/.local/bin/ccc`.

### Python wrapper

```bash
pip install -e .
```

The Python package is a thin subprocess wrapper — all logic lives in the TypeScript `ccc` binary.

### Prerequisites

- **tmux** ≥ 3.0 (`brew install tmux` / `apt install tmux`)
- **Node.js** ≥ 18
- At least one supported backend CLI installed:
  - [Claude Code](https://docs.anthropic.com/claude-code): `npm install -g @anthropic-ai/claude-code`
  - [Cursor Agent](https://docs.cursor.com/agent): `cursor-agent`
  - [Codex](https://github.com/openai/codex): `npm install -g @openai/codex`
  - [OpenCode](https://github.com/opencode-ai/opencode): `opencode`

## Supported backends

| Backend | Flag | CLI command |
|---------|------|-------------|
| Claude Code (default) | — | `claude` |
| Cursor Agent | `--cursor` | `cursor-agent` / `agent` |
| Codex | `--codex` | `codex` |
| OpenCode | `--opencode` | `opencode` |

## Quick start

### CLI

```bash
# Start a session
ccc run myproject --cwd /repo/myproject

# Send a message and get the response
ccc send myproject "Explain the main entry point"

# Check status
ccc status myproject          # ready / thinking / approval / choosing / dead

# Multi-turn conversation
ccc send myproject "Now write tests for it"

# Clean up
ccc kill myproject
```

### Python

```python
from ccc import ClaudeSession

with ClaudeSession.create("demo", cwd="/my/project") as s:
    print(s.send("Explain this codebase"))
    print(s.send("Write tests for it"))
```

## Commands

### Session lifecycle

```
ccc run    <name> [--cwd DIR] [--cursor] [--codex] [--opencode] [--model M]
ccc send   <name> "message" [--no-wait] [--auto-approve] [--timeout T]
ccc last   <name> [--raw] [--full]
ccc status <name> [--porcelain]
ccc ps     [--json]
ccc kill   <name>
ccc clean  [--yes] [--dry-run]
```

### Pane inspection

```
ccc tail   <name> [--lines N] [--full]
ccc read   <name> [--json] [--full] [--heartbeat]
ccc wait   <name> <state> [--timeout T] [--json]
```

States: `ready`, `thinking`, `approval`, `choosing`, `composed`, `dead`, `any-change`.

### Agent control

```
ccc input     <name> <text> [--no-enter]
ccc key       <name> <keys> [--repeat N]
ccc approve   <name> [yes|always|no]
ccc interrupt <name>
ccc model     <name> [model]
```

### Relay (Claude-to-Claude)

```
ccc relay debate "topic" [--role-a NAME] [--role-b NAME] [--rounds N]
ccc relay collab "task"  [--dev NAME] [--reviewer NAME] [--rounds N]
```

### Stream (one-shot)

```
ccc stream "prompt" [--cwd DIR] [--tools T] [--model M]
```

## Agent loop pattern

```bash
ccc send myproject "build the app" --no-wait
ccc wait myproject ready --timeout 300
ccc read myproject --json
```

```python
import subprocess, json, time

def ccc(*args):
    return subprocess.run(["ccc"] + list(args), capture_output=True, text=True).stdout.strip()

ccc("send", "myproject", "refactor auth module", "--no-wait")

while True:
    state = json.loads(ccc("read", "myproject", "--json"))
    if state["state"] == "ready":
        break
    elif state["state"] == "approval":
        ccc("approve", "myproject", "yes")
    time.sleep(1)
```

## Architecture

```
claude-cli-connector/
├── src/                  ← TypeScript (single source of truth)
│   ├── cli.ts            ← citty CLI entry point
│   ├── parser.ts         ← ANSI stripping, ready/permission/choice detection
│   ├── session.ts        ← high-level session API, classifyWindow
│   ├── transport.ts      ← low-level tmux wrapper
│   ├── store.ts          ← JSON session metadata
│   ├── history.ts        ← JSONL conversation logger
│   ├── relay.ts          ← Claude-to-Claude relay
│   └── config.ts         ← global config
├── py/ccc/               ← Python thin wrapper (subprocess → ccc binary)
│   └── __init__.py
├── tests/
│   ├── unit/ts/          ← TypeScript unit tests (vitest)
│   ├── unit/             ← Python wrapper tests
│   └── fixtures/         ← captured terminal frames for parser tests
├── scripts/
│   ├── capture-fixture.mjs  ← capture frames from live sessions
│   └── parse-fixture.mjs    ← run parser on fixture frames
└── dist/                 ← compiled JS (gitignored)
```

## Ready detection

The hardest part of tmux-based session management is knowing when the backend is done generating. `ccc` uses a layered strategy:

1. **Prompt pattern** — detect the idle input prompt (`❯`, `>`, `›`) walking backward through the pane, skipping empty lines, separators, and TUI hints.
2. **Busy indicators** — spinner characters (`⠋⠙⠹…`, `✻`, `▣`), "Thinking…", "Spelunking…" mean still working.
3. **Frame stability** — capture multiple snapshots over ~1s. If content is identical and enough time has passed, classify as stable.

The `classifyWindow()` function combines these into a pure, testable state classifier:
- All frames identical → `analyzeStableLines` (ready / approval / choosing / composed / unknown)
- Frames differ, only input line changes → `typing`
- Frames differ, other content changes → `thinking`

## Development

```bash
npm run build         # compile TypeScript
npm test              # run vitest
make install          # build + npm link
```

## License

MIT
