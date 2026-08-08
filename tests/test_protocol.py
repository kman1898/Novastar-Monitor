"""Tests for novastar_protocol.py — frame building and data parsing."""
import struct
import novastar_protocol as proto


# ── Checksum (formula verified against VX1000 + Sending Card protocol docs) ──


class TestChecksum:
    """SUM = sum(body) + 0x5555, little-endian wire encoding.

    Body = bytes between the 2-byte frame header and the 2-byte checksum.
    """

    def test_vx1000_doc_modeId_read(self):
        """VX1000 §3.1.2: read ModeID body sums to 0x102, +0x5555 = 0x5657."""
        body = bytes.fromhex("0000fe0000000000000000020000000200")
        assert proto.checksum(body) == 0x5657

    def test_vx1000_doc_brightness_zero(self):
        """VX1000 §3.2.1: brightness=0% body sums to 0x500, +0x5555 = 0x5A55."""
        body = bytes.fromhex("0000feff01ffffff010001000000020100") + b"\x00"
        assert proto.checksum(body) == 0x5A55

    def test_vx1000_doc_modeId_response(self):
        """VX1000 §3.1.2: response with payload 0x0C 0x62 yields checksum 0x56C5."""
        body = bytes.fromhex("000000fe0000000000000000020000000200") + b"\x0C\x62"
        assert proto.checksum(body) == 0x56C5

    def test_empty_body(self):
        assert proto.checksum(b"") == 0x5555

    def test_wraps_at_16_bits(self):
        """Sum must wrap to 16 bits."""
        body = b"\xFF" * 200  # sum = 51000 = 0xC738, +0x5555 = 0x11C8D → 0x1C8D
        assert proto.checksum(body) == 0x1C8D


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
        """Last 2 bytes are the documented checksum: sum(body) + 0x5555, little-endian."""
        frame = proto.build_read(1, 0, 1)
        csum_le = struct.unpack("<H", frame[18:20])[0]
        expected = (sum(frame[2:18]) + 0x5555) & 0xFFFF
        assert csum_le == expected

    def test_matches_vx1000_doc_modeId_read(self):
        """VX1000 Control Protocol V1.0 §3.1.2 — read ModeID command.

        Documented wire bytes:
          55 aa 00 00 fe 00 00 00 00 00 00 00 02 00 00 00 02 00 57 56
        """
        frame = proto.build_read(seq=0, register=0x02000000, length=0x0200)
        assert frame.hex() == "55aa0000fe000000000000000200000002005756"


class TestBuildReadCard:
    """Per-card addressing per H_SERIES_FINDINGS §6.5.

    byte[5] = OPT group, byte[6] = 0x01 marker, byte[7] = CHAIN index,
    byte[8] = CARD position within that chain, bytes[9-11] = 0x00.
    """

    def test_length(self):
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, 0, 0)
        assert len(frame) == 20

    def test_header(self):
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, 0, 0)
        assert frame[0:2] == b"\x55\xAA"

    def test_opt_group_byte5_defaults_to_zero(self):
        """Byte 5 is the OPT group — 0x00 in all observed traffic."""
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, 3, 7)
        assert frame[5] == 0x00

    def test_opt_group_byte5_is_the_port_kwarg(self):
        """The `port` kwarg still writes byte 5 — it is NOT the chain."""
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, 3, 7, port=0x02)
        assert frame[5] == 0x02
        assert frame[7] == 3  # chain unaffected by the OPT group

    def test_per_card_marker_byte6(self):
        """Byte 6 must be 0x01 (direct-to-receiving-card command)."""
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, 0, 5)
        assert frame[6] == 0x01

    def test_chain_index_lands_in_byte7(self):
        """Byte 7 is the chain index — 16 chains observed (0-15)."""
        for chain in range(proto.H_MAX_PORTS):
            frame = proto.build_read_card(1, 0x0000000A, 0x5200, chain, 0)
            assert frame[7] == chain

    def test_card_index_lands_in_byte8(self):
        """Byte 8 is the card position within the chain (0-based)."""
        for idx in range(14):
            frame = proto.build_read_card(1, 0x0000000A, 0x5200, 0, idx)
            assert frame[8] == idx

    def test_chain_and_card_are_independent(self):
        """Distinct chain values hit byte7, distinct card values hit byte8."""
        for chain in (0, 1, 3, 9, 15):
            for card in (0, 1, 13, 48, 90):
                frame = proto.build_read_card(1, 0x0000000A, 0x5200, chain, card)
                assert frame[7] == chain
                assert frame[8] == card

    def test_distinct_pairs_produce_distinct_frames(self):
        """The (chain, card) pair must be uniquely encoded — this is what makes
        the §6.5 panel census (245 across 16 chains) countable at all."""
        frames = {
            proto.build_read_card(1, 0x0000000A, 0x5200, c, k)
            for c in range(16) for k in range(5)
        }
        assert len(frames) == 16 * 5

    def test_card_index_high_byte_is_zero(self):
        """Byte 9 is the card index high byte — 0x00 in all observed traffic."""
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, 5, 90)
        assert frame[9] == 0x00

    def test_trailing_zeros(self):
        """Bytes 10-11 must be zeros."""
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, 2, 13)
        assert frame[10:12] == b"\x00\x00"

    def test_register_and_length(self):
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, 0, 0)
        reg = struct.unpack(">I", frame[12:16])[0]
        length = struct.unpack(">H", frame[16:18])[0]
        assert reg == 0x0000000A
        assert length == 0x5200

    def test_matches_capture_format(self):
        """§6.5 layout for chain 3, card 13, reg=0x0000000a, len=0x5200:
          bytes 5-11 = 00 01 03 0d 00 00 00
        """
        frame = proto.build_read_card(0x00C4, 0x0000000A, 0x5200, 3, 13)
        assert frame[4] == 0xFE  # device (broadcast)
        assert frame[5:12] == bytes([0x00, 0x01, 0x03, 0x0D, 0x00, 0x00, 0x00])
        assert struct.unpack(">I", frame[12:16])[0] == 0x0000000A
        assert struct.unpack(">H", frame[16:18])[0] == 0x5200

    def test_checksum_covers_the_chain_byte(self):
        """Changing the chain must change the checksum — proves byte7 is summed."""
        a = proto.build_read_card(1, 0x0000000A, 0x5200, 0, 4)
        b = proto.build_read_card(1, 0x0000000A, 0x5200, 1, 4)
        assert a[18:20] != b[18:20]
        for frame in (a, b):
            expected = (sum(frame[2:18]) + 0x5555) & 0xFFFF
            assert struct.unpack("<H", frame[18:20])[0] == expected

    def test_masks_out_of_range_values(self):
        """Defensive: values are masked to a byte rather than raising."""
        frame = proto.build_read_card(1, 0x0000000A, 0x5200, 0x101, 0x1FF)
        assert frame[7] == 0x01
        assert frame[8] == 0xFF


# ── Response Parsing ────────────────────────────────────────


class TestParseResponse:
    def _make_response(self, reg, payload, seq=1):
        """Build a minimal valid response frame using the documented checksum."""
        header = struct.pack(">H", proto.HEADER_RESPONSE)
        body = struct.pack(">HBB6xIH", seq, 0x00, 0xFE, reg, len(payload)) + payload
        return header + body + struct.pack("<H", proto.checksum(body))

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

    def test_byte_mode_3(self):
        """3-byte registers encode as 0x0300 (high byte = count, low byte = 0)."""
        assert proto.decode_length(0x0300) == 3

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
    """Test H-series port connectivity bitmask parsing.

    Byte[31] is one byte, so it measures exactly 8 chains. §6.5 shows a sender
    card addresses 16, but no capture shows a second bitmask byte — so ports
    9-16 are omitted from the result rather than reported as disconnected.
    """

    def test_all_connected(self):
        """0xFF = all 8 measured ports connected."""
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
        assert result[8] is False

    def test_covers_exactly_8_ports(self):
        """Only 8 bits exist, so only ports 1-8 are reported."""
        data = bytearray(512)
        data[31] = 0xFF
        result = proto.parse_h_port_bitmask(bytes(data))
        assert sorted(result) == list(range(1, 9))
        assert len(result) == proto.H_PORT_BITMASK_BITS

    def test_unmeasured_ports_are_absent_not_false(self):
        """Ports 9-16 were never measured — absent, so callers can't mistake
        an unmeasured chain for a disconnected one."""
        data = bytearray(512)
        data[31] = 0xFF
        result = proto.parse_h_port_bitmask(bytes(data))
        for port in range(9, proto.H_MAX_PORTS + 1):
            assert port not in result

    def test_bitmask_limit_is_below_addressable_chains(self):
        """The 8-bit measurement window is narrower than the 16 addressable
        chains — the contradiction is explicit, not silently papered over."""
        assert proto.H_PORT_BITMASK_BITS == 8
        assert proto.H_MAX_PORTS == 16

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
    """parse_h_card_temperature is DEPRECATED — register 0x00400003 isn't temp.

    These tests pin the legacy raw byte/2.0 behavior so any historical caller
    keeps working. The function itself shouldn't be used for actual temperature
    monitoring; use parse_live_monitoring on REG_LIVE_MONITOR (0x0000000A) instead.
    """

    def test_legacy_value(self):
        """0x5D = 93 → 46.5 (constant value seen in every capture)."""
        data = bytes([0x5D]) + b"\x00" * 511
        assert proto.parse_h_card_temperature(data) == 46.5

    def test_zero(self):
        assert proto.parse_h_card_temperature(b"\x00") == 0.0

    def test_none(self):
        assert proto.parse_h_card_temperature(None) is None

    def test_empty(self):
        assert proto.parse_h_card_temperature(b"") is None


class TestHCardFault:
    """Per-card fault flag from register 0x09050002 — single byte, 0 = no fault."""

    def test_no_fault(self):
        result = proto.parse_h_card_fault(b"\x00")
        assert result == {"fault": False, "code": 0}

    def test_fault_present(self):
        result = proto.parse_h_card_fault(b"\x05")
        assert result == {"fault": True, "code": 5}

    def test_max_fault_code(self):
        result = proto.parse_h_card_fault(b"\xFF")
        assert result == {"fault": True, "code": 255}

    def test_extra_bytes_ignored(self):
        """The doc-confirmed register length is 1 byte; extra bytes don't break parsing."""
        result = proto.parse_h_card_fault(b"\x03\xFF\xFF\xFF")
        assert result == {"fault": True, "code": 3}

    def test_none(self):
        assert proto.parse_h_card_fault(None) is None

    def test_empty(self):
        assert proto.parse_h_card_fault(b"") is None


class TestHCardFaultRegister:
    """The fault register address constant must be addressable correctly."""

    def test_register_address(self):
        addr, length = proto.H_REG_CARD_FAULT
        assert addr == 0x09050002

    def test_register_length_is_one_byte(self):
        _, length = proto.H_REG_CARD_FAULT
        assert proto.decode_length(length) == 1


class TestBitErrorRegister:
    """Register 0x4A010002 — 3-byte per-card bit-error counter (§6.5)."""

    def test_register_address(self):
        addr, _ = proto.H_REG_BIT_ERRORS
        assert addr == 0x4A010002

    def test_register_length_is_three_bytes(self):
        """Split encoding: 3 bytes is byte mode → 0x0300."""
        _, length = proto.H_REG_BIT_ERRORS
        assert length == 0x0300
        assert proto.decode_length(length) == 3


class TestParseBitErrors:
    """Per-card bit-error counter — the only continuous data-integrity signal.

    byte[0] = status (0x05 = present), bytes[1-2] = uint16 LITTLE-endian count.
    """

    def test_capture_baseline_zero_errors(self):
        """Real captured healthy value: 05 00 00."""
        result = proto.parse_bit_errors(bytes.fromhex("050000"))
        assert result["present"] is True
        assert result["errors"] == 0
        assert result["saturated"] is False

    def test_nonzero_errors(self):
        """05 2a 00 → 0x002A = 42 errors (little-endian)."""
        result = proto.parse_bit_errors(bytes.fromhex("052a00"))
        assert result["present"] is True
        assert result["errors"] == 42
        assert result["saturated"] is False

    def test_little_endian_byte_order(self):
        """05 01 02 must decode as 0x0201 = 513, not 0x0102 = 258."""
        result = proto.parse_bit_errors(bytes.fromhex("050102"))
        assert result["errors"] == 513

    def test_high_byte_only(self):
        """05 00 01 → 0x0100 = 256 errors."""
        result = proto.parse_bit_errors(bytes.fromhex("050001"))
        assert result["errors"] == 256

    def test_saturated(self):
        """05 ff ff = counter ceiling — serious data integrity fault."""
        result = proto.parse_bit_errors(bytes.fromhex("05ffff"))
        assert result["present"] is True
        assert result["errors"] == 0xFFFF
        assert result["saturated"] is True

    def test_just_below_saturation_is_not_saturated(self):
        result = proto.parse_bit_errors(bytes.fromhex("05feff"))
        assert result["errors"] == 65534
        assert result["saturated"] is False

    def test_card_not_present(self):
        """Any status other than 0x05 means the card isn't responding."""
        result = proto.parse_bit_errors(bytes.fromhex("000000"))
        assert result["present"] is False
        assert result["status"] == 0x00

    def test_status_byte_preserved(self):
        result = proto.parse_bit_errors(bytes.fromhex("050000"))
        assert result["status"] == 0x05

    def test_extra_bytes_ignored(self):
        """The register is 3 bytes; a longer read still parses the first 3."""
        result = proto.parse_bit_errors(bytes.fromhex("050300ffffff"))
        assert result["errors"] == 3

    def test_short_data(self):
        assert proto.parse_bit_errors(b"\x05\x00") is None

    def test_empty(self):
        assert proto.parse_bit_errors(b"") is None

    def test_none(self):
        assert proto.parse_bit_errors(None) is None

    def test_full_uint16_range_round_trips(self):
        for value in (0, 1, 255, 256, 4096, 30000, 65534, 65535):
            payload = b"\x05" + struct.pack("<H", value)
            assert proto.parse_bit_errors(payload)["errors"] == value
