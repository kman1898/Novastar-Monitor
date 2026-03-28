# NovaStar Monitor

**Version 0.2.0**

A real-time monitoring dashboard for NovaStar LED video processing systems. Connects directly to NovaStar controllers (VX1000, MCTRL660, and compatible) via their binary TCP protocol on port 5200, providing live visibility into temperature, voltage, receiving card status, link redundancy, and more.

## Features

- **Live Monitoring** — Temperature, voltage, brightness, and link status updated every 2 seconds
- **Receiving Card Tracking** — See all connected receiving cards with online/offline status
- **Redundancy Detection** — Primary/backup link status for each output port
- **Temperature & Voltage Charts** — Scrolling history graphs
- **Multi-Device** — Monitor multiple NovaStar controllers simultaneously
- **Alert System** — Configurable temperature and voltage thresholds with real-time alerts
- **System Tray App** — Runs in the background with system tray icon (Windows, macOS, Linux)
- **Web Dashboard** — Full monitoring UI accessible from any browser on the network

## Supported Hardware

- NovaStar VX1000 (tested, protocol fully decoded)
- NovaStar VX-series (VX600, VX1000, VX2000 — same protocol family)
- NovaStar MCTRL-series (MCTRL300, MCTRL660 — should be compatible)
- COEX-series support planned (CX80 Pro, MX40 Pro — uses HTTP API)

## Quick Start

### From Source

```bash
cd src
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8050` in your browser, click **+ Add Device**, and enter your controller's IP address.

### With System Tray (Windows/Linux)

```bash
cd src
pip install -r requirements.txt
python launcher_pc.py
```

### With System Tray (macOS)

```bash
cd src
pip install -r requirements.txt
python launcher_mac.py
```

## Building Executables

See [BUILD.md](BUILD.md) for instructions on creating standalone executables with PyInstaller.

## Protocol

The NovaStar binary protocol on TCP port 5200 was reverse-engineered using Wireshark packet captures. See `docs/VX1000_Protocol_Analysis.md` for the full protocol documentation including frame structure, register map, and data layouts.

### Key Registers

| Register | Description | Data |
|----------|-------------|------|
| `0x06000000` | Brightness | 1 byte (0-255) |
| `0x0000000a` | Live monitoring | Temperature, voltage, card count, link status |
| `0x00000002` | Video/input status | Signal detection, format info |
| `0x00000005` | Device identity | "NSSD" header, model, serial |
| `0x00000000` | System info | HW version, build date, port count |

### Temperature Calibration

```
temperature_celsius = raw_byte / 2.0
voltage_volts = raw_byte * 0.03
```

## Project Structure

```
src/
├── app.py                  # Flask + SocketIO server
├── device_manager.py       # Threaded TCP device poller
├── novastar_protocol.py    # Binary protocol codec
├── launcher_pc.py          # Windows/Linux system tray
├── launcher_mac.py         # macOS system tray
├── launcher_settings.py    # Settings persistence
├── templates/
│   └── index.html          # Dashboard template
├── static/
│   ├── css/style.css       # Dashboard styles
│   └── js/app.js           # Frontend application
├── requirements.txt
├── novastar_monitor.spec   # PyInstaller build spec
└── VERSION.txt
```

## Requirements

- Python 3.10+
- Network access to NovaStar controller on TCP port 5200
- Close NovaLCT/SmartLCT before connecting (can't share TCP connection)

## Changelog

### v0.2.0
- Per-card monitoring via direct receiving card addressing (confirmed from VX1000 Wireshark captures)
- Individual temperature, voltage, link status, and firmware per receiving card
- Auto-detection of receiving card count
- CI/CD pipeline: cross-platform tests, linting, build integrity checks
- Release workflow with macOS/Windows builds and optional code signing
- Test suite (58 tests) covering protocol codec, data parsing, and device management

### v0.1.0
- Initial release
- Flask + SocketIO monitoring dashboard
- NovaStar binary TCP protocol codec (port 5200)
- Multi-device support with threaded polling
- Temperature and voltage charts
- Persistent error log with CSV export
- Configurable alert thresholds
- System tray launcher (Windows, macOS, Linux)
- PyInstaller build spec

## License

MIT
