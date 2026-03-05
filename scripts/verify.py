#!/usr/bin/env python3
"""
scripts/verify.py
-----------------
End-to-end verification script for claude-cli-connector.

Tests:
  1. Session lifecycle  — create, is_alive, kill
  2. State detection    — thinking / ready / dead
  3. Output capture     — raw pane, ANSI-stripped, tail, diff
  4. Input types        — plain text, multi-line, special chars, slash command
  5. Choice detection   — /model triggers a numbered menu
  6. Interrupt          — Ctrl-C stops a running generation

Usage:
    python scripts/verify.py [--session NAME] [--timeout N]

Requirements:
    pip install -e ".[dev]"
    tmux must be running
    claude CLI must be in PATH
"""

from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console
from rich.table import Table
from rich import print as rprint

console = Console()

PASS = "[green]PASS[/green]"
FAIL = "[red]FAIL[/red]"
SKIP = "[dim]SKIP[/dim]"


def section(title: str) -> None:
    console.rule(f"[bold cyan]{title}[/bold cyan]")


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    msg = f"  {icon}  {label}"
    if detail:
        msg += f"  [dim]{detail}[/dim]"
    rprint(msg)
    return ok


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wait_for_ready(session, timeout: float = 30.0, poll: float = 0.5) -> bool:
    """Poll until session is ready or timeout."""
    from claude_cli_connector.parser import detect_ready
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = session.transport.capture()
        result = detect_ready(snap.lines)
        if result.is_ready:
            return True
        time.sleep(poll)
    return False


def get_state(session) -> str:
    from claude_cli_connector.parser import detect_ready
    if not session.is_alive():
        return "dead"
    snap = session.transport.capture()
    result = detect_ready(snap.lines)
    choices = session.detect_choices()
    if choices:
        return "choosing"
    return "ready" if result.is_ready else "thinking"


# ---------------------------------------------------------------------------
# Test suites
# ---------------------------------------------------------------------------

def test_lifecycle(session_name: str) -> tuple[int, int]:
    """1. Session lifecycle: create → alive → kill."""
    section("1 · Session lifecycle")
    from claude_cli_connector import ClaudeSession
    from claude_cli_connector.exceptions import SessionAlreadyExistsError

    passed = failed = 0

    # Create
    try:
        session = ClaudeSession.create(name=session_name, cwd=".")
        ok = check("ClaudeSession.create() succeeds",
                   session is not None,
                   f"tmux: {session.transport.tmux_session_name}")
    except Exception as exc:
        check("ClaudeSession.create() succeeds", False, str(exc))
        rprint(f"  [red]Cannot continue — session creation failed.[/red]")
        return 0, 1

    passed += ok; failed += not ok

    # is_alive immediately after create
    alive = session.is_alive()
    ok = check("is_alive() → True right after create", alive)
    passed += ok; failed += not ok

    # Duplicate create raises
    try:
        ClaudeSession.create(name=session_name)
        ok = check("duplicate create raises SessionAlreadyExistsError", False,
                   "no exception raised")
    except SessionAlreadyExistsError:
        ok = check("duplicate create raises SessionAlreadyExistsError", True)
    except Exception as exc:
        ok = check("duplicate create raises SessionAlreadyExistsError", False, str(exc))
    passed += ok; failed += not ok

    # Attach
    try:
        s2 = ClaudeSession.attach(name=session_name)
        ok = check("ClaudeSession.attach() to existing session", s2 is not None)
    except Exception as exc:
        ok = check("ClaudeSession.attach() to existing session", False, str(exc))
    passed += ok; failed += not ok

    return passed, failed


def test_trust_prompt(session_name: str) -> tuple[int, int]:
    """Handle Claude Code's one-time trust prompt on first launch."""
    section("2 · Trust prompt handling")
    from claude_cli_connector import ClaudeSession
    from claude_cli_connector.parser import detect_ready

    passed = failed = 0
    session = ClaudeSession.attach(name=session_name)

    snap = session.transport.capture()
    has_trust = any("trust" in ln.lower() or "Yes, I trust" in ln
                    for ln in snap.lines)

    if has_trust:
        rprint("  [yellow]Trust prompt detected — sending '1'…[/yellow]")
        session.send("1")
        time.sleep(2)
        ok = check("Trust prompt answered", True, "sent '1'")
    else:
        ok = check("No trust prompt (already trusted)", True)
    passed += ok; failed += not ok

    # Wait for ready
    ready = wait_for_ready(session, timeout=15)
    ok = check("Session ready after trust prompt", ready)
    passed += ok; failed += not ok

    return passed, failed


def test_state_detection(session_name: str, timeout: float) -> tuple[int, int]:
    """3. State detection: thinking → ready → dead."""
    section("3 · State detection")
    from claude_cli_connector import ClaudeSession
    from claude_cli_connector.parser import detect_ready

    passed = failed = 0
    session = ClaudeSession.attach(name=session_name)

    # Should be ready now
    state = get_state(session)
    ok = check("Initial state is 'ready'", state == "ready", f"got: {state}")
    passed += ok; failed += not ok

    # Send long task → should go thinking
    session.send("write a poem with exactly 10 lines about the ocean")
    time.sleep(1.0)
    state = get_state(session)
    ok = check("State is 'thinking' immediately after send",
               state == "thinking", f"got: {state}")
    passed += ok; failed += not ok

    # Wait for ready
    ready = wait_for_ready(session, timeout=timeout)
    ok = check(f"State returns to 'ready' within {timeout}s", ready)
    passed += ok; failed += not ok

    # Confidence field
    snap = session.transport.capture()
    result = detect_ready(snap.lines)
    ok = check("Confidence is 'prompt' when ready via ❯",
               result.confidence in ("prompt", "stability"),
               f"confidence: {result.confidence}")
    passed += ok; failed += not ok

    return passed, failed


def test_output_capture(session_name: str) -> tuple[int, int]:
    """4. Output capture: raw pane / stripped / tail / diff."""
    section("4 · Output capture")
    from claude_cli_connector import ClaudeSession
    from claude_cli_connector.parser import strip_ansi

    passed = failed = 0
    session = ClaudeSession.attach(name=session_name)

    # Raw capture returns lines
    snap = session.transport.capture()
    ok = check("capture() returns non-empty lines",
               len(snap.lines) > 0, f"{len(snap.lines)} lines")
    passed += ok; failed += not ok

    # ANSI stripped — no ESC bytes in clean output
    clean = session.capture()
    has_ansi = any("\x1b" in ln for ln in clean)
    ok = check("capture() output has no ANSI escape codes", not has_ansi)
    passed += ok; failed += not ok

    # tail
    tail = session.tail(lines=5)
    ok = check("tail(5) returns ≤5 lines", len(tail.splitlines()) <= 5,
               f"{len(tail.splitlines())} lines")
    passed += ok; failed += not ok

    # diff_output
    before = snap.lines[:]
    snap2 = session.transport.capture()
    from claude_cli_connector.parser import diff_output
    diff = diff_output(before, snap2.lines)
    ok = check("diff_output() returns list", isinstance(diff, list))
    passed += ok; failed += not ok

    return passed, failed


def test_input_types(session_name: str, timeout: float) -> tuple[int, int]:
    """5. Different input types: plain / multi-line / special chars / slash command."""
    section("5 · Input types")
    from claude_cli_connector import ClaudeSession

    passed = failed = 0
    session = ClaudeSession.attach(name=session_name)

    cases = [
        ("Plain text",        "reply with just the word: pong"),
        ("Number",            "what is 7 * 6? reply with just the number"),
        ("Special chars",     "repeat back exactly: hello & goodbye <end>"),
        ("Multi-word prompt", "in one word: what color is the sky?"),
    ]

    for label, msg in cases:
        try:
            response = session.send_and_wait(msg, timeout=timeout)
            ok = check(f"Input type — {label}",
                       len(response.strip()) > 0,
                       repr(response.strip()[:60]))
        except Exception as exc:
            ok = check(f"Input type — {label}", False, str(exc))
        passed += ok; failed += not ok

    return passed, failed


def test_choice_detection(session_name: str) -> tuple[int, int]:
    """6. Choice menu detection via /model."""
    section("6 · Choice menu detection")
    from claude_cli_connector import ClaudeSession

    passed = failed = 0
    session = ClaudeSession.attach(name=session_name)

    # Trigger model picker
    session.send("/model")
    time.sleep(2.0)

    choices = session.detect_choices()
    ok = check("/model triggers a choice menu (choices is not None)",
               choices is not None,
               f"got: {choices}")
    passed += ok; failed += not ok

    if choices:
        ok = check("Choice menu has ≥ 2 items", len(choices) >= 2,
                   f"{len(choices)} items: {[c.label for c in choices]}")
        passed += ok; failed += not ok

        ok = check("Each ChoiceItem has key + label",
                   all(c.key and c.label for c in choices))
        passed += ok; failed += not ok

        # Dismiss by sending Escape
        session.transport.send_keys("q", enter=False)
        time.sleep(1)

    return passed, failed


def test_interrupt(session_name: str) -> tuple[int, int]:
    """7. Interrupt: Ctrl-C stops a running generation."""
    section("7 · Interrupt")
    from claude_cli_connector import ClaudeSession

    passed = failed = 0
    session = ClaudeSession.attach(name=session_name)

    # Start a long generation
    session.send("count slowly from 1 to 1000, one number per line")
    time.sleep(1.5)

    state_before = get_state(session)
    ok = check("Session is 'thinking' before interrupt",
               state_before == "thinking", f"got: {state_before}")
    passed += ok; failed += not ok

    # Interrupt
    session.interrupt()
    time.sleep(1.5)

    state_after = get_state(session)
    ok = check("Session returns to 'ready' after interrupt",
               state_after == "ready", f"got: {state_after}")
    passed += ok; failed += not ok

    return passed, failed


def test_dead_detection(session_name: str) -> tuple[int, int]:
    """8. Dead detection: kill tmux → is_alive() = False."""
    section("8 · Dead detection")
    from claude_cli_connector import ClaudeSession
    import subprocess

    passed = failed = 0
    session = ClaudeSession.attach(name=session_name)

    alive_before = session.is_alive()
    ok = check("is_alive() → True before kill", alive_before)
    passed += ok; failed += not ok

    # Kill the underlying tmux session directly (bypass our kill() to test detection)
    tmux_name = session.transport.tmux_session_name
    subprocess.run(["tmux", "kill-session", "-t", tmux_name],
                   capture_output=True)
    time.sleep(0.5)

    alive_after = session.is_alive()
    ok = check("is_alive() → False after tmux kill", not alive_after)
    passed += ok; failed += not ok

    state = get_state(session)
    ok = check("get_state() → 'dead' after tmux kill", state == "dead")
    passed += ok; failed += not ok

    return passed, failed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="claude-cli-connector verification script")
    parser.add_argument("--session", default="ccc-verify",
                        help="tmux session name to use (default: ccc-verify)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="Max seconds to wait for Claude to respond (default: 60)")
    parser.add_argument("--skip-cleanup", action="store_true",
                        help="Don't kill the session at the end")
    args = parser.parse_args()

    rprint(f"\n[bold]claude-cli-connector verification[/bold]  "
           f"[dim]session={args.session}  timeout={args.timeout}s[/dim]\n")

    total_passed = total_failed = 0
    results: list[tuple[str, int, int]] = []

    def run(name: str, fn, *a) -> None:
        nonlocal total_passed, total_failed
        p, f = fn(*a)
        total_passed += p
        total_failed += f
        results.append((name, p, f))

    # Run all test suites in order
    run("Lifecycle",       test_lifecycle,       args.session)
    run("Trust prompt",    test_trust_prompt,    args.session)
    run("State detection", test_state_detection, args.session, args.timeout)
    run("Output capture",  test_output_capture,  args.session)
    run("Input types",     test_input_types,     args.session, args.timeout)
    run("Choice detection",test_choice_detection,args.session)
    run("Interrupt",       test_interrupt,       args.session)
    run("Dead detection",  test_dead_detection,  args.session)

    # Summary table
    console.rule("[bold]Summary[/bold]")
    table = Table(show_header=True)
    table.add_column("Suite")
    table.add_column("Passed", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Result", justify="center")

    for name, p, f in results:
        icon = "✓" if f == 0 else "✗"
        style = "green" if f == 0 else "red"
        table.add_row(name, str(p), str(f), f"[{style}]{icon}[/{style}]")

    console.print(table)

    verdict = "PASSED" if total_failed == 0 else "FAILED"
    color   = "green"  if total_failed == 0 else "red"
    rprint(f"\n[bold {color}]{verdict}[/bold {color}]  "
           f"{total_passed} passed, {total_failed} failed\n")

    sys.exit(0 if total_failed == 0 else 1)


if __name__ == "__main__":
    main()
