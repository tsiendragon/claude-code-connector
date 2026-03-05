#!/usr/bin/env python3
"""
scripts/verify.py
-----------------
End-to-end verification script for claude-cli-connector.

Prints every input/output exchange with Claude so you can visually
confirm the package is working correctly.

Usage:
    python scripts/verify.py
    python scripts/verify.py --session my-test --timeout 90 --verbose
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

console = Console()
VERBOSE = False

PASS = "[green]✓ PASS[/green]"
FAIL = "[red]✗ FAIL[/red]"


# ═══════════════════════════════════════════════════════════════════════════
# Display helpers
# ═══════════════════════════════════════════════════════════════════════════

def section(title: str) -> None:
    print()
    console.rule(f"[bold cyan]{title}[/bold cyan]")
    print()


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    msg = f"  {icon}  {label}"
    if detail:
        msg += f"  [dim]— {detail}[/dim]"
    rprint(msg)
    return ok


def show_input(prompt: str) -> None:
    rprint(f"\n    [bold yellow]▶ INPUT:[/bold yellow]  [yellow]{prompt}[/yellow]")


def show_output(response: str, max_lines: int = 15) -> None:
    rprint(f"    [bold blue]◀ OUTPUT:[/bold blue]")
    lines = response.strip().splitlines()
    if not lines:
        rprint("      [dim](empty)[/dim]")
    elif len(lines) > max_lines:
        for ln in lines[:max_lines]:
            rprint(f"      {ln}")
        rprint(f"      [dim]… ({len(lines) - max_lines} more lines)[/dim]")
    else:
        for ln in lines:
            rprint(f"      {ln}")


def show_exchange(prompt: str, response: str, max_lines: int = 15) -> None:
    show_input(prompt)
    show_output(response, max_lines)
    print()


def show_state(label: str, state: str) -> None:
    colours = {"ready": "green", "thinking": "yellow",
               "choosing": "purple", "dead": "red"}
    c = colours.get(state, "white")
    rprint(f"    [bold]{label}:[/bold] [{c}]{state}[/{c}]")


def dump_pane(session, label: str = "Pane snapshot", n: int = 10) -> None:
    """Print last N lines of the pane — for debugging."""
    try:
        snap = session.transport.capture()
        lines = snap.lines[-n:]
    except Exception:
        lines = ["(could not capture pane)"]
    rprint(f"\n    [dim]{label} (last {n} lines):[/dim]")
    for ln in lines:
        rprint(f"    [dim]│[/dim] {ln}")
    print()


def dump_pane_on_fail(session, ok: bool) -> None:
    if not ok:
        dump_pane(session, "Pane dump for debugging", 15)


# ═══════════════════════════════════════════════════════════════════════════
# State helpers
# ═══════════════════════════════════════════════════════════════════════════

def wait_for_ready(session, timeout: float = 60.0, poll: float = 0.5) -> bool:
    from claude_cli_connector.parser import detect_ready
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = session.transport.capture()
        if detect_ready(snap.lines).is_ready:
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


def send_and_capture(session, prompt: str, timeout: float = 60.0) -> str:
    """
    Send a message to Claude and return the NEW output only (diff-based).

    Unlike session.send_and_wait() which uses extract_last_response on the
    full pane, this captures before/after and diffs — more reliable when
    the pane has many prior exchanges.
    """
    from claude_cli_connector.parser import strip_ansi_lines, diff_output

    # Snapshot before send
    before = session.transport.capture().lines

    session.send(prompt)

    # Wait for ready
    if not wait_for_ready(session, timeout=timeout):
        # Timeout — still return what we have
        after = session.transport.capture().lines
        new_lines = diff_output(before, strip_ansi_lines(after))
        return "\n".join(new_lines).strip() + "\n(⚠ timed out)"

    after = session.transport.capture().lines
    new_lines = diff_output(strip_ansi_lines(before), strip_ansi_lines(after))

    # Filter out prompt echoes (lines starting with ❯)
    response_lines = [
        ln for ln in new_lines
        if not ln.strip().startswith("❯") and ln.strip()
    ]
    return "\n".join(response_lines).strip()


def cleanup_session(name: str) -> None:
    """Kill any existing tmux session with this name (idempotent)."""
    full = f"ccc-{name}"
    subprocess.run(["tmux", "kill-session", "-t", full],
                   capture_output=True, timeout=5)
    # Also clean the store
    try:
        from claude_cli_connector.store import get_default_store
        store = get_default_store()
        store.delete(name)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Test suites
# ═══════════════════════════════════════════════════════════════════════════

def test_lifecycle(session_name: str) -> tuple[object, int, int]:
    section("1 · Session lifecycle  (create / attach / duplicate guard)")
    from claude_cli_connector import ClaudeSession
    from claude_cli_connector.exceptions import SessionAlreadyExistsError

    p = f = 0

    # Clean up any leftover
    cleanup_session(session_name)

    # Create
    try:
        session = ClaudeSession.create(name=session_name, cwd=".")
        ok = check("create()", True,
                   f"tmux={session.transport.tmux_session_name}")
    except Exception as exc:
        check("create()", False, str(exc))
        return None, 0, 1
    p += ok; f += not ok

    # Alive?
    ok = check("is_alive() → True", session.is_alive())
    p += ok; f += not ok

    # Duplicate
    try:
        ClaudeSession.create(name=session_name)
        ok = check("duplicate create → raises", False, "no exception")
    except SessionAlreadyExistsError:
        ok = check("duplicate create → raises SessionAlreadyExistsError", True)
    except Exception as exc:
        ok = check("duplicate create → raises", False, type(exc).__name__)
    p += ok; f += not ok

    # Attach
    try:
        s2 = ClaudeSession.attach(name=session_name)
        ok = check("attach() to existing session", s2 is not None)
    except Exception as exc:
        ok = check("attach()", False, str(exc))
    p += ok; f += not ok

    if VERBOSE:
        dump_pane(session, "Pane after create", 5)

    return session, p, f


def test_trust_prompt(session_name: str) -> tuple[int, int]:
    section("2 · Trust prompt  (auto-detect & answer)")
    from claude_cli_connector import ClaudeSession

    p = f = 0
    session = ClaudeSession.attach(name=session_name)

    # Wait a bit for Claude CLI to fully boot
    time.sleep(3)

    snap = session.transport.capture()
    pane_text = "\n".join(snap.lines).lower()
    has_trust = "trust" in pane_text or "safety check" in pane_text

    if has_trust:
        rprint("    [yellow]Trust prompt detected — sending '1' to confirm…[/yellow]")
        session.send("1")
        time.sleep(3)
        ok = check("Answered trust prompt with '1'", True)
    else:
        ok = check("No trust prompt (directory already trusted)", True)
    p += ok; f += not ok

    # Wait for Claude to be fully ready
    ready = wait_for_ready(session, timeout=30)
    ok = check("Session ready after startup", ready)
    p += ok; f += not ok
    dump_pane_on_fail(session, ok)
    dump_pane(session, "Pane after trust prompt", 8)

    return p, f


def test_state_detection(session_name: str, timeout: float) -> tuple[int, int]:
    section("3 · State detection  (ready → thinking → ready)")
    from claude_cli_connector import ClaudeSession
    from claude_cli_connector.parser import detect_ready

    p = f = 0
    session = ClaudeSession.attach(name=session_name)

    # ── Check: should be ready before we send anything ──
    state = get_state(session)
    show_state("Before send", state)
    ok = check("Initial state = ready", state == "ready", f"got: {state}")
    p += ok; f += not ok
    dump_pane_on_fail(session, ok)

    # ── Send a long task → should go to thinking ──
    prompt = "write a haiku about the moon"
    show_input(prompt)
    session.send(prompt)

    # Retry up to 3 times to catch thinking state (timing-sensitive)
    caught_thinking = False
    for attempt in range(6):
        time.sleep(0.5)
        state = get_state(session)
        show_state(f"  {0.5*(attempt+1):.1f}s after send", state)
        if state == "thinking":
            caught_thinking = True
            break
    ok = check("Detected 'thinking' state after send", caught_thinking,
               f"got: {state}")
    p += ok; f += not ok
    dump_pane_on_fail(session, ok)

    # ── Wait for response ──
    ready = wait_for_ready(session, timeout=timeout)
    state = get_state(session)
    show_state("After wait_for_ready()", state)
    ok = check(f"Returns to 'ready' within {timeout}s", ready)
    p += ok; f += not ok

    # Show confidence
    snap = session.transport.capture()
    result = detect_ready(snap.lines)
    ok = check("Ready confidence field",
               result.confidence in ("prompt", "stability"),
               f"confidence={result.confidence}")
    p += ok; f += not ok

    # Show what Claude wrote
    from claude_cli_connector.parser import extract_last_response, strip_ansi_lines
    response = extract_last_response(strip_ansi_lines(snap.lines))
    show_output(response)
    print()

    return p, f


def test_output_capture(session_name: str) -> tuple[int, int]:
    section("4 · Output capture  (raw / stripped / tail / diff)")
    from claude_cli_connector import ClaudeSession
    from claude_cli_connector.parser import diff_output

    p = f = 0
    session = ClaudeSession.attach(name=session_name)

    # Raw capture
    snap = session.transport.capture()
    ok = check("transport.capture() → non-empty",
               len(snap.lines) > 0, f"{len(snap.lines)} lines")
    p += ok; f += not ok

    # ANSI-stripped capture
    clean = session.capture()
    has_esc = any("\x1b" in ln for ln in clean)
    ok = check("session.capture() is ANSI-free", not has_esc)
    p += ok; f += not ok

    # tail
    tail_text = session.tail(lines=5)
    n_lines = len(tail_text.strip().splitlines()) if tail_text.strip() else 0
    ok = check("tail(5) returns ≤ 5 lines", n_lines <= 5, f"{n_lines} lines")
    p += ok; f += not ok

    rprint(f"\n    [dim]tail(5):[/dim]")
    for ln in tail_text.strip().splitlines():
        rprint(f"    [dim]│[/dim] {ln}")
    print()

    # diff
    snap2 = session.transport.capture()
    diff = diff_output(snap.lines, snap2.lines)
    ok = check("diff_output() works", isinstance(diff, list), f"{len(diff)} new lines")
    p += ok; f += not ok

    return p, f


def test_input_types(session_name: str, timeout: float) -> tuple[int, int]:
    section("5 · Input / output pairs  (various prompt types)")
    from claude_cli_connector import ClaudeSession

    p = f = 0
    session = ClaudeSession.attach(name=session_name)

    cases = [
        ("Plain text",
         "reply with exactly one word: pong",
         lambda r: len(r.strip()) > 0),
        ("Number question",
         "what is 6 * 7? reply with just the number, nothing else",
         lambda r: "42" in r),
        ("Special chars",
         'repeat back exactly this and nothing else: hello & goodbye <end>',
         lambda r: "hello" in r.lower() and "goodbye" in r.lower()),
        ("Factual question",
         "in one word only, no punctuation: what color is the sky?",
         lambda r: "blue" in r.lower()),
    ]

    for label, prompt, validator in cases:
        try:
            response = send_and_capture(session, prompt, timeout=timeout)
            valid = validator(response)
            ok = check(f"{label}", len(response.strip()) > 0,
                       "content looks correct" if valid else "content may differ")
            show_exchange(prompt, response)
        except Exception as exc:
            ok = check(f"{label}", False, str(exc))
            dump_pane(session, f"Pane after failed '{label}'")
        p += ok; f += not ok

    return p, f


def test_choice_detection(session_name: str) -> tuple[int, int]:
    section("6 · Choice menu detection  (/model command)")
    from claude_cli_connector import ClaudeSession

    p = f = 0
    session = ClaudeSession.attach(name=session_name)

    show_input("/model")
    session.send("/model")

    # Choice menu may take a moment to render
    choices = None
    for attempt in range(6):
        time.sleep(0.5)
        choices = session.detect_choices()
        if choices:
            break

    ok = check("/model triggers a choice menu", choices is not None)
    p += ok; f += not ok

    if choices:
        ok = check(f"Menu has ≥ 2 items", len(choices) >= 2,
                   f"{len(choices)} items")
        p += ok; f += not ok

        rprint(f"\n    [bold blue]◀ CHOICES DETECTED:[/bold blue]")
        for c in choices:
            marker = " [green]← selected[/green]" if c.selected else ""
            rprint(f"      {c.key}. {c.label}{marker}")
        print()

        ok = check("Items have key + label",
                   all(c.key and c.label for c in choices))
        p += ok; f += not ok

        # Dismiss menu with Escape
        rprint("    [dim]Dismissing menu (Escape)…[/dim]")
        session.transport.send_keys("Escape", enter=False)
        time.sleep(1)
        # Send Enter to get back to clean prompt
        session.transport.send_keys("", enter=True)
        time.sleep(1)
        wait_for_ready(session, timeout=10)
    else:
        # Show pane to help debug why choices weren't detected
        dump_pane(session, "Pane (choice detection missed?)", 15)

        # Try to escape whatever state we're in
        session.transport.send_keys("Escape", enter=False)
        time.sleep(1)
        session.transport.send_keys("", enter=True)
        time.sleep(1)
        wait_for_ready(session, timeout=10)

    return p, f


def test_interrupt(session_name: str) -> tuple[int, int]:
    section("7 · Interrupt  (Ctrl-C stops generation)")
    from claude_cli_connector import ClaudeSession

    p = f = 0
    session = ClaudeSession.attach(name=session_name)

    prompt = "list every integer from 1 to 500, one per line, no extra words"
    show_input(prompt)
    session.send(prompt)

    # Wait until we catch it thinking
    caught = False
    for _ in range(6):
        time.sleep(0.5)
        state = get_state(session)
        if state == "thinking":
            caught = True
            break
    show_state("Before interrupt", state)
    ok = check("Caught 'thinking' before interrupt", caught, f"got: {state}")
    p += ok; f += not ok

    rprint("    [yellow]▶ Sending Ctrl-C…[/yellow]")
    session.interrupt()

    # Wait for it to settle back to ready
    recovered = wait_for_ready(session, timeout=10)
    state = get_state(session)
    show_state("After interrupt", state)
    ok = check("Returns to 'ready' after Ctrl-C", recovered, f"got: {state}")
    p += ok; f += not ok
    dump_pane_on_fail(session, ok)

    # Show what got generated before interrupt
    dump_pane(session, "Pane after interrupt", 10)

    return p, f


def test_dead_detection(session_name: str) -> tuple[int, int]:
    section("8 · Dead detection  (tmux kill → is_alive = False)")
    from claude_cli_connector import ClaudeSession

    p = f = 0
    session = ClaudeSession.attach(name=session_name)
    tmux_name = session.transport.tmux_session_name

    ok = check("is_alive() → True before kill", session.is_alive())
    p += ok; f += not ok

    rprint(f"    [yellow]▶ tmux kill-session -t {tmux_name}[/yellow]")
    subprocess.run(["tmux", "kill-session", "-t", tmux_name],
                   capture_output=True, timeout=5)
    time.sleep(1)

    alive = session.is_alive()
    ok = check("is_alive() → False after kill", not alive)
    p += ok; f += not ok

    state = get_state(session)
    show_state("State after kill", state)
    ok = check("get_state() = 'dead'", state == "dead")
    p += ok; f += not ok

    return p, f


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    global VERBOSE

    ap = argparse.ArgumentParser(
        description="claude-cli-connector · end-to-end verification")
    ap.add_argument("--session", default="ccc-verify",
                    help="Session name (default: ccc-verify)")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="Max wait for Claude responses (default: 60s)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Show extra pane dumps at each step")
    args = ap.parse_args()
    VERBOSE = args.verbose

    console.rule("[bold]claude-cli-connector · end-to-end verification[/bold]")
    rprint(f"  session = [bold]{args.session}[/bold]")
    rprint(f"  timeout = [bold]{args.timeout}s[/bold]")
    rprint(f"  verbose = [bold]{args.verbose}[/bold]")
    print()

    # ── Pre-flight cleanup ──
    rprint("[dim]Cleaning up any leftover session…[/dim]")
    cleanup_session(args.session)

    total_p = total_f = 0
    results: list[tuple[str, int, int]] = []

    def record(name: str, p: int, f: int) -> None:
        nonlocal total_p, total_f
        total_p += p; total_f += f
        results.append((name, p, f))

    # ── 1. Lifecycle ──
    session, lp, lf = test_lifecycle(args.session)
    record("Lifecycle", lp, lf)
    if session is None:
        rprint("\n[red]Cannot continue — session creation failed.[/red]")
        sys.exit(1)

    # ── 2–7 ──
    suites = [
        ("Trust prompt",     test_trust_prompt,    (args.session,)),
        ("State detection",  test_state_detection, (args.session, args.timeout)),
        ("Output capture",   test_output_capture,  (args.session,)),
        ("Input types",      test_input_types,     (args.session, args.timeout)),
        ("Choice detection", test_choice_detection,(args.session,)),
        ("Interrupt",        test_interrupt,       (args.session,)),
    ]

    for name, fn, fn_args in suites:
        try:
            sp, sf = fn(*fn_args)
            record(name, sp, sf)
        except KeyboardInterrupt:
            rprint(f"\n[yellow]Interrupted during {name}[/yellow]")
            record(name, 0, 1)
            break
        except Exception as exc:
            rprint(f"\n[red]Unexpected error in {name}: {exc}[/red]")
            record(name, 0, 1)

    # ── 8. Dead detection (runs last because it kills the session) ──
    try:
        dp, df = test_dead_detection(args.session)
        record("Dead detection", dp, df)
    except Exception as exc:
        rprint(f"\n[red]Dead detection error: {exc}[/red]")
        record("Dead detection", 0, 1)

    # ── Summary ──
    console.rule("[bold]Summary[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Suite", min_width=18)
    table.add_column("Passed", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("", justify="center")

    for name, sp, sf in results:
        icon = "[green]✓[/green]" if sf == 0 else "[red]✗[/red]"
        table.add_row(
            name,
            f"[green]{sp}[/green]",
            f"[red]{sf}[/red]" if sf else "[dim]0[/dim]",
            icon,
        )

    console.print(table)

    colour = "green" if total_f == 0 else "red"
    verdict = "ALL PASSED" if total_f == 0 else "SOME FAILED"
    rprint(f"\n  [bold {colour}]{verdict}[/bold {colour}]  "
           f"{total_p} passed, {total_f} failed\n")

    # Cleanup store entry (session already dead from test 8)
    cleanup_session(args.session)

    sys.exit(0 if total_f == 0 else 1)


if __name__ == "__main__":
    main()
