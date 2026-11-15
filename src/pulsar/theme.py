"""theme.py — shared visual language for the live dashboard and --once output."""

import random

from rich import box
from rich.text import Text

# ---------------------------------------------------------------------------
# Palette (truecolor; Rich downsamples automatically on 256/16-color terminals)
# ---------------------------------------------------------------------------

ACCENT  = "#7dcfff"   # primary accent (titles, logo)
ACCENT2 = "#bb9af7"   # secondary accent
TEXT    = "#c0caf5"   # primary text
MUTED   = "#737aa2"   # labels, units
FAINT   = "#4a5075"   # separators, empty bar track
BORDER  = "#3b4261"   # panel borders
OK      = "#9ece6a"
WARN    = "#e0af68"
HOT     = "#f7768e"

NET_DOWN = "#7dcfff"
NET_UP   = "#bb9af7"
DISK_R   = "#73daca"
DISK_W   = "#ff9e64"
POWER    = "#e0af68"

PANEL_BOX = box.ROUNDED

DISCO_COLORS = [
    "red", "green", "yellow", "blue", "magenta", "cyan",
    "bright_red", "bright_green", "bright_yellow",
    "bright_blue", "bright_magenta", "bright_cyan",
]

_SPARK = "▁▂▃▄▅▆▇█"


def disco_color() -> str:
    return random.choice(DISCO_COLORS)


def pick(color: str, disco: bool = False) -> str:
    """Return *color*, or a random disco color when disco mode is on."""
    return disco_color() if disco else color


# ---------------------------------------------------------------------------
# Color scales
# ---------------------------------------------------------------------------

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lerp_hex(a: str, b: str, t: float) -> str:
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    t = max(0.0, min(1.0, t))
    return "#{:02x}{:02x}{:02x}".format(
        round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t)
    )


def gradient(frac: float) -> str:
    """Continuous OK → WARN → HOT color for a 0..1 fraction."""
    frac = max(0.0, min(1.0, frac))
    if frac < 0.5:
        return _lerp_hex(OK, WARN, frac / 0.5)
    return _lerp_hex(WARN, HOT, (frac - 0.5) / 0.5)


def level(pct: float, warn: float = 50, hot: float = 80, disco: bool = False) -> str:
    """Discrete status color for a percentage (used for numbers)."""
    if disco:
        return disco_color()
    return OK if pct < warn else WARN if pct < hot else HOT


# ---------------------------------------------------------------------------
# Glyph widgets
# ---------------------------------------------------------------------------

def bar(pct: float, width: int = 20, disco: bool = False,
        color: str | None = None) -> Text:
    """
    Thin horizontal gauge.  Each filled cell is coloured by its position on
    the OK→HOT gradient (or by a fixed *color*), with half-cell resolution.
    """
    width = max(1, width)
    pct = max(0.0, min(100.0, pct))
    halves = round(pct / 100 * width * 2)  # number of half-cells to fill
    full, half = divmod(halves, 2)
    t = Text()
    for i in range(full):
        c = disco_color() if disco else color or gradient((i + 0.5) / width)
        t.append("━", style=c)
    if half:
        c = disco_color() if disco else color or gradient((full + 0.5) / width)
        t.append("╸", style=c)
    t.append("━" * (width - full - half), style=FAINT)
    return t


def sparkline(values, width: int, color: str | None = None, floor: float = 1.0,
              disco: bool = False) -> Text:
    """
    Right-aligned sparkline of the last *width* values, scaled to
    max(values, floor).  With color=None each glyph is coloured on the
    OK→HOT gradient by its value relative to *floor* (useful for percents).
    """
    width = max(0, width)
    vals = list(values)[-width:] if width else []
    t = Text()
    t.append("·" * (width - len(vals)), style=FAINT)
    if not vals:
        return t
    top = max(max(vals), floor)
    for v in vals:
        v = max(0.0, v)
        idx = int(v / top * (len(_SPARK) - 1) + 0.5) if top > 0 else 0
        if disco:
            c = disco_color()
        elif color is None:
            c = gradient(v / floor if floor > 0 else 0.0)
        else:
            c = color
        t.append(_SPARK[idx], style=c)
    return t


def kv(t: Text, label: str, value: str, label_w: int = 0,
       value_style: str = TEXT) -> Text:
    """Append a 'label  value' pair with a muted label."""
    t.append(f"{label:<{label_w}}" if label_w else label, style=MUTED)
    t.append(value, style=value_style)
    return t


def section(title: str, color: str = ACCENT2) -> Text:
    """Small in-panel section heading, e.g. '▍ MEMORY'."""
    t = Text()
    t.append("▍", style=color)
    t.append(title.upper(), style=f"bold {color}")
    return t


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_mem(mb: float) -> str:
    return f"{mb:.0f} MB" if mb < 1024 else f"{mb / 1024:.2f} GB"


def fmt_rate(mbps: float) -> str:
    """Human-readable MB/s with adaptive units."""
    if mbps < 1:
        return f"{mbps * 1000:6.1f} KB/s"
    if mbps < 1000:
        return f"{mbps:6.2f} MB/s"
    return f"{mbps / 1000:6.2f} GB/s"


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def freq_text(current: float | None, maximum: float | None) -> str | None:
    if current is not None and maximum is not None:
        return f"{current} / {maximum} GHz"
    if current is not None:
        return f"{current} GHz"
    if maximum is not None:
        return f"{maximum} GHz max"
    return None
