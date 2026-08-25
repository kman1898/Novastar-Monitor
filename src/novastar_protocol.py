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


def encode_length(byte_count):
    """Inverse of `decode_length` for a payload we are sending.

    Byte mode (high byte = count) for anything under 256, which is every
    payload this module writes. Page mode is only reachable for exact
    multiples of 256, and a length that is not representable is an error
    rather than a silent truncation — a wrong length field on a write is a
    frame the device may act on in an unintended way.

    >>> hex(encode_length(1))     # matches the captured clear frame
    '0x100'
    """
    if byte_count < 0:
        raise ValueError("negative length")
    if byte_count < 256:
        return (byte_count << 8) & 0xFF00
    if byte_count % 256 == 0 and byte_count // 256 < 256:
        return byte_count // 256
    raise ValueError(f"length {byte_count} is not representable")


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
                    device=0xFE, sender_card=0x00):
    """Build a 20-byte READ request targeting one card on one chain.

    Per-card addressing (H_SERIES_FINDINGS §6.5 — decoded from
    `H series Bit errors detection.pcapng`, a single-sender-card H2 rig whose
    known 245-panel count is reproduced exactly by counting distinct
    (byte[7], byte[8]) pairs across 16 chains):
      byte[5]  = sender card index, 0-based (`sender_card` kwarg)
      byte[6]  = 0x01  (per-card marker; 0x00 would be a broadcast read)
      byte[7]  = chain index, 0-based (0–15, 16 chains per sender card)
      byte[8]  = card position within that chain, 0-based
      byte[9]  = card index high byte — 0x00 in all observed traffic
      bytes[10-11] = 0x00 0x00

    byte[5] selects the **sender card**, not the OPT group — the OPT group is
    already implied by the chain (0–7 = OPT 1, 8–15 = OPT 2). Verified on a
    two-sender H15: the same (chain 0, card 0) address returns `05 0000` at
    `sender_card=0` and `05 0300` at `sender_card=1` — two different cards,
    the second carrying 3 bit errors. Indices past the installed sender cards
    answer absent. The single-sender H2 captures all used 0x00, which is why
    this byte read as a constant there.

    Every sender card is addressed over the SAME connection to TCP 5201; the
    per-card services on 5202/5203/5204 accept connections but answer reads
    with an empty payload, so they are not usable for enumeration.

        build_read_card(seq, reg, length, chain, card_index, sender_card=n)

    The chain is NOT the `sender_card` kwarg — passing a chain number there
    (the pre-§6.5 mistake) addresses the wrong thing entirely.
    """
    body = struct.pack(">HBB", seq, device, sender_card)
    body += bytes([0x01, chain & 0xFF, card_index & 0xFF, 0x00, 0x00, 0x00])
    body += struct.pack(">IH", register, length)
    return _frame(body)


# ── The one write this codebase can build ─────────────────────────────────
#
# Everything else here is a READ. This app is a monitor, and the single
# behaviour that ever cost the operator control of their wall mid-show was a
# write-shaped one (the W0120 heartbeat). So there is exactly one write
# builder, it does exactly one thing, and it is not parameterised into a
# general-purpose write primitive — a `build_write(register, payload)` would
# make every register in the device reachable from a typo.

# Clears the per-card bit-error counters. Decoded from
# `Bit error 4x clear erros.pcapng`: the operator clicked "clear" in NovaLCT
# four times at the end of the capture and NovaLCT sent four frames identical
# but for the sequence number. Register 0x76000001 appears nowhere else in the
# 434 request frames in that capture.
H_REG_CLEAR_BIT_ERRORS = 0x76000001

# byte[5] = 0xFF and target bytes[7:10] = FF FF FF: NovaLCT broadcasts the
# clear to every sender card and every card on every chain in one frame,
# rather than walking addresses. Kept exactly as captured — a per-card variant
# is not something to invent for a write.
BROADCAST_SENDER_CARD = 0xFF
BROADCAST_TARGET = b"\xff\xff\xff"

# byte[10] in the captured frame. The VX1000 document calls this the
# receiving-card port and says it is 1-based.
CLEAR_BIT_ERRORS_PORT = 0x01

# The payload NovaLCT writes. Not decoded — 0x05 is what it sends, and it is
# also the "card present" status byte in the bit-error read reply, which may
# or may not be a coincidence. Sent verbatim rather than reasoned about.
CLEAR_BIT_ERRORS_PAYLOAD = b"\x05"


def build_clear_bit_errors(seq, device=0xFE):
    """Build the frame NovaLCT sends to clear every bit-error counter.

    THIS WRITES TO THE DEVICE. It is the only frame in this module that does.
    Callers are expected to gate it behind an explicit operator action; see
    `NovaStar_Device.clear_device_bit_errors`.

    Golden bytes (capture, seq=202)::

        55 aa 00 ca fe ff 01 ff ff ff 01 00 76 00 00 01 01 00 05 98 5b

    The counter is cumulative and this is the only known way to reset it, so
    clearing discards a number somebody else may be relying on. That is an
    operator's call to make, not a routine one.
    """
    body = struct.pack(">HBB", seq, device, BROADCAST_SENDER_CARD)
    body += bytes([0x01]) + BROADCAST_TARGET
    body += bytes([CLEAR_BIT_ERRORS_PORT, 0x00])
    body += struct.pack(">I", H_REG_CLEAR_BIT_ERRORS)
    body += struct.pack(">H", encode_length(len(CLEAR_BIT_ERRORS_PAYLOAD)))
    body += CLEAR_BIT_ERRORS_PAYLOAD
    return _frame(body)


# A response frame is 20 bytes of framing plus whatever payload its own length
# field claims: 2 header + 16 body-head (seq, device, port, 6 pad, register,
# length) + payload + 2 checksum. Same layout as the request frames `_frame`
# builds, which is why `enumerate_wall._recv_frame` and
# `NovaStar_Device._percard_read` can read 18 bytes, decode the length and then
# read exactly `payload + 2` more.
RESPONSE_FRAMING_BYTES = 20
RESPONSE_PAYLOAD_OFFSET = 18


def parse_response(data):
    """Parse exactly one response frame. Returns (register_addr, payload) or None.

    Three things are checked, because everything downstream indexes the payload
    positionally — `data[31]` for the H-series port bitmask, `data[0]`/`data[1]`
    for per-card presence and temperature — and positional indexing into the
    wrong bytes does not raise. It produces a plausible wrong reading, which on
    this dashboard is either a false all-clear over a real fault or a false
    fault that pulls somebody out of a show.

    1. Header, as before.

    2. Length. The frame states its own payload size in bytes[16:18], so the
       buffer must be exactly `decode_length(...)` plus the 20 framing bytes.
       The old `data[18:-2]` accepted anything at least 20 bytes long, which
       let two different real failures through:
         - a short read — `NovaStar_Device.read_register` does a single
           `sock.recv`, and TCP is free to return half a frame — produced a
           truncated-but-plausible payload. A 512-byte video-status reply that
           arrived as 40 bytes still yielded a payload, and byte[31] of it was
           read as the port bitmask even though those bytes were never the
           ones the device put there.
         - a coalesced read — the controller answers back-to-back reads in one
           TCP segment under load — produced a payload that ran on into the
           NEXT frame's header, so the tail of one register's data was another
           register's framing bytes.
       Neither looks like an error at the call site; both look like data.
       Requiring the buffer to be exactly one frame is what makes the returned
       payload's length mean something.

    3. Checksum (§3.2.1: sum of the bytes between header and checksum, plus
       0x5555, little-endian). The only end-to-end evidence that these are the
       bytes the device actually sent rather than a resynchronising stream that
       happens to be the right length.

    There is still no sequence-number correlation here: the frame carries the
    sequence at bytes[2:4] but this function is not told which one was asked
    for. The callers that care compare the register instead and drop the socket
    when it differs (`parsed[0] != register` in `_percard_read`, `reg !=
    self.register` in `BinarySenderCardProbe.probe`) — a late reply to a
    timed-out probe is otherwise read as the answer to the next one.
    """
    if not data or len(data) < RESPONSE_FRAMING_BYTES:
        return None
    header = struct.unpack(">H", data[0:2])[0]
    if header != HEADER_RESPONSE:
        return None

    payload_len = decode_length(struct.unpack(">H", data[16:18])[0])
    if len(data) != RESPONSE_FRAMING_BYTES + payload_len:
        return None

    body = bytes(data[2:-2])
    if struct.unpack("<H", data[-2:])[0] != checksum(body):
        return None

    reg = struct.unpack(">I", data[12:16])[0]
    return (reg, bytes(data[RESPONSE_PAYLOAD_OFFSET:-2]))


# ── Data Parsing ──────────────────────────────────────────

def parse_temperature(raw_byte):
    """Convert a raw receiving-card temperature byte to Celsius.

    Units of 0.5 °C, so raw / 2. Confirmed by NovaStar's control protocol
    §4.3.4: "in units of 0.5°C. For example, a value of 104 represents a
    temperature of 52°C."
    """
    return raw_byte / 2.0


# Receiving-card voltage byte. NovaStar's H Series Video Wall Splicers Control
# Protocol (checked in both V1.0.18 and V1.0.20, §4.3.4 and §5.4.2) states it
# outright: "The lower 7 bits represent the voltage value, in units of 0.1V.
# For instance, a value of 172 indicates a voltage of 4.4V." 172 & 0x7F = 44.
#
# This file previously used `raw * 0.03`, and that was a mistake made in this
# project, not a vendor formula: the masked form was replaced because it put
# every card below a 4.7 V alarm threshold on a healthy wall. The threshold was
# what was wrong — receiving cards run around 4.2 V, not 5 V — and it has since
# been lowered separately. Checked against hardware: byte[3] = 0xAA reads 4.2 V
# masked and 5.10 V unmasked, while R0155 reports 4.30 V for cards on the same
# chain. The masked form is the one that agrees with the other protocol.
VOLTAGE_VALUE_MASK = 0x7F
VOLTAGE_UNITS_PER_VOLT = 0.1


def parse_voltage(raw_byte):
    """Convert a raw receiving-card voltage byte to volts."""
    return (raw_byte & VOLTAGE_VALUE_MASK) * VOLTAGE_UNITS_PER_VOLT


def parse_mac(data, offset=18):
    """Extract MAC address string from monitoring payload, or None.

    None — not "" — when the payload is too short to contain one. An empty
    string is a value: it renders as a blank MAC field and reads as a
    statement about the card ("this one has no MAC"), when all that actually
    happened is that we were never sent those six bytes.
    """
    if len(data) < offset + 6:
        return None
    return ":".join(f"{b:02X}" for b in data[offset:offset + 6])


# byte[0] of a live-monitoring reply. Verified against a chain known to hold
# exactly 22 panels: cards 0-21 answered 0x80, and every address past the end
# answered 0xC0 / 0xE0 / 0xE2 / 0xE4 / 0xE6 while repeating the last card's
# readings. Bit 0x40 is therefore "nothing here", and its low bits vary — that
# variation, read without masking, looks exactly like a free-running counter
# and is what previously led to this register being written off as "not
# per-card on H-series". It is per-card; the presence test just has to mask.
LIVE_PRESENT_MASK = 0xC0
LIVE_PRESENT_VALUE = 0x80


def live_monitor_present(status_byte):
    """Whether a live-monitoring reply describes a card that is actually there.

    `0x80` exactly. An absent address still returns a well-formed 82-byte
    payload carrying the PREVIOUS card's temperature and voltage, so "the
    device answered" is not a presence test and treating it as one silently
    invents readings for addresses with no panel on them.
    """
    return (status_byte & LIVE_PRESENT_MASK) == LIVE_PRESENT_VALUE


def parse_live_monitoring(data):
    """
    Parse live monitoring register (0x0000000a) response.
    Returns dict with temperature, voltage, card count, link status, etc.

    `present` is the field to gate on. `online` is kept for the VX1000 callers
    that predate the mask and is deliberately the looser test.

    Readings are only meaningful when `present` — see `live_monitor_present`.
    """
    if not data or len(data) < 20:
        return None

    result = {
        "present": live_monitor_present(data[0]),
        "online": bool(data[0] & 0x80),
        "status_byte": data[0],
        "temperature_raw": data[1],
        "temperature_c": parse_temperature(data[1]),
        "voltage_raw": data[3],
        "voltage_v": round(parse_voltage(data[3]), 2),
        "card_count": data[11] + 1,  # Zero-indexed in protocol
        # 1 and 2 are the documented VX1000 values. On H-series byte[12] also
        # takes other values — a NovaLCT capture of a healthy 286-card wall
        # showed 1 on 162 cards and 11 on the other 124, and the live wall
        # reports 11 for whole chains that are working normally. Mapping
        # "anything else" to DISCONNECTED therefore labelled 124 healthy
        # panels as disconnected. Unrecognised means unrecognised.
        "link_status": ("PRIMARY" if data[12] == 1
                        else "BACKUP" if data[12] == 2
                        else "DISCONNECTED" if data[12] == 0
                        else "UNKNOWN"),
        "link_raw": data[12],
        "connection_type": data[13],
        "firmware": f"{data[14]}.{data[15]}",
        # Every field below is present only if the payload actually reached
        # that offset, and the answer when it did not is None. The fallbacks
        # used to be 0 and "":
        #   - scan multiplier 0 is a value a device can legitimately report,
        #     so a truncated reply was indistinguishable from a real reading
        #     of zero. (The 20-byte guard above means this branch is not
        #     reachable today; it stays because the guard is about the frame,
        #     not about this byte, and "what do we say when we don't know" is
        #     the thing that must not regress if the guard ever moves.)
        #   - "" for the MAC and the hardware revision renders as a blank
        #     field, which reads as a fact about the card rather than as a
        #     read that never got that far.
        # None is the only one of the three that a consumer's `is not None`
        # guard can tell apart from a measurement.
        "scan_multiplier": data[17] if len(data) > 17 else None,
        "mac_address": parse_mac(data) if len(data) >= 24 else None,
        "hw_revision": f"0x{data[24]:02X}" if len(data) >= 25 else None,
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


# Byte[1] of the H-series video status register carries the receiving card's
# data-path link state, and a healthy card reads 0x7F — seven bits, not eight.
# So only the low 7 bits are data paths; bit 7 (0x80) is something else and no
# capture says what.
#
# The denominator and the mask therefore have to agree at 7, and the bug was
# that they did not: `bin(link_byte).count('1')` counted the whole byte against
# a fixed total of 7, so a card reading 0xFF rendered as "8/7" — a fraction the
# wall does not have, on the panel the operator is most likely to be staring at
# because something is wrong with it.
#
# Masking to 7 bits is the correct direction rather than widening the total to
# 8. The evidence (0x7F = all paths connected) says a fully healthy card has
# bit 7 CLEAR; widening would score that healthy card 7/8 and put a missing
# data path on every card on the wall, every poll. Masking says only that bit 7
# is not a path, which is exactly what the capture shows.
H_CARD_LINK_PATHS = 7
H_CARD_LINK_MASK = (1 << H_CARD_LINK_PATHS) - 1


def parse_h_card_link(video_status_data):
    """Parse per-card link path status from H-series video status byte[1].

    Returns (connected_paths, total_paths) or None if data is insufficient.
    Byte[1] = 0x7F means all 7 data paths connected (normal).
    Each cleared bit = one path down (e.g., 0x3D = 5/7 paths).
    Bit 7 is masked off and not counted — see H_CARD_LINK_MASK above.
    """
    if not video_status_data or len(video_status_data) < 2:
        return None
    link_byte = video_status_data[1]
    connected = bin(link_byte & H_CARD_LINK_MASK).count('1')
    return (connected, H_CARD_LINK_PATHS)


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
