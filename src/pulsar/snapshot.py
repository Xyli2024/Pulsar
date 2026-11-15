"""snapshot.py — one-shot output mode for --once flag."""

import json

from rich.console import Console

from .collector import Snapshot, SystemInfo
from .dashboard import _build_hardware, _build_header, _build_procs


def print_table(info: SystemInfo, snap: Snapshot) -> None:
    """Print a static snapshot (same panels as the live dashboard) and exit."""
    console = Console()
    # interval=0 → header shows a single snapshot; no history is available.
    console.print(_build_header(info, interval=0, elapsed=0.0))
    console.print(_build_hardware(snap, history=None))
    console.print(_build_procs(snap, milestone_msg=None, disco=False))


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
            "per_core_freq_ghz": snap.cpu_freq_per_core,
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
        "net_io": {
            "recv_mbps": snap.net_recv_mbps,
            "sent_mbps": snap.net_sent_mbps,
        },
        "top_processes": snap.top_procs,
    }
    print(json.dumps(data, indent=2))
