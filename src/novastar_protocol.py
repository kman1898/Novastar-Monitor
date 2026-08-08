"""
NovaStar Protocol Codec
Binary frame builder and parser for TCP port 5200/5203 communication.
Decoded from VX1000 and H-series Wireshark captures.
"""

import struct

HEADER_REQUEST = 0x55AA
HEADER_RESPONSE = 0xAA55
TCP_PORT = 5200
H_TCP_PORT = 5203
UDP_DISCOVERY_PORT = 5600

# Maximum receiving cards per output port (resolution-dependent).
# At 60×120 panel resolution, the H-series supports up to 91 per port.
H_MAX_CARDS_PER_PORT = 91

# Chains (output ports) addressable per sender card. §6.5: the per-card frame's
# chain field (byte[7]) was observed spanning 0–15 on the H2 rig — 16 chains,
# whose panel counts sum to the operator's known 245. The venue wall uses 15 of
# them (A1–A15); port 16 is spare.
H_MAX_PORTS = 16

# ── VX1000 Register Addresses ────────────────────────────

REG_SYSTEM_INFO     = (0x00000000, 0x0001)  # System info (256 bytes)
REG_FIRMWARE        = (0x02000000, 0x0200)  # Firmware version (2 bytes)
REG_DEVICE_PORT1    = (0x00000005, 0x0001)  # NSSD device info port 1 (256 bytes)
REG_DEVICE_PORT2    = (0x00010005, 0x0001)  # NSSD device info port 2 (256 bytes)
REG_BRIGHTNESS      = (0x06000000, 0x0100)  # Brightness (1 byte, 0-255)
REG_GAMMA           = (0x07000000, 0x0200)  # Gamma mode (2 bytes)
REG_DATETIME        = (0x16000000, 0x0800)  # Date/time (8 bytes)
REG_VIDEO_STATUS    = (0x00000002, 0x0002)  # Video/input status (512 bytes)
REG_LIVE_MONITOR    = (0x0000000a, 0x5200)  # Live monitoring data (~82 bytes)

# ── H-Series Register Addresses ──────────────────────────
# Decoded from H-series Wireshark captures on port 5203.
# The H-series uses the same frame format but different registers
# and a multi-port architecture (up to 16 chains per sender card, each
# with its own daisy-chain of receiving cards).

H_REG_VIDEO_STATUS  = (0x00000002, 0x0002)  # 512 bytes — byte[1]=link, byte[31]=port bitmask
H_REG_SYSTEM_INFO   = (0x00000000, 0x0001)  # 256 bytes — byte[0]=HW type, firmware info
H_REG_FIRMWARE      = (0x02000000, 0x0200)  # 2 bytes — firmware version
H_REG_BRIGHTNESS    = (0x06000000, 0x0100)  # 1 byte — brightness 0-255
H_REG_GAMMA         = (0x07000000, 0x0200)  # 2 bytes — gamma mode
H_REG_DATETIME      = (0x16000000, 0x0800)  # 8 bytes — date/time
H_REG_DEVICE_ID     = (0x00000005, 0x0002)  # 512 bytes — NSSD device identity

# Per-card data channels: 0x00400003 through 0x004E0003 (8 channels).
# Each returns 512 bytes per card. Originally believed channel 0 byte[0]/2.0
# was temperature, but cross-referencing with NovaLCT MonitorSite GUI proved
# the value is a constant (0x5D across all 943 cards) — purpose unknown.
# Real per-card temperature lives in REG_LIVE_MONITOR (0x0000000A) byte[1]/2.0,
# the same VX1000-style register, which works on H-series too.
# Channels 1-7 (0x00420003–0x004E0003) are still undecoded research targets;
# kept here as the base address for that work.
H_REG_CARD_DATA_BASE = 0x00400003
H_REG_CARD_DATA_LEN  = 0x0002  # 512 bytes per channel

# Per-card fault/alarm flag — single byte, 0 = no fault.
# Confirmed from capture: 943 cards all returned 0x00 while NovaLCT GUI
# showed "Quantity of fault: 0". Non-zero values would indicate active
# alarms (specific fault code semantics not yet captured).
H_REG_CARD_FAULT     = (0x09050002, 0x0100)  # 1 byte

# Per-card bit-error counter — 3-byte response (§6.5, decoded from
# `H series Bit errors detection.pcapng`). Length 0x0300 is byte mode:
# high byte = 3, low byte = 0 → decode_length(0x0300) == 3.
# NovaLCT polls this ~10x more often than any other register; it is the only
# continuous data-integrity signal and has no JSON UDP equivalent.
H_REG_BIT_ERRORS     = (0x4A010002, 0x0300)  # 3 bytes


# ── Length Encoding ───────────────────────────────────────
# The 16-bit length field in NovaStar frames uses a split encoding:
#   If low byte != 0 → payload = low_byte × 256 bytes  (page mode)
#   If low byte == 0 → payload = high_byte bytes        (byte mode)

def decode_length(length_field):
    """Decode the 16-bit wire length field to actual payload byte count.

    >>> decode_length(0x0002)  # page mode: 2 × 256 = 512
    512
    >>> decode_length(0x0100)  # byte mode: high byte = 1
    1
    >>> decode_length(0x5200)  # byte mode: high byte = 82
    82
    """
    low = length_field & 0xFF
    high = (length_field >> 8) & 0xFF
    if low != 0:
        return low * 256
    return high


# ── Frame Building ────────────────────────────────────────

# Checksum formula (per VX1000 Control Protocol V1.0 §3.2.1 and Sending Card
# Central Control Protocol V1.3 §4.1):
#
#   SUM = sum(bytes_between_header_and_checksum) + 0x5555
#
# The 0x55 0xAA frame header is NOT included in the sum. The 16-bit result is
# wire-encoded little-endian (SUM_L first, SUM_H second).
#
# Worked example from VX1000 doc: reading ModeID
#   Wire:    55 aa 00 00 fe 00 00 00 00 00 00 00 02 00 00 00 02 00 57 56
#   Body:          00 00 fe 00 00 00 00 00 00 00 02 00 00 00 02 00
#   sum(body) = 0x102, +0x5555 = 0x5657, little-endian wire = 57 56 ✓


def checksum(body):
    """Compute the 16-bit NovaStar frame checksum.

    `body` is the slice of frame bytes between the 2-byte header and the
    2-byte checksum field. The header (0x55 0xAA / 0xAA 0x55) is excluded.
    """
    return (sum(body) + 0x5555) & 0xFFFF


def _frame(body):
    """Wrap a body with the request header and little-endian checksum."""
    return struct.pack(">H", HEADER_REQUEST) + body + struct.pack("<H", checksum(body))


def build_read(seq, register, length, device=0xFE, port=0x00):
    """Build a 20-byte READ request frame (broadcast / sending-card target)."""
    body = struct.pack(">HBB6xIH", seq, device, port, register, length)
    return _frame(body)


def build_read_card(seq, register, length, chain, card_index,
                    device=0xFE, port=0x00):
    """Build a 20-byte READ request targeting one card on one chain.

    Per-card addressing (H_SERIES_FINDINGS §6.5 — decoded from
    `H series Bit errors detection.pcapng`, a single-sender-card H2 rig whose
    known 245-panel count is reproduced exactly by counting distinct
    (byte[7], byte[8]) pairs across 16 chains):
      byte[5]  = OPT group (`port` kwarg) — 0x00 in all observed traffic
      byte[6]  = 0x01  (per-card marker; 0x00 would be a broadcast read)
      byte[7]  = chain index, 0-based (0–15, 16 chains per sender card)
      byte[8]  = card position within that chain, 0-based
      byte[9]  = card index high byte — 0x00 in all observed traffic
      bytes[10-11] = 0x00 0x00

    Intended call pattern: open one TCP connection per sender card (§6.5 maps
    them to TCP 5201/5202/5203, with 5200 as the broadcast/main controller),
    then walk `chain` 0–15 and `card_index` 0–N on that connection:

        build_read_card(seq, reg, length, chain, card_index)

    The chain is NOT the `port` kwarg. `port` is the OPT group in byte[5] and
    stays 0 unless a capture proves otherwise — passing a chain number there
    (the pre-§6.5 mistake) addresses the wrong thing entirely.
    """
    body = struct.pack(">HBB", seq, device, port)
    body += bytes([0x01, chain & 0xFF, card_index & 0xFF, 0x00, 0x00, 0x00])
    body += struct.pack(">IH", register, length)
    return _frame(body)


def parse_response(data):
    """Parse response, returns (register_addr, payload) or None."""
    if not data or len(data) < 20:
        return None
    header = struct.unpack(">H", data[0:2])[0]
    if header != HEADER_RESPONSE:
        return None
    reg = struct.unpack(">I", data[12:16])[0]
    payload = data[18:-2] if len(data) > 20 else b""
    return (reg, payload)


# ── Data Parsing ──────────────────────────────────────────

def parse_temperature(raw_byte):
    """Convert raw temperature byte to Celsius. Calibrated against VX1000."""
    return raw_byte / 2.0


def parse_voltage(raw_byte):
    """Convert raw voltage byte to volts."""
    return raw_byte * 0.03


def parse_mac(data, offset=18):
    """Extract MAC address string from monitoring payload."""
    if len(data) < offset + 6:
        return ""
    return ":".join(f"{b:02X}" for b in data[offset:offset + 6])


def parse_live_monitoring(data):
    """
    Parse live monitoring register (0x0000000a) response.
    Returns dict with temperature, voltage, card count, link status, etc.
    """
    if not data or len(data) < 20:
        return None

    result = {
        "online": bool(data[0] & 0x80),
        "status_byte": data[0],
        "temperature_raw": data[1],
        "temperature_c": parse_temperature(data[1]),
        "voltage_raw": data[3],
        "voltage_v": round(parse_voltage(data[3]), 2),
        "card_count": data[11] + 1,  # Zero-indexed in protocol
        "link_status": "PRIMARY" if data[12] == 1 else "BACKUP" if data[12] == 2 else "DISCONNECTED",
        "link_raw": data[12],
        "connection_type": data[13],
        "firmware": f"{data[14]}.{data[15]}",
        "scan_multiplier": data[17] if len(data) > 17 else 0,
        "mac_address": parse_mac(data) if len(data) >= 24 else "",
        "hw_revision": f"0x{data[24]:02X}" if len(data) >= 25 else "",
    }
    return result


def parse_system_info(data):
    """Parse system info register (0x00000000) response."""
    if not data or len(data) < 30:
        return None
    return {
        "device_type": f"0x{data[2]:02X}",
        "hw_version": data[3],
        "ethernet_ports": data[4],
        "brightness_raw": data[6],
        "input_count": data[8],
        "build_date": f"20{data[22]:02d}-{data[23]:02d}-{data[24]:02d}" if data[22] < 99 else "Unknown",
    }


def parse_nssd(data):
    """Parse NSSD device identity register (0x00000005) response."""
    if not data or len(data) < 12:
        return None
    header = data[:4].decode("ascii", errors="ignore")
    if header != "NSSD":
        return {"header": header, "active": False}
    model_code = struct.unpack(">H", data[4:6])[0]
    return {
        "header": header,
        "active": True,
        "model_code": f"0x{model_code:04X}",
        "serial": data[6:10].hex(),
        "hw_revision": f"0x{data[10]:02X}",
        "fw_byte": f"0x{data[11]:02X}",
    }


# ── H-Series Data Parsing ────────────────────────────────

# Byte[31] of the broadcast video status register is a SINGLE byte, so it can
# only carry 8 bits — it measures 8 chains, not the 16 a sender card addresses
# per §6.5. No capture shows a second bitmask byte anywhere in the register, so
# chains 9-16 are simply not covered by this signal. Reporting them as False
# would fabricate a "disconnected" reading for chains that were never measured,
# so parse_h_port_bitmask omits them from its result instead. For chains above
# 8, use the per-card signals (parse_h_card_link / parse_live_monitoring's
# link_status), which §2.3 lists as the other two data-break detection layers.
H_PORT_BITMASK_BITS = 8


def parse_h_port_bitmask(video_status_data):
    """Extract port connection bitmask from H-series broadcast video status.

    Byte[31] of the broadcast video status register (0x00000002) contains
    a bitmask where each bit represents a connected output port.
    Bit 0 = port 1, bit 1 = port 2, ..., bit 7 = port 8.

    Returns dict mapping port numbers (1-8 only — see H_PORT_BITMASK_BITS) to
    connected (bool). Ports above 8 are absent from the dict rather than False.
    Decoded from H-series Wireshark captures: 0xAF (6 ports connected)
    changed to 0xAC when ports 1 and 2 were physically disconnected.
    """
    if not video_status_data or len(video_status_data) < 32:
        return {}
    bitmask = video_status_data[31]
    return {
        port: bool(bitmask & (1 << (port - 1)))
        for port in range(1, H_PORT_BITMASK_BITS + 1)
    }


def parse_h_card_link(video_status_data):
    """Parse per-card link path status from H-series video status byte[1].

    Returns (connected_paths, total_paths) or None if data is insufficient.
    Byte[1] = 0x7F means all 7 data paths connected (normal).
    Each cleared bit = one path down (e.g., 0x3D = 5/7 paths).
    """
    if not video_status_data or len(video_status_data) < 2:
        return None
    link_byte = video_status_data[1]
    connected = bin(link_byte).count('1')
    return (connected, 7)


# !!! DEPRECATED — DO NOT CALL FOR TEMPERATURE !!!
# Register 0x00400003 returns the constant 0x5D on every card. The function
# below is retained only so historical callers don't crash; any new per-card
# thermal reading must come from parse_live_monitoring() on REG_LIVE_MONITOR.
def parse_h_card_temperature(card_data_payload):
    """DEPRECATED — register 0x00400003 is NOT per-card temperature.

    DO NOT USE THIS FOR TEMPERATURE. It returns a constant, not a reading.

    Originally assumed the H-series per-card data channel 0 carried temperature
    via byte[0]/2.0. Cross-referencing 943-card capture against the NovaLCT
    MonitorSite GUI proved every card returns the constant 0x5D regardless of
    actual thermal state. The real per-card temperature lives in the VX1000-style
    REG_LIVE_MONITOR (0x0000000A) byte[1]/2.0; use parse_live_monitoring() for
    the H-series too.

    This function is kept (returns the raw byte/2.0) only so any historical
    callers don't break, but do not treat its output as temperature.
    """
    if not card_data_payload or len(card_data_payload) < 1:
        return None
    return card_data_payload[0] / 2.0


def parse_h_card_fault(fault_payload):
    """Parse the per-card fault/alarm flag from H-series register 0x09050002.

    Returns dict with `fault` (bool) and `code` (raw byte, 0–255).
    `fault` is True when any non-zero code is present.

    Confirmed from `H series Monitioring Fault and temp readings.pcapng`:
    all 943 cards returned 0x00 while NovaLCT GUI showed "Quantity of fault: 0".
    Specific non-zero fault code semantics are not yet decoded — needs a capture
    taken while a card has an active hardware alarm.
    """
    if not fault_payload or len(fault_payload) < 1:
        return None
    code = fault_payload[0]
    return {"fault": code != 0, "code": code}


def parse_bit_errors(payload):
    """Parse the per-card bit-error counter from H-series register 0x4A010002.

    Returns dict with `present` (bool), `errors` (int), `saturated` (bool) and
    the raw `status` byte, or None if the payload is missing or short.

    Layout per §6.5, decoded from `H series Bit errors detection.pcapng`:
      byte[0]    = status; 0x05 = card present/responding
      bytes[1-2] = bit error count, uint16 LITTLE-endian (0–65535)

    `0xFFFF` is the counter's ceiling, reported as `saturated` — it means a
    serious data-integrity fault rather than a literal 65535 errors. The
    healthy-rig baseline in the capture is `05 00 00` (present, zero errors).
    """
    if not payload or len(payload) < 3:
        return None
    status = payload[0]
    errors = struct.unpack("<H", payload[1:3])[0]
    return {
        "present": status == 0x05,
        "status": status,
        "errors": errors,
        "saturated": errors == 0xFFFF,
    }
