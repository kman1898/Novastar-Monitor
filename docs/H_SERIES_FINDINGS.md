# NovaStar Monitor — H-Series Findings & Architecture Handoff

**Purpose:** Single-document context dump for picking up this work in a fresh Claude Code session. Everything we've decoded about the H-series LED video splicer protocols, the user's wall topology, what's implemented, and what's next.

**User:** Matt Knotts (kman1898) — LED tech, macOS dev environment.
**Repo root (worktree):** `/Users/mattknotts/Nextcloud/LED/LED Wall Tech/Novastar-Monitor/.claude/worktrees/modest-torvalds`
**Branch:** `claude/modest-torvalds`
**Sister project (separate, do not modify here):** `companion-module-novastar-controller` — Bitfocus Companion CONTROL plugin. This app is **monitoring only**, no control.

---

## 1 · The wall (943 cards · 15 chains · 7 pillars)

User-provided diagrams at `/Users/mattknotts/Documents/`. Wall is the LED installation at a venue with multiple architectural sections fed by one H-series chassis.

| Pillar | Canvas X,Y | W × H | Chains | Cards |
|---|---|---|---|---|
| SRR Pillar | 0, 0 | 600 × 1560 | A1 (76), A2 (50) | 126 |
| SR Pillar | 600, 0 | 840 × 1800 | A3 (84), A4 (84), A5 (42) | 210 |
| Main Top | 1440, 0 | 780 × 2160 | A6 (87), A7 (91), A8 (52) | 230 |
| SL Pillar | 2220, 0 | 840 × 1800 | A9 (84), A10 (84), A11 (42) | 210 |
| SLL Pillar | 3060, 0 | 600 × 1560 | A12 (76), A13 (50) | 126 |
| DJ Booth | 0, 1920 | 900 × 120 | A14 (15) | 15 |
| Main Bottom | **2220, 1920** (canvas) | 780 × 240 | A15 (26) | 26 |
| **Total** | | | **15 chains** | **943** |

**Notes:**
- Panel size: **60 × 120 px** per card (cabinet) — applies uniformly.
- Main Bottom's *canvas* coord is (2220, 1920); physically it sits below Main Top. We use canvas coords (operator's choice — drag-drop UI will let users override later).
- Pillar dimensions ≠ exact chain card sums (off by 4 in SRR/Main Top/SLL because the rectangles aren't perfectly filled). Trust chain counts, not grid math.
- Each chain enters at top-right of its sub-region and snakes leftward in standard boustrophedon (per user; Q1 confirmed on diagram inspection but exact serpentine-walk algorithm hasn't been written yet).

### OPT (fiber) → chain mapping

H-series **output card** has **16 logical ports** split across 2 OPT (optical fiber) outputs:

```
H-series chassis
├── O-5  PRIMARY output card  (SN 003168010000008b)
│    ├── OPT 1  fiber → ports 1–8   → chains A1–A8
│    └── OPT 2  fiber → ports 9–16  → chains A9–A15 + port 16 spare
└── O-6  BACKUP output card   (mirrors O-5; OPT 3, OPT 4)
```

**Per-card addressing format** — operator-facing shorthand: `(slot, port, card_index)`, i.e. **`1-1-1` = sending card 1, port 1, card 1**.

> ### ⛔ STOP — the binary layout below is SUPERSEDED. Read [§6.5](#65--corrected-binary-per-card-addressing-the-key-to-accurate-enumeration) FIRST.
>
> The `byte[5]=port` claim in the table below is **wrong** and was implemented
> from this section once already, costing real time. The verified layout is
> **byte[5] = sender card index, byte[7] = chain index, byte[8] = card
> position**, over a SINGLE connection to TCP 5201 — not one connection per
> sender card, which is a second wrong model that also cost time (5202/5203/
> 5204 accept connections and answer every read with an empty payload). §6.5
> supersedes this table; it is kept only because the JSON row is still
> accurate and because the wrong models appear in old captures and old code.

| Form | How slot/port/card are encoded |
|---|---|
| **JSON UDP `R0155`** | `param0=slotId`, `param1=portId`, `param2=cardId_low_byte`, `param3=cardId_high_byte` (16-bit card_id split low/high). **Caveat:** this path silently under-reports — see §6.6. |
| **Binary TCP** | ~~`byte[5]=port` … `byte[8]=card_index`~~ **SUPERSEDED — see §6.5.** Correct: `byte[5]=sender card`, `byte[7]=chain`, `byte[8]=card position`, all over one connection to TCP 5201. Implemented in `build_read_card()` in [src/novastar_protocol.py](src/novastar_protocol.py). |

The wall documented in this section is **943 panels/cards** (sum of A1–A15 chain
card counts) on a single sender card. That is *not* the same wall as the
3-sender-card COSMIC MEADOW config discussed later — see §6.5's closing note.

---

## 2 · Protocol landscape on H-series at 192.168.0.10

The H-series exposes **multiple parallel protocols**, all live simultaneously. In order of capability/preference:

| Tier | Protocol | Port | Status | Use for |
|---|---|---|---|---|
| 🥇 | **JSON UDP**, `R0xxx`/`W0xxx` commands | UDP 6000 | confirmed working in captures | Primary — modern, documented |
| 🥈 | **HTTP REST API** (web UI backend) | TCP 80 (nginx) | endpoints discovered | Secondary — auth-gated, also rich |
| 🥉 | **Binary**, 0x55AA frames | TCP 5200–5204 | already implemented | Fallback for older firmware |
| 🏅 | **G4A v2.0 broadcast discovery** | UDP 5600 | observed | Auto-find devices on LAN |

### 2.1 · JSON UDP commands (port 6000)

**Documented in H-Series PDF V1.0.19** at `/Users/mattknotts/Nextcloud/LED/LED Wall Tech/Manuals/Novastar/Control Protocols/H Series/`:

| Command | Purpose | Useful for |
|---|---|---|
| `R0100` | Get Device Details | Top-level device info, slot list, MAC, memory |
| `R0102` | Get Slot Information | Per-slot detail with linkstatus 0–3 (cable/backup states) |
| `R0103` | Get Connector Information | Per-connector signal status |
| `R0155` | Get Receiving Card Information | **Per-card temp/voltage/link** (replaces binary polling) |
| `R0401` | Get Screen Details | Full screen config |
| `R0405` | Get Screen Output Information | Per-output topology + pixel positions |

**Undocumented** (mined from sister `companion-module-novastar-controller` repo):

| Command | Companion module name | Notes |
|---|---|---|
| `R0118` | get_device_init_status | Wait-for-ready gate |
| `R0200` | get_input_list | Input source enumeration |
| `R0226` | get_input_list_simplify | Lightweight input poll |
| `R0300` | get_output_list | All output ports |
| `R0301` | get_output_details | Per-output detail |
| **`R0400`** | **get_screen_list** | **Wall topology — the holy grail** |
| `W0120` | device_heartbeat | Required keepalive |
| `W041A` | DEFAULT_COMMAND | Mystery init kick |
| `W0A00` | get_ipc_input_list | IP camera inputs |
| `W0B03` | get_ndi_input_list | NDI inputs |

**Format:** array of one or more command objects, e.g. `[{"cmd":"R0100","param0":0}]`. Response is JSON in the same shape.

**To get exact request/response shapes for undocumented commands:** check the Companion module source code OR run a fresh Wireshark capture of NovaLCT or Companion hammering them.

### 2.2 · HTTP REST API (port 80)

Web UI uses these endpoints. Auth via `POST /api/user/login` returning a session token. All POST with JSON body `{"deviceId":0}` typically.

| Endpoint | Returns |
|---|---|
| `/api/screen/readAllList` | **Full screen topology** with `screenInterfaces[]` containing per-output `x/y/width/height/isCardOnline/interfaceType` |
| `/api/device/readDetail` | Device health: MAC, memory, fanList, powerList, genlock, slotList |
| `/api/device/readSlot` | Per-slot info |
| `/api/device/readGenLockWorkStatus` | Genlock status |
| `/api/input/readAllList`, `/api/input/groupList`, `/api/input/readSourceList` | Input enumeration |
| `/api/preset/readList`, `/api/preset/groupList` | Preset enumeration |
| `/api/main/initStatus` | Device readiness |
| `/api/main/readVideoServerInfo` | Video server config |
| `/api/network/groupList` | Network groups |

**Connector type codes** (from H-series PDF):

| Code | Type |
|---|---|
| 1 | EXP |
| 2–3 | Single/Dual Link DVI |
| 4–6 | HDMI 1.3/1.4/2.0 |
| 7–8 | DP 1.1/1.2 |
| 9 | 3G-SDI |
| 13 | RJ45 |
| 15–16 | HDBaseT / HDBaseT-4K |
| **17** | **Optical Fiber** ← what OPT outputs report as |
| 18 | 12G-SDI |

### 2.3 · Binary protocol (TCP 5200–5204)

Already implemented in [src/novastar_protocol.py](src/novastar_protocol.py). Key points:

**Checksum (FIXED in commit pending push):**
```
SUM = sum(bytes_between_header_and_checksum) + 0x5555
wire encoding: little-endian (SUM_L SUM_H)
```
The 0x55 0xAA frame header is **not** included in the sum. Verified against worked examples in both VX1000 Control Protocol V1.0 and Sending Card Central Control Protocol V1.3.

**Per-card addressing** (from VX1000 Wireshark captures, used in `build_read_card`):
- `byte[6] = 0x01` — direct-to-receiving-card command marker
- `byte[7] = 0x00`
- `byte[8] = card_index` (0-based)
- `bytes[9–11] = 0x00 0x00 0x00`

**Key registers (reverse-engineered, NOT in any official doc — inferred from NovaLCT captures):**
- `0x0000000A` length 0x5200 — Live monitoring (~82 bytes, includes temp/voltage/link)
- `0x9E000013` — Receiving card config
- `0x00000005` — NSSD device identity
- `0x06000000` — Brightness
- `0x00000002` — Video status (broadcast or per-port)

**H-series-specific binary registers** (also reverse-engineered):
- `H_REG_VIDEO_STATUS` `(0x00000002, 0x0002)` — broadcast video status; byte[31] = port connectivity bitmask
- `H_REG_CARD_DATA_BASE` `0x00400003` — per-card data channel 0 (temperature in byte[0]/2.0)
- 8 channels exist (0x00400003 through 0x004E0003); only channel 0 (temperature) is decoded

**Newly-observed registers from H-series monitoring captures (per-card, per-cycle):**

| Register | Length | Status | Notes |
|---|---|---|---|
| `0x09050002` | 1 B | **DECODED — Fault flag** ✅ | Per-card alarm. `0x00` = no fault. Validated against `Fault and temp readings.pcapng`: all 943 cards returned 0x00 while the NovaLCT GUI showed 0 active faults. Non-zero = fault code (specific values not yet seen — need a fault-trigger capture). |
| `0x00400003` | 512 B | **NOT temperature** ❌ | Originally thought to be per-card temp via `byte[0] / 2.0`. **Wrong.** All 943 cards return constant `0x5D` (= 46.5°C) regardless of actual card state. Existing `parse_h_card_temperature()` in [src/novastar_protocol.py](src/novastar_protocol.py:289) returns this constant and is misleading — needs fixing. Real purpose unknown (board-level reference? calibration? bias offset?). |
| `0x00420003`–`0x004E0003` | 512 B × 7 | unknown | The other 7 H-series per-card data channels. Polled every cycle but no decode yet. May also be constants. |
| `0x0500001B` | 1 B | unknown | Single-byte, polled most aggressively (2838×/cycle) — strong candidate for another status/state flag |
| `0x80070003` | 68 B | unknown | High-bit address space (possibly extended/special addressing) |
| `0x20040014` | 8 B | unknown | |
| `0x04000008` | 4 B | unknown | Paired with `0x04000009` |
| `0x04000009` | 4 B | unknown | Paired with `0x04000008` |

**Confirmed real per-card telemetry source:** the **VX1000-style `0x0000000A` live-monitor register works on H-series too** and is what NovaLCT actually uses for per-card temperature. Range observed: 35–43 °C across 943 cards, exactly matching the NovaLCT MonitorSite V2.6 GUI screenshot. Use `parse_live_monitoring()` (already implemented), NOT `parse_h_card_temperature()`.

**NovaLCT distinguishes three monitoring concepts as separate views.** Our app should mirror this — they are NOT the same thing and should not be conflated:

| Concept | Definition | Granularity | Source register | Decoder | Captured & confirmed |
|---|---|---|---|---|---|
| **Temperature** | Per-card thermal reading | per-card | `0x0000000A` byte[1] / 2.0 | `parse_live_monitoring()['temperature_c']` | ✅ matches NovaLCT MonitorSite GUI exactly (35–43 °C across 943 cards) |
| **Fault / Alarm** | Card-level hardware alarm (temp out of range, voltage anomaly, hardware error) | per-card | `0x09050002` byte[0] | needs new `parse_h_card_fault()` | partially — confirmed 0=OK; non-zero fault codes need a real fault to be triggered |
| **Data break** | Video signal interrupted between cards (cable pull / broken link / fiber disconnect) | per-port + per-card | `0x00000002` byte[31] (port bitmask) **AND** `0x0000000A` byte[12] (per-card link status) | `parse_h_port_bitmask()` + `parse_live_monitoring()['link_status']` | ✅ validated in `Basic Reading with backup.pcapng` — pulling primary cables on chain ports 1+2 changed bitmask from `0xAF` → `0xAC` (bits 0,1 cleared) |

**Voltage** (also from `0x0000000A` byte[3], decoded as `(byte & 0x7F) × 0.1` — see "Voltage formula" below) belongs alongside Temperature as continuous per-card telemetry.

### Three layers of data break detection (all H-series binary protocol)

| Layer | Register | Granularity | What it tells you |
|---|---|---|---|
| **Port (chain) level** | `0x00000002` byte[31] (broadcast read) | 8 bits → 8 ports | Which chains have data flowing |
| **Card path level** | per-card `0x00000002` byte[1] | 7 bits → 7 paths/card | All 7 redundant data paths reaching this card up, or N/7 (degraded) |
| **Card-level link** | per-card `0x0000000A` byte[12] | enum: PRIMARY / BACKUP / DISCONNECTED | Whether this card receives via primary feed, has failed over to backup, or has no signal |

### Other unknown registers (still research targets)

`0x0500001B`, `0x80070003`, `0x20040014`, `0x04000008/9`, and per-card data channels 1–7 may back additional NovaLCT screens we haven't captured yet (input signal status, scan freq, color calibration per card, etc.). Each is a separate decode investigation; none block the MVP dashboard.

**Per-cycle polling load:** NovaLCT issues ~15 distinct register reads per card every refresh cycle (= 14,145 reads for the 943-card wall). For our app to be efficient we should poll fewer registers — only `0x0000000A` (temp/voltage/link) and `0x09050002` (fault flag) are needed for the MVP dashboard. The other 6 unknown registers are research targets but not blocking.

### Code fixes needed — ✅ ALL DONE

| Bug | Status |
|---|---|
| `parse_h_card_temperature()` returns a constant `46.5°C` for all H-series cards | ✅ Kept but hard-deprecated with a blocking banner comment; no caller remains |
| Per-card temp/voltage/link path for H-series uses wrong register | ✅ `_poll_h_port()` reads `0x0000000A` and parses with `parse_live_monitoring()` |
| Per-card fault flag never read | ✅ `H_REG_CARD_FAULT` + `parse_h_card_fault()` implemented and wired in |

### 2.4 · Topology auto-discovery from binary protocol

Broadcast video status register `0x00000002` byte[31] holds **port connectivity bitmask**: bit N = port N+1 connected. Verified across two captures (0xAF normal, 0xAC after disconnecting backup ports). Byte[1] holds per-card link path status (0x7F = 7/7 paths, lower = degraded).

---

## 3 · What's implemented (this branch)

### Files modified or created

| File | Status | Purpose |
|---|---|---|
| [src/novastar_protocol.py](src/novastar_protocol.py) | modified | Binary codec. Checksum fixed; **§6.5 byte7/byte8 addressing**; bit-error register; dead helpers pruned |
| [src/h_series_json.py](src/h_series_json.py) | new | JSON UDP client (port 6000). Batching, persistent socket, per-class timeouts, isolated heartbeat |
| [src/device_manager.py](src/device_manager.py) | modified | Chain-aware polling, batched card reads, per-device stop events, double-buffered state |
| [src/app.py](src/app.py) | modified | **Per-card alerting with tiering**, freshness on `/api/wall_live`, atomic writes, IP validation, no import-time init |
| [src/enumerate_wall.py](src/enumerate_wall.py) | new | CLI that builds `wall_live_snapshot.json`. Refuses to send a packet without `--yes-contact-hardware` |
| [src/wall_config.py](src/wall_config.py) | new | Operator-authored **physical** wall map (loader/merger/validator, atomic save). See its docstring — deliberately kept, UI still missing |
| [src/static/js/wall_view.js](src/static/js/wall_view.js) | new | Live device tree. Dead SVG path removed (962 → ~430 lines), IIFE-scoped, escaped |
| [src/static/js/app.js](src/static/js/app.js) | modified | IIFE-scoped, XSS fixed, delegated handlers, real peak temp, gap-aware charts |

### Test status: **434 tests, all passing** (`python3 -m pytest tests/ -q`)

Suites: `test_protocol`, `test_h_series_json`, `test_device_manager`, `test_app`,
`test_wall_config`, `test_enumerate_wall`, `test_demo_device`, `test_version`.
No test opens a socket to real hardware; importing `app` starts no poller.

**Frontend has no automated tests** — the JS was verified by static checks and by
replaying the real snapshot through the grouping logic. That remains a gap.

---

## 4 · What's pending / next steps

### ✅ Done (do not redo — verify against source before re-planning)

- `src/h_series_json.py` **built**, and since extended with batching, a
  persistent socket, per-call-class timeouts and an isolated heartbeat socket.
- JSON UDP **is** the primary H-series path, binary is the automatic fallback.
- **Wall view UI shipped** — but as a live device tree (sender card → OPT →
  port → chain), not the SVG/pillar renderer originally sketched here. The SVG
  path was built, found unreachable, and deleted.
- **Live status overlay shipped** — colour modes Temperature / Power / Online /
  Bit Errors, driven by real per-card readings.
- **OPT-level grouping shipped** (as columns in the device tree, not pills).
- **Per-card alerting shipped** with wall → chain → card tiering.

### Immediate (blocked on hardware access)

1. **Verify the §6.6 `card_id` offset hypothesis** by running
   `src/enumerate_wall.py <ip> --transport binary --yes-contact-hardware` and
   comparing the total against the operator's known panel count. This is the
   single highest-value open question — it decides whether ~120 cards are real
   panels the JSON path can't see.
2. **Verify `enumerate_wall.py`'s `ASSUMPTIONS` block** (sender-card → TCP port
   ordering, card position vs `recvCardId`, presence semantics).
3. **Vendor `socket.io.min.js` 4.7.5** into `src/static/js/` from an
   internet-connected machine and repoint the tag in `index.html`. Until then
   an isolated show network loses live push (the REST fallback covers it).

### Short-term

4. **Bridge physical ↔ logical** — `wall_config.py` holds the operator's pillar
   map keyed by port 1–15; the live path is keyed `(slot, port, card_id)`. The
   config's `opt` field is the hook. Without this bridge an alert says
   "slot 20 · port 4 · card 37" when the operator needs "SL pillar, third row".
5. **Drag-drop pillar editor** writing `wall_layout.json` (the endpoints and
   validators already exist and are tested; only the UI is missing).
6. **Frontend tests** — there are none. `drawChart`'s gap handling, the
   freshness badge, and the colour-mode logic are all untested.
7. **Failure tracing** — when a mid-chain card dies, dim everything downstream
   on the same chain (its data physically routes through the dead card).
8. **Calibrate `BIT_ERROR_WARNING`** (currently 100, a reasoned estimate) against
   a real healthy-wall baseline. Keep `BER_ALERT` in `wall_view.js` equal to it.

### Medium-term

9. **HTTP REST client** for the port-80 API — screen list + device detail.
10. **G4A UDP 5600 discovery** — "Find devices on network" button.
11. **Decode the remaining registers** — `0x0500001B`, `0x80070003`,
    `0x20040014`, `0x04000008/9`, per-card data channels 1–7.

### Longer-term

13. **CVT monitoring** — CVTs (fiber→ethernet converters between H-series and panels) are NOT on the IP network, only direct fiber. The H-series is our only window to CVT health: OPT link state = fiber up, OPT sub-port active = CVT receiving and rebroadcasting. **No separate CVT protocol client is needed.**
14. **Multi-device support** — fleet may include several H-series + other models (VX1000, etc).
15. **Inter-device backup detection** — primary/backup card states.

---

## 5 · Key files for cold-start orientation

In rough priority order, files to read first when picking up this work:

1. [src/wall_config_default.json](src/wall_config_default.json) — the wall topology (pillars, chains, card counts)
2. [src/wall_config.py](src/wall_config.py) — loader/merger/validator + `render_wall()` semantics
3. [src/novastar_protocol.py](src/novastar_protocol.py) — protocol codec, look at `checksum()` and `build_read_card()` first
4. [src/device_manager.py](src/device_manager.py) — `_poll_h_series()` and `_poll_h_port()` show the polling loop
5. [tests/test_protocol.py](tests/test_protocol.py) `TestChecksum` class — golden bytes from VX1000 + Sending Card docs proving the checksum is correct
6. This document
7. [docs/VX1000_Protocol_Analysis.md](docs/VX1000_Protocol_Analysis.md) — older analysis from initial reverse-engineering

---

## 6 · Reference materials

### Protocol PDFs
- `/Users/mattknotts/Nextcloud/LED/LED Wall Tech/Manuals/Novastar/Control Protocols/H Series/H Series Video Wall Splicers Control Protocol V1.0.19.pdf` — H-series JSON UDP protocol spec (R0100, R0155, R0405, etc.)
- `/Users/mattknotts/Downloads/NovaStar_Controller_Complete_Context.pdf` — 207-page consolidated Companion module dev context covering 10 device families. **Section 4 (pp. 19-208) has all the protocol docs.** Read VX1000 (pp. 111-130) and Sending Card Central Control (pp. 53-62) for binary protocol confirmation.

### Wireshark captures (in `/Users/mattknotts/Documents/`)
- `H series Monitioring Basic Reading.pcapng` — original H-series capture
- `H series Monitioring Basic Reading with backup.pcapng` — same after disconnecting backup ports (validated port bitmask byte[31])
- `H series Monitioring Basic Reading and send rcfgx.pcapng` — includes UDP 6000 R0100 sample + (truncated) rcfgx push attempt
- `H series Monitioring webui.pcapng` — web UI HTTP API endpoints captured
- `H series Monitioring Monitor Refresh.pcapng` — full binary 5203 monitoring sweep (57k frames, 20 MB). Single device (.10), per-card reads with byte[5]=0, card_index range 0–91. Reveals 6 undecoded register addresses being polled per card per cycle (see binary-registers section).

### Wall reference image
- `/Users/mattknotts/Documents/` includes a wall-layout PNG showing all 7 pillars with chain entry/exit markers (green A1–A15 dots = data entry, red B1–B15 dots = chain terminus) and serpentine arrows. Use this as the visual ground truth for the wall topology.

### Sister project (READ-ONLY reference for undocumented commands)
- `companion-module-novastar-controller` — Bitfocus Companion module by user. Source of the 10 undocumented R0xxx/W0xxx commands. Do NOT modify; this is a separate project.

---

## 6.5 · CORRECTED binary per-card addressing (the key to accurate enumeration)

**This supersedes all earlier addressing assumptions.** Decoded from
`H series Bit errors detection.pcapng` + `H series More.pcapng` (a single-card
H2 test rig that should report exactly 245 panels — and does, with this model).

### Sender-card selection — CORRECTED 2026-08-09 (byte[5], not the TCP port)

The device answers a `"rqProMI:"` broadcast on UDP 3800 with:

```
rpProMI:App,0161 H_SUB_CARD@^^@5201 H_SUB_CARD@^^@5202 H_SUB_CARD@^^@5203
```

It is tempting to read that as one TCP service per sender card. **It is not.**
Tested against a live two-sender H15: 5202/5203/5204 accept the connection and
then answer *every* read with an **empty payload**. Enumerating them as
separate services is why sender cards 2+ came back with zero cards on every
chain.

The sender card is selected by **byte[5] of the read frame**, over the one
connection to **TCP 5201**:

```
tcp 5201  byte[5]=0  chain 0 card 0  →  05 00 00   sender card 1 (slot 20, fw V4.5.1.81)
tcp 5201  byte[5]=1  chain 0 card 0  →  05 03 00   sender card 2 (slot 22, fw V4.8.1.4)
tcp 5201  byte[5]=2  chain 0 card 0  →  a7 56 00   absent
tcp 5201  byte[5]=3  chain 0 card 0  →  a8 56 00   absent
```

Same address, different sender card, different card behind it — the second one
carrying 3 bit errors while the first has none. byte[5] is therefore the
**sender card index**, not the "OPT group" the earlier table called it. The OPT
group is already implied by the chain (0–7 = OPT 1, 8–15 = OPT 2), so it never
needed a byte of its own. Every capture that read byte[5] as a constant 0x00
came from the single-sender H2 rig, where it is.

`--sender-cards N` numbers are 1-based; the wire byte is `N-1`.

### Sender card numbering: derive it from the SLOT, never from discovery

```
card_number = (slot - 20) / 2 + 1        byte[5] = card_number - 1
```

Output cards occupy every second chassis slot from 20, so slots 20, 22, 28, 30
are cards **1, 2, 5, 6** — which is exactly what the operator calls them.

The `rqProMI` reply advertises services on 5201-5204. Reading those as "cards
1, 2, 3, 4" is wrong on any chassis with backups in higher slots, and it is a
silent wrongness: `enumerate_wall.py` scanned `byte[5]` 0-3 and **never probed
cards 5 and 6 at all**. An idle backup answers nothing either way, so the
result looked correct — right up until a failover, which is the exact moment
the data matters. Take the card numbers from the controller's slot list.

### What a data break actually looks like (operator-verified, cable pulled)

The operator pulled the cable at panel 12 of a 22-panel chain (sender card 1,
OPT 1 port 4) twice. It produced **two completely different signatures**, and a
monitoring tool has to recognise both.

**A — the backup is carrying the tail.** Every panel still answers. Probing
the primary and the backup separately shows the chain split at the break:

```
card 1 (slot 20, PRIMARY)  panels 1-22:  PPPPPPPPPPP...........
card 5 (slot 28, BACKUP)   panels 1-22:  ...........PPPPPPPPPPP   <- 5 bit errors each
```

The primary feeds up to the break, the backup feeds from the far end, and
together they cover all 22 — which is why the wall stays lit and why **"all
cards online" is not evidence of a healthy wall**. The backup's panels carry a
non-zero bit-error count; the primary's are clean.

Probed through the primary alone, the same fault reads as every panel present
with errors from 12 onward:

```
panels  1-11 : 0        panel 12 : 2        panels 13-22 : 2
```

**B — nothing is carrying it.** The chain simply stops:

```
panels  1-11 : present, 0 errors        panels 12-22 : no answer at all
```

Signature B is only distinguishable from an empty chain — or from a throttled
controller (§6.6) — **against the known inventory**. A control chain probed in
the same pass still answering its full length is what rules out the controller
having stopped talking to us.

In both signatures the **first affected panel is the break point**: errors
propagate downstream because each card repeats to the next. A single card with
errors and clean cards after it is one bad card, not a break, and reporting it
as one sends someone to the wrong end of a cable run.

Implemented in `NovaStar_Device.detect_chain_breaks`.

### Clearing the bit-error counters (the one write this app makes)

Captured from NovaLCT in `Bit error 4x clear erros.pcapng`, where the operator
clicked clear four times and NovaLCT sent four frames identical but for the
sequence number:

```
55 aa 00 ca fe ff 01 ff ff ff 01 00 76 00 00 01 01 00 05 98 5b
```

Register `0x76000001`, one payload byte `0x05`, broadcast (`byte[5] = 0xFF`,
target `FF FF FF`) so it clears every card on every chain of every sender card
at once. The register appears nowhere else in the 434 request frames of that
capture. `build_clear_bit_errors()` reproduces all five captured frames
byte-for-byte.

The counter is cumulative and this is the only known way to reset it, so
clearing discards evidence of an intermittent link for whoever looks next.
`set_bit_error_baseline()` is the non-destructive alternative: it zeroes the
displayed number and leaves the hardware counter alone.

### Sender slots: R0405 is right, R0155 only answers for the active ones

R0405 on the H15 lists slots **20, 22, 28, 30**, and all four are sender cards
— **two primary and two backup** (operator-confirmed). What differs is which
ones answer:

| Slot | R0155 | Binary per-card read | Role |
|---|---|---|---|
| 20 | answers | 250 cards across 7 chains | primary |
| 22 | answers | 36 cards across 2 chains | primary |
| 28 | silent | `byte[5]=2` → absent | backup |
| 30 | silent | `byte[5]=3` → absent | backup |
| 21, 23 | `ack: "Error"` | — | not slots |

A backup answers nothing on either protocol until it takes over, so an
enumeration legitimately finds cards behind only two of the four. **Do not read
that as "only two sender cards exist"** — an earlier version of this document
said exactly that, and it is wrong.

Which of the four is backing up which has **not** been established from the
device; the pairing above is inferred from slot order alone. The controller
does not appear to expose a primary/backup flag anywhere we have looked, which
is the subject of a question to NovaStar.

Practical consequence for the dashboard: "sender cards installed" (4, from
R0405) and "sender cards carrying panels" (2, from the inventory) are different
numbers and both are worth showing. A drop in the second without a change in
the first is a failover or a dead fibre.

### Per-card request frame layout (20-byte read)

```
offset 0-1   55 AA            header
offset 2-3   seq (BE)
offset 4     FE               device (broadcast)
offset 5     SENDER CARD      ← byte5: 0-based sender card index
offset 6     01               per-card marker (00 = broadcast read)
offset 7     CHAIN index      ← byte7: which chain/sub-port (0..15)
offset 8     CARD index       ← byte8: card position within that chain (0..N)
offset 9     card index high? (0x00 in all observed)
offset 10-11 00 00
offset 12-15 register (BE)
offset 16-17 length (BE, NovaStar split encoding)
offset 18-19 checksum (LE, sum(body)+0x5555)
```

**THE KEY FIX:** per-card address is **`byte7` = chain index, `byte8` = card
position** — NOT `byte5`/`byte8` as earlier code assumed, and NOT the JSON UDP
`R0155` `(slot, port, card)` which is a limited wrapper that caps out early and
silently returns null for cards it can't reach.

### Verified: single-card H2 = 245 panels across 16 chains

From `Bit errors detection.pcapng`, counting distinct `(byte7, byte8)`:

| byte7 (chain) | panels | | byte7 | panels |
|---|---|---|---|---|
| 0 | 14 | | 8 | 16 |
| 1 | 28 | | 9 | 7 |
| 2 | 14 | | 10 | 7 |
| 3 | **49** | | 11 | 7 |
| 4 | 14 | | 12 | 7 |
| 5 | 28 | | 13 | 7 |
| 6 | 16 | | 14 | 7 |
| 7 | 16 | | 15 | 8 |

**Total = 245** ✓ (matches operator's known panel count exactly).

NovaLCT probes one card *past* each chain's end to find the boundary, and
sends a single probe to empty chains 16-31 — those extra responses are NOT
real panels (the `More.pcapng` apparent "277" is 245 real + boundary/empty probes).

### Bit-error register `0x4A010002`

Per-card, 3-byte response:

| byte | meaning |
|---|---|
| 0 | status (`0x05` = card present/responding) |
| 1-2 | **bit error count**, uint16 little-endian (0–65535; `0xFFFF` = saturated = serious data integrity fault) |

Polled ~10× more often than other registers (it's the continuous integrity check).
Only reachable on the binary TCP path, no JSON UDP equivalent.

### Disconnect detection

Compare live per-chain panel count vs a known-good baseline. A drop = disconnect.
Terminal-panel loss shortens the chain (e.g. 50→49 contiguous); a mid-chain
failure truncates discovery downstream of the break.

### Two distinct devices in the captures (don't conflate)

- **COSMIC MEADOW**: 3 sender cards (TCP 5201/5202/5203), ~1548 panels total
- **H2 test rig** (`Bit errors detection` / `More`): 1 sender card, 245 panels, 16 chains

---

## 6.6 · Later corrections (supersede anything earlier that conflicts)

### Register `0x0000000A` is NOT per-card on H-series — never enumerate with it

On VX1000 it is the live-monitoring register. On H-series it answers for
**every** `(chain, card)` address, occupied or not, with a free-running
counter — successive probes return `6f 56 00…`, `75 56 00…`, `76 56 00…`
regardless of which address was asked about. The absent marker is
distinguishable only against the bit-error register:

| Address | `0x4A010002` (biterr) | `0x0000000A` (live) |
|---|---|---|
| chain 6 card 21 (real card) | `05 00 00` present | `?? 56 00` |
| chain 6 card 22 (empty) | `d7 56 00` **absent** | `?? 56 00` |
| chain 15 card 0 (empty chain) | `b4 56 00` **absent** | `?? 56 00` |

Because "returned a well-formed payload" is the only presence test `0x0000000A`
supports, a walk using it never finds a boundary — the only thing that ends a
chain is a **timeout**, so the reported chain length measures controller load.
That produced 7, then 9, then 22 cards on the same 22-card chain across three
runs. `enumerate_wall.py` now refuses `--probe-register live` outright.

Use `0x4A010002`: `byte[0] == 0x05` means present, anything else absent.

### Register `0x0000000A` IS per-card — earlier entry in this doc was wrong

An earlier revision of §6.6 said this register "answers for every address with
a free-running counter" and should never be used for enumeration. That was a
mistake in the probe, not in the device: the read was issued with length
`0x0010`, which `decode_length` expands to **4096 bytes**, so every reply was
misframed and the bytes being examined were garbage. The "counter" was the low
bits of a status byte read at the wrong offset.

Read correctly (length `0x5200` = 82 bytes) it is the most useful register on
the device. One read per card returns:

| Offset | Meaning |
|---|---|
| `byte[0]` | presence — `0x80` present, bit `0x40` set = nothing at this address |
| `byte[1] / 2` | temperature °C (units of 0.5 °C — vendor §4.3.4) |
| `(byte[3] & 0x7F) × 0.1` | voltage (lower 7 bits, units of 0.1 V — vendor §4.3.4 / §5.4.2) |
| `byte[12]` | link status |

Verified twice, independently:

- **NovaLCT capture** (`Monitoring.pcapng`): NovaLCT polls this register once
  for each of the wall's 286 cards, and `byte[1]/2` reproduces the operator's
  stated 36–43 °C across all of them (7 cards at 36, 4 at 43, peak at 38).
- **Live hardware**, on a chain known to hold exactly 22 panels: cards 0–21
  answered `0x80`; cards 22–25 answered `0xC0/0xE0/0xE2/0xE4/0xE6`.

**The trap that caused the original misreading:** an absent address still
returns a well-formed 82-byte payload carrying the PREVIOUS card's temperature
and voltage. "The device answered" is therefore not a presence test, and any
decoder that skips the byte[0] mask invents a plausible panel for every empty
address on the wall. Mask with `(b0 & 0xC0) == 0x80`.

Practical consequence: this is now the default enumeration register and the
app's whole-wall read. R0155 answers roughly 150 cards before the controller
stops (§6.6), which left 250 of 286 panels with no readings; the binary
register covers every card in one pass and yields link status as well.

### `byte[12]` link status: 1 and 11 both mean working

The VX1000 mapping is 1 = PRIMARY, 2 = BACKUP. On H-series `byte[12]` also
takes other values: the NovaLCT capture of a healthy 286-card wall showed 1 on
162 cards and **11 on the other 124**, and whole chains that are working
normally report 11. Mapping "anything else" to DISCONNECTED therefore labelled
124 healthy panels as disconnected. Only `0` is treated as disconnected now;
anything unrecognised is `UNKNOWN`. What 11 actually means is undecoded.

### `powerNStatus` non-zero does NOT mean a failed supply

`0 = healthy` held up. `non-zero = failed` did not. Fifteen cards reported
`power0Status: 1` **and** `power1Status: 1` while simultaneously reporting
41–42 °C and 4.0–4.1 V over R0155. A card cannot measure and transmit its own
temperature through a failed primary supply — it is powered and talking. Read
as "both supplies failed", it raised a warning every polling cycle on a lit,
healthy wall.

Most likely it means a supply that is not fitted or not monitored on that
panel model; panels with a single PSU still have two status fields. Until
NovaStar confirms it, non-zero is reported as **unknown**, never as a fault,
and the raw values are kept for later.

### Silence is not absence — two separate causes, both truncated the wall

An enumeration walk ends a chain at the first address the device says is
empty. On this hardware "says is empty" and "says nothing" are easy to
conflate, and both of the following silently under-reported the wall. Neither
fails loudly; both produce plausible numbers.

**Cause 1 — an empty address answers more slowly than an occupied one.**
The controller has to wait out its own read to a card that isn't there. At a
0.5 s socket timeout **every chain on the wall ended on a timeout** rather than
on the device's answer; at 1.5 s they end on a real answer. `enumerate_wall.py`
now defaults `DEFAULT_PROBE_TIMEOUT = 1.5`.

**Cause 2 — the controller degrades over a long sweep.** After an unbatched
R0155 sweep (~3000 requests) it stopped answering R0155 **entirely**, and
recovered only after ~40–50 s of quiet:

```
t+0s silent   t+10s silent   t+20s silent   t+30s silent   t+40s ok   t+50s ok
```

The binary path degrades the same way, progressively rather than all at once.
Chain 6 of the test wall has 22 cards. Probed on a rested device it reads 22
every time. Reached ~200 probes into a full sweep it read 7, then 9, then —
once pacing and rest-and-retry were added — 21:

| Conditions | chain 6 reads |
|---|---|
| rested, single chain | **22** (correct) |
| full sweep, 0.5 s timeout, no pacing | 7, then 9 |
| full sweep, paced 0.15 s, 1.5 s timeout, 2 × 10 s rests | 21 |

Mitigations now in `enumerate_wall.py`: `--pace` (0.15 s after every probe),
`--silence-rests` / `--rest-seconds` (pause and re-ask before accepting
silence), and retries that stop as soon as the device answers *anything* so a
real boundary costs one probe rather than three. Where silence still wins, the
count is reported as an explicit **LOWER BOUND** rather than as fact.

This also means any *monitoring* poll loop has to stay well clear of that
budget: sustained per-card binary polling is not viable, which is the case for
SNMP (a full device picture in 0.8 s) as the primary transport.

### Voltage formula — settled by the vendor document

**Correct: `(raw & 0x7F) * 0.1`.** Not `raw * 0.03`.

NovaStar's *H Series Video Wall Splicers Control Protocol* states the encoding
outright, in §4.3.4 and §5.4.2, with identical wording in V1.0.18 and V1.0.20:

> The lower 7 bits represent the voltage value, in units of 0.1V. For instance,
> a value of 172 indicates a voltage of 4.4V.

172 & 0x7F = 44 → 4.4 V. The same section gives the temperature worked example
that this project already matched: "a value of 104 represents a temperature of
52°C", i.e. units of 0.5 °C. Same encoding as binary register `0x0000000A`
byte[3] (`parse_voltage()` in `novastar_protocol.py`).

**This section previously said the opposite,** and the reasoning it gave is
worth keeping visible because it was a plausible-looking mistake:

> Evidence from the 1374-card snapshot: raw values span 165–173.
> `(raw & 0x7F) / 10` → 3.7–4.5 V, every card below the app's own 4.7 V alarm.
> `raw * 0.03` → 4.95–5.19 V, a healthy 5 V rail.

Two things are wrong with that. First, the arithmetic conclusion — 165 & 0x7F =
37 → **3.7 V** and 173 & 0x7F = 45 → **4.5 V** — is correct, and it is exactly
what `src/wall_live_snapshot.json.cosmic-meadow-backup` actually stores: 1374
cards spanning 3.7–4.5 V, with 1253 of them at 4.2 or 4.3 V. The 4.95–5.19 V
figure was never observed; it was produced by the formula, not measured.

Second, "a healthy 5 V rail" was an assumption, not a finding. **These receiving
cards run at roughly 4.2 V.** The 4.7 V alarm was above their entire normal
range, so it was guaranteed to fire once per card per cycle no matter which
formula was used — the alerts were evidence about the *threshold*, and were
misread as evidence about the *decode*. The floor is now `DEFAULT_VOLTAGE_MIN =
3.8` in `app.py` (`LEGACY_VOLTAGE_MIN = 4.7` survives only to migrate old
settings files off it).

Two independent confirmations beyond the vendor text:

- **Cross-schema agreement.** The centi-schema firmware reports `volt` 410–440
  → 4.10–4.40 V. On the same chain, byte-schema cards decode to 4.2 V masked
  and 5.10 V unmasked. Only the masked form agrees.
- **Bit 7 is never clear in the data**, which is what made the unmasked form
  look self-consistent: every raw value simply came out 12.8 V too high, and
  uniformly enough to pass for a rail.

Decoded by `h_series_json.decode_voltage_byte()` via `parse_receiving_card()`;
`device_manager` no longer duplicates it. Pinned by the vendor worked examples
in `tests/test_protocol.py::TestVendorWorkedExamples` and
`tests/test_h_series_json.py::TestByteDecoders`.

### Bit-error register `0x4A010002` — implemented

3-byte response: byte0 = status (`0x05` = card present), bytes 1–2 = **uint16
little-endian** error count, `0xFFFF` = saturated. Cumulative since card
power-on, so single-digit totals are ordinary cable noise on a long chain.

Alert threshold **100** — `BIT_ERROR_WARNING` in `app.py`, mirrored as
`BER_ALERT` in `wall_view.js`. **Keep those equal**: a red cell that raises no
alert (or vice versa) teaches the operator to distrust one of the two. The
threshold is a reasoned estimate, not a measured baseline — see §4 item 8.

Binary path only; there is no JSON UDP equivalent, so a JSON-only install gets
power alerts but not bit-error alerts. Protocol limitation, not a bug.

### ⚠️ HYPOTHESIS — JSON `R0155` under-reports; `recvCardId` ≠ card position

**Not verified against hardware. Strong evidence, but treat as a hypothesis.**

In the captured 1374-card snapshot, **24 of 27 chains start at `card_id` 5**,
never 0. Only port 0 of each sender card starts at 0. Every chain is otherwise
perfectly contiguous:

| Sender card | port 0 | ports 1+ |
|---|---|---|
| 1 | starts at 0 | all start at **5** |
| 2 | starts at 0 | start at **5** (one at 6, one at 7) |
| 3 | starts at 0 | all start at **5** |

Panels physically absent from positions 0–4 on 24 separate chains is not
plausible. The likeliest reading is that **the JSON `recvCardId` is not the same
index as the binary card position** — R0155 refuses the low addresses.

That accounts for roughly **120** of the ~174 missing cards (1374 enumerated vs
1548 known). It is also consistent with the independently-observed fact that
R0155 silently returns nothing for cards it cannot reach.

**How to settle it:** `src/enumerate_wall.py <ip> --transport binary
--yes-contact-hardware`. The binary path uses §6.5 addressing and does not
depend on `recvCardId` at all. Compare its total to the known panel count.

### Two different walls appear in these captures — do not conflate

- **943 panels / 15 chains / one sender card** — the 7-pillar A1–A15 venue wall
  in §1. Every `H series Monitioring*.pcapng` capture is this, on TCP 5203 only.
- **COSMIC MEADOW, ~1548 panels / 3 sender cards** (9 + 10 + 9 ports) — the
  later config. No capture covers it: all of them touch 5203 alone, so they can
  only ever show one card's worth.
- **245 panels / 16 chains / one sender card** — the H2 test rig in the
  `Bit errors detection` / `More` captures. This is the one that *verifies*
  §6.5, since counting distinct `(byte7, byte8)` pairs reproduces 245 exactly.

---

## 7 · User preferences (do not violate)

- **Git:** All work on `dev/draft` branch (or worktree branch like `claude/modest-torvalds`). PRs target `dev/draft`. Main only receives merged PRs.
- **Commit attribution:** Always commit as kman1898 with `60245031+kman1898@users.noreply.github.com`. **Never include Co-Authored-By Claude lines or any AI attribution.** Use `git -c user.name=... -c user.email=...` to override per-commit.
- **Brevity:** User is action-oriented. Tight responses, no preamble, no end-of-turn summaries unless asked.
- **No emojis** in code or commits unless explicitly requested.
- **No screenshots** unless absolutely needed for visual verification (image accumulation hits Claude Code's per-session limit fast). When verification is needed, ask for "one screenshot only."
- **This is a monitoring app, not control.** Brightness and other write/control concerns are handled by the separate Companion module — out of scope here.
