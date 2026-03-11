#!/usr/bin/env python3
"""
CCC Test Suite — Phase 1 + Phase 2
Records evidence to JSON files in tests/state/
"""

import subprocess
import json
import os
import sys
import time
import threading
import datetime

CCC = os.path.expanduser("~/.local/bin/ccc")
TEST_CWD = "/Users/mj/repos/claude-cli-connector/ccc-ts"
STATE_DIR = f"{TEST_CWD}/tests/state"
os.makedirs(STATE_DIR, exist_ok=True)

ts_str = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
EVIDENCE_FILE = f"{STATE_DIR}/evidence-{ts_str}.json"
REGISTRY_FILE = f"{STATE_DIR}/registry-{ts_str}.json"

print(f"Timestamp: {ts_str}", file=sys.stderr)
print(f"Evidence: {EVIDENCE_FILE}", file=sys.stderr)

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


def run_cmd(args, timeout=120):
    """Run command, return (stdout, stderr, exit_code, duration_ms)"""
    start = time.time()
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        duration = int((time.time() - start) * 1000)
        return result.stdout, result.stderr, result.returncode, duration
    except subprocess.TimeoutExpired:
        duration = int((time.time() - start) * 1000)
        return "", "TIMEOUT", -1, duration
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        return "", str(e), -2, duration


def step(label, description, args, timeout=120, notes=""):
    """Run a step and return result dict."""
    cmd_str = " ".join(str(a) for a in args)
    print(f"  [{label}] {cmd_str[:80]}", file=sys.stderr)
    stdout, stderr, exit_code, duration = run_cmd(args, timeout=timeout)
    result = {
        "cmd": cmd_str,
        "description": description,
        "stdout": stdout[:3000],
        "stderr": stderr[:500],
        "exitCode": exit_code,
        "durationMs": duration
    }
    if notes:
        result["notes"] = notes
    status = "OK" if exit_code == 0 else f"FAIL(exit={exit_code})"
    print(f"         -> [{status}] {duration}ms", file=sys.stderr)
    if exit_code != 0 and stderr.strip():
        print(f"         stderr: {stderr[:150].strip()}", file=sys.stderr)
    if stdout.strip():
        print(f"         stdout: {stdout[:100].strip()}", file=sys.stderr)
    return result


def tmux_alive(session_name):
    """Check if a tmux session exists."""
    _, _, code, _ = run_cmd(["tmux", "has-session", "-t", f"ccc-{session_name}"], timeout=5)
    return code == 0


save_evidence()

# ============================================================
# PHASE 1: Core Features — All Backends
# ============================================================
print("\n========== PHASE 1: Core Features ==========", file=sys.stderr)

backends_config = [
    ("claude",    None),
    ("cursor",    "--cursor"),
    ("codex",     "--codex"),
    ("opencode",  "--opencode"),
]

for backend, flag in backends_config:
    print(f"\n---------- Backend: {backend} ----------", file=sys.stderr)
    session = f"test-{backend}-1"
    b = evidence["phases"]["phase1"][backend]

    # S1: run
    run_args = [CCC, "run", session, "--cwd", TEST_CWD]
    if flag:
        run_args.append(flag)
    b["S1_run"] = step("S1_run", f"Start {backend} session", run_args, timeout=30)
    save_evidence()

    if b["S1_run"]["exitCode"] != 0:
        print(f"  SKIP rest of {backend} — session failed to start", file=sys.stderr)
        b["S1_attach"] = {"cmd": "skipped", "exitCode": -99, "stdout": "", "stderr": "session start failed", "durationMs": 0, "description": "skipped"}
        continue

    # Wait for UI to settle
    time.sleep(4)

    # S1: attach
    b["S1_attach"] = step("S1_attach", f"Attach to {backend} session (alive check)",
                           [CCC, "attach", session], timeout=10)
    save_evidence()

    # S2: send → reply
    print(f"\n  [S2] send PONG test", file=sys.stderr)
    b["S2"] = step("S2", f"Send 'Reply with exactly: PONG' — response must contain PONG",
                   [CCC, "send", session, "Reply with exactly: PONG", "--timeout", "120"],
                   timeout=135)
    save_evidence()

    # S3: status --json
    b["S3"] = step("S3", f"status --json — state should be ready, lastResponse non-empty",
                   [CCC, "status", session, "--json"], timeout=15)
    save_evidence()

    # S4: last
    print(f"\n  [S4] last response", file=sys.stderr)
    if backend == "opencode":
        b["S4"] = step("S4", f"last --full (opencode requires --full)",
                       [CCC, "last", session, "--full"], timeout=15)
    else:
        b["S4"] = step("S4", f"last response — should contain PONG",
                       [CCC, "last", session], timeout=15)
    save_evidence()

    # S5: approve (claude and cursor only)
    if backend in ["claude", "cursor"]:
        print(f"\n  [S5] approval test", file=sys.stderr)

        # Wait for ready
        b["S5_wait_ready"] = step("S5_wait_ready", "Wait ready before approval test",
                                   [CCC, "wait", session, "ready", "--timeout", "60"], timeout=70)
        save_evidence()

        b["S5_send"] = step("S5_send", "Send prompt that triggers bash permission",
                            [CCC, "send", session, "Use the Bash tool to run: date", "--no-wait"],
                            timeout=15)
        save_evidence()

        # Give Claude time to show the prompt
        time.sleep(8)

        b["S5_status"] = step("S5_status", "Check status (expect approval state)",
                              [CCC, "status", session, "--json"], timeout=15)
        save_evidence()

        b["S5_wait_approval"] = step("S5_wait_approval", "Wait for approval state",
                                     [CCC, "wait", session, "approval", "--timeout", "60"], timeout=70)
        save_evidence()

        b["S5_approve"] = step("S5_approve", "Approve the permission with 'yes'",
                               [CCC, "approve", session, "yes"], timeout=15)
        save_evidence()

        b["S5_wait_ready2"] = step("S5_wait_ready2", "Wait ready after approval",
                                    [CCC, "wait", session, "ready", "--timeout", "60"], timeout=70)
        save_evidence()

        b["S5_last"] = step("S5_last", "Last response — should contain date output",
                            [CCC, "last", session], timeout=15)
        save_evidence()

    # S6: auto-approve (claude and cursor only)
    if backend in ["claude", "cursor"]:
        print(f"\n  [S6] auto-approve test", file=sys.stderr)
        b["S6"] = step("S6", "Send with --auto-approve — must complete without hanging",
                       [CCC, "send", session, "Use Bash to run: whoami", "--auto-approve", "--timeout", "120"],
                       timeout=135)
        save_evidence()

    # S7: kill
    print(f"\n  [S7] kill session", file=sys.stderr)
    b["S7_kill"] = step("S7_kill", f"Kill {backend} session",
                        [CCC, "kill", session], timeout=15)
    save_evidence()

    b["S7_attach"] = step("S7_attach", "Attach after kill — must exit non-zero",
                          [CCC, "attach", session], timeout=10)
    save_evidence()

# -------- Boundary Scenarios (claude only) --------
print("\n---------- Boundary Scenarios (claude) ----------", file=sys.stderr)
boundary_session = "test-claude-boundary"
bb = evidence["phases"]["phase1"]["boundary"]

bb["setup"] = step("setup", "Start claude session for boundary tests",
                   [CCC, "run", boundary_session, "--cwd", TEST_CWD], timeout=30)
save_evidence()

if bb["setup"]["exitCode"] == 0:
    time.sleep(4)
    bb["wait_ready"] = step("wait_ready", "Wait for session ready",
                             [CCC, "wait", boundary_session, "ready", "--timeout", "60"], timeout=70)
    save_evidence()

    # B1: Long reply
    print(f"\n  [B1] Long reply test", file=sys.stderr)
    bb["B1"] = step("B1", "Long reply: count 1 to 100 one per line — reply must be non-empty",
                    [CCC, "send", boundary_session, "Count from 1 to 100, one number per line", "--timeout", "120"],
                    timeout=135)
    save_evidence()

    # Wait for ready after B1
    bb["B1_wait"] = step("B1_wait", "Wait ready after B1",
                          [CCC, "wait", boundary_session, "ready", "--timeout", "60"], timeout=70)
    save_evidence()

    # B2: Timeout error
    print(f"\n  [B2] Timeout error test", file=sys.stderr)
    bb["B2"] = step("B2", "Timeout error: --timeout 5 must return error quickly not hang",
                    [CCC, "send", boundary_session, "Think about philosophy for a very long time", "--timeout", "5"],
                    timeout=15,
                    notes="Should exit non-zero within ~5s")
    save_evidence()

    # Interrupt and wait for ready
    time.sleep(1)
    bb["B2_interrupt"] = step("B2_interrupt", "Interrupt after timeout test",
                               [CCC, "interrupt", boundary_session], timeout=10)
    save_evidence()

    bb["B2_wait"] = step("B2_wait", "Wait ready after B2",
                          [CCC, "wait", boundary_session, "ready", "--timeout", "60"], timeout=70)
    save_evidence()

    # B3: Dead session mid-wait
    print(f"\n  [B3] Dead session mid-wait test", file=sys.stderr)

    # Kill tmux session after 4 seconds in a background thread
    kill_done = threading.Event()
    def kill_tmux_after_delay():
        time.sleep(4)
        result = subprocess.run(
            ["tmux", "kill-session", "-t", f"ccc-{boundary_session}"],
            capture_output=True
        )
        print(f"  [B3] Killed tmux ccc-{boundary_session} (code={result.returncode})", file=sys.stderr)
        kill_done.set()

    killer = threading.Thread(target=kill_tmux_after_delay, daemon=True)
    killer.start()

    bb["B3"] = step("B3", "Dead session mid-wait: kill tmux after 4s — must error not hang",
                    [CCC, "send", boundary_session, "Count from 1 to 99999 slowly one per line", "--timeout", "30"],
                    timeout=45,
                    notes="tmux killed after 4s; should error/timeout not hang forever")
    save_evidence()
    kill_done.wait(timeout=10)

else:
    print("  SKIP boundary — session failed to start", file=sys.stderr)

# ---- Phase 1 Pass Check ----
p1_claude = evidence["phases"]["phase1"]["claude"]
phase1_passed = (
    p1_claude.get("S1_run", {}).get("exitCode", -1) == 0 and
    p1_claude.get("S2", {}).get("exitCode", -1) == 0
)
evidence["phase1_passed"] = phase1_passed
save_evidence()

print(f"\n========== Phase 1 passed: {phase1_passed} ==========", file=sys.stderr)

# ============================================================
# PHASE 2: Auxiliary Features
# ============================================================
if phase1_passed:
    print("\n========== PHASE 2: Auxiliary Features ==========", file=sys.stderr)

    aux_session = "test-aux-1"
    p2 = evidence["phases"]["phase2"]

    # S8: wait
    print(f"\n  [S8] wait test", file=sys.stderr)
    p2["S8_run"] = step("S8_run", "Start aux session (claude)",
                        [CCC, "run", aux_session, "--cwd", TEST_CWD], timeout=30)
    save_evidence()

    if p2["S8_run"]["exitCode"] == 0:
        time.sleep(4)

        p2["S8_send"] = step("S8_send", "Send 'say hi' --no-wait",
                             [CCC, "send", aux_session, "say hi", "--no-wait"], timeout=15)
        save_evidence()

        p2["S8_wait"] = step("S8_wait", "Wait for ready state --json — state must be 'ready'",
                             [CCC, "wait", aux_session, "ready", "--timeout", "60", "--json"], timeout=70)
        save_evidence()

        # S9: model list
        print(f"\n  [S9] model list", file=sys.stderr)
        p2["S9_wait_ready"] = step("S9_wait_ready", "Wait ready before model test",
                                    [CCC, "wait", aux_session, "ready", "--timeout", "30"], timeout=40)
        save_evidence()

        p2["S9"] = step("S9", "List/switch models — must list >=2 models",
                        [CCC, "model", aux_session], timeout=20)
        save_evidence()

        time.sleep(1)
        p2["S9_escape"] = step("S9_escape", "Dismiss any modal with Escape",
                               [CCC, "key", aux_session, "Escape"], timeout=5)
        save_evidence()

        p2["S9_wait_ready2"] = step("S9_wait_ready2", "Wait ready after model",
                                    [CCC, "wait", aux_session, "ready", "--timeout", "15"], timeout=20)
        save_evidence()

        # S10: history
        print(f"\n  [S10] history", file=sys.stderr)
        p2["S10_send"] = step("S10_send", "Send message for history test",
                              [CCC, "send", aux_session, "say hello for history test", "--timeout", "60"], timeout=75)
        save_evidence()

        p2["S10"] = step("S10", "View conversation history — must show entries",
                         [CCC, "history", aux_session], timeout=15)
        save_evidence()

        # S11: tail
        print(f"\n  [S11] tail", file=sys.stderr)
        p2["S11"] = step("S11", "Tail last 20 lines — must be non-empty",
                         [CCC, "tail", aux_session, "--lines", "20"], timeout=15)
        save_evidence()

        # S12: ps / clean
        print(f"\n  [S12] ps and clean", file=sys.stderr)
        p2["S12_ps"] = step("S12_ps", "List sessions — must include test-aux-1",
                            [CCC, "ps"], timeout=15)
        save_evidence()

        p2["S12_clean"] = step("S12_clean", "Clean --dry-run — must not error",
                               [CCC, "clean", "--dry-run"], timeout=15)
        save_evidence()

        # S13: relay debate
        print(f"\n  [S13] relay debate", file=sys.stderr)
        p2["S13"] = step("S13", "Relay debate: TypeScript vs Python --rounds 1 — both speakers must appear",
                         [CCC, "relay", "debate", "Is TypeScript better than Python?",
                          "--rounds", "1", "--cwd", TEST_CWD],
                         timeout=300,
                         notes="May take 2-5 min")
        save_evidence()

        # S14: stream
        print(f"\n  [S14] stream", file=sys.stderr)
        p2["S14"] = step("S14", "Stream one-shot query — stdout must contain STREAM_OK",
                         [CCC, "stream", "Reply with exactly: STREAM_OK", "--timeout", "60"],
                         timeout=75)
        save_evidence()

        # Keep aux session alive
        if tmux_alive(aux_session):
            evidence["active_sessions"] = [aux_session]
        save_evidence()
    else:
        print("  SKIP Phase 2 — aux session failed to start", file=sys.stderr)
else:
    print("  SKIP Phase 2 — Phase 1 did not pass for claude", file=sys.stderr)

# ---- Final Save ----
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

# Summary
print("\n--- SUMMARY ---", file=sys.stderr)
for backend in ["claude", "cursor", "codex", "opencode"]:
    b = evidence["phases"]["phase1"][backend]
    if not b:
        print(f"  {backend}: no data", file=sys.stderr)
        continue
    s1 = b.get("S1_run", {}).get("exitCode", "?")
    s2 = b.get("S2", {}).get("exitCode", "?")
    s7 = b.get("S7_kill", {}).get("exitCode", "?")
    print(f"  {backend}: S1={s1} S2={s2} S7={s7}", file=sys.stderr)

p1_boundary = evidence["phases"]["phase1"]["boundary"]
b1 = p1_boundary.get("B1", {}).get("exitCode", "?")
b2 = p1_boundary.get("B2", {}).get("exitCode", "?")
b3 = p1_boundary.get("B3", {}).get("exitCode", "?")
print(f"  boundary: B1={b1} B2={b2} B3={b3}", file=sys.stderr)

if phase1_passed:
    p2 = evidence["phases"]["phase2"]
    for sk in ["S8_wait", "S9", "S10", "S11", "S12_ps", "S13", "S14"]:
        ec = p2.get(sk, {}).get("exitCode", "?")
        print(f"  phase2/{sk}: exit={ec}", file=sys.stderr)

print(f"\nPhase 1 passed: {phase1_passed}", file=sys.stderr)
print(f"Active sessions: {evidence.get('active_sessions', [])}", file=sys.stderr)

# Print file paths to stdout
print(EVIDENCE_FILE)
print(REGISTRY_FILE)
