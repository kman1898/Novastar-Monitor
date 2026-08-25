# NovaStar Monitor

A read-only monitoring dashboard for NovaStar LED video processing systems.
It is built around **H-series video wall splicers** driving thousands of
receiving cards (panels) across daisy-chains, and it answers one question
fast: *which panel is in trouble, and where is it in the wall?*

## What it does — and what it deliberately does not

**It monitors.** Per receiving card: temperature, voltage, brightness, both
power-supply status flags, link/data-break state, and the bit-error counter.
Per device: connection state, firmware, brightness, screen topology.

**It does not control.** There is no brightness write, no preset recall, no
configuration push — not one `W0xxx` command anywhere in the polling or
enumeration paths (the only exception is the `W0120` keepalive heartbeat,
which the H-series expects from any attached controller and which changes
nothing). Control lives in a separate Bitfocus Companion module and is out of
scope here on purpose: this app is safe to point at a wall during a show.

## Features

- **Wall View** — every card on the wall as one coloured cell, laid out the way
  the controller actually reports it: sender card → OPT fibre → port → chain.
  Colour modes: Temperature (NovaLCT-style heat bands), Power, Online, Bit
  Errors. Refreshes every 5 s, and only while the tab is visible.
- **Per-card alerting** — thresholds are evaluated on *each card*, never on the
  wall average. See [Alerting](#alerting).
- **Dashboard** — per-device cards with live monitoring aggregates and
  scrolling temperature/voltage history (300 samples).
- **Error Log** — persistent, filterable by severity and active/resolved, with
  acknowledge/resolve and CSV export.
- **Multi-device** — several controllers polled concurrently, one thread each.
- **Simulation mode** — a simulated VX1000 with 14 cards, so the UI can be
  explored with no hardware attached (Settings toggle, tray menu, or `--demo`).
- **System tray app** — Windows, macOS, Linux.

## Supported hardware

| Family | Transport the app uses | Notes |
|---|---|---|
| **NovaStar H-series video wall splicers** | **JSON over UDP 6000** (primary), binary TCP 5203 (fallback) | The main target. Multi-sender-card chassis supported; each sender card is its own TCP service on 5201/5202/5203. |
| NovaStar VX-series (VX600, VX1000, VX2000) | Binary TCP 5200 | VX1000 protocol fully decoded from captures; VX600/VX2000 are the same protocol family. |
| NovaStar MCTRL-series (MCTRL300, MCTRL660) | Binary TCP 5200 | Same frame format — expected to work, not verified against hardware. |
| COEX-series (CX80 Pro, MX40 Pro) | — | Not implemented. Uses an HTTP API. |

The device type is chosen by the port you enter when adding a device: **5203 →
H-series**, anything else → VX1000-family binary. The Add Device dialog offers
both as presets.

### Which protocol path, and why

**H-series → JSON UDP 6000 first.** It is a documented protocol ("H Series
Video Wall Splicers Control Protocol V1.0.19"), returns structured JSON instead
of reverse-engineered registers, and reports its own topology. Per poll cycle
the app issues `R0100` (device details), `R0400` (screen list), `R0405` per
screen (output topology), `R0300` (output list), and one `R0155` per known
receiving card — batched 8 commands per datagram so a 1374-card wall is ~172
round trips instead of 1374. A `W0120` heartbeat runs on its own socket every
3 s so it never queues behind card reads.

**Binary TCP is the fallback.** If UDP 6000 never answers (older firmware,
filtered UDP), the app drops to the binary path after 3 cycles and says so.
Once JSON has answered even once, transient failures never demote a device.

**The binary path sees things JSON cannot.** The per-card bit-error counter
(register `0x4A010002`) has no JSON equivalent, and the JSON `R0155` path is
known to under-report which cards exist — which is why enumeration defaults to
binary. See [docs/H_SERIES_FINDINGS.md](docs/H_SERIES_FINDINGS.md).

## Quick start

### From source

```bash
cd src
pip install -r requirements.txt
python app.py
```

Open **`http://127.0.0.1:8060`**, click **+ Add Device**, pick H-series or
VX1000, and enter the controller's IP.

`src/start.sh` does the same thing with a dependency check. Override the port
with `NSM_PORT`; `NSM_DEBUG=1` turns on the Flask reloader (off by default —
with it on, two processes would poll the same controller).

### With the system tray

```bash
cd src
python launcher_pc.py     # Windows / Linux
python launcher_mac.py    # macOS
```

Both default to port 8060 and remember host/port in the launcher settings.

### Standalone executables

See [BUILD.md](BUILD.md) (PyInstaller, spec at `src/novastar_monitor.spec`).

## Enumerating the wall — required before the Wall View works

The controller will happily tell you about a card you ask about, but nothing
gives you the full inventory in one call. So the card list is built once, by a
sweep, and cached in `src/wall_live_snapshot.json`. The poller then refreshes
exactly those known cards each cycle.

**Until you run the enumeration, the Wall View is empty** ("No wall data yet —
run the enumeration to build a snapshot") and per-card monitoring stays idle.

```bash
cd src
python enumerate_wall.py 192.168.0.10                          # dry run
python enumerate_wall.py 192.168.0.10 --yes-contact-hardware   # actually sweep
```

`--yes-contact-hardware` is **required**. Without it the tool prints exactly
what it would contact — IP, TCP/UDP targets, chain range, worst-case probe
count, output path — and exits without opening a socket. The wall is usually
live production hardware; nothing gets sent by accident.

Everything it sends is a READ. It never sends a `W0xxx` command, not even the
heartbeat, so it cannot change device state.

Useful flags:

| Flag | Effect |
|---|---|
| `--transport binary` (default) | Authoritative per-chain walk over TCP 520N. Sender cards are discovered with an `rqProMI:` datagram on UDP 3800. |
| `--transport json` | The old `R0155` sweep, for comparison. Known to under-report; prints a warning and a count of silent addresses. |
| `--sender-cards 1,2,3` | Skip discovery and scan these sender cards. |
| `--chains 0-15`, `--max-cards N` | Narrow the sweep. |
| `--no-readings` | Enumerate only; skip the `R0155` temperature/voltage pass. |
| `-o PATH` | Write somewhere other than `src/wall_live_snapshot.json`. |

If the sweep finds zero cards it refuses to write, rather than overwriting a
good snapshot with an empty one. Re-run it after any physical change to the
wall.

## Configuration

| What | Where | Default |
|---|---|---|
| Devices, poll interval, alert thresholds | Settings tab → `src/novastar_settings.json` | poll 10 s, temp warn 60 °C, temp critical 75 °C, min voltage 4.7 V |
| Web server port | `NSM_PORT` env var / launcher settings | 8060 |
| Card inventory | `src/wall_live_snapshot.json` (written by `enumerate_wall.py`) | none — must be generated |
| Physical wall map (pillars) | `src/wall_config.json`, falls back to bundled `wall_config_default.json` | the venue's 7-pillar / 15-chain map |
| Pillar display overrides | `src/wall_layout.json`, falls back to `wall_layout_default.json` | empty |

Poll interval is clamped to 1–3600 s server-side (the UI suggests 5–30). The
default of 10 s matches the cadence the official Companion splicer module uses:
fresh enough to feel live, light enough to run 24/7.

`wall_config.json` / `wall_layout.json` describe *where a chain physically hangs
in the room* — something the controller can never report, since it only knows
logical `(slot, port, card)` addresses. They are served by `/api/wall_config`,
`/api/wall_layout` and `/api/wall_rendered`. **No UI consumes them yet**; the
drag-drop pillar editor that will is not built. Today's Wall View is driven
entirely by live enumeration data via `/api/wall_live`.

## Alerting

Thresholds are evaluated **per receiving card**, never on the device average.
On a 1374-card wall one card at 95 °C moves the mean by about 0.04 °C, so an
average can never cross a 75 °C threshold — it would miss the exact condition
the app exists to catch.

Four per-card signals are checked every cycle:

| Signal | Fires when |
|---|---|
| Temperature | `>= temp_warning` (WARNING), `>= temp_critical` (CRITICAL) |
| Voltage | `< voltage_min` (WARNING); `< 0.5 V` is CRITICAL — that is a dead supply, not a low one |
| Power supplies | exactly one supply flagged → CRITICAL (running unprotected); both flagged → WARNING (usually a transient read artifact, not a double failure) |
| Bit errors | `>= 100` → WARNING; counter saturated at `0xFFFF` → CRITICAL |

### Tiering, so one fault is one line

Raw per-card alerts on a wall this size would produce 1374 log entries for one
failed power leg. Breaches are grouped by severity and metric, then collapsed:

- **3 or more chains** affected → a single **wall-level** alert
- **5 or more cards on one chain** → a single **chain-level** alert
- otherwise → one alert per card

On top of that: 60 s cooldown per card+metric+severity (300 s for device
rollups), and a hard cap of 20 alerts written per poll cycle. Device-level
rollups use the **maximum** card reading, never the mean.

The Wall View's bit-error threshold (`BER_ALERT` in `static/js/wall_view.js`)
mirrors `BIT_ERROR_WARNING` in `app.py`. **Keep the two equal** — a red cell
that raises no alert teaches the operator to distrust both.

## Deployment caveats

Two things to know before putting this on a show network.

### 1. `socket.io` is loaded from a public CDN

`src/templates/index.html` pulls the socket.io client from
`cdnjs.cloudflare.com`. **LED walls routinely run on isolated networks with no
internet route, and there that script never loads.** The dashboard does not
break — `app.js` detects the missing `io` global, shows a banner, and falls
back to polling the REST endpoints — but you lose live push updates and get
periodic refreshes instead.

**Pending manual fix:** download `socket.io.min.js` **4.7.5** on a machine with
internet, commit it to `src/static/js/`, and repoint the `<script>` tag at
`url_for('static', filename='js/socket.io.min.js')`. No copy of the client
ships with flask-socketio or python-socketio, so it has to be fetched once by
hand. This has not been done yet.

### 2. `src/wall_live_snapshot.json` is per-install runtime state

It is **gitignored**. A fresh checkout has no wall — the Wall View is empty and
per-card polling stays idle until `enumerate_wall.py` is run against the actual
hardware. The same applies to `novastar_settings.json`, `error_log.json`,
`wall_config.json` and `wall_layout.json`: site-specific, generated at run time,
never committed.

## Protocol reference

- **[docs/H_SERIES_FINDINGS.md](docs/H_SERIES_FINDINGS.md)** — the H-series
  reverse-engineering record and architecture handoff. Read §6.5 first: it
  carries the verified per-card addressing model.
- **[docs/VX1000_Protocol_Analysis.md](docs/VX1000_Protocol_Analysis.md)** —
  the original binary protocol analysis (frame structure, register map).

### Binary frame essentials

Checksum is `sum(bytes between header and checksum) + 0x5555`, wire-encoded
little-endian; the `55 AA` header is **not** in the sum. Per-card reads put the
**chain index in byte[7]** and the **card position in byte[8]**.

### Key registers

| Register | Description |
|---|---|
| `0x0000000A` | Live monitoring — temperature, voltage, card count, link status (works on H-series too) |
| `0x00000002` | Video/input status — byte[1] per-card link paths, byte[31] port bitmask |
| `0x4A010002` | Per-card bit-error counter (3 bytes) |
| `0x09050002` | Per-card fault/alarm flag (1 byte, 0 = no fault) |
| `0x00000005` | Device identity ("NSSD") |
| `0x00000000` | System info |
| `0x06000000` | Brightness |

### Calibration

```
temperature_celsius = raw_byte / 2.0
voltage_volts       = raw_byte * 0.03
```

Both apply to the binary registers and to the H-series JSON `R0155` `temp` /
`voltage` fields. The voltage formula is **not** `(raw & 0x7F) / 10` — that
variant under-reports by roughly 0.9 V and flags every healthy card on a 5 V
rail as below the 4.7 V alarm.

## Project structure

```
src/
├── app.py                    # Flask + SocketIO server, REST API, alerting
├── device_manager.py         # Threaded per-device pollers (JSON UDP + binary)
├── novastar_protocol.py      # Binary frame codec + register map
├── h_series_json.py          # H-series JSON UDP client (port 6000, batched)
├── enumerate_wall.py         # CLI: sweep the wall, write the card snapshot
├── wall_config.py            # Physical wall map loader/merger/validator
├── demo_device.py            # Simulated VX1000 for the UI
├── launcher_pc.py            # Windows/Linux system tray
├── launcher_mac.py           # macOS system tray
├── launcher_settings.py      # Launcher settings persistence
├── start.sh                  # Quick-start script
├── templates/
│   └── index.html            # Dashboard / Wall / Errors / Settings tabs
├── static/
│   ├── css/style.css
│   └── js/
│       ├── app.js            # Dashboard, devices, alerts, settings
│       └── wall_view.js      # Wall View renderer
├── wall_config_default.json  # Bundled pillar/port map
├── wall_layout_default.json  # Bundled (empty) layout overrides
├── requirements.txt
├── novastar_monitor.spec     # PyInstaller build spec
└── VERSION.txt

docs/
├── H_SERIES_FINDINGS.md      # H-series protocol + architecture handoff
└── VX1000_Protocol_Analysis.md

tests/                        # 434 tests
```

Generated at run time and gitignored: `src/novastar_settings.json`,
`src/error_log.json`, `src/wall_config.json`, `src/wall_layout.json`,
`src/wall_live_snapshot.json`, `src/logs/`.

## Requirements

- Python 3.10+
- Network access to the controller:
  - H-series: **UDP 6000** (primary), TCP 5200–5203 (fallback + enumeration),
    UDP 3800 (sender-card discovery)
  - VX1000 family: **TCP 5200**
- For the binary TCP path, close NovaLCT / SmartLCT first — a controller TCP
  service already held by another client will not serve this app as well. The
  JSON UDP path is stateless request/response and is not affected the same way.

## Tests

```bash
python3 -m pytest tests/ -q     # 434 tests
```

No test opens a socket to real hardware; importing `app` never starts a poller.

## Changelog

### v0.4.0

- **H-series JSON UDP protocol (port 6000) is now the primary path** for
  H-series devices — `R0100`/`R0400`/`R0405`/`R0300`/`R0155` plus the `W0120`
  heartbeat, with the binary TCP path kept as automatic fallback
- JSON client batches 8 commands per datagram, reuses one socket, and gives the
  heartbeat its own socket so it never queues behind per-card reads
- **`src/enumerate_wall.py`** — new read-only CLI that sweeps the wall and
  writes `wall_live_snapshot.json`; requires an explicit
  `--yes-contact-hardware` flag, and defaults to the binary transport because
  `R0155` under-reports which cards exist
- **Corrected binary per-card addressing**: chain index in byte[7], card
  position in byte[8] (was byte[5]/byte[8]); verified against a 245-panel rig
- **Corrected voltage formula** to `raw * 0.03`; the previous
  `(raw & 0x7F) / 10` under-reported by ~0.9 V and produced false low-voltage
  alarms on every healthy card
- **Bit-error register `0x4A010002` implemented** — per-card counter with a
  saturation flag, surfaced as a Wall View colour mode and an alert
- **Per-card alerting** replaces device-average alerting, with wall → chain →
  card tiering, per-key cooldowns and a per-cycle cap
- Per-card fault flag (`0x09050002`) decoded; `parse_h_card_temperature()`
  deprecated after `0x00400003` proved to be a constant, not temperature
- Rebuilt Wall View: sender card → OPT → port → chain tree with Temperature /
  Power / Online / Bit Errors modes and explicit Live-vs-Snapshot freshness
- Fixed frame checksum to the documented `+0x5555` formula, little-endian
- Default poll interval is now 10 s (single constant shared by the manager and
  the settings UI — the two used to disagree)
- Default web port is now 8060 across the app, launchers and `start.sh`
- Flask debug/reloader is opt-in via `NSM_DEBUG` (it used to be hard-coded on,
  which polled live hardware from two processes)
- Atomic writes for settings, error log and wall layout
- Dashboard degrades to REST polling when the socket.io CDN is unreachable
- Test suite expanded to **434 tests**

### v0.3.0
- Simulation mode: toggle a simulated VX1000 (14 cards) from Settings, system tray, or CLI (`--demo`)
- Structured JSON logging with rotation (20 MB, 2 backups), event/request/error logging
- Fix release workflow: `workflow_dispatch` now creates a proper tag for GitHub releases
- Fix receiving card online/offline status display in dashboard
- Test suite expanded to 70 tests

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

## Changelog

### v0.3.0
- Simulation mode: toggle a simulated VX1000 (14 cards) from Settings, system tray, or CLI (`--demo`)
- Structured JSON logging with rotation (20 MB, 2 backups), event/request/error logging
- Fix release workflow: `workflow_dispatch` now creates a proper tag for GitHub releases
- Fix receiving card online/offline status display in dashboard
- Test suite expanded to 70 tests

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
