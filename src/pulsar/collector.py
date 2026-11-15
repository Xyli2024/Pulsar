"""collector.py — hardware data collection."""

import platform
import subprocess
import time
from dataclasses import dataclass, field

import psutil

from .ioreport import read_component_power, read_cpu_frequencies
from .power import read_power, read_registry_property, read_smc_values


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class SystemInfo:
    """Static hardware identity, read once at startup."""
    cpu_model: str
    cpu_cores: int
    cpu_freq_current: float | None   # GHz
    cpu_freq_max: float | None       # GHz
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
    net_recv_mbps: float               # MB/s received across all interfaces
    net_sent_mbps: float               # MB/s sent across all interfaces
    cpu_freq_per_core: list[float | None]  # active frequency (GHz) by logical core
    top_procs: list[dict]              # list of {pid, name, cpu, mem_mb}
    timestamp: float = field(default_factory=time.time)
    adapter_w: float | None = None
    battery_w: float | None = None      # positive when charging, negative when discharging
    system_w: float | None = None
    gpu_percent: float | None = None
    temperatures_c: dict[str, float] = field(default_factory=dict)
    fans_rpm: dict[int, float] = field(default_factory=dict)
    battery_percent: float | None = None
    battery_status: str | None = None
    cpu_w: float | None = None
    gpu_w: float | None = None


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
    try:
        freq = psutil.cpu_freq()
    except (AttributeError, OSError, RuntimeError):
        freq = None
    current_ghz = max_ghz = None
    if freq is not None:
        # On macOS psutil reports a nominal frequency as its current value.
        if not psutil.MACOS and freq.current is not None and freq.current > 0:
            current_ghz = round(freq.current / 1000, 2)
        if freq.max is not None and freq.max > 0:
            max_ghz = round(freq.max / 1000, 2)

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

# Module-level state for I/O delta calculations
_last_disk_io = None
_last_disk_time = None
_last_net_io = None
_last_net_time = None


def collect(top_n: int = 5, proc_filter: list[str] | None = None) -> Snapshot:
    """Collect one live snapshot of all hardware metrics."""
    global _last_disk_io, _last_disk_time, _last_net_io, _last_net_time

    # CPU — interval=None for non-blocking read;
    # first call after startup may return 0.0, which is expected
    cpu_per_core = psutil.cpu_percent(percpu=True, interval=None)
    cpu_avg = round(sum(cpu_per_core) / len(cpu_per_core), 1)

    # Only show per-core frequencies when they map to the CPU usage rows.
    cpu_freq_per_core = []
    if psutil.MACOS:
        cpu_freq_per_core = read_cpu_frequencies(len(cpu_per_core))
    else:
        try:
            raw_freqs = psutil.cpu_freq(percpu=True)
        except (AttributeError, OSError, RuntimeError):
            raw_freqs = []
        if (raw_freqs and len(raw_freqs) == len(cpu_per_core)
                and all(f.current is not None and f.current > 0 for f in raw_freqs)):
            cpu_freq_per_core = [round(f.current / 1000, 2) for f in raw_freqs]

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

    # Network I/O — aggregate across all interfaces
    net_io = psutil.net_io_counters()
    if _last_net_io is not None and net_io is not None:
        dt_net = now - _last_net_time
        recv_mbps = round((net_io.bytes_recv - _last_net_io.bytes_recv) / 1e6 / dt_net, 2)
        sent_mbps = round((net_io.bytes_sent - _last_net_io.bytes_sent) / 1e6 / dt_net, 2)
        recv_mbps = max(recv_mbps, 0.0)
        sent_mbps = max(sent_mbps, 0.0)
    else:
        recv_mbps = 0.0
        sent_mbps = 0.0
    _last_net_io = net_io
    _last_net_time = now

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
    smc = read_smc_values(b"Tp01", b"Tp05", b"Tg05", b"TB0T", b"F0Ac", b"F1Ac")
    temperatures_c = {}
    for label, keys in (("CPU", (b"Tp01", b"Tp05")), ("GPU", (b"Tg05",)), ("Bat", (b"TB0T",))):
        readings = [smc.get(key) for key in keys]
        valid = [value for value in readings if value is not None and 0 < value < 130]
        if valid:
            temperatures_c[label] = max(valid)
    fans_rpm = {}
    for index, key in enumerate((b"F0Ac", b"F1Ac"), start=1):
        value = smc.get(key)
        if value is not None and value >= 0:
            fans_rpm[index] = value

    gpu_stats = read_registry_property(b"IOAccelerator", b"PerformanceStatistics")
    gpu_percent = gpu_stats.get("Device Utilization %") if isinstance(gpu_stats, dict) else None
    if gpu_percent is not None and (type(gpu_percent) not in (int, float) or not 0 <= gpu_percent <= 100):
        gpu_percent = None

    try:
        battery = psutil.sensors_battery()
    except (AttributeError, OSError, RuntimeError):
        battery = None
    plugged = battery.power_plugged if battery is not None else None
    battery_status = None
    if plugged is False:
        battery_status = "On battery"
    elif plugged is True:
        charging = read_registry_property(b"AppleSmartBattery", b"IsCharging")
        if charging is True:
            battery_status = "Charging"
        elif charging is False:
            battery_status = "Not charging"
        else:
            battery_status = "Plugged in"
    adapter_w, battery_w, system_w = read_power(smc, plugged=plugged)
    cpu_w, gpu_w = read_component_power()

    return Snapshot(
        cpu_per_core=[round(p, 1) for p in cpu_per_core],
        cpu_avg=cpu_avg,
        mem_used=mem_used,
        mem_total=mem_total,
        mem_percent=round(mem.percent, 1),
        disk_read_mbps=read_mbps,
        disk_write_mbps=write_mbps,
        net_recv_mbps=recv_mbps,
        net_sent_mbps=sent_mbps,
        cpu_freq_per_core=cpu_freq_per_core,
        top_procs=procs[:top_n],
        adapter_w=adapter_w,
        battery_w=battery_w,
        system_w=system_w,
        gpu_percent=gpu_percent,
        temperatures_c=temperatures_c,
        fans_rpm=fans_rpm,
        battery_percent=battery.percent if battery is not None and 0 <= battery.percent <= 100 else None,
        battery_status=battery_status,
        cpu_w=cpu_w,
        gpu_w=gpu_w,
    )
