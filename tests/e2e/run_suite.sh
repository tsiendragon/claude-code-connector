#!/usr/bin/env bash
set -uo pipefail

CCC="$HOME/.local/bin/ccc"
TEST_CWD="/Users/mj/repos/claude-cli-connector/ccc-ts"
STATE_DIR="$TEST_CWD/tests/state"
TS=$(date +%Y-%m-%dT%H-%M-%S)
EVIDENCE="$STATE_DIR/evidence-$TS.json"
REGISTRY="$STATE_DIR/registry-$TS.json"
mkdir -p "$STATE_DIR"

echo "Evidence: $EVIDENCE"
echo "Timestamp: $TS"

# Helper to run a command and capture result
run_step() {
  local label="$1"
  local desc="$2"
  shift 2
  local start_ms=$(date +%s%3N)
  local stdout_file=$(mktemp)
  local stderr_file=$(mktemp)
  "$@" >"$stdout_file" 2>"$stderr_file"
  local exit_code=$?
  local end_ms=$(date +%s%3N)
  local duration=$((end_ms - start_ms))
  local stdout=$(cat "$stdout_file" | head -c 2000)
  local stderr=$(cat "$stderr_file" | head -c 500)
  rm -f "$stdout_file" "$stderr_file"

  # Escape for JSON
  local stdout_json=$(echo "$stdout" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")
  local stderr_json=$(echo "$stderr" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")
  local cmd_json=$(echo "$*" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))")
  local desc_json=$(echo "$desc" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))")

  STEP_RESULT=$(cat <<JSON
{"label": "$label", "description": $desc_json, "cmd": $cmd_json, "stdout": $stdout_json, "stderr": $stderr_json, "exitCode": $exit_code, "durationMs": $duration}
JSON
)
  echo "$STEP_RESULT"

  if [ $exit_code -eq 0 ]; then
    echo "[OK] $label ($duration ms)" >&2
  else
    echo "[FAIL] $label (exit $exit_code, $duration ms)" >&2
    echo "  stderr: $stderr" >&2
  fi

  return $exit_code
}

# Initialize evidence file
cat > "$EVIDENCE" << 'JSONEOF'
{"timestamp": "PLACEHOLDER", "phases": {"phase1": {"claude": {}, "cursor": {}, "codex": {}, "opencode": {}, "boundary": {}}, "phase2": {}}, "phase1_passed": false, "active_sessions": []}
JSONEOF

# Use Python to build the evidence incrementally
python3 << 'PYEOF'
import subprocess, json, os, time, sys

CCC = os.path.expanduser("~/.local/bin/ccc")
TEST_CWD = "/Users/mj/repos/claude-cli-connector/ccc-ts"
STATE_DIR = f"{TEST_CWD}/tests/state"
TS = os.environ.get("TS", "")

# Read TS from environment if available
import datetime
ts_str = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

EVIDENCE_FILE = f"{STATE_DIR}/evidence-{ts_str}.json"
REGISTRY_FILE = f"{STATE_DIR}/registry-{ts_str}.json"

print(f"Writing evidence to: {EVIDENCE_FILE}", file=sys.stderr)

evidence = {
    "timestamp": ts_str,
    "phases": {
        "phase1": {
            "claude": {},
            "cursor": {},
            "codex": {},
            "opencode": {},
            "boundary": {}
        },
        "phase2": {}
    },
    "phase1_passed": False,
    "active_sessions": []
}

def save_evidence():
    with open(EVIDENCE_FILE, "w") as f:
        json.dump(evidence, f, indent=2)

def run(args, timeout=120, stdin_text=None, env=None):
    """Run a command and return (stdout, stderr, exit_code, duration_ms)"""
    start = time.time()
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.PIPE if stdin_text else None,
            input=stdin_text, env=env
        )
        duration = int((time.time() - start) * 1000)
        return result.stdout, result.stderr, result.returncode, duration
    except subprocess.TimeoutExpired:
        duration = int((time.time() - start) * 1000)
        return "", "TIMEOUT", -1, duration
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        return "", str(e), -2, duration

def step(label, description, args, timeout=120, stdin_text=None):
    """Run a step and return the result dict"""
    cmd_str = " ".join(str(a) for a in args)
    print(f"  Running {label}: {cmd_str}", file=sys.stderr)
    stdout, stderr, exit_code, duration = run(args, timeout=timeout, stdin_text=stdin_text)
    result = {
        "cmd": cmd_str,
        "description": description,
        "stdout": stdout[:3000],
        "stderr": stderr[:500],
        "exitCode": exit_code,
        "durationMs": duration
    }
    status = "OK" if exit_code == 0 else f"FAIL(exit={exit_code})"
    print(f"    [{status}] {duration}ms", file=sys.stderr)
    if exit_code != 0 and stderr:
        print(f"    stderr: {stderr[:200]}", file=sys.stderr)
    return result

def tmux_capture(session_name):
    """Capture tmux pane for a ccc session"""
    stdout, stderr, code, _ = run(["tmux", "capture-pane", "-p", "-t", f"ccc-{session_name}"], timeout=5)
    if code == 0:
        return stdout[:1000]
    return "(no pane)"

save_evidence()

# ============================================================
# PHASE 1: Core Features — All Backends
# ============================================================
print("\n=== PHASE 1: Core Features ===", file=sys.stderr)

phase1_claude_passed = False
backends = ["claude", "cursor", "codex", "opencode"]

for backend in backends:
    print(f"\n--- Backend: {backend} ---", file=sys.stderr)

    session = f"test-{backend}-1-{ts_str}"
    b = evidence["phases"]["phase1"][backend]

    # Backend flag
    flag_map = {"claude": None, "cursor": "--cursor", "codex": "--codex", "opencode": "--opencode"}
    flag = flag_map[backend]

    # S1: createSession
    print(f"\n[S1] Create session for {backend}", file=sys.stderr)
    run_args = [CCC, "run", session, "--cwd", TEST_CWD]
    if flag:
        run_args.append(flag)
    b["S1_run"] = step("S1_run", f"Start {backend} session", run_args, timeout=30)
    save_evidence()

    if b["S1_run"]["exitCode"] != 0:
        print(f"  SKIP rest of {backend} — session failed to start", file=sys.stderr)
        b["S1_attach"] = {"cmd": "skipped", "exitCode": -99, "stdout": "", "stderr": "session start failed", "durationMs": 0}
        continue

    # Wait for session to be ready
    time.sleep(3)

    # S1 attach
    b["S1_attach"] = step("S1_attach", f"Attach to {backend} session (alive check)",
                           [CCC, "attach", session], timeout=10)
    save_evidence()

    # S2: send → reply
    print(f"\n[S2] Send message to {backend}", file=sys.stderr)
    b["S2"] = step("S2", f"Send 'Reply with exactly: PONG' to {backend}",
                   [CCC, "send", session, "Reply with exactly: PONG", "--timeout", "120"],
                   timeout=135)
    save_evidence()

    # S3: status --json
    print(f"\n[S3] Status for {backend}", file=sys.stderr)
    b["S3"] = step("S3", f"Status --json for {backend}",
                   [CCC, "status", session, "--json"], timeout=15)
    save_evidence()

    # S4: last
    print(f"\n[S4] Last response for {backend}", file=sys.stderr)
    if backend == "opencode":
        b["S4"] = step("S4", f"Last --full response for {backend}",
                       [CCC, "last", session, "--full"], timeout=15)
    else:
        b["S4"] = step("S4", f"Last response for {backend}",
                       [CCC, "last", session], timeout=15)
    save_evidence()

    # S5: approve (claude and cursor only)
    if backend in ["claude", "cursor"]:
        print(f"\n[S5] Approve test for {backend}", file=sys.stderr)

        # Wait for ready first
        b["S5_wait_ready"] = step("S5_wait_ready", f"Wait ready before approval test",
                                   [CCC, "wait", session, "ready", "--timeout", "60"], timeout=70)
        save_evidence()

        # Send command that triggers permission
        b["S5_send"] = step("S5_send", f"Send bash command to trigger approval",
                            [CCC, "send", session, "Use the Bash tool to run: date", "--no-wait"],
                            timeout=15)
        save_evidence()

        time.sleep(5)

        # Check status
        b["S5_status"] = step("S5_status", f"Check status for approval state",
                              [CCC, "status", session, "--json"], timeout=15)
        save_evidence()

        # Wait for approval
        b["S5_wait_approval"] = step("S5_wait_approval", f"Wait for approval state",
                                     [CCC, "wait", session, "approval", "--timeout", "60"], timeout=70)
        save_evidence()

        # Approve
        b["S5_approve"] = step("S5_approve", f"Approve the permission",
                               [CCC, "approve", session, "yes"], timeout=15)
        save_evidence()

        # Wait for ready after approval
        b["S5_wait_ready2"] = step("S5_wait_ready2", f"Wait ready after approval",
                                    [CCC, "wait", session, "ready", "--timeout", "60"], timeout=70)
        save_evidence()

        # Get last response
        b["S5_last"] = step("S5_last", f"Last response after approval (should contain date output)",
                            [CCC, "last", session], timeout=15)
        save_evidence()

    # S6: send --auto-approve (claude and cursor only)
    if backend in ["claude", "cursor"]:
        print(f"\n[S6] Auto-approve test for {backend}", file=sys.stderr)

        b["S6"] = step("S6", f"Send with --auto-approve",
                       [CCC, "send", session, "Use Bash to run: whoami", "--auto-approve", "--timeout", "120"],
                       timeout=135)
        save_evidence()

    # S7: kill session
    print(f"\n[S7] Kill session for {backend}", file=sys.stderr)
    b["S7_kill"] = step("S7_kill", f"Kill {backend} session",
                        [CCC, "kill", session], timeout=15)
    save_evidence()

    b["S7_attach"] = step("S7_attach", f"Attach after kill (must fail)",
                          [CCC, "attach", session], timeout=10)
    save_evidence()

    if backend == "claude":
        claude_s2_ok = "PONG" in b["S2"].get("stdout", "").upper() or b["S2"]["exitCode"] == 0
        phase1_claude_passed = claude_s2_ok

# Boundary scenarios (claude only)
print(f"\n--- Boundary Scenarios (claude) ---", file=sys.stderr)
boundary_session = f"test-claude-boundary-{ts_str}"
bb = evidence["phases"]["phase1"]["boundary"]

# Create boundary session
bb["setup"] = step("boundary_setup", "Start claude session for boundary tests",
                   [CCC, "run", boundary_session, "--cwd", TEST_CWD], timeout=30)
save_evidence()

if bb["setup"]["exitCode"] == 0:
    # Wait for ready
    bb["wait_ready"] = step("boundary_wait_ready", "Wait for session ready",
                             [CCC, "wait", boundary_session, "ready", "--timeout", "60"], timeout=70)
    save_evidence()

    # B1: Long reply
    print(f"\n[B1] Long reply test", file=sys.stderr)
    bb["B1"] = step("B1", "Long reply: count 1 to 100 one per line",
                    [CCC, "send", boundary_session, "Count from 1 to 100, one number per line", "--timeout", "120"],
                    timeout=135)
    save_evidence()

    # B2: Timeout error
    print(f"\n[B2] Timeout error test", file=sys.stderr)
    bb["B2"] = step("B2", "Timeout: must error fast not hang",
                    [CCC, "send", boundary_session, "Think about philosophy for a very long time", "--timeout", "5"],
                    timeout=15)
    save_evidence()

    # Wait for ready after timeout
    bb["B2_wait"] = step("B2_wait", "Wait ready after timeout test",
                          [CCC, "wait", boundary_session, "ready", "--timeout", "60"], timeout=70)
    save_evidence()

    # B3: Dead session mid-wait
    print(f"\n[B3] Dead session mid-wait test", file=sys.stderr)
    # Start a send with --no-wait, kill tmux after 3s, then check waitReady behavior
    # We'll do this by starting the send with a long timeout, then killing tmux
    import threading

    def kill_tmux_after_delay():
        time.sleep(4)
        subprocess.run(["tmux", "kill-session", "-t", f"ccc-{boundary_session}"],
                      capture_output=True)
        print(f"  [B3] Killed tmux session ccc-{boundary_session}", file=sys.stderr)

    killer_thread = threading.Thread(target=kill_tmux_after_delay, daemon=True)
    killer_thread.start()

    bb["B3"] = step("B3", "Dead session mid-wait: send then tmux killed (must error not hang)",
                    [CCC, "send", boundary_session, "Count from 1 to 10000 slowly", "--timeout", "30"],
                    timeout=40)
    save_evidence()

    killer_thread.join(timeout=10)

evidence["phases"]["phase1"]["boundary"] = bb
save_evidence()

# Check phase1 passed
p1 = evidence["phases"]["phase1"]["claude"]
phase1_passed = (
    p1.get("S1_run", {}).get("exitCode", -1) == 0 and
    p1.get("S2", {}).get("exitCode", -1) == 0
)
evidence["phase1_passed"] = phase1_passed
save_evidence()

print(f"\n=== Phase 1 passed: {phase1_passed} ===", file=sys.stderr)

# ============================================================
# PHASE 2: Auxiliary Features (only if phase1 passed)
# ============================================================
if phase1_passed:
    print("\n=== PHASE 2: Auxiliary Features ===", file=sys.stderr)

    aux_session = f"test-aux-1-{ts_str}"
    p2 = evidence["phases"]["phase2"]

    # S8: wait
    print(f"\n[S8] wait test", file=sys.stderr)
    p2["S8_run"] = step("S8_run", "Start aux session",
                        [CCC, "run", aux_session, "--cwd", TEST_CWD], timeout=30)
    save_evidence()

    if p2["S8_run"]["exitCode"] == 0:
        p2["S8_send"] = step("S8_send", "Send 'say hi' --no-wait",
                             [CCC, "send", aux_session, "say hi", "--no-wait"], timeout=15)
        save_evidence()

        p2["S8_wait"] = step("S8_wait", "Wait for ready state --json",
                             [CCC, "wait", aux_session, "ready", "--timeout", "60", "--json"], timeout=70)
        save_evidence()

        # S9: model list
        print(f"\n[S9] model list", file=sys.stderr)
        p2["S9_wait_ready"] = step("S9_wait_ready", "Wait ready before model test",
                                    [CCC, "wait", aux_session, "ready", "--timeout", "30"], timeout=40)
        save_evidence()

        p2["S9"] = step("S9", "List/switch models",
                        [CCC, "model", aux_session], timeout=20)
        save_evidence()

        # Dismiss any modal that opened
        time.sleep(1)
        p2["S9_escape"] = step("S9_escape", "Dismiss any modal",
                               [CCC, "key", aux_session, "Escape"], timeout=5)
        save_evidence()

        p2["S9_wait_ready2"] = step("S9_wait_ready2", "Wait ready after model",
                                    [CCC, "wait", aux_session, "ready", "--timeout", "15"], timeout=20)
        save_evidence()

        # S10: history
        print(f"\n[S10] history", file=sys.stderr)
        p2["S10_send"] = step("S10_send", "Send message for history test",
                              [CCC, "send", aux_session, "say hello for history test", "--timeout", "60"], timeout=75)
        save_evidence()

        p2["S10"] = step("S10", "View conversation history",
                         [CCC, "history", aux_session], timeout=15)
        save_evidence()

        # S11: tail
        print(f"\n[S11] tail", file=sys.stderr)
        p2["S11"] = step("S11", "Tail last 20 lines",
                         [CCC, "tail", aux_session, "--lines", "20"], timeout=15)
        save_evidence()

        # S12: ps / clean
        print(f"\n[S12] ps and clean", file=sys.stderr)
        p2["S12_ps"] = step("S12_ps", "List sessions (ps)",
                            [CCC, "ps"], timeout=15)
        save_evidence()

        p2["S12_clean"] = step("S12_clean", "Clean dry-run",
                               [CCC, "clean", "--dry-run"], timeout=15)
        save_evidence()

        # S13: relay
        print(f"\n[S13] relay debate", file=sys.stderr)
        p2["S13"] = step("S13", "Relay debate TypeScript vs Python --rounds 1",
                         [CCC, "relay", "debate", "Is TypeScript better than Python?", "--rounds", "1", "--cwd", TEST_CWD],
                         timeout=300)
        save_evidence()

        # S14: stream
        print(f"\n[S14] stream", file=sys.stderr)
        p2["S14"] = step("S14", "Stream one-shot query",
                         [CCC, "stream", "Reply with exactly: STREAM_OK", "--timeout", "60"],
                         timeout=75)
        save_evidence()

        # Keep aux session alive for observer
        evidence["active_sessions"] = [aux_session]
        save_evidence()

        print(f"\nKeeping aux session alive: {aux_session}", file=sys.stderr)
    else:
        print("  SKIP Phase 2 — aux session failed to start", file=sys.stderr)
else:
    print("  SKIP Phase 2 — Phase 1 did not pass", file=sys.stderr)

save_evidence()

# Write registry
registry = {
    "timestamp": ts_str,
    "active_sessions": evidence.get("active_sessions", []),
    "evidence_file": EVIDENCE_FILE
}
with open(REGISTRY_FILE, "w") as f:
    json.dump(registry, f, indent=2)

print(f"\n\nEvidence written to: {EVIDENCE_FILE}", file=sys.stderr)
print(f"Registry written to: {REGISTRY_FILE}", file=sys.stderr)
print("DO NOT KILL SESSIONS — Observer needs them alive", file=sys.stderr)

# Also print to stdout for the bash script
print(EVIDENCE_FILE)
print(REGISTRY_FILE)
PYEOF
