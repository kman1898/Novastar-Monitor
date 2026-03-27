# NovaStar VX1000 Protocol Analysis
## Decoded from Wireshark captures — March 27, 2026

### Connection Details
- **Device:** NovaStar VX1000 at 192.168.0.10
- **PC:** 192.168.0.8 running NovaLCT
- **TCP Port:** 5200 (binary protocol)
- **UDP Port:** 5600 (device discovery broadcast)

---

## 1. Frame Structure (TCP Port 5200)

### READ Request (20 bytes)
```
Offset  Size  Field           Example     Notes
──────  ────  ──────────────  ──────────  ──────────────────────────
0-1     2     Header          55 AA       Magic bytes (request)
2-3     2     Sequence        00 6C       Incrementing sequence number
4       1     Device Address  FE          0xFE = broadcast/sending card
5       1     Port Index      00          0x00=global, 0x01=port 1
6-11    6     Reserved        00 00 00    Always zeros for reads
                              00 00 00
12-15   4     Register Addr   02 00 00 00 Target register to read
16-17   2     Read Length     02 00       Bytes to read
18-19   2     Checksum        C3 56       Frame checksum
```

### READ Response
```
Offset  Size  Field           Example     Notes
──────  ────  ──────────────  ──────────  ──────────────────────────
0-1     2     Header          AA 55       Magic bytes (response, reversed)
2-3     2     Sequence        00 6C       Matches request sequence
4       1     Ack/Status      00          0x00 = success
5       1     Device Address  FE          Echo of request
6-11    6     Reserved        00 00 00    Mirrors request
                              00 00 00
12-15   4     Register Addr   02 00 00 00 Echo of register
16-17   2     Data Length     02 00       Echo of length
18-N    var   Payload         0C 62       Actual data
N+1-2   2     Checksum        31 57       Frame checksum
```

### WRITE Command (20 + payload bytes)
```
Offset  Size  Field           Example     Notes
──────  ────  ──────────────  ──────────  ──────────────────────────
0-1     2     Header          55 AA       Magic bytes
2-3     2     Sequence        00 94       Sequence number
4       1     Device Address  FE          Sending card
5       1     Port Index      00          Target port
6       1     Command         01          0x01 = write to receiving cards
7-9     3     Target Address  FF FF FF    0xFFFFFF=broadcast, 0x00FFFF=port-specific
10      1     Port            01          Receiving card port (1-based)
11      1     Reserved        00
12-15   4     Register Addr   E3 01 00 02 Receiving card register
16-17   2     Data Length     04 00       Payload size
18-N    var   Payload         F0 F0 F0 00 Data to write
N+1-2   2     Checksum        A1 5D       Frame checksum
```

---

## 2. UDP Discovery (Port 5600)

Broadcast to 255.255.255.255 every ~1 second.
```
Header: "NOVA" (4 bytes: 4E 4F 56 41)
Body:   JSON payload
```
```json
{"searchInfo":[],"advSearchInfo":{"brand":{},"serial":{},"modelId":{}}}
```
Both the PC (192.168.0.8) and another device (192.168.0.9) broadcast discovery packets.

---

## 3. Register Map — Sending Card (Reads)

### Core Polling Registers (read every cycle)

| Register     | Len  | Description                | Data Example              |
|------------- |------|----------------------------|---------------------------|
| `0x02000000` | 2    | Protocol/firmware version  | `0C 62` (3170 decimal)    |
| `0x06000000` | 1    | **Brightness** (0-255)     | `A8` = 168 = 65.9%        |
| `0x07000000` | 2    | Gamma/display mode         | `00 08`                   |
| `0x16000000` | 8    | **Date/Time**              | `15 09 06 00 0D C7 2D 00` |
| `0x00000014` | 88   | Unknown (all zeros)        | zeros                     |
| `0x00000005` | 256  | **Device Info (Port 1)**   | "NSSD" + device data      |
| `0x00010005` | 256  | Device Info (Port 2)       | zeros (no device on port 2)|
| `0x00000000` | 256  | **System Info**            | See below                 |

### System Info Register (0x00000000) — Decoded
```
Offset  Data              Meaning
──────  ────────────────  ─────────────────────────────
0-1     00 00             Reserved
2       14                Device type (0x14 = 20)
3       11                Hardware version (0x11 = 17)
4       02                Number of Ethernet ports
5       00                Reserved
6       A8                Current brightness (168/255)
7       00                Reserved
8       04                Number of inputs
9-11    00 00 01          Flags
16-17   00 00             Reserved
22      15                Build year (0x15 = 21 → 2021)
23      09                Build month (September)
24      06                Build day (6th)
25      00                Reserved
26-27   0D C7             Build number? (3527)
28      2D                Additional version (45)
30      55                Device signature (0x55 = 'U')
62-65   93 19 8A B6       Hardware serial / UUID
66-67   0F 75             Additional ID
```

### Device Info Register (0x00000005) — "NSSD" Packet
```
Offset  Data        Meaning
──────  ──────────  ──────────────────
0-3     4E535344    "NSSD" = NovaStar Sending Device
4-5     F3 58       Device model code
6-9     E9 03 07 00 Serial / unique ID
10      1C          Hardware revision
11      56          Firmware version byte
12-14   90 00 00 00 Status flags
15-17   B1 00 00 00 Feature flags  
18-19   04 9F       Receiving card capacity
```

---

## 4. Receiving Card Status Register (0x0000000a) — LIVE MONITORING

This is the key register for monitoring. Polled repeatedly, with **values that change each read** (bytes 1 and 3 fluctuate):

```
Offset  Sample Values   Meaning (hypothesized)
──────  ──────────────  ──────────────────────────────
0       80              Status byte (0x80 = online/connected)
1       6C-76           **TEMPERATURE** (fluctuates: 108-118 → likely raw ADC)
2       00              Reserved
3       AC-AE           **VOLTAGE** (fluctuates: 172-174 → raw ADC)
4-10    00 00 00 00     Reserved / additional status
        00 00 00
11      0D              Receiving card count or port status
12      01              Link status (01 = primary)
13      01              Connection type
14-15   02 10           Firmware version (2.16)
16      00              Reserved
17      10              Refresh rate / scan multiplier
18-23   32 54 76 98     **MAC Address / Card Serial**: 10:32:54:76:98:BA
        BA
24      0C              Hardware revision
25-26   00 E4           Additional data (0xE4 = 228)
27+     00...           Padding/reserved
```

**Temperature conversion (estimated):**
- Raw values seen: 0x6C=108, 0x6E=110, 0x70=112, 0x72=114, 0x74=116, 0x76=118
- Likely: temp_celsius = raw_value * 0.5 → range 54-59°C
- Or: temp_celsius = raw_value - 60 → range 48-58°C
- Needs calibration against NovaLCT's displayed value

**Voltage conversion (estimated):**
- Raw values: 0xAC=172, 0xAD=173, 0xAE=174
- Likely: voltage = raw_value * 0.03 → ~5.1-5.2V
- Needs calibration

---

## 5. Receiving Card Per-Port Reads (0x00XX2013)

Monitoring reads individual receiving cards via incrementing addresses:
```
0x00102013  → Receiving card at port index 0x10 (card 1)
0x00202013  → Receiving card at port index 0x20 (card 2)
0x00302013  → Receiving card at port index 0x30 (card 3)
...
0x00A02013  → Receiving card at port index 0xA0 (card 10)
```
These read individual card status. Combined with register `0x01000113` which returns per-card hardware info.

---

## 6. Input Status Register (0x00000002) — Two Formats

### Format A: Sending Card Video Status (when read as 512 bytes with port=0)
```
Offset  Data                    Meaning
──────  ──────────────────────  ──────────────────────────
0       1C                      Input flags
1-5     FF FF FF FF FF          Port capability mask
6       3F                      Active port mask
7       00                      Reserved
8-9     32 00                   Width: 50 (×32 = 1600?)  
10-11   34 0D                   Input identifier
12-13   51 0D                   Refresh/format
14      01                      Signal detected (1=yes)
15      01                      Input valid
17      00                      Test pattern status
18-19   00 00                   Reserved
21      02                      Color depth
22-23   10 68                   Horizontal total: 4200
24-25   00 D0                   Vertical total: 208
26-27   00 20                   H active: 32
28-29   00 40                   V active: 64
30-31   00 40                   Pixel clock related
```

### Format B: Sending Card Network Status (when read with different params)
```
0       00                      Network mode
1-10    FF FF FF FF FF FF FF    MAC/connection data
        FF FF FF
11-12   00 00                   Reserved
13-14   FF FF                   Link status
17      26                      Year (0x26 = 38 → 2026?)
18      03                      Month (March)
19      27                      Day (39 → 0x27 = 39? or literal 27)
20-21   16 12                   Hour:Minute (22:18)
22      30                      Seconds (48)
23      01                      Flags
25      80                      Device status
28-29   1A 41                   Temperature? (0x1A41 = 6721)
```

---

## 7. Write Commands Identified

### Brightness Write
```
Register:  0xE3010002 (to all receiving cards, port 1)
Payload:   F0 F0 F0 00  → Red=240, Green=240, Blue=240, Reserved=0
Target:    0xFFFFFF (broadcast to all receiving cards)
```

### Brightness Enable/Single-byte
```
Register:  0x01000002 (to all receiving cards)
Payload:   Single byte (brightness apply trigger)
```

### Color Adjustment Writes
```
0x02000002 → Red channel adjustment
0x03000002 → Blue channel adjustment  
0x04000002 → Green channel adjustment
0x05000002 → Additional color parameter
0x5E000001 → Color matrix/3D LUT data (11 bytes)
```

### Test Pattern / Apply Command
```
Register:  0x11000001 (to all receiving cards)
Payload:   00 (commit/apply changes)
```

---

## 8. Key Findings for Our Monitor App

**What we can poll for monitoring (no panels needed for testing):**
1. `0x06000000` → Brightness (1 byte, 0-255 scale)
2. `0x0000000a` → Receiving card live status (temp, voltage, link, serial)
3. `0x00000002` → Input signal status + video format
4. `0x00000000` → System info (uptime, config)
5. `0x00000005` → Device identification ("NSSD" packet)
6. `0x00XX2013` → Per-port receiving card status
7. `0x16000000` → Controller date/time

**What changes between polls (live data):**
- Register `0x0000000a` bytes 1 and 3 fluctuate → **temperature and voltage**
- Register `0x00000002` byte 22 increments → **seconds counter (real-time clock)**

**Temperature calibration needed:**
Do you know what temperature NovaLCT showed for the receiving cards during capture?
That will let us map the raw ADC values (0x6C-0x76 range = 108-118) to actual Celsius.
