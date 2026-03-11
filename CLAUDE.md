# CLAUDE.md — ccc

## Project Overview

`ccc` is a CLI tool that manages Claude Code (and Cursor agent) sessions running inside tmux panes. It provides session lifecycle management, message sending, response extraction, and conversation history.

## Architecture (Single Implementation)

**TypeScript is the single source of truth.** The Python package is a thin subprocess wrapper — it delegates all logic to the `ccc` binary.

```
claude-cli-connector/
├── src/                    ← TypeScript (primary implementation)
│   ├── cli.ts              ← citty CLI entry point
│   ├── parser.ts           ← ANSI stripping, ready detection, response extraction
│   ├── session.ts          ← high-level session API
│   ├── transport.ts        ← low-level tmux wrapper (child_process)
│   ├── store.ts            ← JSON session metadata (~/.local/share/ccc/sessions.json)
│   ├── history.ts          ← JSONL conversation logger
│   ├── relay.ts            ← Claude-to-Claude relay (debate/collab)
│   ├── config.ts           ← global config (sessionPrefix for NanoClaw)
│   └── index.ts            ← npm library entry point
├── dist/                   ← compiled JS (gitignored)
├── py/                     ← Python thin wrapper (~240 lines total)
│   └── ccc/
│       └── __init__.py     ← ClaudeSession, SessionManager, relay_*, stream()
├── tests/
│   ├── unit/               ← Python unit tests
│   ├── integration/        ← integration tests
│   └── e2e/                ← end-to-end ccc CLI tests
├── docs/                   ← design docs (keep)
├── package.json
├── tsconfig.json
├── pyproject.toml          ← Python package: ccc (no runtime deps)
├── Makefile
└── install.sh              ← bun-based binary install script
```

## Python Wrapper Design

`py/ccc/__init__.py` calls the `ccc` binary via subprocess. No parser, transport, or session logic in Python.

```python
from ccc import ClaudeSession

with ClaudeSession.create("demo", cwd="/my/project") as s:
    print(s.send("Explain this codebase"))
```

All methods map directly to `ccc` CLI commands:
- `s.send(msg)` → `ccc send <name> <msg>`
- `s.last()` → `ccc last <name>`
- `s.status()` → `ccc status <name> --porcelain`
- `s.read()` → `ccc read <name> --json`
- `s.wait()` → `ccc wait <name> ready`

## Supported Backends

- **claude** (default) — Claude Code CLI (`claude`)
- **cursor** — Cursor agent (`cursor-agent`), flag: `--cursor`

## Testing Environment

### Current: macOS (MacBook)

- **OS**: macOS (Apple Silicon)
- **tmux**: system tmux or Homebrew tmux
- **Node.js**: ≥ 18
- **Status**: Primary development and testing environment.

### TODO: Remote Ubuntu

- tmux version compatibility
- Node.js availability and PATH
- Session persistence across SSH disconnects
- UTF-8 locale for ❯, ⏺ characters

## Key Implementation Notes

### Ready Detection (parser.ts)

Claude Code CLI output format:
```
❯ user question here          ← user input
⏺ Claude's response...        ← response marker
───────────────────── ▪▪▪ ─   ← separator
❯\u00a0                        ← idle prompt (may have trailing NBSP)
──────────────────────────    ← bottom separator
  ? for shortcuts             ← TUI hint footer
```

`detectReady()` walks backward skipping empty lines, separators, and TUI hint lines to find the idle `❯`. Then checks BUSY patterns first (spinners, "Thinking...").

`extractLastResponse()` uses the same backward-walk strategy to find the idle `❯`, then the preceding user-input `❯ <text>`, and returns everything between them.

### Permission Detection (parser.ts)

**Format A** (Allow once/always/Deny):
```
⏺ Claude wants to run: Bash(date)
  Allow Bash?
❯ Allow once
  Allow always
  Deny
```

**Format B** (numbered list):
```
Do you want to proceed?
❯ 1. Yes
  2. Yes, allow reading from repos/
  3. No
```

### Commands

```
ccc run    <name> [--cwd DIR] [--cursor] [--model M]
ccc send   <name> "message" [--no-wait] [--auto-approve] [--timeout T]
ccc last   <name> [--raw]
ccc status <name> [--porcelain]
ccc read   <name> [--json] [--full] [--heartbeat]
ccc wait   <name> <state> [--timeout T]
ccc tail   <name> [--lines N] [--full]
ccc kill   <name>
ccc ps
ccc clean  [--yes] [--dry-run]
ccc approve <name> [yes|always|no]
ccc input  <name> <text> [--no-enter]
ccc key    <name> <keys> [--repeat N]
ccc model  <name> [model]
ccc interrupt <name>
ccc relay  debate "topic" [--role-a] [--role-b] [--rounds N]
ccc relay  collab "task"  [--dev] [--reviewer] [--rounds N]
ccc stream "prompt" [--cwd DIR] [--tools T] [--model M]
```

## Communication Preferences

- 称呼用户为 **老板**
