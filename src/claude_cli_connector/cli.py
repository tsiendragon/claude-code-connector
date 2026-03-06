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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
