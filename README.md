# pulsar

A live, terminal-native hardware dashboard for developers and researchers who run heavy computations and don't want to leave the terminal.

## Features

- **Per-core CPU load** — color-coded bars (green / yellow / red), adapts to heatmap grid for machines with more than 8 cores
- **Memory pressure** — used vs. total with a percentage bar
- **Disk I/O throughput** — read and write MB/s, computed as a live delta
- **Top processes** — ranked by CPU%, showing PID, name, CPU%, and memory
- **`--once --format json`** — machine-readable snapshot for logging and scripting
- **Disco mode** (`--disco`) — every element cycles through random colors on each refresh
- **Fireworks Mode** — press `s` during the live dashboard, or triggers automatically when a core hits 100% for 5+ seconds
- **Uptime milestones** — quiet messages at 5 / 15 / 30 / 42 / 60 minutes

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

# One-shot snapshot
pulsar --once
pulsar --once --format json > snapshot.json

# Show top 10 processes
pulsar --top 10

# Disco mode
pulsar --disco
```

## Options

| Option | Default | Description |
|---|---|---|
| `-p, --proc NAME` | — | Filter to processes whose name contains NAME (repeatable) |
| `-i, --interval FLOAT` | `1.0` | Refresh interval in seconds |
| `--once` | off | Take a single snapshot and exit |
| `--format [table\|json]` | `table` | Output format for `--once` mode |
| `-n, --top INTEGER` | `5` | Number of top processes to display |
| `--disco` | off | Enable disco mode |

## Requirements

- Python 3.10+
- `psutil >= 5.9`
- `rich >= 13.0`
- `click >= 8.0`

## Contributors

| Name | Role |
|---|---|
| [xyli2024](https://github.com/Xyli2024) | Author |
| [Claude (Anthropic)](https://www.anthropic.com/claude) | AI Assistant |

## License

MIT — see [LICENSE](LICENSE) for details.