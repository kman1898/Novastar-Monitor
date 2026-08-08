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
> **byte[7] = chain index, byte[8] = card position**, with one TCP connection
> per sender card. §6.5 supersedes this table; it is kept only because the
> JSON row is still accurate and because the wrong model appears in old
> captures and old code.

| Form | How slot/port/card are encoded |
|---|---|
| **JSON UDP `R0155`** | `param0=slotId`, `param1=portId`, `param2=cardId_low_byte`, `param3=cardId_high_byte` (16-bit card_id split low/high). **Caveat:** this path silently under-reports — see §6.6. |
| **Binary TCP** | ~~`byte[5]=port` … `byte[8]=card_index`~~ **SUPERSEDED — see §6.5.** Correct: `byte[7]=chain`, `byte[8]=card position`, one TCP connection per sender card (5201/5202/5203). Implemented in `build_read_card()` in [src/novastar_protocol.py](src/novastar_protocol.py). |

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

**Voltage** (also from `0x0000000A` byte[3] × 0.03) belongs alongside Temperature as continuous per-card telemetry.

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

### Sender-card → TCP port mapping (from UDP 3800 `rqProMI:` discovery)

The device answers a `"rqProMI:"` broadcast on UDP 3800 with:

```
rpProMI:App,0161 H_SUB_CARD@^^@5201 H_SUB_CARD@^^@5202 H_SUB_CARD@^^@5203
```

→ Each **sender card is its own TCP service**:

| TCP port | Target |
|---|---|
| 5200 | main controller / broadcast |
| 5201 | sender card 1 (`H_SUB_CARD`) |
| 5202 | sender card 2 |
| 5203 | sender card 3 |

To enumerate a multi-card chassis (e.g. COSMIC MEADOW's 3 cards), open a
separate TCP connection to **each** of 5201/5202/5203.

### Per-card request frame layout (20-byte read)

```
offset 0-1   55 AA            header
offset 2-3   seq (BE)
offset 4     FE               device (broadcast)
offset 5     OPT group        usually 0x00 (rarely used)
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

### Voltage formula — was wrong, caused real false alarms

**Correct: `raw * 0.03`.** Not `(raw & 0x7F) / 10`.

Same encoding as binary register `0x0000000A` byte[3] (`parse_voltage()` in
`novastar_protocol.py`, documented in `docs/VX1000_Protocol_Analysis.md`).

Evidence from the 1374-card snapshot: raw values span **165–173**.

| Formula | Result | Verdict |
|---|---|---|
| `(raw & 0x7F) / 10` | 3.7–4.5 V | **every card** below the app's own 4.7 V alarm |
| `raw * 0.03` | 4.95–5.19 V | a healthy 5 V rail |

The masked form silently subtracts 12.8 units (the high bit is never clear in
the data). It produced a stream of `Voltage 4.25V below minimum threshold of
4.7V` alerts against a completely healthy wall. Decoded by
`h_series_json.parse_receiving_card()`; `device_manager` no longer duplicates it.

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
