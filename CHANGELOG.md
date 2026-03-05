# Changelog

All notable changes to `claude-cli-connector` are documented here.
This project follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Fixed
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
