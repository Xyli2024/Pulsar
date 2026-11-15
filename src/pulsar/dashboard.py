"""dashboard.py — live TUI dashboard using rich."""

import math
import os
import queue
import random
import select
import shutil
import signal
import sys
import threading
import time
from collections import deque

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.control import Control
from rich.live import Live
from rich.panel import Panel
from rich.screen import Screen
from rich.table import Table
from rich.text import Text

from .collector import SystemInfo, Snapshot, collect
from . import facts as _facts
from . import theme as th
from .theme import DISCO_COLORS

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

# Fact bar animation
_TYPING_SECS    = 20.0   # target duration to type full body text
_STABLE_SECS    = 10.0   # static display time after typing completes
_FADE_PALETTE   = [th.TEXT,"grey74", "grey54", "grey39", "grey27", "grey15", "grey7"]
_FADE_STEP_SECS = 0.125  # seconds per fade step  (7 × 0.125 ≈ 875 ms total)
_LOOP_TICK      = 0.10   # main loop tick; collect() is gated separately by interval
_MIN_CHARS_SEC  = 9.0    # slowest allowed typing speed (chars / sec)
_MAX_CHARS_SEC  = 15.0   # fastest allowed typing speed (chars / sec)


def _fact_char_interval(text: str) -> float:
    """Seconds per character so that len(text) chars type in ~_TYPING_SECS."""
    chars_per_sec = max(_MIN_CHARS_SEC, min(_MAX_CHARS_SEC, len(text) / _TYPING_SECS))
    return 1.0 / chars_per_sec


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
                        # os.read bypasses Python's TextIOWrapper buffer so that
                        # the full escape sequence (e.g. \x1b[A) is captured in
                        # one shot — reading via sys.stdin.read(1) would drain
                        # only the ESC byte while leaving [A in Python's buffer,
                        # causing select() to see no data and drop the sequence.
                        data = os.read(fd, 32)
                        i = 0
                        while i < len(data):
                            b = data[i]
                            if b == 0x1b:  # ESC — look ahead for CSI sequence
                                if i + 2 < len(data) and data[i + 1] == ord("["):
                                    if data[i + 2] == ord("A"):
                                        key_queue.put("UP")
                                        i += 3
                                        continue
                                    elif data[i + 2] == ord("B"):
                                        key_queue.put("DOWN")
                                        i += 3
                                        continue
                                key_queue.put("ESC")  # lone ESC — cancel kill mode
                                i += 1
                            else:
                                key_queue.put(chr(b))
                                i += 1
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

class _History:
    """Rolling per-metric history used for the sparklines."""

    def __init__(self, maxlen: int = 240) -> None:
        self.cpu      = deque(maxlen=maxlen)
        self.mem      = deque(maxlen=maxlen)
        self.net_down = deque(maxlen=maxlen)
        self.net_up   = deque(maxlen=maxlen)
        self.disk_r   = deque(maxlen=maxlen)
        self.disk_w   = deque(maxlen=maxlen)

    def push(self, snap: Snapshot) -> None:
        self.cpu.append(snap.cpu_avg)
        self.mem.append(snap.mem_percent)
        self.net_down.append(snap.net_recv_mbps)
        self.net_up.append(snap.net_sent_mbps)
        self.disk_r.append(snap.disk_read_mbps)
        self.disk_w.append(snap.disk_write_mbps)


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

def _panel(body, title: str, icon: str, *, subtitle: str | None = None,
           accent: str = th.ACCENT, border: str = th.BORDER,
           disco: bool = False) -> Panel:
    """Uniform rounded panel with an icon + uppercase title on the top-left."""
    head = Text()
    head.append(f" {icon} ", style=f"bold {th.pick(accent, disco)}")
    head.append(f"{title.upper()} ", style=f"bold {th.TEXT}")
    sub = Text(f" {subtitle} ", style=th.MUTED) if subtitle else None
    return Panel(
        body,
        title=head, title_align="left",
        subtitle=sub, subtitle_align="right",
        box=th.PANEL_BOX,
        border_style=th.pick(border, disco),
        padding=(0, 1),
    )


def _build_header(info: SystemInfo, interval: float, elapsed: float = 0.0,
                  disco: bool = False) -> Panel:
    accent = th.pick(th.ACCENT, disco)

    # Row 1 — logo on the left, session status on the right
    logo = Text()
    logo.append("◉" if int(elapsed) % 2 == 0 else "◎", style=f"bold {accent}")
    logo.append(" P U L S A R", style=f"bold {th.TEXT}")
    logo.append("   live hardware monitor", style=th.MUTED)

    status = Text(justify="right")
    if interval > 0:
        th.kv(status, "session ", th.fmt_duration(elapsed))
        status.append("   ·   ", style=th.FAINT)
        th.kv(status, "every ", f"{interval:g}s")
    else:  # one-shot (--once) output
        status.append("snapshot", style=th.MUTED)
    status.append("   ·   ", style=th.FAINT)
    status.append(time.strftime("%H:%M:%S"), style=f"bold {accent}")

    # Row 2 — static machine identity
    ident = Text(overflow="ellipsis")
    th.kv(ident, "CPU ", info.cpu_model)
    ident.append(f" · {info.cpu_cores} threads", style=th.MUTED)
    freq = th.freq_text(info.cpu_freq_current, info.cpu_freq_max)
    if freq:
        ident.append(f" · {freq}", style=th.MUTED)
    ident.append("    ")
    th.kv(ident, "GPU ", info.gpu_model)
    ident.append("    ")
    th.kv(ident, "RAM ", f"{info.ram_total} GB")

    grid = Table.grid(expand=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)
    grid.add_row(logo, status)
    body = Group(grid, ident)
    return Panel(body, box=th.PANEL_BOX, border_style=th.pick(th.BORDER, disco),
                 padding=(0, 1))


class _CpuView:
    """Width-aware CPU body: average gauge, history sparkline, per-core grid."""

    _GAP = 3  # spaces between core columns

    def __init__(self, snap: Snapshot, history: "_History | None", disco: bool) -> None:
        self.snap, self.history, self.disco = snap, history, disco

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        snap, disco = self.snap, self.disco
        w = max(20, options.max_width)
        cores = snap.cpu_per_core
        n = len(cores)
        freqs = snap.cpu_freq_per_core if len(snap.cpu_freq_per_core) == n else [None] * n
        has_freq = any(f is not None for f in freqs)

        t = Text(no_wrap=True, overflow="crop")

        # Average gauge — label (5) + bar + value (7)
        avg_c = th.level(snap.cpu_avg, disco=disco)
        t.append("AVG  ", style=f"bold {th.MUTED}")
        t.append_text(th.bar(snap.cpu_avg, w - 12, disco))
        t.append(f" {snap.cpu_avg:5.1f}%", style=f"bold {avg_c}")
        t.append("\n")

        # History sparkline, aligned under the bar (live mode only)
        if self.history is not None:
            t.append("     ")
            t.append_text(th.sparkline(self.history.cpu, w - 12, color=None,
                                       floor=100.0, disco=disco))
            t.append("  hist", style=th.FAINT)
            t.append("\n")
        t.append("\n")

        # Per-core grid, laid out column-major
        label_w = len(str(max(n - 1, 0))) + 2          # "C" + digits + space
        fixed = label_w + 5 + (6 if has_freq else 0)   # + " 100%" (+ " 3.20G")
        cols = 1 if n <= 8 else 2 if n <= 24 else 3
        while cols > 1 and (w - self._GAP * (cols - 1)) // cols - fixed < 6:
            cols -= 1
        cell_w = (w - self._GAP * (cols - 1)) // cols
        bar_w = max(3, cell_w - fixed)
        rows = math.ceil(n / cols) if n else 0

        for r in range(rows):
            for c in range(cols):
                i = c * rows + r
                if i >= n:
                    continue
                if c:
                    t.append(" " * self._GAP)
                pct = cores[i]
                t.append(f"C{i:<{label_w - 1}}", style=th.MUTED)
                t.append_text(th.bar(pct, bar_w, disco))
                t.append(f" {pct:3.0f}%", style=th.level(pct, disco=disco))
                if has_freq:
                    f = freqs[i]
                    t.append(f" {f:4.2f}G" if f is not None else "      ", style=th.FAINT)
            if r < rows - 1:
                t.append("\n")
        yield t


class _SystemView:
    """Width-aware system body: memory, I/O with sparklines, power, sensors."""

    _LABEL_W = 8

    def __init__(self, snap: Snapshot, history: "_History | None", disco: bool,
                 compact: bool = False) -> None:
        self.snap, self.history, self.disco = snap, history, disco
        self.compact = compact

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        snap, disco, hist = self.snap, self.disco, self.history
        w = max(20, options.max_width)
        L = self._LABEL_W
        t = Text(no_wrap=True, overflow="crop")

        def head(title: str, first: bool = False) -> None:
            if not first:
                t.append("\n" if self.compact else "\n\n")
            t.append_text(th.section(title, th.pick(th.ACCENT2, disco)))

        def append_pairs(cells: list[Text]) -> None:
            for index in range(0, len(cells), 2):
                pair = cells[index:index + 2]
                joined = Text("  ").join(pair)
                for line in ([joined] if joined.cell_len <= options.max_width else pair):
                    t.append("\n")
                    t.append_text(line)

        # --- Memory ------------------------------------------------------
        head("Memory", first=True)
        mc = th.level(snap.mem_percent, 70, 90, disco)
        capacity = f"{snap.mem_used:.2f} / {snap.mem_total} GB"
        memory_inline = self.compact and w - L - 9 - len(capacity) >= 3
        t.append(f"\n{'used':<{L}}", style=th.MUTED)
        memory_bar_width = w - L - 7 - (len(capacity) + 2 if memory_inline else 0)
        t.append_text(th.bar(snap.mem_percent, memory_bar_width, disco))
        t.append(f" {snap.mem_percent:5.1f}%", style=f"bold {mc}")
        t.append("  " if memory_inline else f"\n{'':<{L}}", style=th.MUTED)
        t.append(f"{snap.mem_used:.2f}", style=th.TEXT)
        t.append(f" / {snap.mem_total} GB", style=th.MUTED)

        # --- I/O ---------------------------------------------------------
        head("Throughput")
        rows = (
            ("net ↓",  snap.net_recv_mbps,   hist.net_down if hist else None, th.NET_DOWN),
            ("net ↑",  snap.net_sent_mbps,   hist.net_up   if hist else None, th.NET_UP),
            ("disk r", snap.disk_read_mbps,  hist.disk_r   if hist else None, th.DISK_R),
            ("disk w", snap.disk_write_mbps, hist.disk_w   if hist else None, th.DISK_W),
        )
        spark_w = w - L - 11 - 2
        compact_rates = []
        for label, value, series, color in rows:
            c = th.pick(color, disco)
            if self.compact:
                rate = Text.assemble((f"{label} ", th.MUTED), (th.fmt_rate(value), f"bold {c}"))
                compact_spark = (w - 2) // 2 - rate.cell_len - 1
                if series is not None and compact_spark >= 4:
                    rate.append(" ").append_text(th.sparkline(series, compact_spark, c, floor=0.05, disco=disco))
                compact_rates.append(rate)
                continue
            t.append(f"\n{label:<{L}}", style=th.MUTED)
            t.append(th.fmt_rate(value), style=f"bold {c}")
            if series is not None and spark_w >= 4:
                t.append("  ")
                t.append_text(th.sparkline(series, spark_w, c, floor=0.05, disco=disco))
        if self.compact:
            append_pairs(compact_rates)

        # --- Power -------------------------------------------------------
        readings = [
            ("adapter", snap.adapter_w, 1),
            ("battery", snap.battery_w, 1),
            ("system",  snap.system_w,  1),
            ("cpu",     snap.cpu_w,     2),
            ("gpu",     snap.gpu_w,     2),
        ]
        readings = [r for r in readings if r[1] is not None]
        if readings:
            head("Power")
            pc = th.pick(th.POWER, disco)
            compact_power = []
            for label, watts, decimals in readings:
                sign = "+" if label == "battery" and watts > 0 else ""
                if self.compact:
                    compact_power.append(Text.assemble(
                        (f"{label} ", th.MUTED), (f"{sign}{watts:.{decimals}f}", f"bold {pc}"), (" W", th.MUTED),
                    ))
                    continue
                t.append(f"\n{label:<{L}}", style=th.MUTED)
                t.append(f"{sign}{watts:.{decimals}f}".rjust(7), style=f"bold {pc}")
                t.append(" W", style=th.MUTED)
            if self.compact:
                append_pairs(compact_power)

        # --- Sensors -----------------------------------------------------
        has_sensors = (snap.gpu_percent is not None or snap.temperatures_c
                       or snap.fans_rpm or snap.battery_percent is not None
                       or snap.battery_status)
        if has_sensors:
            head("Sensors")
            sensor_width = max(1, options.max_width - L)

            def sensor_line(label: str, value: Text) -> None:
                for index, line in enumerate(value.wrap(console, sensor_width, overflow="fold")):
                    t.append(f"\n{label if index == 0 else '':<{L}}", style=th.MUTED)
                    t.append_text(line)

            if snap.gpu_percent is not None:
                g = snap.gpu_percent
                t.append(f"\n{'gpu':<{L}}", style=th.MUTED)
                t.append_text(th.bar(g, w - L - 7, disco))
                t.append(f" {g:5.0f}%", style=f"bold {th.level(g, disco=disco)}")
            if snap.temperatures_c:
                temperatures = snap.temperatures_c.items()
                full = "  ".join(f"{label} {value:.0f}°" for label, value in temperatures)
                compact = Text(full).cell_len > sensor_width
                parts = [Text.assemble(
                    (f"{label} ", th.MUTED),
                    (f"{value:.0f}{'' if compact else '°'}", f"bold {th.level(value, 60, 85, disco)}"),
                ) for label, value in temperatures]
                sensor_line("temp °C" if compact else "temp", Text(" " if compact else "  ").join(parts))
            if snap.fans_rpm:
                t.append(f"\n{'fans':<{L}}", style=th.MUTED)
                for j, (index, rpm) in enumerate(snap.fans_rpm.items()):
                    if j:
                        t.append("  ")
                    t.append(f"F{index} ", style=th.MUTED)
                    t.append(f"{rpm:.0f}", style=f"bold {th.pick(th.TEXT, disco)}")
                t.append(" rpm", style=th.MUTED)
            if snap.battery_percent is not None or snap.battery_status:
                details = Text()
                if snap.battery_percent is not None:
                    b = snap.battery_percent
                    bc = th.pick(th.OK if b > 40 else th.WARN if b > 15 else th.HOT, disco)
                    details.append(f"{b:.0f}%", style=f"bold {bc}")
                if snap.battery_status:
                    if details:
                        details.append("  ")
                    details.append(snap.battery_status, style=th.MUTED)
                if snap.battery_percent is not None:
                    bar_width = min(max(4, min(12, w - L - 22)), sensor_width - details.cell_len - 1)
                    if bar_width > 0:
                        details = th.bar(b, bar_width, color=bc).append(" ").append_text(details)
                sensor_line("battery", details)
        yield t


def _build_fact_bar(fact: dict, char_pos: int, anim_state: str,
                    fade_step: int = 0, disco: bool = False,
                    compact: bool = False) -> Panel | Text:
    tag_color = fact.get("color", "white")
    body      = fact["text"]
    t = Text()

    if anim_state == "fading":
        fade_color = _FADE_PALETTE[min(fade_step, len(_FADE_PALETTE) - 1)]
        t.append(f" {fact['tag']} ", style=f"bold {fade_color}")
        t.append("  ")
        t.append(body, style=fade_color)
    else:
        # Tag rendered as a solid "chip" in the fact's category color
        t.append(f" {fact['tag']} ", style=f"bold black on {tag_color}")
        t.append("  ")
        if anim_state == "typing":
            t.append(body[:char_pos], style=th.TEXT)
            if char_pos < len(body):
                pac = "ᗧ" if char_pos % 2 == 0 else "ᗦ"
                t.append(pac, style="bold yellow")
                # Remaining text shown as pellets for Pac-Man to eat
                dots = min(len(body) - char_pos - 1, 6)
                t.append(" ·" * (dots // 2), style=th.FAINT)
        else:  # stable
            t.append(body, style=th.TEXT)

    if compact:
        t.no_wrap, t.overflow = True, "ellipsis"
        return t
    return Panel(t, box=th.PANEL_BOX, border_style=th.pick(th.FAINT, disco),
                 padding=(0, 1))


def _build_keys_bar(disco: bool = False) -> Text:
    """Bottom key-hint strip with keycap-style chips."""
    cap = f"bold #1a1b26 on {th.pick(th.ACCENT, disco)}"
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(" ")
    for key, label in (("q", "quit"), ("k", "kill"), ("K", "force kill"),
                       ("y", "fireworks")):
        t.append(f" {key} ", style=cap)
        t.append(f" {label}   ", style=th.MUTED)
    if disco:
        t.append(" ♪ DISCO ♪ ", style=f"bold {th.disco_color()}")
    return t


def _build_kill_prompt(kill_input: str, sig: signal.Signals) -> Panel:
    sig_name  = "SIGKILL" if sig == signal.SIGKILL else "SIGTERM"
    sig_color = th.HOT    if sig == signal.SIGKILL else th.WARN
    t = Text()
    t.append(f" {sig_name} ", style=f"bold #1a1b26 on {sig_color}")
    t.append("   target ", style=th.MUTED)
    t.append("› ", style=f"bold {sig_color}")
    t.append(kill_input, style=f"bold {th.TEXT}")
    t.append("▌", style=f"blink {th.ACCENT}")
    t.append("\n")
    t.append(" ⏎ ", style=f"bold {th.TEXT}")
    t.append("confirm   ", style=th.MUTED)
    t.append(" esc ", style=f"bold {th.TEXT}")
    t.append("cancel   ", style=th.MUTED)
    t.append("· PID, or part of a listed process name", style=th.FAINT)
    return _panel(t, "Kill process", "✖", accent=sig_color, border=sig_color)


class _HardwarePanels:
    def __init__(self, cpu, system, cpu_sub: str | None = None,
                 disco: bool = False) -> None:
        self.cpu = cpu
        self.system = system
        self.cpu_sub = cpu_sub
        self.disco = disco

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        cpu = _panel(self.cpu, "Processor", "◆", subtitle=self.cpu_sub, disco=self.disco)
        system = _panel(self.system, "System", "◇", accent=th.ACCENT2, disco=self.disco)
        cpu_width = (options.max_width * 3 + 4) // 5
        system_width = options.max_width - cpu_width
        if system_width < 12:
            yield cpu
            yield system
            return

        # Count wrapped rows at each column's actual width before adding padding.
        cpu_height = len(console.render_lines(cpu, options.update(width=cpu_width, height=None)))
        system_height = len(console.render_lines(system, options.update(width=system_width, height=None)))
        cpu.height = system.height = max(cpu_height, system_height)

        grid = Table.grid(expand=True)
        grid.add_column(width=cpu_width)
        grid.add_column(width=system_width)
        grid.add_row(cpu, system)
        yield grid


def _build_hardware(snap: Snapshot, history: "_History | None",
                    disco: bool = False, compact: bool = False) -> _HardwarePanels:
    n = len(snap.cpu_per_core)
    has_freq = (len(snap.cpu_freq_per_core) == n
                and any(f is not None for f in snap.cpu_freq_per_core))
    cpu_sub = f"{n} threads" + (" · freq in GHz" if has_freq else "")
    return _HardwarePanels(_CpuView(snap, history, disco), _SystemView(snap, history, disco, compact),
                           cpu_sub=cpu_sub, disco=disco)


def _build_procs(snap: Snapshot, milestone_msg: str | None, disco: bool) -> Panel:
    table = Table(
        show_header=True, header_style=f"bold {th.MUTED}", box=None,
        padding=(0, 1), expand=True, show_edge=False,
    )
    table.add_column("#",    justify="right", width=2, style=th.FAINT)
    table.add_column("PID",  justify="right", width=7, style=th.MUTED)
    table.add_column("NAME", ratio=1, min_width=12, no_wrap=True, overflow="ellipsis")
    table.add_column("CPU",  width=17, no_wrap=True)
    table.add_column("MEM",  justify="right", width=9, no_wrap=True)

    for rank, p in enumerate(snap.top_procs, start=1):
        cpu_c = th.level(p["cpu"], disco=disco)
        cpu_cell = th.bar(p["cpu"], 10, disco)
        cpu_cell.append(f" {p['cpu']:5.1f}%", style=f"bold {cpu_c}")
        name_style = f"bold {th.TEXT}" if rank == 1 else th.TEXT
        table.add_row(
            str(rank), str(p["pid"]), Text(p["name"], style=name_style),
            cpu_cell, Text(th.fmt_mem(p["mem_mb"]), style=th.TEXT),
        )
    if not snap.top_procs:
        table.add_row("", "", Text("no matching processes", style=f"italic {th.MUTED}"), "", "")

    body: list = [table]
    if milestone_msg:
        note = Text()
        note.append("\n★ ", style=f"bold {th.WARN}")
        note.append(milestone_msg, style=f"bold {th.WARN}")
        body.append(note)
    return _panel(Group(*body), "Top processes", "▤", subtitle="sorted by CPU",
                  accent=th.OK, disco=disco)


def _build_renderable(info: SystemInfo, snap: Snapshot, interval: float,
                      milestone_msg: str | None = None,
                      disco: bool = False,
                      kill_prompt: Panel | None = None,
                      fact: dict | None = None,
                      fact_char_pos: int = 0,
                      fact_anim_state: str = "stable",
                      fact_fade_step: int = 0,
                      history: "_History | None" = None,
                      elapsed: float = 0.0,
                      compact: bool = False) -> Group:
    """Compose the full dashboard as a Rich renderable Group."""
    header = _build_header(info, interval, elapsed, disco)
    hardware = _build_hardware(snap, history, disco, compact)
    procs = _build_procs(snap, milestone_msg, disco)

    parts: list = [header, hardware, procs]
    if kill_prompt is not None:
        parts.append(kill_prompt)
    if fact:
        parts.append(_build_fact_bar(fact, fact_char_pos, fact_anim_state,
                                     fact_fade_step, disco, compact))
    parts.append(_build_keys_bar(disco))
    return Group(*parts)


# ---------------------------------------------------------------------------
# Main-buffer painter (Apple Terminal)
# ---------------------------------------------------------------------------

def _use_main_screen() -> bool:
    """Apple Terminal shows its scrollback above the alternate screen and can
    leave the view parked in history, so the dashboard starts below a page of
    blank lines.  There we paint in place on the normal buffer instead."""
    return os.environ.get("TERM_PROGRAM") == "Apple_Terminal"


class _MainScreenLive:
    """Minimal stand-in for ``Live(screen=True)`` that never switches buffers.

    On entry the shell's visible lines are scrolled up into the scrollback
    (nothing is erased), then each refresh homes the cursor and overwrites the
    whole window with a full-size frame, so no clear sequences are needed.
    """

    def __init__(self, renderable: RenderableType, console: Console) -> None:
        self.console = console
        self._renderable = renderable

    def __enter__(self) -> "_MainScreenLive":
        self.console.show_cursor(False)
        # From any cursor row, height-1 newlines push every line that already
        # holds output into the scrollback and leave a blank window.
        self.console.file.write("\n" * (self.console.height - 1))
        self.refresh()
        return self

    def __exit__(self, *exc) -> None:
        # Leave the last frame in place and continue below it.
        self.console.file.write("\r\n")
        self.console.show_cursor(True)
        self.console.file.flush()

    def update(self, renderable: RenderableType, refresh: bool = False) -> None:
        self._renderable = renderable
        if refresh:
            self.refresh()

    def refresh(self) -> None:
        # Screen pads/crops to exactly the window size without a trailing
        # newline, so the frame can never scroll the window.
        with self.console:  # buffer the frame into a single write
            self.console.control(Control.home())
            self.console.print(Screen(self._renderable), end="")


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

    # Kill-mode state
    kill_mode  = False
    kill_input = ""
    kill_sig   = signal.SIGTERM

    # Facts animation state
    fact_idx            = 0
    current_fact        = _facts.get_fact(0)
    milestone_override: dict | None = None  # when set, shown instead of current_fact
    fact_anim_state     = "typing"   # "typing" | "stable" | "fading"
    fact_char_pos       = 0
    fact_fade_step      = 0
    fact_char_int       = _fact_char_interval(current_fact["text"])
    fact_next_char_time = time.monotonic()
    fact_stable_start   = 0.0
    fact_next_fade_time = 0.0

    # Fireworks state — fw_frame >= _FW_TOTAL_FRAMES means "not playing"
    fw_frame  = _FW_TOTAL_FRAMES
    fw_bursts: list[tuple] = []

    key_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    _start_keyboard_thread(key_queue, stop_event)

    collect(top_n=top_n, proc_filter=proc_filter)
    time.sleep(min(interval, 0.3))
    snap = collect(top_n=top_n, proc_filter=proc_filter)  # initial reading
    last_collect_time = time.monotonic()
    history = _History()
    history.push(snap)

    def render(kill_prompt: Panel | None = None) -> Group:
        # Reads the loop's current state from the enclosing scope at call time.
        return _build_renderable(info, snap, interval,
                                 milestone_msg=milestone_msg, disco=disco,
                                 kill_prompt=kill_prompt,
                                 fact=milestone_override or current_fact,
                                 fact_char_pos=fact_char_pos,
                                 fact_anim_state=fact_anim_state,
                                 fact_fade_step=fact_fade_step,
                                 history=history,
                                 elapsed=time.monotonic() - start_time,
                                 compact=console.height < 45)

    # Enter the alternate screen with a frame ready to paint (Apple Terminal:
    # paint in place on the main buffer instead). A separate startup clear is
    # redundant and can move old screen contents into history.
    main_screen = _use_main_screen()
    if main_screen:
        painter = _MainScreenLive(render(), console)
    else:
        painter = Live(render(), console=console, screen=True, auto_refresh=False)
    with painter as live:
        terminal_size = console.size
        try:
            while True:
                now     = time.monotonic()
                elapsed = now - start_time
                size = console.size
                resized = size != terminal_size
                if resized:
                    # Clear cells left behind by the previous terminal dimensions.
                    # Main-buffer frames already overwrite every cell, and a
                    # clear there would push the frame into the scrollback.
                    if not main_screen:
                        console.clear()
                    terminal_size = size

                prev_kill_mode  = kill_mode
                prev_kill_input = kill_input

                # --- Keyboard ---
                quit_requested = False
                while True:
                    try:
                        key = key_queue.get_nowait()
                    except queue.Empty:
                        break

                    if kill_mode:
                        if key in ("ESC", "\x1b"):
                            kill_mode = False
                            kill_input = ""
                        elif key in ("\r", "\n"):
                            # Execute kill
                            target = kill_input.strip()
                            if target:
                                try:
                                    pid = int(target)
                                    name = next((p["name"] for p in snap.top_procs
                                                 if p["pid"] == pid), "")
                                except ValueError:
                                    match = next((p for p in snap.top_procs
                                                  if target.lower() in p["name"].lower()), None)
                                    if match:
                                        pid, name = match["pid"], match["name"]
                                    else:
                                        pid, name = None, None
                                if pid is not None:
                                    sig_name = "SIGKILL" if kill_sig == signal.SIGKILL else "SIGTERM"
                                    try:
                                        os.kill(pid, kill_sig)
                                        milestone_msg = (f"Sent {sig_name} → PID {pid}"
                                                         + (f"  ({name})" if name else ""))
                                    except (ProcessLookupError, PermissionError) as e:
                                        milestone_msg = f"kill failed: {e}"
                                else:
                                    milestone_msg = f"No process matching '{target}'"
                                milestone_until = now + 4.0
                            kill_mode = False
                            kill_input = ""
                        elif key in ("\x7f", "\x08"):  # Backspace
                            kill_input = kill_input[:-1]
                        elif len(key) == 1 and 32 <= ord(key) <= 126:
                            kill_input += key
                    else:
                        if key in ("q", "Q", "\x03"):
                            quit_requested = True
                        elif key == "k":
                            kill_mode, kill_input, kill_sig = True, "", signal.SIGTERM
                        elif key == "K":
                            kill_mode, kill_input, kill_sig = True, "", signal.SIGKILL
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
                    live.update(render(), refresh=True)
                    _draw_sparkle_overlay(
                        live.console.file, fw_frame, cols, rows, fw_bursts
                    )
                    fw_frame += 1
                    time.sleep(_FW_FRAME_DURATION)
                    continue

                # --- Kill mode: redraw on input or resize, skip collect ---
                if kill_mode:
                    if kill_input != prev_kill_input or not prev_kill_mode or resized:
                        live.update(render(_build_kill_prompt(kill_input, kill_sig)),
                                    refresh=True)
                    time.sleep(0.05)
                    continue

                # --- Normal dashboard ---

                # If kill mode just ended, clear the prompt immediately
                if prev_kill_mode and not kill_mode:
                    live.update(render(), refresh=True)

                # Collect hardware data at most once per interval
                collected = False
                if now - last_collect_time >= interval:
                    snap = collect(top_n=top_n, proc_filter=proc_filter)
                    last_collect_time = now
                    collected = True
                    history.push(snap)

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

                    # Milestones → inject into fact bar as Pac-Man animation
                    if now >= milestone_until:
                        milestone_msg = None
                    for threshold, msg in MILESTONES:
                        if threshold not in seen_milestones and elapsed >= threshold:
                            seen_milestones.add(threshold)
                            milestone_override  = {"tag": "★", "color": "yellow", "text": msg}
                            fact_char_pos       = 0
                            fact_anim_state     = "typing"
                            fact_fade_step      = 0
                            fact_char_int       = _fact_char_interval(msg)
                            fact_next_char_time = now

                    if proc_filter and not snap.top_procs and "warned" not in seen_milestones:
                        seen_milestones.add("warned")
                        milestone_msg = f"No processes matched: {', '.join(proc_filter)}"
                        milestone_until = now + 5.0

                # Advance fact animation state machine; track whether a redraw is needed
                fact_changed = False
                if fact_anim_state == "typing":
                    if now >= fact_next_char_time:
                        fact_char_pos      += 1
                        fact_next_char_time = now + fact_char_int
                        fact_changed        = True
                        if fact_char_pos >= len(current_fact["text"]):
                            fact_anim_state   = "stable"
                            fact_stable_start = now

                elif fact_anim_state == "stable":
                    if now - fact_stable_start >= _STABLE_SECS:
                        fact_anim_state     = "fading"
                        fact_fade_step      = 0
                        fact_next_fade_time = now + _FADE_STEP_SECS
                        fact_changed        = True

                elif fact_anim_state == "fading":
                    if now >= fact_next_fade_time:
                        fact_fade_step     += 1
                        fact_next_fade_time = now + _FADE_STEP_SECS
                        fact_changed        = True
                        if fact_fade_step >= len(_FADE_PALETTE):
                            milestone_override  = None
                            fact_idx           += 1
                            current_fact        = _facts.get_fact(fact_idx)
                            fact_char_pos       = 0
                            fact_anim_state     = "typing"
                            fact_fade_step      = 0
                            fact_char_int       = _fact_char_interval(current_fact["text"])
                            fact_next_char_time = now

                # Redraw for data, animation, or window changes.
                if collected or fact_changed or resized:
                    live.update(render(), refresh=True)
                time.sleep(_LOOP_TICK)

        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()

    console.print(f"[bold {th.ACCENT}]◉[/] [{th.MUTED}]pulsar signing off. stay curious.[/]")
