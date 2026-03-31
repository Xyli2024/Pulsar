"""snapshot.py — one-shot output mode for --once flag."""

import json
import time

from rich.console import Console
from rich.table import Table

from .collector import Snapshot, SystemInfo


def print_table(info: SystemInfo, snap: Snapshot) -> None:
    """Print a static rich table to stdout and exit."""
    console = Console()

    # Header
    console.print(
        f"[bold cyan]pulsar[/bold cyan]  "
        f"{info.cpu_model}  •  {info.cpu_cores} cores  •  "
        f"{info.cpu_freq_current} GHz"
    )
    console.print(
        f"GPU: {info.gpu_model}  •  RAM: {info.ram_total} GB"
    )
    console.print()

    # CPU table
    cpu_table = Table(title="CPU per Core", show_header=True)
    cpu_table.add_column("Core", style="dim", width=6)
    cpu_table.add_column("Load", width=30)
    cpu_table.add_column("  %", justify="right", width=6)

    for i, pct in enumerate(snap.cpu_per_core):
        bar = _bar(pct)
        color = _color(pct)
        cpu_table.add_row(
            str(i),
            f"[{color}]{bar}[/{color}]",
            f"[{color}]{pct}%[/{color}]",
        )
    cpu_table.add_row("avg", _bar(snap.cpu_avg), f"{snap.cpu_avg}%", style="bold")
    console.print(cpu_table)

    # Memory + Disk
    console.print(
        f"[bold]Memory:[/bold]  {snap.mem_used} / {snap.mem_total} GB  "
        f"({snap.mem_percent}%)  "
        f"[{'green' if snap.mem_percent < 70 else 'yellow' if snap.mem_percent < 90 else 'red'}]"
        f"{_bar(snap.mem_percent)}[/]"
    )
    console.print(
        f"[bold]Disk I/O:[/bold]  "
        f"Read {snap.disk_read_mbps} MB/s  •  Write {snap.disk_write_mbps} MB/s"
    )
    console.print()

    # Processes table
    proc_table = Table(title=f"Top {len(snap.top_procs)} Processes", show_header=True)
    proc_table.add_column("PID", justify="right", width=7)
    proc_table.add_column("Name", width=24)
    proc_table.add_column("CPU%", justify="right", width=6)
    proc_table.add_column("MEM", justify="right", width=10)

    for p in snap.top_procs:
        mem_str = f"{p['mem_mb']} MB" if p["mem_mb"] < 1024 else f"{round(p['mem_mb']/1024, 2)} GB"
        proc_table.add_row(
            str(p["pid"]),
            p["name"],
            f"{p['cpu']}%",
            mem_str,
        )
    console.print(proc_table)


def print_json(info: SystemInfo, snap: Snapshot) -> None:
    """Print a plain JSON snapshot to stdout (no color codes)."""
    data = {
        "timestamp": snap.timestamp,
        "system": {
            "cpu_model": info.cpu_model,
            "cpu_cores": info.cpu_cores,
            "cpu_freq_ghz": info.cpu_freq_current,
            "gpu_model": info.gpu_model,
            "ram_total_gb": info.ram_total,
        },
        "cpu": {
            "per_core_percent": snap.cpu_per_core,
            "avg_percent": snap.cpu_avg,
        },
        "memory": {
            "used_gb": snap.mem_used,
            "total_gb": snap.mem_total,
            "percent": snap.mem_percent,
        },
        "disk_io": {
            "read_mbps": snap.disk_read_mbps,
            "write_mbps": snap.disk_write_mbps,
        },
        "top_processes": snap.top_procs,
    }
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _color(pct: float) -> str:
    if pct < 50:
        return "green"
    elif pct < 80:
        return "yellow"
    return "red"
