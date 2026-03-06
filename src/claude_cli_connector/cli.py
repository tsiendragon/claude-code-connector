"""
cli.py
------
Typer-based CLI entry point.  Installed as the ``ccc`` command.

Commands (tmux mode — default)
------------------------------
  ccc run       <name> [--cwd DIR] [--cmd STR]   Start a new Claude session
  ccc attach    <name>                            Attach to an existing session
  ccc send      <name> <message>                  Send a message and print response
  ccc tail      <name> [--lines N]                Print last N lines of pane
  ccc status    <name> [--porcelain]              Show session state
  ccc ps                                          List all known sessions
  ccc kill      <name>                            Kill a session
  ccc clean     [--yes] [--dry-run]               Remove dead session records
  ccc interrupt <name>                            Send Ctrl-C to a session

Commands (stream-json mode)
---------------------------
  ccc stream    <prompt> [--cwd DIR] [--tools ...]  One-shot stream-json query
"""

from __future__ import annotations

import sys
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from claude_cli_connector.exceptions import ConnectorError
from claude_cli_connector.parser import detect_ready
from claude_cli_connector.session import ClaudeSession
from claude_cli_connector.store import get_default_store

app = typer.Typer(
    name="ccc",
    help="Claude CLI Connector – manage Claude Code CLI sessions from the terminal.",
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True, style="bold red")


def _bail(msg: str) -> None:
    err_console.print(f"[red]Error:[/red] {msg}")
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# run  (tmux mode)
# ---------------------------------------------------------------------------

@app.command()
def run(
    name: Annotated[str, typer.Argument(help="Unique session name")],
    cwd: Annotated[str, typer.Option("--cwd", "-d", help="Working directory")] = ".",
    command: Annotated[str, typer.Option("--cmd", "-c", help="Claude CLI executable")] = "claude",
    startup_wait: Annotated[float, typer.Option(help="Seconds to wait for boot")] = 2.0,
) -> None:
    """Start a new Claude CLI session in a background tmux pane."""
    try:
        session = ClaudeSession.create(
            name=name, cwd=cwd, command=command, startup_wait=startup_wait
        )
        rprint(f"[green]✓[/green] Session [bold]{name}[/bold] started "
               f"(tmux: {session.transport.tmux_session_name})")
        rprint(f"  cwd: {cwd}")
        rprint(f"  To send a message: [bold]ccc send {name} \"your message\"[/bold]")
    except ConnectorError as exc:
        _bail(str(exc))


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------

@app.command()
def attach(
    name: Annotated[str, typer.Argument(help="Session name to attach to")],
) -> None:
    """Attach to an existing Claude CLI session."""
    try:
        session = ClaudeSession.attach(name=name)
        alive = session.is_alive()
        status = "[green]alive[/green]" if alive else "[red]dead[/red]"
        rprint(f"[green]✓[/green] Attached to [bold]{name}[/bold] – {status}")
    except ConnectorError as exc:
        _bail(str(exc))


# ---------------------------------------------------------------------------
# send  (tmux mode)
# ---------------------------------------------------------------------------

@app.command()
def send(
    name: Annotated[str, typer.Argument(help="Session name")],
    message: Annotated[str, typer.Argument(help="Message to send to Claude")],
    timeout: Annotated[float, typer.Option(help="Max seconds to wait")] = 300.0,
    no_wait: Annotated[bool, typer.Option("--no-wait", help="Fire and forget")] = False,
) -> None:
    """Send a message to a Claude CLI session and print the response."""
    try:
        session = ClaudeSession.attach(name=name)
        if no_wait:
            session.send(message)
            rprint(f"[green]✓[/green] Sent (no-wait).")
        else:
            rprint(f"[dim]Sending to [bold]{name}[/bold]…[/dim]")
            response = session.send_and_wait(message, timeout=timeout)
            console.print(response)
    except ConnectorError as exc:
        _bail(str(exc))


# ---------------------------------------------------------------------------
# tail
# ---------------------------------------------------------------------------

@app.command()
def tail(
    name: Annotated[str, typer.Argument(help="Session name")],
    lines: Annotated[int, typer.Option("--lines", "-n", help="Number of lines")] = 40,
) -> None:
    """Print the last N lines of a Claude CLI session pane."""
    try:
        session = ClaudeSession.attach(name=name)
        console.print(session.tail(lines=lines))
    except ConnectorError as exc:
        _bail(str(exc))


# ---------------------------------------------------------------------------
# ps  (list sessions)
# ---------------------------------------------------------------------------

@app.command()
def ps() -> None:
    """List all known Claude CLI sessions."""
    store = get_default_store()
    records = store.list_all()

    if not records:
        rprint("[dim]No sessions found.[/dim]")
        raise typer.Exit(0)

    table = Table(title="Claude CLI Sessions", show_lines=True)
    table.add_column("Name", style="bold cyan")
    table.add_column("tmux session")
    table.add_column("cwd")
    table.add_column("alive", justify="center")
    table.add_column("created", style="dim")

    import datetime

    for r in records:
        # Try to determine liveness by checking tmux.
        try:
            s = ClaudeSession.attach(name=r.name)
            alive_str = "[green]✓[/green]" if s.is_alive() else "[red]✗[/red]"
        except Exception:
            alive_str = "[red]✗[/red]"

        created = datetime.datetime.fromtimestamp(r.created_at).strftime("%Y-%m-%d %H:%M")
        table.add_row(r.name, r.tmux_session_name, r.cwd, alive_str, created)

    console.print(table)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status(
    name: Annotated[str, typer.Argument(help="Session name")],
    porcelain: Annotated[bool, typer.Option("--porcelain", help="Machine-readable: print only thinking|ready|dead")] = False,
) -> None:
    """Show the current state of a Claude CLI session (thinking / ready / dead)."""
    try:
        session = ClaudeSession.attach(name=name)
    except ConnectorError as exc:
        if porcelain:
            print("dead")
        else:
            _bail(str(exc))
        raise typer.Exit(1)

    if not session.is_alive():
        if porcelain:
            print("dead")
        else:
            rprint(f"[red]✗[/red] [bold]{name}[/bold] — [red]dead[/red] (tmux session gone)")
        raise typer.Exit(1)

    snapshot = session.transport.capture()
    result = detect_ready(snapshot.lines)
    choices = session.detect_choices()

    if choices:
        state = "choosing"
    elif result.is_ready:
        state = "ready"
    else:
        state = "thinking"

    if porcelain:
        print(state)
        return

    # Human-readable output
    STATE_STYLE = {
        "ready":    "[green]ready[/green]",
        "thinking": "[yellow]thinking[/yellow]",
        "choosing": "[purple]choosing[/purple]",
    }
    rprint(f"[bold]{name}[/bold] — {STATE_STYLE[state]}  "
           f"[dim](confidence: {result.confidence})[/dim]")

    if choices:
        rprint(f"  [dim]choices:[/dim]")
        for c in choices:
            rprint(f"    [purple]{c.key}.[/purple] {c.label}")


# ---------------------------------------------------------------------------
# kill
# ---------------------------------------------------------------------------

@app.command()
def kill(
    name: Annotated[str, typer.Argument(help="Session name to kill")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Kill a Claude CLI session."""
    if not yes:
        confirmed = typer.confirm(f"Kill session '{name}'?")
        if not confirmed:
            raise typer.Abort()
    try:
        session = ClaudeSession.attach(name=name)
        session.kill()
        rprint(f"[green]✓[/green] Session [bold]{name}[/bold] killed.")
    except ConnectorError as exc:
        _bail(str(exc))


# ---------------------------------------------------------------------------
# clean  (remove dead session records)
# ---------------------------------------------------------------------------

@app.command()
def clean(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show what would be removed without removing")] = False,
) -> None:
    """Remove stale session records whose tmux sessions no longer exist."""
    store = get_default_store()
    records = store.list_all()

    if not records:
        rprint("[dim]No sessions found.[/dim]")
        raise typer.Exit(0)

    dead: list[str] = []
    alive: list[str] = []

    for r in records:
        try:
            s = ClaudeSession.attach(name=r.name)
            if s.is_alive():
                alive.append(r.name)
            else:
                dead.append(r.name)
        except Exception:
            dead.append(r.name)

    if not dead:
        rprint("[green]✓[/green] All sessions are alive — nothing to clean.")
        raise typer.Exit(0)

    rprint(f"Found [bold]{len(dead)}[/bold] dead session(s): {', '.join(dead)}")
    if alive:
        rprint(f"  ([dim]{len(alive)} alive session(s) will be kept[/dim])")

    if dry_run:
        rprint("[dim]Dry run — no records removed.[/dim]")
        raise typer.Exit(0)

    if not yes:
        confirmed = typer.confirm("Remove these dead session records?")
        if not confirmed:
            raise typer.Abort()

    for name in dead:
        store.delete(name)

    rprint(f"[green]✓[/green] Removed {len(dead)} dead session record(s).")


# ---------------------------------------------------------------------------
# interrupt
# ---------------------------------------------------------------------------

@app.command()
def interrupt(
    name: Annotated[str, typer.Argument(help="Session name")],
) -> None:
    """Send Ctrl-C to a running Claude CLI session."""
    try:
        session = ClaudeSession.attach(name=name)
        session.interrupt()
        rprint(f"[yellow]⚡[/yellow] Interrupted session [bold]{name}[/bold].")
    except ConnectorError as exc:
        _bail(str(exc))


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

@app.command()
def history(
    name: Annotated[Optional[str], typer.Argument(help="Session name (omit to list all)")] = None,
    last: Annotated[int, typer.Option("--last", "-n", help="Show last N entries")] = 0,
    run: Annotated[Optional[str], typer.Option("--run", "-r", help="Specific run ID")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON lines")] = False,
) -> None:
    """
    View conversation history for a session.

    Without arguments, lists all sessions that have history.
    With a session name, shows the conversation log.

    Examples:
        ccc history                  # list sessions with history
        ccc history myproject        # full history for myproject
        ccc history myproject -n 10  # last 10 entries
        ccc history myproject --json # raw JSONL output
    """
    from claude_cli_connector.history import (
        list_sessions_with_history,
        list_session_runs,
        read_full_session_history,
        read_history_file,
    )
    import datetime
    import json

    if name is None:
        # List all sessions with history
        sessions = list_sessions_with_history()
        if not sessions:
            rprint("[dim]No conversation history found.[/dim]")
            raise typer.Exit(0)

        table = Table(title="Sessions with History", show_lines=True)
        table.add_column("Session", style="bold cyan")
        table.add_column("Runs", justify="right")
        table.add_column("Latest run", style="dim")

        for sname in sessions:
            runs = list_session_runs(sname)
            latest = runs[-1].stem if runs else ""
            table.add_row(sname, str(len(runs)), latest)

        console.print(table)
        return

    # Show history for a specific session
    if run:
        from claude_cli_connector.history import _history_dir
        run_path = _history_dir() / name / f"{run}.jsonl"
        entries = read_history_file(run_path)
    else:
        entries = read_full_session_history(name)

    if not entries:
        rprint(f"[dim]No history for session '{name}'.[/dim]")

        # Show available runs
        runs = list_session_runs(name)
        if runs:
            rprint("[dim]Available runs:[/dim]")
            for r in runs:
                rprint(f"  [cyan]{r.stem}[/cyan]")
        raise typer.Exit(0)

    if last > 0:
        entries = entries[-last:]

    if json_out:
        for entry in entries:
            print(entry.to_json())
        return

    # Human-readable output
    ROLE_STYLE = {
        "user": "[bold blue]USER[/bold blue]",
        "assistant": "[bold green]CLAUDE[/bold green]",
        "system": "[dim]SYSTEM[/dim]",
        "tool": "[yellow]TOOL[/yellow]",
    }

    for entry in entries:
        ts = datetime.datetime.fromtimestamp(entry.ts).strftime("%H:%M:%S")
        role_label = ROLE_STYLE.get(entry.role, entry.role)
        content = entry.content

        # Truncate long messages for display
        if len(content) > 500:
            content = content[:500] + "…"

        rprint(f"[dim]{ts}[/dim] {role_label}  {content}")


# ---------------------------------------------------------------------------
# stream  (stream-json mode — one-shot)
# ---------------------------------------------------------------------------

@app.command()
def stream(
    prompt: Annotated[str, typer.Argument(help="Prompt to send to Claude")],
    cwd: Annotated[str, typer.Option("--cwd", "-d", help="Working directory")] = ".",
    command: Annotated[str, typer.Option("--cmd", help="Claude CLI executable")] = "claude",
    tools: Annotated[Optional[str], typer.Option("--tools", "-t", help="Comma-separated allowed tools")] = None,
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="Model name")] = None,
    timeout: Annotated[float, typer.Option(help="Max seconds to wait")] = 300.0,
    raw: Annotated[bool, typer.Option("--raw", help="Print raw JSON events")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose output")] = True,
) -> None:
    """
    One-shot query using stream-json mode (structured output).

    This uses ``claude -p --output-format stream-json`` for programmatic
    access without tmux.  Ideal for scripts, CI/CD, and automation.

    Example:
        ccc stream "Explain this codebase" --cwd /my/project --tools Bash,Read
    """
    from claude_cli_connector.transport_stream import StreamJsonTransport

    allowed = tools.split(",") if tools else []

    transport = StreamJsonTransport(
        _name="ccc-stream",
        _cwd=cwd,
        _command=command,
        _allowed_tools=allowed,
        _model=model or "",
        _verbose=verbose,
    )

    try:
        transport.start()
        rprint(f"[dim]Stream-JSON mode → sending prompt…[/dim]")

        if raw:
            # Raw mode: print each JSON event
            import json
            transport.send(prompt)
            transport.end_input()  # EOF → triggers claude -p processing
            for evt in transport.iter_events(timeout=timeout):
                console.print_json(json.dumps(evt.data))
                if evt.type in ("result", "eof"):
                    break
        else:
            msg = transport.send_and_collect(prompt, timeout=timeout)
            if msg.content:
                console.print(msg.content)
            else:
                rprint("[dim]No text content in response.[/dim]")

            if msg.session_id:
                rprint(f"\n[dim]session: {msg.session_id}[/dim]")
            if msg.cost_usd > 0:
                rprint(f"[dim]cost: ${msg.cost_usd:.4f}[/dim]")
    except ConnectorError as exc:
        _bail(str(exc))
    except KeyboardInterrupt:
        rprint("\n[yellow]Interrupted.[/yellow]")
    finally:
        transport.kill()


# ---------------------------------------------------------------------------
# relay  (Claude-to-Claude)
# ---------------------------------------------------------------------------

relay_app = typer.Typer(
    help="Relay two Claude instances — debate or collaborate.",
)
app.add_typer(relay_app, name="relay")


@relay_app.command()
def debate(
    topic: Annotated[str, typer.Argument(help="Topic for debate")],
    role_a: Annotated[str, typer.Option("--role-a", "-a", help="Name for first Claude")] = "Position A",
    role_b: Annotated[str, typer.Option("--role-b", "-b", help="Name for second Claude")] = "Position B",
    prompt_a: Annotated[str, typer.Option("--prompt-a", help="System prompt for A")] = "",
    prompt_b: Annotated[str, typer.Option("--prompt-b", help="System prompt for B")] = "",
    rounds: Annotated[int, typer.Option("--rounds", "-r", help="Max debate rounds")] = 5,
    timeout: Annotated[float, typer.Option("--timeout", "-t", help="Timeout per round (seconds)")] = 300.0,
    transport: Annotated[str, typer.Option("--transport", help="tmux or stream-json")] = "stream-json",
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="Model name")] = None,
    cwd: Annotated[str, typer.Option("--cwd", "-d", help="Working directory")] = ".",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose output")] = False,
) -> None:
    """
    Start a debate between two Claude instances.

    Each Claude gets a role and they alternate discussing the topic.

    Examples:

        ccc relay debate "Is AI beneficial?" --role-a Optimist --role-b Skeptic

        ccc relay debate "Python vs Rust for CLI tools" -a "Python fan" -b "Rust fan" -r 3
    """
    import asyncio
    from claude_cli_connector.relay import (
        RelayOrchestrator, RelayConfig, RelayRole, RelayMode,
    )
    from claude_cli_connector.transport_base import TransportMode
    from rich.panel import Panel

    sys_a = prompt_a or f"You are \"{role_a}\". Argue your position convincingly."
    sys_b = prompt_b or f"You are \"{role_b}\". Argue your position convincingly."

    transport_mode = TransportMode.STREAM_JSON if transport == "stream-json" else TransportMode.TMUX

    config = RelayConfig(
        mode=RelayMode.DEBATE,
        role_a=RelayRole(name=role_a, system_prompt=sys_a, model=model or ""),
        role_b=RelayRole(name=role_b, system_prompt=sys_b, model=model or ""),
        initial_topic=topic,
        max_rounds=rounds,
        round_timeout=timeout,
        transport_mode=transport_mode,
        cwd=cwd,
        verbose=verbose,
    )

    rprint(f"\n[bold]Debate:[/bold] {topic}")
    rprint(f"  [cyan]{role_a}[/cyan] vs [magenta]{role_b}[/magenta]  •  {rounds} rounds  •  {transport}\n")

    def on_turn(turn):
        style = "cyan" if turn.speaker == role_a else "magenta"
        header = f"[{style}]Round {turn.round_num} — {turn.speaker}[/{style}]"
        content = turn.content[:2000] + ("…" if len(turn.content) > 2000 else "")
        console.print(Panel(content, title=header, expand=True))
        if turn.cost_usd > 0:
            rprint(f"  [dim]cost: ${turn.cost_usd:.4f}[/dim]")

    try:
        orch = RelayOrchestrator(config)
        result = asyncio.run(orch.run(on_turn=on_turn))
        _display_relay_result(result)
    except ConnectorError as exc:
        _bail(str(exc))
    except KeyboardInterrupt:
        rprint("\n[yellow]Relay interrupted.[/yellow]")


@relay_app.command()
def collab(
    task: Annotated[str, typer.Argument(help="Task description")],
    dev: Annotated[str, typer.Option("--dev", help="Developer role name")] = "Developer",
    reviewer: Annotated[str, typer.Option("--reviewer", help="Reviewer role name")] = "Reviewer",
    dev_prompt: Annotated[str, typer.Option("--dev-prompt", help="System prompt for developer")] = "",
    reviewer_prompt: Annotated[str, typer.Option("--reviewer-prompt", help="System prompt for reviewer")] = "",
    rounds: Annotated[int, typer.Option("--rounds", "-r", help="Max iteration rounds")] = 5,
    timeout: Annotated[float, typer.Option("--timeout", "-t", help="Timeout per round (seconds)")] = 300.0,
    transport: Annotated[str, typer.Option("--transport", help="tmux or stream-json")] = "stream-json",
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="Model name")] = None,
    cwd: Annotated[str, typer.Option("--cwd", "-d", help="Working directory")] = ".",
    tools: Annotated[Optional[str], typer.Option("--tools", help="Comma-separated allowed tools")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose output")] = False,
) -> None:
    """
    Start a code collaboration relay: Developer writes, Reviewer reviews.

    They iterate until the reviewer approves (says LGTM) or max rounds.

    Examples:

        ccc relay collab "Implement an LRU cache in Python" --rounds 3

        ccc relay collab "Write a binary search tree" --dev "Coder" --reviewer "Tester"
    """
    import asyncio
    from claude_cli_connector.relay import (
        RelayOrchestrator, RelayConfig, RelayRole, RelayMode,
    )
    from claude_cli_connector.transport_base import TransportMode
    from rich.panel import Panel

    sys_dev = dev_prompt or f"You are \"{dev}\". Write clean, well-documented code."
    sys_rev = reviewer_prompt or f"You are \"{reviewer}\". Review code thoroughly. Say LGTM when satisfied."

    transport_mode = TransportMode.STREAM_JSON if transport == "stream-json" else TransportMode.TMUX
    allowed_tools = tools.split(",") if tools else []

    config = RelayConfig(
        mode=RelayMode.COLLAB,
        role_a=RelayRole(name=dev, system_prompt=sys_dev, model=model or ""),
        role_b=RelayRole(name=reviewer, system_prompt=sys_rev, model=model or ""),
        task_description=task,
        max_rounds=rounds,
        round_timeout=timeout,
        transport_mode=transport_mode,
        cwd=cwd,
        allowed_tools=allowed_tools,
        verbose=verbose,
    )

    rprint(f"\n[bold]Collab:[/bold] {task}")
    rprint(f"  [green]{dev}[/green] ↔ [yellow]{reviewer}[/yellow]  •  max {rounds} rounds  •  {transport}\n")

    def on_turn(turn):
        style = "green" if turn.speaker == dev else "yellow"
        header = f"[{style}]Iteration {turn.round_num} — {turn.speaker}[/{style}]"
        content = turn.content[:2000] + ("…" if len(turn.content) > 2000 else "")
        console.print(Panel(content, title=header, expand=True))
        if turn.cost_usd > 0:
            rprint(f"  [dim]cost: ${turn.cost_usd:.4f}[/dim]")

    try:
        orch = RelayOrchestrator(config)
        result = asyncio.run(orch.run(on_turn=on_turn))
        _display_relay_result(result)
    except ConnectorError as exc:
        _bail(str(exc))
    except KeyboardInterrupt:
        rprint("\n[yellow]Relay interrupted.[/yellow]")


def _display_relay_result(result) -> None:
    """Pretty-print relay completion summary."""
    from claude_cli_connector.relay import RelayResult

    duration = result.end_time - result.start_time
    rprint(f"\n[bold]Relay complete[/bold]")
    rprint(f"  Mode: {result.mode}")
    rprint(f"  Rounds: {result.rounds_completed}")
    rprint(f"  Status: {result.final_state}")
    rprint(f"  Duration: {duration:.1f}s")
    if result.total_cost_usd > 0:
        rprint(f"  Total cost: ${result.total_cost_usd:.4f}")
    if result.history_path:
        rprint(f"  History: [dim]{result.history_path}[/dim]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
