"""
NovaStar Protocol Codec
Binary frame builder and parser for TCP port 5200 communication.
Decoded from VX1000 Wireshark captures.
"""

import struct

HEADER_REQUEST = 0x55AA
HEADER_RESPONSE = 0xAA55
TCP_PORT = 5200
UDP_DISCOVERY_PORT = 5600

# ── Register Addresses ────────────────────────────────────

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


def per_card_address(card_index):
    """Register address for a specific receiving card (1-based index)."""
    return 0x00002013 | ((card_index * 0x10) << 16)


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
