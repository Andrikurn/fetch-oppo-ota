# fetch-oppo-ota

A fast, clean **TUI & CLI downloader** for **OPPO, OnePlus, and realme** Full OTA firmware packages, built with Python, `uv`, `httpx`, `rich`, and `textual`.

## Features

- **Dual OTA sources**:
  - **Oxygen Updater catalog**: Browse & live-search OPPO, OnePlus, and realme devices grouped by model.
  - **Direct OPPO / Oplus server query (`Ctrl+O` / `direct`)**: Query OPPO's official `update/v5` (mobile) and `update/v3` (PC) endpoints directly across `GL`, `EU`, `IN`, and `CN` regions with optional custom OTA version and NV Carrier ID.
- **Clean single-screen TUI**:
  - Browse & live-search devices grouped by model (regions stay hidden until you select a device with regional variants).
  - Deduplicated base model numbers (e.g. `CPH2791` instead of `CPH2791EEA, CPH2791IN, ...`).
  - Inspect version, size, filename, and changelog before downloading.
  - Always-visible Downloads panel with Model, File, Status, Progress, dynamic Speed (`B/s`, `KB/s`, `MB/s`, `GB/s`), and ETA.
- **Multi-connection segmented downloads**: Probes HTTP `Range` support and splits each OTA across configurable parallel connections (default `8`) using lock-free `os.pwrite` writes.
- **Crash-safe resume**: Atomically saves per-segment byte progress to `<filename>.ota.state.json` alongside `<filename>.part`. Interrupted or paused downloads resume from the exact byte offset.
- **Auto-refreshing CDN URLs**: Automatically refreshes expiring Oplus/OPPO CDN download links on start, `403`/`410`, or repeated connection drops.
- **Parallel download queue & live rate limiting**: Download multiple OTA packages concurrently, with live-adjustable **global** and **per-file** speed limits (`Ctrl+S`).
- **Persistent settings**: Saves your preferences automatically to your OS config directory:
  - **Linux**: `~/.config/fetch-oppo-ota/settings.json`
  - **macOS**: `~/Library/Application Support/fetch-oppo-ota/settings.json`
  - **Windows**: `%APPDATA%\fetch-oppo-ota\settings.json`
- **Clean output filenames**: Saves packages as `<fwversion>.zip` and verifies MD5 checksums in the background.

---

## Requirements

- Python `>= 3.11`
- [`uv`](https://docs.astral.sh/uv/) (recommended)

## Installation

```bash
uv tool install git+https://github.com/Andrikurn/fetch-oppo-ota.git
```

Or for local development:

```bash
git clone https://github.com/Andrikurn/fetch-oppo-ota.git
cd fetch-oppo-ota
uv sync
```

---

## Usage

### Interactive TUI (default)

```bash
uv run fetch-oppo-ota
```

With optional overrides:

```bash
uv run fetch-oppo-ota -o ~/otas -c 8 -p 3 --global-limit 20M --file-limit 10M
```

#### TUI Keybindings

| Key | Action |
| --- | --- |
| `/` | Focus the search bar |
| `Enter` | Select device / jump from search to device list |
| `Ctrl+O` | Direct OPPO server query (`update/v5` & `update/v3`) |
| `d` | Start downloading the selected OTA |
| `p` | Pause / Resume selected download |
| `x` | Cancel selected download |
| `Ctrl+S` | Open Settings (parallel downloads, connections, speed limits) |
| `q` | Quit (active downloads save resume state) |

### Headless CLI

```bash
# List supported OPPO devices (or --brand oneplus|realme|all)
uv run fetch-oppo-ota list --brand oppo -q "Find X8"

# Inspect latest Full OTA metadata for a device ID
uv run fetch-oppo-ota info --device-id <ID>

# Download one or more devices in parallel with Rich progress bars
uv run fetch-oppo-ota download --device-id <ID1> --device-id <ID2> -c 8 -p 2 --global-limit 15M

# Query OPPO's official update/v5 & v3 servers directly (no Oxygen Updater)
uv run fetch-oppo-ota direct --model PHY110 --region CN --coloros 15
uv run fetch-oppo-ota direct --model PHY110 --region CN --coloros 15 --download

# Direct query with explicit OTA version and NV Carrier ID
uv run fetch-oppo-ota direct \
  --model PHY110 \
  --region CN \
  --nv-id 10010111 \
  --coloros 15 \
  --ota-version PHY110_11.A.01_0001_000000000001
```

---

## Project Structure

```text
src/fetch_oppo_ota/
├── api.py         # Async Oxygen Updater API v2.10 client (httpx)
├── oplus_api.py   # Direct OPPO / Oplus update/v5 & v3 encrypted client (cryptography)
├── cli.py         # CLI entrypoint & headless subcommands (list, info, download, direct)
├── config.py      # Cross-platform persistent settings (platformdirs)
├── downloader.py  # Multi-connection, resumable, rate-limited parallel download engine
├── models.py      # Data models, device/region/model parsing, formatting helpers
└── tui.py         # Single-screen Textual TUI, Direct OPPO & Settings modals
```

---

## Credits & Attribution

- **[Oxygen Updater / OS Updater (`oxygen-updater/os-updater`)](https://github.com/oxygen-updater/os-updater)** — This tool uses the Oxygen Updater v2.10 backend API and community-maintained OTA database. All credit for maintaining the OTA server infrastructure and device catalog belongs to the Oxygen Updater team and its contributors.
- Built with **[Textual](https://github.com/Textualize/textual)**, **[Rich](https://github.com/Textualize/rich)**, **[HTTPX](https://github.com/encode/httpx)**, **[platformdirs](https://github.com/tox-dev/platformdirs)**, and **[uv](https://github.com/astral-sh/uv)**.

## Disclaimer

This is an unofficial, independent tool and is **not** affiliated with, endorsed by, or connected to OPPO, OnePlus, realme, or Oxygen Updater. Use at your own risk and always verify firmware compatibility with your device model and region before flashing.

## License

[MIT](LICENSE)
