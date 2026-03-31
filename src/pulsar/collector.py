"""collector.py — hardware data collection via psutil."""

import platform
import subprocess
import time
from dataclasses import dataclass, field

import psutil


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class SystemInfo:
    """Static hardware identity, read once at startup."""
    cpu_model: str
    cpu_cores: int
    cpu_freq_current: float   # GHz
    cpu_freq_max: float       # GHz
    gpu_model: str
    ram_total: float          # GB


@dataclass
class Snapshot:
    """One point-in-time reading of all live metrics."""
    cpu_per_core: list[float]          # percent, one per logical core
    cpu_avg: float                     # percent
    mem_used: float                    # GB
    mem_total: float                   # GB
    mem_percent: float                 # percent
    disk_read_mbps: float
    disk_write_mbps: float
    top_procs: list[dict]              # list of {pid, name, cpu, mem_mb}
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Static hardware info
# ---------------------------------------------------------------------------

def _get_cpu_model() -> str:
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=2
            )
            name = result.stdout.strip()
            if name:
                return name
        elif system == "Linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "Unknown CPU"


def _get_gpu_model() -> str:
    try:
        result = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if "Chipset Model" in line or "Chip Model" in line:
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "unavailable on this platform"


def get_system_info() -> SystemInfo:
    """Read static hardware identity. Call once at startup."""
    freq = psutil.cpu_freq()
    current_ghz = round(freq.current / 1000, 2) if freq else 0.0
    max_ghz = round(freq.max / 1000, 2) if freq and freq.max else current_ghz

    return SystemInfo(
        cpu_model=_get_cpu_model(),
        cpu_cores=psutil.cpu_count(logical=True),
        cpu_freq_current=current_ghz,
        cpu_freq_max=max_ghz,
        gpu_model=_get_gpu_model(),
        ram_total=round(psutil.virtual_memory().total / 1e9, 1),
    )


# ---------------------------------------------------------------------------
# Live snapshot
# ---------------------------------------------------------------------------

# Module-level state for disk I/O delta calculation
_last_disk_io = None
_last_disk_time = None


def collect(top_n: int = 5, proc_filter: list[str] | None = None) -> Snapshot:
    """Collect one live snapshot of all hardware metrics."""
    global _last_disk_io, _last_disk_time

    # CPU — interval=None for non-blocking read;
    # first call after startup may return 0.0, which is expected
    cpu_per_core = psutil.cpu_percent(percpu=True, interval=None)
    cpu_avg = round(sum(cpu_per_core) / len(cpu_per_core), 1)

    # Memory
    mem = psutil.virtual_memory()
    mem_used = round(mem.used / 1e9, 2)
    mem_total = round(mem.total / 1e9, 1)

    # Disk I/O — compute MB/s delta between consecutive calls
    now = time.monotonic()
    disk_io = psutil.disk_io_counters()
    if _last_disk_io is not None and disk_io is not None:
        dt = now - _last_disk_time
        read_mbps = round((disk_io.read_bytes - _last_disk_io.read_bytes) / 1e6 / dt, 2)
        write_mbps = round((disk_io.write_bytes - _last_disk_io.write_bytes) / 1e6 / dt, 2)
        read_mbps = max(read_mbps, 0.0)
        write_mbps = max(write_mbps, 0.0)
    else:
        read_mbps = 0.0
        write_mbps = 0.0
    _last_disk_io = disk_io
    _last_disk_time = now

    # Top processes
    procs = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            name = info["name"] or ""
            if proc_filter and not any(f.lower() in name.lower() for f in proc_filter):
                continue
            mem_mb = round(info["memory_info"].rss / 1e6, 1) if info["memory_info"] else 0.0
            procs.append({
                "pid": info["pid"],
                "name": name,
                "cpu": info["cpu_percent"] or 0.0,
                "mem_mb": mem_mb,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    procs.sort(key=lambda p: p["cpu"], reverse=True)

    return Snapshot(
        cpu_per_core=[round(p, 1) for p in cpu_per_core],
        cpu_avg=cpu_avg,
        mem_used=mem_used,
        mem_total=mem_total,
        mem_percent=round(mem.percent, 1),
        disk_read_mbps=read_mbps,
        disk_write_mbps=write_mbps,
        top_procs=procs[:top_n],
    )
