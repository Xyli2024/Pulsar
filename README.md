# Pulsar

![CI](https://github.com/Xyli2024/pulsar/actions/workflows/ci.yml/badge.svg)

A live, terminal-native hardware dashboard for developers and researchers who run heavy computations and don't want to leave the terminal.

## Features

- **Per-core CPU load** — colour-coded bars arranged in multiple columns when space allows on machines with more than 8 cores
- **CPU frequency per core** — live GHz reading alongside each core's load bar
- **Memory pressure** — used vs. total with a live percentage bar
- **Disk I/O throughput** — read and write MB/s, computed as a real-time delta
- **Network I/O** — receive and send MB/s aggregated across all interfaces
- **Top processes** — ranked by CPU%, showing PID, name, CPU%, and memory; press `k` / `K` to send SIGTERM / SIGKILL to any process by PID or name
- **Power and sensors** — adapter, battery, system, CPU and GPU power in watts, plus GPU load, temperatures, fan speed and battery status where supported
- **Fun facts bar** — 500+ built-in facts with a Pac-Man typing animation
- **`--once --format json`** — machine-readable snapshot for logging, scripting, and data pipelines
- **Disco mode** (`--disco`) — every element cycles through random terminal colours on each refresh
- **Fireworks** — press `y` during the live dashboard, or triggers automatically when any core stays at 100% for 5+ seconds
- **Uptime milestones** — playful messages at 5 / 15 / 30 / 42 / 60 minutes, delivered via the Pac-Man fact bar

## Installation

```bash
git clone https://github.com/Xyli2024/pulsar.git
cd pulsar
pip install -e .
```

## Usage

```bash
# Launch the live dashboard
pulsar

# Monitor only specific processes
pulsar --proc python --proc ipopt

# Faster refresh rate
pulsar --interval 0.5

# Show top 10 processes
pulsar --top 10

# One-shot snapshot (table)
pulsar --once

# One-shot snapshot (JSON) — pipe-friendly
pulsar --once --format json > snapshot.json

# Disco mode
pulsar --disco
```

## Dashboard keyboard shortcuts

| Key | Action |
|---|---|
| `k` | Kill prompt (SIGTERM) — type a PID or process name, then Enter |
| `K` | Kill prompt (SIGKILL) — same as above, force-kill |
| `y` | Trigger fireworks animation |
| `q` / Ctrl-C | Quit |

In the kill prompt: **Esc** cancels without sending any signal.

## CLI options

| Option | Default | Description |
|---|---|---|
| `-p, --proc NAME` | — | Filter to processes whose name contains NAME (repeatable) |
| `-i, --interval FLOAT` | `1.0` | Refresh interval in seconds |
| `--once` | off | Print a single snapshot and exit |
| `--format [table\|json]` | `table` | Output format for `--once` mode |
| `-n, --top INTEGER` | `5` | Number of top processes to display |
| `--disco` | off | Enable disco mode |

## Python API

```python
from pulsar import get_system_info, collect

info = get_system_info()          # static hardware identity (call once)
snap = collect(top_n=5)           # one live snapshot

print(snap.cpu_avg)               # average CPU %
print(snap.mem_percent)           # memory usage %
print(snap.net_recv_mbps)         # network receive MB/s
print(snap.disk_read_mbps)        # disk read MB/s
```

See `pulsar_tutorial.ipynb` for a full walkthrough.

## Requirements

- Python 3.10+
- `psutil >= 5.9`
- `rich >= 13.0`
- `click >= 8.0`

## Privacy

Pulsar collects hardware and process statistics locally. Pulsar makes no network requests. Snapshots can contain process names and PIDs, so review them before sharing.

## Contributors

| Name | Role |
|---|---|
| [Xyli2024](https://github.com/Xyli2024) | Author |
| [Claude (Anthropic)](https://www.anthropic.com/claude) | AI Assistant |
| Codex (OpenAI) | AI Assistant |

## License

MIT — see [LICENSE](LICENSE) for details.
