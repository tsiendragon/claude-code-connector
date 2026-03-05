"""
cli.py
------
Typer-based CLI entry point.  Installed as the ``ccc`` command.

Commands
--------
  ccc run    <name> [--cwd DIR] [--cmd STR]   Start a new Claude session
  ccc attach <name>                            Attach to an existing session
  ccc send   <name> <message>                  Send a message and print response
  ccc tail   <name> [--lines N]                Print last N lines of pane
  ccc ps                                       List all known sessions
  ccc kill   <name>                            Kill a session
  ccc interrupt <name>                         Send Ctrl-C to a session
"""

from __future__ import annotations

import sys
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from claude_cli_connector.exceptions import ConnectorError
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
# run
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
# send
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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
