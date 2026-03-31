"""dashboard.py — live TUI dashboard using rich."""

import math
import queue
import random
import select
import shutil
import sys
import threading
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .collector import SystemInfo, Snapshot, collect

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MILESTONES = [
    (5 * 60,  "5 minutes in. You're basically a sysadmin now."),
    (15 * 60, "Still here? Your code is not going to optimize itself."),
    (30 * 60, "30 minutes. Have you tried turning it off and on again?"),
    (42 * 60, "42 minutes. The answer, apparently, is not in this dashboard."),
    (60 * 60, "1 hour. At this point, you live here. We've prepared a small room."),
]

QUOTES = [
    "It's not a bug — it's an undocumented feature.",
    "The cloud is just someone else's computer.",
    "Real programmers count from 0.",
    "Debugging is like being a detective in a crime movie where you are also the murderer.",
    "It's always DNS.",
    "To understand recursion, you must first understand recursion.",
    "There's no place like 127.0.0.1.",
    "Works on my machine. Ship the machine.",
    "sudo make me a sandwich.",
    "99 little bugs in the code. Fix one, compile again. 127 little bugs in the code.",
    "In theory, theory and practice are the same. In practice, they're not.",
    "The best code is no code at all.",
    "A computer lets you make more mistakes faster than any invention — except maybe tequila.",
    "If at first you don't succeed, call it version 1.0.",
    "Weeks of coding can save you hours of planning.",
]

DISCO_COLORS = [
    "red", "green", "yellow", "blue", "magenta", "cyan",
    "bright_red", "bright_green", "bright_yellow",
    "bright_blue", "bright_magenta", "bright_cyan",
]

# Fireworks animation
_FW_TOTAL_FRAMES   = 25    # frames per show
_FW_FRAME_DURATION = 0.08  # seconds per frame  (~2 s total)
_FW_BRIGHT = ["✦", "★", "✸", "✺", "*", "+"]
_FW_DIM    = ["·", "°", ".", "｡", "˙"]

# Raw ANSI codes for the sparkle overlay (bypasses Rich, written directly to
# the terminal file so the dashboard underneath is left intact)
_ANSI_FG = {
    "red":            "\033[31m",  "green":          "\033[32m",
    "yellow":         "\033[33m",  "blue":           "\033[34m",
    "magenta":        "\033[35m",  "cyan":           "\033[36m",
    "bright_red":     "\033[91m",  "bright_green":   "\033[92m",
    "bright_yellow":  "\033[93m",  "bright_blue":    "\033[94m",
    "bright_magenta": "\033[95m",  "bright_cyan":    "\033[96m",
}
_ANSI_BOLD = "\033[1m"
_ANSI_DIM  = "\033[2m"
_ANSI_RST  = "\033[0m"


# ---------------------------------------------------------------------------
# Keyboard — background thread
# ---------------------------------------------------------------------------

def _start_keyboard_thread(key_queue: queue.Queue, stop_event: threading.Event) -> None:
    if not sys.stdin.isatty():
        return

    def _worker():
        try:
            import termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                # setcbreak: immediate keystrokes, OPOST preserved, ISIG preserved
                tty.setcbreak(fd)
                while not stop_event.is_set():
                    if select.select([sys.stdin], [], [], 0.05)[0]:
                        ch = sys.stdin.read(1)
                        if ch == "\x1b":
                            while select.select([sys.stdin], [], [], 0.02)[0]:
                                sys.stdin.read(1)
                            continue
                        key_queue.put(ch)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(int(pct / 100 * width), width))
    return "█" * filled + "░" * (width - filled)


def _color(pct: float, disco: bool = False) -> str:
    if disco:
        return random.choice(DISCO_COLORS)
    return "green" if pct < 50 else "yellow" if pct < 80 else "red"


def _mem_color(pct: float, disco: bool = False) -> str:
    if disco:
        return random.choice(DISCO_COLORS)
    return "green" if pct < 70 else "yellow" if pct < 90 else "red"


# ---------------------------------------------------------------------------
# Fireworks animation
# ---------------------------------------------------------------------------

def _make_burst_centers(cols: int, rows: int, count: int = 3) -> list[tuple]:
    """Pick random burst origins, each with its own color."""
    return [
        (
            random.randint(cols // 6, 5 * cols // 6),
            random.randint(rows // 6, 2 * rows // 3),
            random.choice(DISCO_COLORS),
        )
        for _ in range(count)
    ]


def _draw_sparkle_overlay(
    out, frame: int, cols: int, rows: int, bursts: list[tuple]
) -> None:
    """
    Draw one fireworks frame as an ANSI-positioned overlay on top of whatever
    Rich has already rendered.  The dashboard underneath is untouched; the
    next live.update() wipes the overlay automatically.

    Glow effect: each leading-edge spark gets bold+bright colour; its four
    cardinal neighbours receive a dim halo in the same colour.
    """
    progress   = frame / _FW_TOTAL_FRAMES
    max_radius = min(cols * 0.45, rows * 0.85)
    cur_radius = progress * max_radius

    # (col, row) → (char, ansi_prefix)
    grid: dict[tuple[int, int], tuple[str, str]] = {}

    for cx, cy, color in bursts:
        fg = _ANSI_FG.get(color, "")

        # Leading edge — bold bright chars
        for deg in range(0, 360, 4):
            rad = math.radians(deg)
            x = int(cx + cur_radius * math.cos(rad))
            y = int(cy + cur_radius * math.sin(rad) * 0.5)
            if 1 <= x < cols - 1 and 1 <= y < rows - 1 and random.random() < 0.75:
                grid[(x, y)] = (random.choice(_FW_BRIGHT), _ANSI_BOLD + fg)
                # Glow halo — dim neighbours
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    hx, hy = x + dx, y + dy
                    if (1 <= hx < cols - 1 and 1 <= hy < rows - 1
                            and (hx, hy) not in grid
                            and random.random() < 0.6):
                        grid[(hx, hy)] = (random.choice(_FW_DIM), fg)

        # Trailing sparks — dimmer inner rings
        for trail in range(1, 5):
            r = cur_radius - trail * 2.5
            if r <= 0:
                continue
            for deg in range(0, 360, 8):
                rad = math.radians(deg)
                x = int(cx + r * math.cos(rad))
                y = int(cy + r * math.sin(rad) * 0.5)
                if (1 <= x < cols - 1 and 1 <= y < rows - 1
                        and (x, y) not in grid
                        and random.random() < 0.40):
                    grid[(x, y)] = (random.choice(_FW_DIM), _ANSI_DIM + fg)

    # Fade: randomly drop sparks in the last 40 % of the animation
    if progress > 0.6:
        fade = (progress - 0.6) / 0.4
        grid = {k: v for k, v in grid.items() if random.random() > fade}

    # Write to terminal using ANSI cursor-addressing
    buf = ["\033[s"]  # save cursor position
    for (col, row), (char, prefix) in grid.items():
        buf.append(f"\033[{row + 1};{col + 1}H{prefix}{char}{_ANSI_RST}")
    buf.append("\033[u")  # restore cursor position
    out.write("".join(buf))
    out.flush()


# ---------------------------------------------------------------------------
# Dashboard panel builders
# ---------------------------------------------------------------------------

def _build_header(info: SystemInfo, interval: float, disco: bool = False) -> Panel:
    c = random.choice(DISCO_COLORS) if disco else "cyan"
    t = Text()
    t.append("pulsar", style=f"bold {c}")
    t.append(
        f"  •  {info.cpu_model}  •  {info.cpu_cores} cores"
        f"  •  {info.cpu_freq_current} / {info.cpu_freq_max} GHz max\n",
        style="white",
    )
    t.append(
        f"GPU: {info.gpu_model}  •  RAM: {info.ram_total} GB"
        f"  •  refresh: {interval}s"
        f"  •  [dim]y[/dim] fireworks  •  [dim]q[/dim] quit",
        style="dim",
    )
    return Panel(t)


def _build_cpu_text(snap: Snapshot, disco: bool = False) -> Text:
    t = Text()
    n = len(snap.cpu_per_core)

    if n <= 8:
        for i, pct in enumerate(snap.cpu_per_core):
            c = _color(pct, disco)
            t.append(f"Core {i}  ", style="dim")
            t.append(_bar(pct), style=c)
            t.append(f"  {pct:5.1f}%\n", style=c)
        t.append("─" * 34 + "\n", style="dim")
        c = _color(snap.cpu_avg, disco)
        t.append("avg     ", style="dim")
        t.append(_bar(snap.cpu_avg), style=c)
        t.append(f"  {snap.cpu_avg:5.1f}%", style=f"bold {c}")
    else:
        c = _color(snap.cpu_avg, disco)
        t.append(f"avg {snap.cpu_avg:.1f}%  ", style=f"bold {c}")
        t.append(_bar(snap.cpu_avg) + "\n\n", style=c)
        for row_start in range(0, n, 4):
            row = snap.cpu_per_core[row_start: row_start + 4]
            for j in range(len(row)):
                t.append(f"  {row_start + j:>3}  ", style="dim")
            t.append("\n")
            for pct in row:
                t.append(f" {pct:5.1f}%", style=_color(pct, disco))
            t.append("\n\n")
    return t


def _build_stats_text(snap: Snapshot, disco: bool = False) -> Text:
    t = Text()
    mc = _mem_color(snap.mem_percent, disco)
    t.append("Memory\n", style="bold")
    t.append(f"  {snap.mem_used} / {snap.mem_total} GB  ({snap.mem_percent:.1f}%)\n", style=mc)
    t.append("  " + _bar(snap.mem_percent) + "\n\n", style=mc)
    dc = random.choice(DISCO_COLORS) if disco else "white"
    t.append("Disk I/O\n", style="bold")
    t.append(f"  Read   {snap.disk_read_mbps:7.2f} MB/s\n", style=dc)
    t.append(f"  Write  {snap.disk_write_mbps:7.2f} MB/s", style=dc)
    return t


def _build_renderable(info: SystemInfo, snap: Snapshot, interval: float,
                      milestone_msg: str | None = None,
                      disco: bool = False) -> Group:
    """Compose the full dashboard as a Rich renderable Group."""
    header = _build_header(info, interval, disco)

    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(ratio=2)
    grid.add_row(
        Panel(_build_cpu_text(snap, disco),   title="CPU",    border_style="dim"),
        Panel(_build_stats_text(snap, disco), title="System", border_style="dim"),
    )

    proc_table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    proc_table.add_column("PID",  justify="right", width=7)
    proc_table.add_column("Name", width=26)
    proc_table.add_column("CPU%", justify="right", width=6)
    proc_table.add_column("MEM",  justify="right", width=10)
    for p in snap.top_procs:
        c = _color(p["cpu"], disco)
        mem_str = (f"{p['mem_mb']} MB" if p["mem_mb"] < 1024
                   else f"{p['mem_mb'] / 1024:.2f} GB")
        proc_table.add_row(
            str(p["pid"]), p["name"],
            f"[{c}]{p['cpu']}%[/{c}]", mem_str,
        )

    footer = Text(f"\n  ★  {milestone_msg}", style="bold yellow") if milestone_msg else Text()
    procs = Panel(Group(proc_table, footer), title="Top Processes", border_style="dim")

    return Group(header, grid, procs)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(info: SystemInfo, interval: float = 1.0, top_n: int = 5,
        proc_filter: list[str] | None = None, disco: bool = False) -> None:
    """Run the live dashboard until the user quits."""

    console = Console()

    if not sys.stdout.isatty():
        console.print(
            "[bold red]Error:[/bold red] the live dashboard requires a real terminal.\n"
            "Use [bold]pulsar --once[/bold] for a single snapshot instead.",
            highlight=False,
        )
        return

    start_time          = time.monotonic()
    core_overload_since: dict[int, float] = {}
    seen_milestones: set = set()
    milestone_msg: str | None = None
    milestone_until     = 0.0

    # Fireworks state — fw_frame >= _FW_TOTAL_FRAMES means "not playing"
    fw_frame  = _FW_TOTAL_FRAMES
    fw_bursts: list[tuple] = []

    key_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    _start_keyboard_thread(key_queue, stop_event)

    collect(top_n=top_n, proc_filter=proc_filter)
    time.sleep(min(interval, 0.3))
    snap = collect(top_n=top_n, proc_filter=proc_filter)  # initial reading

    with Live(console=console, screen=True, auto_refresh=False) as live:
        try:
            while True:
                now     = time.monotonic()
                elapsed = now - start_time

                # --- Keyboard ---
                quit_requested = False
                while True:
                    try:
                        key = key_queue.get_nowait()
                    except queue.Empty:
                        break
                    if key in ("q", "Q", "\x03"):
                        quit_requested = True
                    elif key in ("y", "Y") and fw_frame >= _FW_TOTAL_FRAMES:
                        cols, rows = shutil.get_terminal_size((80, 24))
                        count      = random.randint(2, 6)
                        fw_bursts  = _make_burst_centers(cols, rows - 1, count)
                        fw_frame   = 0
                if quit_requested:
                    break

                # --- Fireworks frame (overlay — dashboard stays visible) ---
                if fw_frame < _FW_TOTAL_FRAMES:
                    cols, rows = shutil.get_terminal_size((80, 24))
                    # Refresh the dashboard first, then paint sparkles on top
                    live.update(
                        _build_renderable(info, snap, interval,
                                          milestone_msg=milestone_msg,
                                          disco=disco),
                        refresh=True,
                    )
                    _draw_sparkle_overlay(
                        live.console.file, fw_frame, cols, rows, fw_bursts
                    )
                    fw_frame += 1
                    time.sleep(_FW_FRAME_DURATION)
                    continue

                # --- Normal dashboard ---

                snap = collect(top_n=top_n, proc_filter=proc_filter)

                # Auto-trigger fireworks when a core pegs 100 % for 5+ seconds
                for i, pct in enumerate(snap.cpu_per_core):
                    if pct >= 99.9:
                        core_overload_since.setdefault(i, now)
                        if (now - core_overload_since[i] >= 5.0
                                and fw_frame >= _FW_TOTAL_FRAMES):
                            cols, rows = shutil.get_terminal_size((80, 24))
                            fw_bursts  = _make_burst_centers(cols, rows - 1,
                                             random.randint(2, 6))
                            fw_frame   = 0
                    else:
                        core_overload_since.pop(i, None)

                # Milestones
                if now >= milestone_until:
                    milestone_msg = None
                for threshold, msg in MILESTONES:
                    if threshold not in seen_milestones and elapsed >= threshold:
                        seen_milestones.add(threshold)
                        milestone_msg = msg
                        milestone_until = now + 5.0

                if proc_filter and not snap.top_procs and "warned" not in seen_milestones:
                    seen_milestones.add("warned")
                    milestone_msg = f"No processes matched: {', '.join(proc_filter)}"
                    milestone_until = now + 5.0

                live.update(
                    _build_renderable(info, snap, interval,
                                      milestone_msg=milestone_msg, disco=disco),
                    refresh=True,
                )
                time.sleep(interval)

        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()

    console.print("[dim]pulsar signing off. stay curious.[/dim]")
