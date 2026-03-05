# claude-cli-connector · Web UI Demo

A minimal FastAPI web server that provides a browser-based interface for
interacting with one or more running Claude Code CLI sessions.

## Quick start

```bash
# 1. Install the connector package (from repo root)
pip install -e ..

# 2. Install demo dependencies
pip install -r requirements.txt

# 3. Start the server
python server.py
# → http://localhost:8765
```

## What you can do in the Web UI

| Action | How |
|---|---|
| Create a new Claude session | Fill in "session name" + "working directory" → click **＋ New Session** |
| Send a message | Type in the input box → press **Enter** |
| See real-time output | Pane content streams via SSE every 200ms |
| Respond to choice menus | Purple choice buttons appear automatically when Claude asks you to choose |
| Switch models | Type `/model opus`, `/model sonnet`, or `/model haiku` in the input box |
| Interrupt generation | Click **⚡ Interrupt** (sends Ctrl-C) |
| Kill a session | Click **✕ Kill** → confirm |
| Switch between sessions | Click session name in left sidebar |

## REST API

The same API is usable from curl / Python directly:

```bash
# List sessions
curl http://localhost:8765/api/sessions

# Create a session
curl -X POST http://localhost:8765/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"name": "myproj", "cwd": "/path/to/repo"}'

# Send a message
curl -X POST http://localhost:8765/api/sessions/myproj/send \
  -H 'Content-Type: application/json' \
  -d '{"text": "Explain the main module"}'

# Stream output (SSE)
curl -N http://localhost:8765/api/sessions/myproj/stream

# Interrupt
curl -X POST http://localhost:8765/api/sessions/myproj/interrupt

# Kill
curl -X DELETE http://localhost:8765/api/sessions/myproj
```

## SSE event format

Each SSE event contains a JSON payload:

```json
// Pane output update (every 200ms)
{"type": "output", "full_text": "...", "status": "thinking|ready|choosing", "choices": null}

// Choice menu detected
{"type": "choices", "choices": [{"key": "1", "label": "claude-opus-4-5"}, ...]}

// Session died
{"type": "dead"}

// Error
{"type": "error", "message": "..."}
```

## Architecture

```
Browser  ──SSE──►  GET /api/sessions/{name}/stream   (pane polling loop)
         ◄──POST─  /api/sessions/{name}/send          (tmux send-keys)
         ◄──POST─  /api/sessions                      (create session)

server.py
  └─ FastAPI + sse-starlette
       └─ claude_cli_connector.ClaudeSession
            └─ TmuxTransport (libtmux)
                 └─ tmux pane running `claude`
```

## Configuration

| Env var | Default | Description |
|---|---|---|
| `CCC_DEMO_PORT` | `8765` | Port to listen on |
| `CCC_LOG_LEVEL` | `INFO` | Log level (DEBUG/INFO/WARNING) |

## Extending

To add authentication, wrap the FastAPI app with any standard middleware
(e.g. HTTP Basic Auth via `starlette.middleware.authentication`).

To persist conversation history, add a `messages: list[dict]` field to the
session state and append to it on every `send` call.
