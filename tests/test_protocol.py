"""Tests for novastar_protocol.py — frame building and data parsing."""
import struct
import novastar_protocol as proto


# ── Frame Building ──────────────────────────────────────────


class TestBuildRead:
    def test_length(self):
        frame = proto.build_read(1, 0x0000000A, 0x5200)
        assert len(frame) == 20

    def test_header(self):
        frame = proto.build_read(1, 0x0000000A, 0x5200)
        assert frame[0:2] == b"\x55\xAA"

    def test_sequence(self):
        frame = proto.build_read(0x1234, 0, 1)
        assert struct.unpack(">H", frame[2:4])[0] == 0x1234

    def test_device_and_port(self):
        frame = proto.build_read(1, 0, 1, device=0xFE, port=0x01)
        assert frame[4] == 0xFE
        assert frame[5] == 0x01

    def test_reserved_bytes_are_zero(self):
        """Broadcast reads have bytes 6-11 all zeros."""
        frame = proto.build_read(1, 0x0000000A, 0x5200)
        assert frame[6:12] == b"\x00" * 6

    def test_register_encoding(self):
        frame = proto.build_read(1, 0x0000000A, 0x5200)
        reg = struct.unpack(">I", frame[12:16])[0]
        assert reg == 0x0000000A

    def test_length_encoding(self):
        frame = proto.build_read(1, 0x0000000A, 0x5200)
        length = struct.unpack(">H", frame[16:18])[0]
        assert length == 0x5200

    def test_checksum_present(self):
        """Last 2 bytes are a uint16 checksum."""
        frame = proto.build_read(1, 0, 1)
        csum = struct.unpack(">H", frame[18:20])[0]
        expected = sum(frame[0:18]) & 0xFFFF
        assert csum == expected


class TestBuildReadCard:
    def test_length(self):
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, card_index=0)
        assert len(frame) == 20

    def test_header(self):
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, card_index=0)
        assert frame[0:2] == b"\x55\xAA"

    def test_per_card_addressing_byte6(self):
        """Byte 6 must be 0x01 (direct-to-receiving-card command)."""
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, card_index=5)
        assert frame[6] == 0x01

    def test_per_card_addressing_byte7(self):
        """Byte 7 must be 0x00."""
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, card_index=5)
        assert frame[7] == 0x00

    def test_per_card_addressing_card_index(self):
        """Byte 8 is the card index (0-based)."""
        for idx in range(14):
            frame = proto.build_read_card(1, 0x0000000A, 0x5200, card_index=idx)
            assert frame[8] == idx

    def test_per_card_addressing_trailing_zeros(self):
        """Bytes 9-11 must be zeros."""
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, card_index=13)
        assert frame[9:12] == b"\x00\x00\x00"

    def test_register_and_length(self):
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, card_index=0)
        reg = struct.unpack(">I", frame[12:16])[0]
        length = struct.unpack(">H", frame[16:18])[0]
        assert reg == 0x0000000A
        assert length == 0x5200

    def test_matches_capture_format(self):
        """Verify frame matches the structure seen in VX1000 Wireshark captures.

        Captured per-card read for card 13, reg=0x0000000a, len=0x5200:
          bytes 6-11 = 01 00 0d 00 00 00
        """
        frame = proto.build_read_card(0x00C4, 0x0000000A, 0x5200, card_index=13)
        assert frame[4] == 0xFE  # device
        assert frame[5] == 0x00  # port
        assert frame[6:12] == bytes([0x01, 0x00, 0x0D, 0x00, 0x00, 0x00])
        assert struct.unpack(">I", frame[12:16])[0] == 0x0000000A
        assert struct.unpack(">H", frame[16:18])[0] == 0x5200


class TestBuildWrite:
    def test_minimum_length(self):
        frame = proto.build_write(1, 0x06000000, b"\xA8")
        # header (HHBBB3sBxIH = 18 bytes) + 1 payload + 2 checksum = 21
        assert len(frame) == 18 + 1 + 2

    def test_header(self):
        frame = proto.build_write(1, 0x06000000, b"\xA8")
        assert frame[0:2] == b"\x55\xAA"


# ── Response Parsing ────────────────────────────────────────


class TestParseResponse:
    def _make_response(self, reg, payload, seq=1):
        """Build a minimal valid response frame."""
        header = struct.pack(">HHBB6xIH",
                             proto.HEADER_RESPONSE, seq, 0x00, 0xFE, reg, len(payload))
        data = header + payload + struct.pack(">H", proto.checksum(header + payload))
        return data

    def test_valid_response(self):
        resp = self._make_response(0x0000000A, b"\x80" + b"\x00" * 25)
        result = proto.parse_response(resp)
        assert result is not None
        reg, payload = result
        assert reg == 0x0000000A

    def test_rejects_short_data(self):
        assert proto.parse_response(b"\xAA\x55" + b"\x00" * 10) is None

    def test_rejects_wrong_header(self):
        assert proto.parse_response(b"\x55\xAA" + b"\x00" * 20) is None

    def test_empty_payload(self):
        resp = self._make_response(0x06000000, b"")
        result = proto.parse_response(resp)
        assert result is not None
        assert result[1] == b""


# ── Data Parsing ────────────────────────────────────────────


class TestParseTemperature:
    def test_known_values(self):
        """Values confirmed from VX1000 captures."""
        assert proto.parse_temperature(108) == 54.0
        assert proto.parse_temperature(118) == 59.0

    def test_zero(self):
        assert proto.parse_temperature(0) == 0.0


class TestParseVoltage:
    def test_known_values(self):
        """Values confirmed from VX1000 captures."""
        assert abs(proto.parse_voltage(172) - 5.16) < 0.01
        assert abs(proto.parse_voltage(174) - 5.22) < 0.01


class TestParseLiveMonitoring:
    def _make_payload(self, status=0x80, temp=116, volt=172, card_count=13,
                      link=1, fw_major=2, fw_minor=16, scan=16):
        """Build a 26-byte payload matching VX1000 capture format."""
        data = bytearray(26)
        data[0] = status
        data[1] = temp
        data[3] = volt
        data[11] = card_count  # zero-indexed
        data[12] = link
        data[13] = 0x01
        data[14] = fw_major
        data[15] = fw_minor
        data[17] = scan
        data[18:24] = bytes([0x32, 0x54, 0x76, 0x98, 0xBA, 0x0C])
        return bytes(data)

    def test_online_flag(self):
        result = proto.parse_live_monitoring(self._make_payload(status=0x80))
        assert result["online"] is True

    def test_offline_flag(self):
        result = proto.parse_live_monitoring(self._make_payload(status=0x00))
        assert result["online"] is False

    def test_temperature(self):
        result = proto.parse_live_monitoring(self._make_payload(temp=116))
        assert result["temperature_c"] == 58.0

    def test_voltage(self):
        result = proto.parse_live_monitoring(self._make_payload(volt=173))
        assert result["voltage_v"] == 5.19

    def test_card_count(self):
        result = proto.parse_live_monitoring(self._make_payload(card_count=13))
        assert result["card_count"] == 14

    def test_link_status_primary(self):
        result = proto.parse_live_monitoring(self._make_payload(link=1))
        assert result["link_status"] == "PRIMARY"

    def test_link_status_backup(self):
        result = proto.parse_live_monitoring(self._make_payload(link=2))
        assert result["link_status"] == "BACKUP"

    def test_link_status_disconnected(self):
        result = proto.parse_live_monitoring(self._make_payload(link=0))
        assert result["link_status"] == "DISCONNECTED"

    def test_firmware(self):
        result = proto.parse_live_monitoring(self._make_payload(fw_major=2, fw_minor=16))
        assert result["firmware"] == "2.16"

    def test_mac_address(self):
        result = proto.parse_live_monitoring(self._make_payload())
        assert result["mac_address"] == "32:54:76:98:BA:0C"

    def test_rejects_short_data(self):
        assert proto.parse_live_monitoring(b"\x80" * 10) is None

    def test_rejects_none(self):
        assert proto.parse_live_monitoring(None) is None

    def test_all_14_cards_capture_values(self):
        """Verify parsing works for the exact byte values seen in Wireshark captures.

        Card temperatures from capture: 55, 56, 58, 57, 59, 57, 58, 58, 57, 58, 57, 56, 56, 54
        Card voltages from capture: 5.19, 5.19, 5.16, 5.16, 5.16, 5.19, 5.16, 5.19,
                                     5.19, 5.19, 5.22, 5.22, 5.19, 5.22
        """
        raw_temps = [110, 112, 116, 114, 118, 114, 116, 116, 114, 116, 114, 112, 112, 108]
        raw_volts = [173, 173, 172, 172, 172, 173, 172, 173, 173, 173, 174, 174, 173, 174]
        for i, (t, v) in enumerate(zip(raw_temps, raw_volts)):
            result = proto.parse_live_monitoring(self._make_payload(temp=t, volt=v))
            assert result["online"] is True
            assert result["temperature_c"] == t / 2.0
            assert result["link_status"] == "PRIMARY"


class TestParseNSSD:
    def test_valid_nssd(self):
        data = b"NSSD\x00\x58\xe9\x03\x07\x00\x1c\x56"
        result = proto.parse_nssd(data)
        assert result["header"] == "NSSD"
        assert result["active"] is True
        assert result["model_code"] == "0x0058"

    def test_inactive(self):
        data = b"\x00" * 12
        result = proto.parse_nssd(data)
        assert result["active"] is False

    def test_short_data(self):
        assert proto.parse_nssd(b"NSS") is None


# ── Length Encoding ────────────────────────────────────────


class TestEncodeLength:
    """Test the NovaStar split-format length encoding."""

    def test_1_byte(self):
        assert proto.encode_length(1) == 0x0100

    def test_2_bytes(self):
        assert proto.encode_length(2) == 0x0200

    def test_8_bytes(self):
        assert proto.encode_length(8) == 0x0800

    def test_82_bytes(self):
        assert proto.encode_length(82) == 0x5200

    def test_256_bytes(self):
        assert proto.encode_length(256) == 0x0001

    def test_512_bytes(self):
        assert proto.encode_length(512) == 0x0002


class TestDecodeLength:
    """Test the NovaStar split-format length decoding."""

    def test_page_mode_1(self):
        assert proto.decode_length(0x0001) == 256

    def test_page_mode_2(self):
        assert proto.decode_length(0x0002) == 512

    def test_byte_mode_1(self):
        assert proto.decode_length(0x0100) == 1

    def test_byte_mode_2(self):
        assert proto.decode_length(0x0200) == 2

    def test_byte_mode_8(self):
        assert proto.decode_length(0x0800) == 8

    def test_byte_mode_82(self):
        assert proto.decode_length(0x5200) == 82

    def test_round_trip(self):
        for byte_count in [1, 2, 8, 82, 256, 512]:
            encoded = proto.encode_length(byte_count)
            assert proto.decode_length(encoded) == byte_count

    def test_existing_registers_decode_correctly(self):
        """Verify all existing register definitions decode to expected sizes."""
        assert proto.decode_length(proto.REG_SYSTEM_INFO[1]) == 256
        assert proto.decode_length(proto.REG_FIRMWARE[1]) == 2
        assert proto.decode_length(proto.REG_BRIGHTNESS[1]) == 1
        assert proto.decode_length(proto.REG_GAMMA[1]) == 2
        assert proto.decode_length(proto.REG_DATETIME[1]) == 8
        assert proto.decode_length(proto.REG_VIDEO_STATUS[1]) == 512
        assert proto.decode_length(proto.REG_LIVE_MONITOR[1]) == 82


# ── H-Series Parsing ──────────────────────────────────────


class TestHPortBitmask:
    """Test H-series port connectivity bitmask parsing."""

    def test_all_connected(self):
        """0xFF = all 8 low ports connected."""
        data = bytearray(512)
        data[31] = 0xFF
        result = proto.parse_h_port_bitmask(bytes(data))
        assert result[1] is True
        assert result[8] is True

    def test_none_connected(self):
        data = bytearray(512)
        data[31] = 0x00
        result = proto.parse_h_port_bitmask(bytes(data))
        assert result[1] is False
        assert result[15] is False

    def test_capture_original(self):
        """0xAF from original capture = 6 ports connected."""
        data = bytearray(512)
        data[31] = 0xAF  # 10101111
        result = proto.parse_h_port_bitmask(bytes(data))
        assert result[1] is True   # bit 0
        assert result[2] is True   # bit 1
        assert result[3] is True   # bit 2
        assert result[4] is True   # bit 3
        assert result[5] is False  # bit 4
        assert result[6] is True   # bit 5

    def test_capture_backup(self):
        """0xAC from backup capture = ports 1,2 disconnected."""
        data = bytearray(512)
        data[31] = 0xAC  # 10101100
        result = proto.parse_h_port_bitmask(bytes(data))
        assert result[1] is False  # bit 0 — port 1 disconnected
        assert result[2] is False  # bit 1 — port 2 disconnected
        assert result[3] is True   # bit 2 — port 3 still up
        assert result[4] is True   # bit 3

    def test_short_data(self):
        assert proto.parse_h_port_bitmask(b"\x00" * 10) == {}

    def test_none_data(self):
        assert proto.parse_h_port_bitmask(None) == {}


class TestHCardLink:
    """Test H-series per-card link path status."""

    def test_all_paths(self):
        data = bytearray(512)
        data[1] = 0x7F  # 01111111 = 7 paths
        result = proto.parse_h_card_link(bytes(data))
        assert result == (7, 7)

    def test_partial_paths(self):
        data = bytearray(512)
        data[1] = 0x3D  # 00111101 = 5 paths
        result = proto.parse_h_card_link(bytes(data))
        assert result == (5, 7)

    def test_no_paths(self):
        data = bytearray(512)
        data[1] = 0x00
        result = proto.parse_h_card_link(bytes(data))
        assert result == (0, 7)

    def test_short_data(self):
        assert proto.parse_h_card_link(b"\x1c") is None

    def test_none(self):
        assert proto.parse_h_card_link(None) is None


class TestHCardTemperature:
    """Test H-series per-card temperature parsing."""

    def test_capture_value(self):
        """0x5D = 93 → 46.5°C from capture."""
        data = bytes([0x5D]) + b"\x00" * 511
        assert proto.parse_h_card_temperature(data) == 46.5

    def test_zero(self):
        assert proto.parse_h_card_temperature(b"\x00") == 0.0

    def test_max(self):
        assert proto.parse_h_card_temperature(b"\xFF") == 127.5

    def test_none(self):
        assert proto.parse_h_card_temperature(None) is None

    def test_empty(self):
        assert proto.parse_h_card_temperature(b"") is None


class TestHCardDataRegister:
    """Test H-series per-card data channel register addressing."""

    def test_channel_0(self):
        addr, length = proto.h_card_data_register(0)
        assert addr == 0x00400003

    def test_channel_1(self):
        addr, length = proto.h_card_data_register(1)
        assert addr == 0x00420003

    def test_channel_7(self):
        addr, length = proto.h_card_data_register(7)
        assert addr == 0x004E0003

    def test_length_is_512(self):
        for ch in range(8):
            _, length = proto.h_card_data_register(ch)
            assert proto.decode_length(length) == 512


class TestHPortVideoRegister:
    """Test H-series per-port video status register addressing."""

    def test_port_1(self):
        addr, length = proto.h_port_video_register(1)
        assert addr == 0x00010002

    def test_port_2(self):
        addr, length = proto.h_port_video_register(2)
        assert addr == 0x00020002

    def test_port_15(self):
        addr, length = proto.h_port_video_register(15)
        assert addr == 0x000F0002

    def test_length_is_512(self):
        _, length = proto.h_port_video_register(1)
        assert proto.decode_length(length) == 512


class TestHSystemInfo:
    """Test H-series system info parsing."""

    def test_capture_data(self):
        """From capture: byte[0]=0x09 (A8s), bytes[1:5]=45 04 06 02."""
        data = bytes.fromhex("0945040602010405015100000000000000000000")
        result = proto.parse_h_system_info(data)
        assert result["hw_type"] == "0x09"
        assert result["fpga_major"] == 0x45
        assert result["fpga_minor"] == 4
        assert result["fw_revision"] == 6

    def test_short_data(self):
        assert proto.parse_h_system_info(b"\x09" * 5) is None

    def test_none(self):
        assert proto.parse_h_system_info(None) is None
