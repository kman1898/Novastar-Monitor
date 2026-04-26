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
H_MAX_PORTS = 15

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
REG_PORT_INFO       = (0x01000113, 0x0100)  # Port hardware info
REG_CARD_CONFIG     = (0x9E000013, 0x0200)  # Receiving card config

# ── H-Series Register Addresses ──────────────────────────
# Decoded from H-series Wireshark captures on port 5203.
# The H-series uses the same frame format but different registers
# and a multi-port architecture (up to 15 output ports, each
# with its own daisy-chain of receiving cards).

H_REG_VIDEO_STATUS  = (0x00000002, 0x0002)  # 512 bytes — byte[1]=link, byte[31]=port bitmask
H_REG_SYSTEM_INFO   = (0x00000000, 0x0001)  # 256 bytes — byte[0]=HW type, firmware info
H_REG_FIRMWARE      = (0x02000000, 0x0200)  # 2 bytes — firmware version
H_REG_BRIGHTNESS    = (0x06000000, 0x0100)  # 1 byte — brightness 0-255
H_REG_GAMMA         = (0x07000000, 0x0200)  # 2 bytes — gamma mode
H_REG_DATETIME      = (0x16000000, 0x0800)  # 8 bytes — date/time
H_REG_DEVICE_ID     = (0x00000005, 0x0002)  # 512 bytes — NSSD device identity
H_REG_FPGA_FW       = (0x00000008, 0x0002)  # 512 bytes — FPGA firmware per card
H_REG_MCU_FW        = (0x00000009, 0x0002)  # 512 bytes — MCU firmware per card

# Per-card data channels: 0x00400003 through 0x004E0003 (8 channels).
# Channel 0 (0x00400003): byte[0] / 2.0 = temperature in Celsius.
H_REG_CARD_DATA_BASE = 0x00400003
H_REG_CARD_DATA_LEN  = 0x0002  # 512 bytes per channel


def per_card_address(card_index):
    """Register address for a specific receiving card (1-based index)."""
    return 0x00002013 | ((card_index * 0x10) << 16)


def h_card_data_register(channel):
    """H-series per-card data channel register (0-based, 0-7).

    Channel 0 = 0x00400003 (temperature in byte[0])
    Channel 1 = 0x00420003
    ...
    Channel 7 = 0x004E0003
    """
    addr = H_REG_CARD_DATA_BASE + (channel * 0x00020000)
    return (addr, H_REG_CARD_DATA_LEN)


def h_port_video_register(port_num):
    """H-series per-port video status register.

    Port 1 = 0x00010002, Port 2 = 0x00020002, etc.
    Returns 512 bytes when the port is connected.
    """
    addr = 0x00000002 + (port_num << 16)
    return (addr, 0x0002)


# ── Length Encoding ───────────────────────────────────────
# The 16-bit length field in NovaStar frames uses a split encoding:
#   If low byte != 0 → payload = low_byte × 256 bytes  (page mode)
#   If low byte == 0 → payload = high_byte bytes        (byte mode)

def encode_length(byte_count):
    """Encode a payload byte count into the 16-bit wire format.

    >>> encode_length(1)    # 1 byte  → 0x0100
    256
    >>> encode_length(512)  # 512 bytes → 0x0002
    2
    """
    if byte_count <= 0:
        return 0
    if byte_count % 256 == 0:
        pages = byte_count // 256
        if pages <= 255:
            return pages  # low byte = pages, high byte = 0
    if byte_count <= 255:
        return byte_count << 8  # high byte = count, low byte = 0
    # Fallback: can't encode exactly, use page mode rounded up
    return (byte_count + 255) // 256


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

def checksum(data):
    return sum(data) & 0xFFFF


def build_read(seq, register, length, device=0xFE, port=0x00):
    """Build a 20-byte READ request frame (broadcast / sending-card target)."""
    frame = struct.pack(">HHBB6xIH", HEADER_REQUEST, seq, device, port, register, length)
    frame += struct.pack(">H", checksum(frame))
    return frame


def build_read_card(seq, register, length, card_index, device=0xFE, port=0x00):
    """Build a 20-byte READ request targeting a specific receiving card.

    Per-card addressing (confirmed from VX1000 Wireshark captures):
      byte[6] = 0x01  (direct-to-receiving-card command)
      byte[7] = 0x00
      byte[8] = card_index  (0-based, 0x00–0x0D for 14 cards)
      bytes[9-11] = 0x00 0x00 0x00
    """
    frame = struct.pack(">HHBB", HEADER_REQUEST, seq, device, port)
    frame += bytes([0x01, 0x00, card_index & 0xFF, 0x00, 0x00, 0x00])
    frame += struct.pack(">IH", register, length)
    frame += struct.pack(">H", checksum(frame))
    return frame


def build_write(seq, register, payload, device=0xFE, port=0x00,
                target=b"\xFF\xFF\xFF", target_port=0x01):
    """Build a WRITE command frame."""
    frame = struct.pack(">HHBBB3sBxIH",
        HEADER_REQUEST, seq, device, port, 0x01,
        target, target_port, register, len(payload))
    frame += payload
    frame += struct.pack(">H", checksum(frame))
    return frame


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


def parse_h_port_bitmask(video_status_data):
    """Extract port connection bitmask from H-series broadcast video status.

    Byte[31] of the broadcast video status register (0x00000002) contains
    a bitmask where each bit represents a connected output port.
    Bit 0 = port 1, bit 1 = port 2, ..., bit 14 = port 15.

    Returns dict mapping port numbers (1-15) to connected (bool).
    Decoded from H-series Wireshark captures: 0xAF (6 ports connected)
    changed to 0xAC when ports 1 and 2 were physically disconnected.
    """
    if not video_status_data or len(video_status_data) < 32:
        return {}
    bitmask = video_status_data[31]
    return {port: bool(bitmask & (1 << (port - 1))) for port in range(1, H_MAX_PORTS + 1)}


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


def parse_h_card_temperature(card_data_payload):
    """Parse temperature from H-series per-card data channel 0.

    Register 0x00400003, byte[0] / 2.0 = temperature in Celsius.
    Confirmed from capture: all cards returned 0x5D (93) = 46.5°C.
    """
    if not card_data_payload or len(card_data_payload) < 1:
        return None
    return card_data_payload[0] / 2.0


def parse_h_system_info(data):
    """Parse H-series per-card system info register (0x00000000).

    Returns hardware type and firmware version info.
    byte[0] = HW type (e.g., 0x09 for A8s receiving cards)
    bytes[1:10] = firmware version components
    """
    if not data or len(data) < 10:
        return None
    return {
        "hw_type": f"0x{data[0]:02X}",
        "fpga_major": data[1],
        "fpga_minor": data[2],
        "fw_revision": data[3],
        "fw_sub": data[4],
        "version_bytes": data[5:10].hex(),
    }
