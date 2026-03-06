# Changelog

All notable changes to `claude-cli-connector` are documented here.
This project follows [Semantic Versioning](https://semver.org/).

---

## [0.2.0] — 2026-03-06

### Added — Multi-transport architecture
- **`transport_base.py`** — Abstract `BaseTransport` base class, `Message`,
  `TransportEvent`, and `TransportMode` enum (`tmux`, `stream-json`, `sdk`, `acp`).
- **`transport_stream.py`** — `StreamJsonTransport` using `claude -p
  --output-format stream-json --input-format stream-json` for structured
  bidirectional communication via subprocess stdin/stdout.  Supports sync
  (`send_and_collect`) and async (`async_send_and_collect`) APIs, NDJSON
  event iteration, content-block deltas, and agent SDK stream events.
- **`transport_sdk.py`** — `SdkTransport` wrapping the official
  `claude-agent-sdk` Python package.  Provides `connect()`, `disconnect()`,
  `receive_messages()`, and `send_and_collect()`.
  *⚠ NOT TESTED — requires `ANTHROPIC_API_KEY` and `pip install claude-agent-sdk`.*
- **`transport_acp.py`** — `AcpTransport` implementing ACP (Agent Client
  Protocol) JSON-RPC 2.0 over stdio.  Spawns `claude-agent-acp` subprocess,
  manages sessions via `agent/newSession` / `agent/prompt` / `agent/cancel`.
  *⚠ NOT TESTED — requires `ANTHROPIC_API_KEY` and `claude-agent-acp` binary.*
  Reference: [zed-industries/claude-agent-acp](https://github.com/zed-industries/claude-agent-acp).
- **CLI `ccc stream`** — One-shot stream-json query command.
  `ccc stream "prompt" --cwd . --tools Bash,Read --raw` for scripts & CI/CD.
- **Optional `sdk` extra** — `pip install claude-cli-connector[sdk]` installs
  `claude-agent-sdk`.
- 50 new unit tests covering all three new transports (137 total).

### Added — Conversation history
- **`history.py`** — `ConversationLogger` persists user↔assistant messages
  as JSONL files under `~/.local/share/claude-cli-connector/history/{session}/`.
  Automatically logs `send()` and `send_and_wait()` in tmux mode, and
  `send()` / `send_and_collect()` in stream-json mode.
- **`HistoryEntry`** dataclass with `to_json()` / `from_json()` round-trip.
- Helper functions: `list_sessions_with_history()`, `list_session_runs()`,
  `read_full_session_history()`, `read_history_file()`.
- **CLI `ccc history`** — View conversation history for any session.
  Supports `--last N`, `--run <id>`, `--json` output.
  Without arguments, lists all sessions that have history.
- Override storage location with `$CCC_HISTORY_DIR` env var.
- 23 new unit tests for history module (160 total).

### Fixed
- `TmuxTransport.create()` now raises `SessionAlreadyExistsError` (not
  `TransportError`) when a duplicate session is detected.
- `libtmux` compatibility: replaced removed `Server.find_where()` with
  `srv.sessions.get(session_name=..., default=None)` (breaking change in 0.17).
- `capture_pane(start=0, end=-1)` silently returned `[]` in libtmux ≥ 0.17;
  switched to no-argument call.
- Choice detection: arrow/bullet lines scattered across conversation history
  (Claude Code uses `❯` as its input prompt) were falsely detected as a
  selection menu. Fixed by requiring a **contiguous** block of arrow/bullet
  lines — real choice menus are always consecutive.

---

## [0.1.0] — 2026-03-05

Initial release of the `claude-cli-connector` package.

### Core package (`claude_cli_connector`)

**Transport layer (`transport.py`)**
- `TmuxTransport` — wraps libtmux to create, attach to, and communicate with
  Claude CLI sessions running inside tmux panes.
- `PaneSnapshot` datatype holding raw pane lines plus a monotonic timestamp.
- `SESSION_PREFIX = "ccc-"` naming convention for managed tmux sessions.
- `TransportError` raised on libtmux failures.

**Session layer (`session.py`)**
- `ClaudeSession` — high-level API for a single Claude CLI session.
- `ClaudeSession.create()` — spawn a new tmux session and start Claude CLI.
- `ClaudeSession.attach()` — reconnect to an existing tmux session by name.
- `send_and_wait(text)` — send a message and block until Claude responds.
- `wait_ready(timeout)` — poll until Claude's idle prompt is detected.
- `capture()`, `tail(n)`, `new_output_since_last_capture()` — pane capture
  helpers with ANSI stripping.
- `detect_choices()` — detect interactive selection menus (model picker, etc.).
- `interrupt()`, `is_alive()`, `is_ready()`, `kill()` — lifecycle controls.
- Context-manager support (`with ClaudeSession.create(...) as s:`).

**Manager layer (`manager.py`)**
- `SessionManager` — registry for multiple concurrent `ClaudeSession` objects.
- `create()`, `attach()`, `get()`, `kill()`, `kill_all()` — lifecycle.
- `send_all(text)` — broadcast a message to all sessions.
- `collect_responses(timeout)` — wait for all sessions to be ready; returns
  `{name: response_text}` mapping.
- `prune_dead()` — remove sessions that are no longer alive.
- `list_sessions()`, `list_stored_sessions()` — introspection.

**Parser (`parser.py`)**
- `strip_ansi(text)` / `strip_ansi_lines(lines)` — remove VT100/ANSI escape
  sequences (CSI, OSC, Fe single-byte) and bare carriage returns.
- `detect_ready(lines, ...)` → `ReadinessResult` — three-layer heuristic:
  ① spinner/busy pattern check, ② idle-prompt pattern match, ③ stability
  (pane content unchanged for N seconds).
- `detect_choices(lines)` → `list[ChoiceItem] | None` — detect numbered-list
  and arrow/bullet interactive selection menus.
- `extract_last_response(lines)` — best-effort extraction of the last
  assistant response from a full pane capture.
- `diff_output(before, after)` — return new lines since a previous snapshot.

**Store (`store.py`)**
- `SessionRecord` (Pydantic model) — persisted session metadata.
- `SessionStore` — atomic JSON persistence at
  `~/.local/share/claude-cli-connector/sessions.json` (overridable via
  `CCC_STORE_PATH` env var).
- `save()`, `get()`, `delete()`, `list_all()`, `touch()` operations.

**CLI (`cli.py`)**
- `ccc` Typer application with subcommands: `run`, `attach`, `send`, `tail`,
  `ps`, `kill`, `interrupt`.

**Exceptions (`exceptions.py`)**
- `ConnectorError`, `SessionNotFoundError`, `SessionAlreadyExistsError`,
  `SessionTimeoutError`, `TransportError`.

### Validation demo (`demo/`)
- `demo/server.py` — FastAPI + SSE web UI for end-to-end validation.
  Streams pane output to the browser, accepts user input, detects and renders
  interactive choice menus, supports interrupt. **Not part of the core package.**

### Tests
- 87 unit tests across `parser`, `transport`, `session`, `store`, `manager`
  modules (no tmux required, CI-safe).
- Integration test suite under `tests/integration/` (requires tmux + Claude
  CLI; skipped by default, enabled with `pytest --run-integration`).

### Documentation
- `docs/PRD.md` — product requirements, use cases, and milestone plan.
- `docs/TECH_DESIGN.md` — architecture, module design, key decisions, and
  unit test strategy.

### Build & tooling
- `pyproject.toml` with `setuptools.build_meta` build backend, full PyPI
  metadata, `dev` extras (pytest, ruff, mypy, pytest-cov).
- `Makefile` with `test`, `cov`, `lint`, `fmt`, `typecheck`, `build`,
  `publish`, `clean`, `install-dev` targets.
