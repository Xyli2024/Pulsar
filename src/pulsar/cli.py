"""cli.py — command-line entry point for pulsar."""

import sys
import time

import click

from .collector import collect, get_system_info
from .dashboard import run as run_dashboard
from .snapshot import print_json, print_table


@click.command()
@click.option(
    "--proc", "-p",
    multiple=True,
    metavar="NAME",
    help="Filter to processes whose name contains NAME (repeatable).",
)
@click.option(
    "--interval", "-i",
    default=1.0,
    show_default=True,
    type=float,
    help="Dashboard refresh interval in seconds.",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Take a single snapshot and exit instead of running live.",
)
@click.option(
    "--format", "fmt",
    default="table",
    show_default=True,
    type=click.Choice(["table", "json"], case_sensitive=False),
    help="Output format for --once mode.",
)
@click.option(
    "--top", "-n",
    default=5,
    show_default=True,
    type=int,
    help="Number of top processes to display.",
)
@click.option(
    "--disco",
    is_flag=True,
    default=False,
    help="Enable disco mode (random colors on every refresh).",
)
def main(proc, interval, once, fmt, top, disco):
    """pulsar — a live hardware dashboard for the terminal.

    Press  y  to trigger fireworks.
    Press  q  or Ctrl-C to quit.
    """
    # Validate interval
    if interval <= 0:
        raise click.BadParameter(
            f"must be greater than 0 (got {interval})",
            param_hint="--interval",
        )

    # Validate top
    if top < 1:
        raise click.BadParameter(
            f"must be at least 1 (got {top})",
            param_hint="--top",
        )

    proc_filter = list(proc) if proc else None
    info = get_system_info()

    if once:
        # Single snapshot mode
        collect(top_n=top, proc_filter=proc_filter)   # prime disk I/O
        time.sleep(min(interval, 0.5))
        snap = collect(top_n=top, proc_filter=proc_filter)

        if proc_filter and not snap.top_procs:
            click.echo(
                f"Warning: no running processes matched: {', '.join(proc_filter)}",
                err=True,
            )

        if fmt == "json":
            print_json(info, snap)
        else:
            print_table(info, snap)
        return

    # Live dashboard mode
    try:
        run_dashboard(
            info=info,
            interval=interval,
            top_n=top,
            proc_filter=proc_filter,
            disco=disco,
        )
    except KeyboardInterrupt:
        click.echo("\n[dim]pulsar signing off. stay curious.[/dim]")
        sys.exit(0)
