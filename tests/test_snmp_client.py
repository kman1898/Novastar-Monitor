"""Tests for the read-only SNMPv2c client.

Nothing here touches real hardware — every exchange runs against a loopback
UDP stub that speaks just enough SNMP to answer GET and GETNEXT from a canned
MIB. The MIB values are the ones actually captured from the operator's H15
running V2.0.0.6, including the embedded JSON documents and the
`ERROR: BizIdError` sentinel the device returns for SET-gated OIDs.

The one piece of ground truth that does NOT come from this module is
`CAPTURED_SYSDESCR_GET`: a verified-on-the-wire GetRequest, hardcoded, so the
encoder is pinned to real bytes rather than to its own decoder.
"""

import io
import ast
import json
import socket
import struct
import threading
import tokenize
import pytest

import snmp_client as sc


# ── Verified wire bytes ───────────────────────────────────────────────────
# GetRequest for sysDescr.0 (1.3.6.1.2.1.1.1.0), community "public",
# request-id 0x7a69c0e5. Captured from a working exchange.
CAPTURED_SYSDESCR_GET = bytes.fromhex(
    "302902010104067075626c6963a01c02047a69c0e5020100020100"
    "300e300c06082b060102010101000500"
)


# ── Captured H15 payloads ─────────────────────────────────────────────────

DEVICE_SUMMARY_JSON = json.dumps({
    "ARMVersion": "V2.0.0.6",
    "MAC": "54-b5-6c-0a-3e-7e",
    "SN": "16081800D74C0000",
    "cpuStatus": 0,
    "fansCount": 10,
    "powersCount": 4,
})

FANS_JSON = json.dumps(
    [{"fanId": i, "speed": 0, "status": 0} for i in range(10)])

PSUS_JSON = json.dumps(
    [{"iSignal": 1, "powerId": i, "status": 0, "voltage": 0}
     for i in range(4)])

INPUT_SUMMARY_JSON = json.dumps({
    "SN": "16081800D74C0000",
    "inputSourceCount": 1,
    "status": 1,
    "version": "2.0.0.6",
})

OUTPUT_SUMMARY_JSON = json.dumps({
    "SN": "16081800D74C0000",
    "portCount": 4,
    "status": 0,
    "version": "2.0.0.6",
})

BIZ_ID_ERROR = "ERROR: BizIdError"

# How often a stub thread checks its stop flag. This is pure teardown latency
# — almost every test builds a stub — so it is kept well below the client
# timeouts rather than at a "realistic" value.
POLL_INTERVAL = 0.02

# Deadline for the tests where the stub deliberately says nothing (timeout,
# malformed reply, stale request id). Short is safe here: the assertion is
# that nothing usable arrived, and loopback either delivers in microseconds or
# not at all.
NO_REPLY_TIMEOUT = 0.06

DEV = sc.OID_DEVICE
INP = sc.OID_INPUT
OUT = sc.OID_OUTPUT
SCR = sc.OID_SCREEN


# ── Value builders for the stub MIB ───────────────────────────────────────


class Val:
    """A raw (tag, value-octets) pair to be served by the stub."""

    def __init__(self, tag, data):
        self.tag = tag
        self.data = data


def integer(n):
    return Val(sc.TAG_INTEGER, sc._int_octets(n))


def octets(text):
    return Val(sc.TAG_OCTET_STRING, text.encode("utf-8"))


def opaque_float(value):
    """Opaque-wrapped IEEE-754 single, exactly as net-snmp renders `Float:`."""
    inner = b"\x9f\x78\x04" + struct.pack(">f", value)
    return Val(sc.TAG_OPAQUE, inner)


def opaque_double(value):
    inner = b"\x9f\x79\x08" + struct.pack(">d", value)
    return Val(sc.TAG_OPAQUE, inner)


def gauge(n):
    return Val(sc.TAG_GAUGE32, n.to_bytes(4, "big"))


def timeticks(n):
    return Val(sc.TAG_TIMETICKS, n.to_bytes(4, "big"))


def ip_address(dotted):
    return Val(sc.TAG_IP_ADDRESS, bytes(int(p) for p in dotted.split(".")))


def marker(tag):
    return Val(tag, b"")


# The captured device, as a MIB the stub can serve.
H15_MIB = {
    DEV + ".0": octets(DEVICE_SUMMARY_JSON),
    DEV + ".1": octets("2026-08-08 17:32:18"),
    DEV + ".2": octets("H15"),
    DEV + ".3": octets("V2.0.0.6"),
    DEV + ".4": octets("16081800D74C0000"),
    DEV + ".5": octets("54-b5-6c-0a-3e-7e"),
    DEV + ".6": octets("192.168.0.10"),
    DEV + ".7": octets("V2.0.0.6"),
    DEV + ".8": integer(0),
    DEV + ".9": integer(10),
    DEV + ".10": integer(4),
    DEV + ".11": integer(0),
    DEV + ".12": integer(0),
    DEV + ".13": integer(0),
    DEV + ".14": integer(0),
    DEV + ".15": integer(0),
    DEV + ".16": octets(FANS_JSON),
    DEV + ".17": octets(PSUS_JSON),

    INP + ".1": integer(6),
    INP + ".2.1": integer(1),
    INP + ".2.2": octets("2.0.0.6"),
    INP + ".2.3": octets("16081800D74C0000"),
    INP + ".2.4": integer(1),
    INP + ".3": octets(INPUT_SUMMARY_JSON),
    INP + ".5.1": integer(1),
    INP + ".5.2": integer(3840),
    INP + ".5.3": integer(2160),
    INP + ".5.4": opaque_float(60.0),
    INP + ".5.5": integer(6),

    OUT + ".1": integer(8),
    OUT + ".2.1": integer(0),
    OUT + ".2.2": octets("2.0.0.6"),
    OUT + ".2.3": octets("16081800D74C0000"),
    OUT + ".2.4": integer(4),
    OUT + ".3": octets(OUTPUT_SUMMARY_JSON),
    # .5.2 is deliberately absent — the real device answered .1, .3 and .4 only.
    OUT + ".5.1": integer(1),
    OUT + ".5.3": integer(1),
    OUT + ".5.4": integer(0),
    # SET-gated: readable, but every value is the error sentinel.
    OUT + ".6": octets(BIZ_ID_ERROR),
    OUT + ".7.1": octets(BIZ_ID_ERROR),
    OUT + ".7.2": octets(BIZ_ID_ERROR),

    SCR + ".1": integer(2),
    SCR + ".2.1": octets("CIRCUIT MOM"),
    SCR + ".2.2": integer(3840),
    SCR + ".2.3": integer(2160),
    SCR + ".2.4": opaque_float(60.0),
    SCR + ".2.5": integer(10),
    SCR + ".2.6": integer(0),
    SCR + ".2.7": integer(0),
}


# ── Loopback SNMP stub ────────────────────────────────────────────────────


def encode_varbind(oid, val):
    if val is None:
        body = sc.encode_oid(oid) + sc.encode_null()
    else:
        body = (sc.encode_oid(oid)
                + bytes([val.tag]) + sc.encode_length(len(val.data)) + val.data)
    return sc.encode_sequence(body)


def encode_response(request_id, varbinds, community="public",
                    error_status=0, error_index=0, pdu_type=sc.PDU_RESPONSE,
                    version=sc.SNMP_VERSION_2C):
    payload = b"".join(encode_varbind(oid, val) for oid, val in varbinds)
    pdu_body = (sc.encode_integer(request_id)
                + sc.encode_integer(error_status)
                + sc.encode_integer(error_index)
                + sc.encode_sequence(payload))
    pdu = bytes([pdu_type]) + sc.encode_length(len(pdu_body)) + pdu_body
    return sc.encode_sequence(
        sc.encode_integer(version) + sc.encode_octet_string(community) + pdu)


def decode_request(data):
    """Minimal request parser for the stub: -> (pdu_type, request_id, [oids])."""
    tag, body, _ = sc.parse_tlv(data)
    _, _, off = sc.parse_tlv(body, 0)              # version
    _, community, off = sc.parse_tlv(body, off)    # community
    pdu_type, pdu, _ = sc.parse_tlv(body, off)

    _, rid_bytes, off = sc.parse_tlv(pdu, 0)
    request_id = sc.decode_integer(rid_bytes)
    _, _, off = sc.parse_tlv(pdu, off)             # error-status
    _, _, off = sc.parse_tlv(pdu, off)             # error-index
    _, vb_list, _ = sc.parse_tlv(pdu, off)

    oids = []
    off = 0
    while off < len(vb_list):
        _, vb, off = sc.parse_tlv(vb_list, off)
        _, name, _ = sc.parse_tlv(vb, 0)
        oids.append(sc.format_oid(sc.decode_oid(name)))
    return pdu_type, request_id, oids, community.decode("utf-8", "replace")


class SNMPStub:
    """UDP loopback agent serving a canned MIB.

    Records every PDU type it is asked for in `self.pdu_types`, which is what
    lets a test prove the client never emits anything but GET / GETNEXT.
    `reply_hook(pdu_type, request_id, oids)` can override the response with raw
    bytes (or None to stay silent) for the malformed / timeout cases.
    """

    def __init__(self, mib=None, reply_hook=None, community="public"):
        self.mib = dict(mib or {})
        self.sorted_oids = sorted(self.mib, key=sc.parse_oid)
        self.reply_hook = reply_hook
        self.community = community
        self.pdu_types = []
        self.requests = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(POLL_INTERVAL)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                pdu_type, request_id, oids, _community = decode_request(data)
            except (TypeError, ValueError):
                continue
            self.pdu_types.append(pdu_type)
            self.requests.append((pdu_type, oids))
            if self.reply_hook is not None:
                reply = self.reply_hook(pdu_type, request_id, oids)
            else:
                reply = self._answer(pdu_type, request_id, oids)
            if reply is None:
                continue
            try:
                self._sock.sendto(reply, addr)
            except OSError:
                return

    def _answer(self, pdu_type, request_id, oids):
        varbinds = []
        for oid in oids:
            if pdu_type == sc.PDU_GET:
                val = self.mib.get(oid)
                if val is None:
                    varbinds.append((oid, marker(sc.TAG_NO_SUCH_OBJECT)))
                else:
                    varbinds.append((oid, val))
            elif pdu_type == sc.PDU_GET_NEXT:
                nxt = self._next(oid)
                if nxt is None:
                    varbinds.append((oid, marker(sc.TAG_END_OF_MIB_VIEW)))
                else:
                    varbinds.append((nxt, self.mib[nxt]))
            else:                       # pragma: no cover - never sent
                return None
        return encode_response(request_id, varbinds, community=self.community)

    def _next(self, oid):
        target = sc.parse_oid(oid)
        for candidate in self.sorted_oids:
            if sc.parse_oid(candidate) > target:
                return candidate
        return None

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            self._sock.close()
        except Exception:
            pass


@pytest.fixture
def h15():
    """A stub serving the captured H15 MIB, plus a client wired to it."""
    stub = SNMPStub(H15_MIB)
    client = sc.SNMPClient("127.0.0.1", port=stub.port, timeout=0.5)
    try:
        yield stub, client
    finally:
        client.close()
        stub.stop()


# ══ BER round-trips ════════════════════════════════════════════════════════


def test_encode_length_short_and_long_form():
    assert sc.encode_length(0) == b"\x00"
    assert sc.encode_length(127) == b"\x7f"
    assert sc.encode_length(128) == b"\x81\x80"
    assert sc.encode_length(300) == b"\x82\x01\x2c"


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 256, 3840, 2160,
                                   -1, -128, 0x7a69c0e5, 2147483647])
def test_integer_round_trip(value):
    tag, body, end = sc.parse_tlv(sc.encode_integer(value))
    assert tag == sc.TAG_INTEGER
    assert end == len(sc.encode_integer(value))
    assert sc.decode_integer(body) == value


def test_positive_integer_gets_leading_zero_pad():
    """128 must encode as 00 80, not 80 — else the agent reads it as -128."""
    assert sc.encode_integer(128) == b"\x02\x02\x00\x80"
    assert sc.decode_integer(b"\x80") == -128


@pytest.mark.parametrize("text", [
    "H15", "V2.0.0.6", "CIRCUIT MOM", "54-b5-6c-0a-3e-7e",
    "2026-08-08 17:32:18", "", DEVICE_SUMMARY_JSON, FANS_JSON, PSUS_JSON,
])
def test_octet_string_round_trip(text):
    tag, body, _ = sc.parse_tlv(sc.encode_octet_string(text))
    assert tag == sc.TAG_OCTET_STRING
    assert sc.decode_octet_string(body) == text


def test_octet_string_non_utf8_falls_back_to_bytes():
    assert sc.decode_octet_string(b"\xff\xfe\x00") == b"\xff\xfe\x00"


def test_null_round_trip():
    tag, body, _ = sc.parse_tlv(sc.encode_null())
    assert tag == sc.TAG_NULL
    assert body == b""
    assert sc.decode_value(sc.TAG_NULL, body) is None


@pytest.mark.parametrize("oid", [
    "1.3.6.1.2.1.1.1.0",
    "1.3.6.1.4.1.319",
    "1.3.6.1.4.1.319.10.10.1.0",
    "1.3.6.1.4.1.319.10.10.30.7.2",
    "0.0",
    "2.100.3",
])
def test_oid_round_trip(oid):
    tag, body, _ = sc.parse_tlv(sc.encode_oid(oid))
    assert tag == sc.TAG_OID
    assert sc.format_oid(sc.decode_oid(body)) == oid


def test_oid_first_two_arcs_are_packed():
    """1.3... starts with 0x2b (40*1+3), not 0x01 0x03."""
    _, body, _ = sc.parse_tlv(sc.encode_oid("1.3.6.1.2.1.1.1.0"))
    assert body == bytes.fromhex("2b06010201010100")


def test_oid_multibyte_arc():
    """319 needs base-128 continuation: 0x82 0x3f."""
    _, body, _ = sc.parse_tlv(sc.encode_oid("1.3.6.1.4.1.319"))
    assert body.endswith(b"\x82\x3f")


@pytest.mark.parametrize("bad", [None, "", "...", "1", "1.x.3", "1.-2.3",
                                 "3.1.1", 42, object()])
def test_encode_oid_rejects_garbage(bad):
    assert sc.encode_oid(bad) is None


def test_oid_packed_head_needing_two_octets():
    """2.100.3 packs to 40*2+100 = 180, which needs base-128 continuation.

    Regression: decoding byte[0] as `40*a + b` turned this into 3.9.52.3.
    """
    _, body, _ = sc.parse_tlv(sc.encode_oid("2.100.3"))
    assert body == b"\x81\x34\x03"
    assert sc.decode_oid(body) == (2, 100, 3)


def test_decode_oid_rejects_truncated_continuation():
    assert sc.decode_oid(b"\x2b\x82") is None


@pytest.mark.parametrize("value", [60.0, 59.94, 30.0, 24.0, 0.0])
def test_opaque_float_round_trip(value):
    decoded = sc.decode_value(sc.TAG_OPAQUE, opaque_float(value).data)
    assert isinstance(decoded, float)
    assert decoded == pytest.approx(value, rel=1e-6)


def test_opaque_double_round_trip():
    decoded = sc.decode_value(sc.TAG_OPAQUE, opaque_double(59.94).data)
    assert decoded == pytest.approx(59.94)


def test_opaque_unknown_payload_stays_bytes():
    """An Opaque we can't identify is returned raw, never guessed at."""
    assert sc.decode_value(sc.TAG_OPAQUE, b"\x9f\x7a\x02\x01\x02") == \
        b"\x9f\x7a\x02\x01\x02"
    assert sc.decode_value(sc.TAG_OPAQUE, b"abc") == b"abc"


def test_unsigned_application_types():
    assert sc.decode_value(sc.TAG_GAUGE32, gauge(4000000000).data) == 4000000000
    assert sc.decode_value(sc.TAG_TIMETICKS, timeticks(123456).data) == 123456
    assert sc.decode_value(sc.TAG_COUNTER32, (7).to_bytes(4, "big")) == 7


def test_ip_address_decodes_dotted():
    assert sc.decode_value(sc.TAG_IP_ADDRESS,
                           ip_address("192.168.0.10").data) == "192.168.0.10"


def test_exception_markers_decode_to_falsey_singletons():
    for tag, expected in ((sc.TAG_NO_SUCH_OBJECT, sc.NO_SUCH_OBJECT),
                          (sc.TAG_NO_SUCH_INSTANCE, sc.NO_SUCH_INSTANCE),
                          (sc.TAG_END_OF_MIB_VIEW, sc.END_OF_MIB_VIEW)):
        value = sc.decode_value(tag, b"")
        assert value is expected
        assert not value
        assert sc.usable_value(value) is None


def test_unknown_tag_survives_as_bytes():
    assert sc.decode_value(0x7F, b"\x01\x02") == b"\x01\x02"


# ══ Request encoding ═══════════════════════════════════════════════════════


def test_get_request_matches_captured_bytes():
    """Pinned to a verified on-the-wire GetRequest, not to our own decoder."""
    built = sc.build_request("1.3.6.1.2.1.1.1.0", "public",
                             request_id=0x7a69c0e5, pdu_type=sc.PDU_GET)
    assert built == CAPTURED_SYSDESCR_GET


def test_getnext_differs_only_in_pdu_tag():
    args = ("1.3.6.1.2.1.1.1.0", "public", 0x7a69c0e5)
    get = sc.build_request(*args, pdu_type=sc.PDU_GET)
    nxt = sc.build_request(*args, pdu_type=sc.PDU_GET_NEXT)
    assert nxt[4:] != get[4:]
    assert nxt.replace(b"\xa1", b"\xa0", 1) == get


def test_build_request_multi_oid():
    built = sc.build_request([DEV + ".2", DEV + ".3"], "public", 1)
    parsed = decode_request(built)
    assert parsed[0] == sc.PDU_GET
    assert parsed[2] == [DEV + ".2", DEV + ".3"]


def test_build_request_rejects_bad_input():
    assert sc.build_request([], "public", 1) is None
    assert sc.build_request(["1.3.6", "nope"], "public", 1) is None
    assert sc.build_request(None, "public", 1) is None


def test_version_field_is_2c():
    assert sc.SNMP_VERSION_2C == 1
    built = sc.build_request(DEV + ".2", "public", 1)
    _, body, _ = sc.parse_tlv(built)
    _, ver, _ = sc.parse_tlv(body, 0)
    assert sc.decode_integer(ver) == 1


def test_new_request_id_is_positive_31_bit():
    for _ in range(200):
        rid = sc.new_request_id()
        assert 0 < rid < 0x80000000


# ══ Response decoding ══════════════════════════════════════════════════════


def test_parse_response_round_trip():
    packet = encode_response(1234, [(DEV + ".2", octets("H15")),
                                    (DEV + ".9", integer(10))])
    parsed = sc.parse_response(packet, expect_request_id=1234)
    assert parsed["error_status"] == 0
    assert parsed["varbinds"] == [(DEV + ".2", "H15"), (DEV + ".9", 10)]


def test_parse_response_rejects_wrong_request_id():
    packet = encode_response(1, [(DEV + ".2", octets("H15"))])
    assert sc.parse_response(packet, expect_request_id=2) is None
    assert sc.parse_response(packet, expect_request_id=1) is not None


def test_parse_response_rejects_non_response_pdu():
    packet = encode_response(1, [(DEV + ".2", octets("H15"))],
                             pdu_type=sc.PDU_GET)
    assert sc.parse_response(packet) is None


def test_parse_response_reports_error_status():
    packet = encode_response(1, [], error_status=5, error_index=1)
    parsed = sc.parse_response(packet)
    assert parsed["error_status"] == 5
    assert parsed["error_index"] == 1


# ══ GET / GETNEXT against the stub ═════════════════════════════════════════


def test_get_scalar_string(h15):
    _stub, client = h15
    assert client.get(DEV + ".2") == "H15"
    assert client.get(DEV + ".3") == "V2.0.0.6"
    assert client.get(DEV + ".1") == "2026-08-08 17:32:18"


def test_get_scalar_integer(h15):
    _stub, client = h15
    assert client.get(DEV + ".9") == 10
    assert client.get(DEV + ".10") == 4
    assert client.get(DEV + ".8") == 0


def test_get_opaque_float_framerate(h15):
    _stub, client = h15
    assert client.get(INP + ".5.4") == pytest.approx(60.0)
    assert client.get(SCR + ".2.4") == pytest.approx(60.0)


def test_get_missing_oid_returns_none(h15):
    _stub, client = h15
    assert client.get(DEV + ".999") is None


def test_get_invalid_oid_returns_none(h15):
    _stub, client = h15
    assert client.get("not an oid") is None


def test_get_many_single_pdu(h15):
    stub, client = h15
    before = len(stub.requests)
    values = client.get_many([DEV + ".2", DEV + ".3", DEV + ".9"])
    assert values == {DEV + ".2": "H15", DEV + ".3": "V2.0.0.6", DEV + ".9": 10}
    assert len(stub.requests) - before == 1     # really one datagram


def test_get_many_omits_unanswerable_oids(h15):
    _stub, client = h15
    values = client.get_many([DEV + ".2", DEV + ".999"])
    assert values == {DEV + ".2": "H15"}
    assert DEV + ".999" not in values


def test_get_many_empty_input(h15):
    _stub, client = h15
    assert client.get_many([]) == {}


def test_get_next_advances(h15):
    _stub, client = h15
    assert client.get_next(DEV + ".2") == (DEV + ".3", "V2.0.0.6")


def test_error_status_response_yields_nothing():
    def hook(pdu_type, request_id, oids):
        return encode_response(request_id, [(oids[0], octets("H15"))],
                               error_status=5, error_index=1)

    stub = SNMPStub(H15_MIB, reply_hook=hook)
    client = sc.SNMPClient("127.0.0.1", port=stub.port, timeout=0.3)
    try:
        assert client.get(DEV + ".2") is None
        assert client.get_many([DEV + ".2"]) == {}
        assert client.walk(DEV) == []
    finally:
        client.close()
        stub.stop()


# ══ ERROR: BizIdError ══════════════════════════════════════════════════════


def test_is_error_value_recognises_the_sentinel():
    assert sc.is_error_value(BIZ_ID_ERROR)
    assert sc.is_error_value("ERROR: something else entirely")
    assert sc.is_error_value("  ERROR: leading whitespace")
    assert not sc.is_error_value("H15")
    assert not sc.is_error_value("no ERROR: here")   # prefix only
    assert not sc.is_error_value(0)
    assert not sc.is_error_value(None)
    assert not sc.is_error_value(b"ERROR: bytes")


def test_get_on_set_gated_oid_returns_none(h15):
    """A perfectly valid OctetString that is really a failure report."""
    _stub, client = h15
    assert client.get(OUT + ".6") is None
    assert client.get(OUT + ".7.1") is None


def test_walk_omits_error_values_but_keeps_going(h15):
    """.30.6 / .30.7.x sit between readable OIDs — skip them, don't stop."""
    _stub, client = h15
    walked = dict(client.walk(OUT))
    assert OUT + ".6" not in walked
    assert OUT + ".7.1" not in walked
    assert OUT + ".7.2" not in walked
    assert walked[OUT + ".1"] == 8              # before the error block
    assert BIZ_ID_ERROR not in walked.values()


def test_usable_value_normalises_absent_data():
    assert sc.usable_value(BIZ_ID_ERROR) is None
    assert sc.usable_value(sc.NO_SUCH_INSTANCE) is None
    assert sc.usable_value("H15") == "H15"
    assert sc.usable_value(0) == 0              # a real zero survives


# ══ Walk bounding ══════════════════════════════════════════════════════════


def test_walk_stays_inside_subtree(h15):
    _stub, client = h15
    for root in (DEV, INP, OUT, SCR):
        for oid, _value in client.walk(root):
            assert oid.startswith(root + ".")


def test_walk_exits_subtree_instead_of_running_on(h15):
    """.1 is followed in the MIB by .20 — the walk must stop at the boundary."""
    stub, client = h15
    walked = client.walk(DEV)
    assert walked, "device subtree should not be empty"
    assert all(oid.startswith(DEV + ".") for oid, _ in walked)
    assert not any(oid.startswith(INP) for oid, _ in walked)
    # It really did ask past the last leaf and then stop.
    assert stub.requests[-1][0] == sc.PDU_GET_NEXT


def test_walk_respects_max_varbinds(h15):
    stub, client = h15
    before = len(stub.requests)
    walked = client.walk(DEV, max_varbinds=3)
    assert len(walked) <= 3
    assert len(stub.requests) - before <= 3


def test_walk_max_varbinds_zero_or_bad_returns_empty(h15):
    stub, client = h15
    before = len(stub.requests)
    assert client.walk(DEV, max_varbinds=0) == []
    assert client.walk(DEV, max_varbinds=-1) == []
    assert client.walk(DEV, max_varbinds="lots") == []
    assert len(stub.requests) == before, "must not touch the network at all"


def test_walk_bad_root_returns_empty(h15):
    _stub, client = h15
    assert client.walk("nonsense") == []
    assert client.walk(None) == []


def test_walk_stops_when_agent_does_not_advance():
    """A firmware that answers GETNEXT with the same OID must not spin."""
    state = {"count": 0}

    def hook(pdu_type, request_id, oids):
        state["count"] += 1
        return encode_response(request_id, [(DEV + ".1", octets("stuck"))])

    stub = SNMPStub({}, reply_hook=hook)
    client = sc.SNMPClient("127.0.0.1", port=stub.port, timeout=0.3)
    try:
        walked = client.walk(DEV, max_varbinds=sc.MAX_WALK_VARBINDS)
        assert len(walked) == 1
        assert state["count"] == 2, "one advance, then one repeat, then stop"
    finally:
        client.close()
        stub.stop()


def test_walk_stops_on_end_of_mib_view():
    def hook(pdu_type, request_id, oids):
        return encode_response(request_id,
                               [(DEV + ".1", marker(sc.TAG_END_OF_MIB_VIEW))])

    stub = SNMPStub({}, reply_hook=hook)
    client = sc.SNMPClient("127.0.0.1", port=stub.port, timeout=0.3)
    try:
        assert client.walk(DEV) == []
        assert len(stub.requests) == 1
    finally:
        client.close()
        stub.stop()


def test_default_walk_bound_is_finite():
    assert 0 < sc.MAX_WALK_VARBINDS <= 4096


def test_is_under():
    assert sc.is_under(sc.parse_oid(DEV + ".1"), sc.parse_oid(DEV))
    assert sc.is_under(sc.parse_oid(DEV), sc.parse_oid(DEV))
    assert not sc.is_under(sc.parse_oid(INP + ".1"), sc.parse_oid(DEV))
    # .10 must not be treated as inside .1 — prefix compare is per-arc.
    assert not sc.is_under(sc.parse_oid(sc.H_SERIES_BASE + ".10"),
                           sc.parse_oid(sc.H_SERIES_BASE + ".1"))
    assert not sc.is_under(None, sc.parse_oid(DEV))


# ══ Timeouts and malformed packets ═════════════════════════════════════════


def test_timeout_returns_none():
    stub = SNMPStub(H15_MIB, reply_hook=lambda *a: None)   # never answers
    client = sc.SNMPClient("127.0.0.1", port=stub.port, timeout=NO_REPLY_TIMEOUT)
    try:
        assert client.get(DEV + ".2") is None
        assert client.get_many([DEV + ".2"]) == {}
        assert client.get_next(DEV + ".2") is None
        assert client.walk(DEV) == []
    finally:
        client.close()
        stub.stop()


def test_unreachable_port_returns_none():
    """Nothing listening at all — still None, never an exception."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    client = sc.SNMPClient("127.0.0.1", port=dead_port, timeout=NO_REPLY_TIMEOUT)
    try:
        assert client.get(DEV + ".2") is None
        assert client.walk(DEV) == []
    finally:
        client.close()


MALFORMED = [
    b"",
    b"\x30",
    b"\x30\x82",
    b"\x30\x80\x02\x01\x01",                       # indefinite length
    b"\x30\x7f\x02\x01\x01",                       # length overruns buffer
    b"\x30\x03\x02\x01\x01",                       # truncated after version
    b"\x30\x06\x02\x01\x01\x04\x01\x41",           # no PDU
    b"\xff" * 40,
    bytes(range(64)),
    b"\x30\x84\xff\xff\xff\xff\x02\x01\x01",       # absurd declared length
]


@pytest.mark.parametrize("packet", MALFORMED)
def test_parse_response_survives_malformed_packets(packet):
    assert sc.parse_response(packet) is None


@pytest.mark.parametrize("packet", MALFORMED)
def test_client_survives_malformed_replies(packet):
    stub = SNMPStub(H15_MIB, reply_hook=lambda *a: packet)
    client = sc.SNMPClient("127.0.0.1", port=stub.port, timeout=NO_REPLY_TIMEOUT)
    try:
        assert client.get(DEV + ".2") is None
        assert client.walk(DEV) == []
    finally:
        client.close()
        stub.stop()


def test_parse_response_rejects_non_bytes():
    assert sc.parse_response("not bytes") is None
    assert sc.parse_response(None) is None


def test_parse_tlv_guards():
    assert sc.parse_tlv(b"", 0) is None
    assert sc.parse_tlv(b"\x30", 0) is None
    assert sc.parse_tlv(b"\x30\x05\x00", 0) is None      # overruns
    assert sc.parse_tlv(b"\x30\x00", -1) is None
    assert sc.parse_tlv("string", 0) is None


def test_one_bad_varbind_does_not_lose_the_rest():
    """A varbind whose name isn't an OID is skipped, not fatal."""
    good = encode_varbind(DEV + ".2", octets("H15"))
    junk = sc.encode_sequence(sc.encode_integer(1) + sc.encode_null())
    pdu_body = (sc.encode_integer(7) + sc.encode_integer(0)
                + sc.encode_integer(0)
                + sc.encode_sequence(junk + good))
    pdu = bytes([sc.PDU_RESPONSE]) + sc.encode_length(len(pdu_body)) + pdu_body
    packet = sc.encode_sequence(sc.encode_integer(1)
                                + sc.encode_octet_string("public") + pdu)
    parsed = sc.parse_response(packet, 7)
    assert parsed["varbinds"] == [(DEV + ".2", "H15")]


def test_stale_reply_is_ignored_then_real_one_accepted():
    """A late answer to a previous request must not be read as this one's."""
    def hook(pdu_type, request_id, oids):
        return encode_response(request_id ^ 0xFFFF,
                               [(oids[0], octets("STALE"))])

    stub = SNMPStub(H15_MIB, reply_hook=hook)
    client = sc.SNMPClient("127.0.0.1", port=stub.port, timeout=NO_REPLY_TIMEOUT)
    try:
        assert client.get(DEV + ".2") is None
    finally:
        client.close()
        stub.stop()


def test_client_reuses_one_socket(h15):
    _stub, client = h15
    client.get(DEV + ".2")
    first = client._sock
    client.get(DEV + ".3")
    assert client._sock is first
    client.close()
    assert client._sock is None
    client.close()              # idempotent


def test_community_string_is_sent(h15):
    stub, _client = h15
    other = sc.SNMPClient("127.0.0.1", port=stub.port, community="monitor",
                          timeout=0.3)
    try:
        built = sc.build_request(DEV + ".2", other.community, 1)
        assert decode_request(built)[3] == "monitor"
    finally:
        other.close()


def test_default_community_is_public():
    assert sc.DEFAULT_COMMUNITY == "public"
    assert sc.SNMPClient("127.0.0.1").community == "public"


# ══ Named monitoring layer ═════════════════════════════════════════════════


@pytest.fixture
def monitor(h15):
    stub, client = h15
    return stub, sc.HSeriesSNMPMonitor(client)


def test_device_health_named_scalars(monitor):
    _stub, mon = monitor
    health = mon.get_device_health()
    assert health["model"] == "H15"
    assert health["firmware"] == "V2.0.0.6"
    assert health["serial_number"] == "16081800D74C0000"
    assert health["mac"] == "54-b5-6c-0a-3e-7e"
    assert health["ip"] == "192.168.0.10"
    assert health["arm_version"] == "V2.0.0.6"
    assert health["device_time"] == "2026-08-08 17:32:18"
    assert health["temperature_status"] == 0
    assert health["temperature_ok"] is True
    assert health["fan_count"] == 10
    assert health["psu_count"] == 4


def test_device_health_decodes_summary_json(monitor):
    _stub, mon = monitor
    summary = mon.get_device_health()["summary"]
    assert summary["SN"] == "16081800D74C0000"
    assert summary["MAC"] == "54-b5-6c-0a-3e-7e"
    assert summary["ARMVersion"] == "V2.0.0.6"
    assert summary["cpuStatus"] == 0
    assert summary["fansCount"] == 10


def test_device_health_cpu_status_comes_from_summary(monitor):
    _stub, mon = monitor
    assert mon.get_device_health()["cpu_status"] == 0


def test_device_health_decodes_fans(monitor):
    _stub, mon = monitor
    fans = mon.get_device_health()["fans"]
    assert len(fans) == 10
    assert [f["fan_id"] for f in fans] == list(range(10))
    assert all(f["ok"] is True for f in fans)
    # speed 0 on a running wall is a placeholder: surfaced raw, no unit, and
    # never as a health verdict.
    assert all(f["speed_raw"] == 0 for f in fans)
    assert "speed_rpm" not in fans[0]


def test_device_health_decodes_psus(monitor):
    """PSUS_JSON is what the operator's H15 actually returned: four supplies,
    every one `iSignal: 1, status: 0`, on a device running a live wall.

    `iSignal` is the power field. NovaStar R&D, by email: "Regarding the device
    power status, please use the iSignal field. Meaning: Power status (0: not
    connected to power, 1: connected to power)." The documented `.1.17`
    polarity was correct all along — it describes `iSignal`, which their own
    example for that OID omits. All four here read 1, so all four are connected
    and `ok`. `status` reads 0 and is carried raw: R&D said nothing about what
    it means on this OID, so nothing may be derived from it.
    """
    _stub, mon = monitor
    psus = mon.get_device_health()["psus"]
    assert len(psus) == 4
    assert [p["power_id"] for p in psus] == [0, 1, 2, 3]
    assert all(p["connected"] is True for p in psus)
    assert all(p["ok"] is True for p in psus)
    assert all(p["i_signal"] == 1 for p in psus)
    assert all(p["voltage_raw"] == 0 for p in psus)
    assert "voltage_v" not in psus[0]


def test_an_undocumented_status_never_overrides_isignal(monitor):
    """`status` on `.1.17` has no confirmed meaning and may not move a verdict.

    NovaStar R&D named `iSignal` as the power field and said nothing at all
    about `status`. So a supply that is connected stays connected however odd
    its `status` reads, and a supply that is NOT connected stays not-connected
    even when `status` reads the value this codebase once called healthy. Both
    directions are asserted, because this file has previously encoded a rule
    that happened to be right on the one sample it was written against.
    """
    connected = sc.parse_psus(json.dumps(
        [{"iSignal": 1, "powerId": 0, "status": 1, "voltage": 0}]))[0]
    assert connected["connected"] is True
    assert connected["ok"] is True
    assert connected["status"] == 1          # carried through, judged by nobody

    absent = sc.parse_psus(json.dumps(
        [{"iSignal": 0, "powerId": 1, "status": 0, "voltage": 0}]))[0]
    assert absent["connected"] is False
    assert absent["ok"] is False
    assert absent["status"] == 0


def test_a_supply_with_no_isignal_is_unknown_not_a_fault(monitor):
    """Absent `iSignal` is the one case left that earns an abstention.

    The power verdict has exactly one documented source. If it is missing or
    unparseable there is nothing to fall back on — `status` is not a fallback,
    it is an undocumented number — so the answer is None, not a guess in
    either direction. app.js relies on this: `ok: null` renders as "status not
    reported", never as a red FAILED badge.
    """
    for entry in ({"powerId": 0, "status": 0},
                  {"iSignal": None, "powerId": 0, "status": 0},
                  {"iSignal": "n/a", "powerId": 0, "status": 0}):
        psu = sc.parse_psus(json.dumps([entry]))[0]
        assert psu["connected"] is None
        assert psu["ok"] is None


def test_fan_and_psu_verdicts_come_from_different_fields(monitor):
    """Fans are judged on `status`; PSUs are not judged on `status` at all.

    This replaces a test that asserted the two OIDs "agree that zero is
    healthy". They do not agree, because they are not answering the same
    question. `.1.16` fan status really is `Normal: 0 / Abnormal: 1` per
    NovaStar's table, and that is unchanged. `.1.17` never had a `status`
    polarity to agree or disagree with — its documented `0: Not connected /
    1: Connected` describes `iSignal`, per NovaStar R&D by email, and a PSU's
    `status` reads 0 on both a connected supply and a disconnected one here
    without changing the verdict either time.
    """
    healthy_fan = sc.parse_fans(json.dumps([{"fanId": 0, "status": 0}]))[0]
    failed_fan = sc.parse_fans(json.dumps([{"fanId": 1, "status": 1}]))[0]
    assert healthy_fan["ok"] is True
    assert failed_fan["ok"] is False

    # Identical `status: 0` on both supplies; only `iSignal` differs, and only
    # `iSignal` decides.
    live = sc.parse_psus(json.dumps(
        [{"iSignal": 1, "powerId": 0, "status": 0}]))[0]
    dead = sc.parse_psus(json.dumps(
        [{"iSignal": 0, "powerId": 1, "status": 0}]))[0]
    assert live["ok"] is True
    assert dead["ok"] is False
    assert live["status"] == dead["status"] == 0


def test_device_health_flags_failures():
    """Fan 3 abnormal, PSU 2 not connected to power, temperature abnormal.

    PSU 2 gets `iSignal: 0` — the one documented way a supply can be called
    bad on this OID (NovaStar R&D, by email). Its `status` is left at the 0 the
    healthy supplies report, so the flagging cannot be coming from `status`.
    PSU 1 gets a non-zero `status` with `iSignal` still 1 and must NOT be
    flagged: an undocumented number is not evidence of anything.
    """
    mib = dict(H15_MIB)
    fans = json.loads(FANS_JSON)
    fans[3]["status"] = 1
    psus = json.loads(PSUS_JSON)
    psus[1]["status"] = 2
    psus[2]["iSignal"] = 0
    mib[DEV + ".16"] = octets(json.dumps(fans))
    mib[DEV + ".17"] = octets(json.dumps(psus))
    mib[DEV + ".8"] = integer(1)

    stub = SNMPStub(mib)
    client = sc.SNMPClient("127.0.0.1", port=stub.port, timeout=0.5)
    try:
        health = sc.HSeriesSNMPMonitor(client).get_device_health()
        assert health["failed_fans"] == [3]
        # `ok` is the iSignal-derived `connected` flag, so these two lists hold
        # the same ids by construction — only PSU 2, and never PSU 1.
        assert health["failed_psus"] == [2]
        assert health["disconnected_psus"] == [2]
        assert health["temperature_ok"] is False
        assert health["psus"][1]["ok"] is True and health["psus"][1]["status"] == 2
    finally:
        client.close()
        stub.stop()


def test_device_health_unmapped_statuses_land_in_extra(monitor):
    """`.1.11`-`.1.15` stay raw in `extra` — but no longer because we can't
    name them.

    The old reason ("five OIDs against three described meanings") is
    disproved: NovaStar's table describes all five — .11 genlock, .12 genlock
    frame rate, .13 system working status, .14 CPU status, .15 memory status.
    They stay raw because nothing consumes them and their polarity alternates
    (.13 is `0: Abnormal`, .14/.15 are `Normal: 0`), so an unrequested `*_ok`
    here would be a verdict nobody checks the polarity of.
    """
    _stub, mon = monitor
    extra = mon.get_device_health()["extra"]
    assert set(extra) == {"11", "12", "13", "14", "15"}
    # And they are NOT invented into named health fields.
    health = mon.get_device_health()
    for guess in ("genlock_status", "memory_status", "genlock"):
        assert guess not in health


def test_device_health_never_leaks_raw_json_strings(monitor):
    _stub, mon = monitor
    health = mon.get_device_health()
    for key, value in health.items():
        assert not (isinstance(value, str) and value.startswith("{"))
        assert not (isinstance(value, str) and value.startswith("["))


def test_device_health_on_dead_device_is_all_none():
    stub = SNMPStub(H15_MIB, reply_hook=lambda *a: None)
    client = sc.SNMPClient("127.0.0.1", port=stub.port, timeout=NO_REPLY_TIMEOUT)
    try:
        health = sc.HSeriesSNMPMonitor(client).get_device_health()
        assert health["model"] is None
        assert health["summary"] is None
        assert health["fans"] == []
        assert health["psus"] == []
        assert health["failed_fans"] == []
        assert health["temperature_ok"] is None
    finally:
        client.close()
        stub.stop()


def test_input_status(monitor):
    _stub, mon = monitor
    result = mon.get_input_status()
    assert result["card_count"] == 6
    assert result["summary"]["inputSourceCount"] == 1
    assert result["summary"]["version"] == "2.0.0.6"
    assert result["signal"]["width"] == 3840
    assert result["signal"]["height"] == 2160
    assert result["signal"]["framerate"] == pytest.approx(60.0)
    assert result["signal"]["signal_type"] == 6
    assert result["signal"]["signal_status"] == 1
    # The seven .20.2.x OIDs stay raw rather than being guessed into names.
    assert set(result["extra"]) == {"2.1", "2.2", "2.3", "2.4"}


def test_output_status(monitor):
    _stub, mon = monitor
    result = mon.get_output_status()
    assert result["card_count"] == 8
    assert result["summary"]["portCount"] == 4
    assert result["card"] == {
        "slot_status": 0,
        "firmware": "2.0.0.6",
        "serial_number": "16081800D74C0000",
        "port_count": 4,
    }
    # `.30.5.x` is a field table for ONE port, so there is no port-number
    # dimension in this result at all. The key that claimed there was is gone.
    assert "port_link" not in result


def test_output_port_subtree_is_a_field_table_not_a_port_map(monitor):
    """`.30.5.x` names FIELDS of one port; it does not index over ports.

    This decoded as `port_link[N]` — "the link state of port N" — with the
    absent `.5.2` written off as a harmless gap. It is not a gap. NovaStar's
    official OID table gives `.30.5.x` as fields describing the ONE (slot,
    port) selected by a `.30.4` SET, exactly like the sibling `.20.5.x` input
    signal table this module already decodes correctly. `.5.2` has no row in
    that table and was never observed, so it stays unnamed rather than guessed.

    The cost of the old reading was a false CRITICAL on healthy hardware. Our
    H15 answers `{1: 0, 3: 0, 4: 0}`, which came out as "three of sixteen ports
    report no link" and produced "Output port 1: link lost" on a lit wall.
    Read as fields it says: primary link 0, backup inactive, backup not linked
    — and the last two are precisely what a wall with an idle backup should
    report. The alert was firing on a healthy backup, not on a fault.
    """
    _stub, mon = monitor
    port = mon.get_output_status()["port"]
    # The fixture MIB carries the real device's three answers: .5.1, .5.3, .5.4.
    assert port == {
        "link_status": 1,
        "backup_working": 1,
        "backup_link": 0,
    }
    # Named fields, not port numbers — nothing keyed by an integer.
    assert all(isinstance(k, str) for k in port)


def test_output_port_field_values_are_passed_through_raw(monitor):
    """No `*_ok` verdict is minted here, and absence stays absence.

    Which port these fields describe depends on a `.30.4` SET this read-only
    client never issues, so the module cannot say whose link status it is
    holding — and a verdict on an unidentified port is how the removed
    ports-down CRITICAL happened. `.30.5.2` is missing from the walk, and a
    field the device did not answer must not appear as a zero.
    """
    _stub, mon = monitor
    result = mon.get_output_status()
    assert result["port"]["backup_link"] == 0        # the int, not False
    assert result["port"]["link_status"] == 1        # the int, not True
    # `.5.2` has no name and no row in the table; it is neither named nor
    # invented, and the walk never returned it in the first place.
    assert "5.2" not in result["extra"]


def test_output_status_drops_set_gated_oids(monitor):
    _stub, mon = monitor
    result = mon.get_output_status()
    assert "6" not in result["extra"]
    assert "7.1" not in result["extra"]
    assert BIZ_ID_ERROR not in result["extra"].values()


def test_screens(monitor):
    _stub, mon = monitor
    result = mon.get_screens()
    assert result["screen_count"] == 2
    assert result["fields"]["1"] == "CIRCUIT MOM"
    assert result["fields"]["2"] == 3840
    assert result["fields"]["3"] == 2160
    assert result["fields"]["4"] == pytest.approx(60.0)
    assert result["fields"]["5"] == 10


def test_get_identity_is_one_datagram(monitor):
    stub, mon = monitor
    before = len(stub.requests)
    identity = mon.get_identity()
    assert len(stub.requests) - before == 1
    assert identity == {
        "model": "H15",
        "firmware": "V2.0.0.6",
        "serial_number": "16081800D74C0000",
        "mac": "54-b5-6c-0a-3e-7e",
        "ip": "192.168.0.10",
    }


def test_monitor_can_build_its_own_client():
    mon = sc.HSeriesSNMPMonitor(host="127.0.0.1", port=1, timeout=0.05)
    assert isinstance(mon.client, sc.SNMPClient)
    assert mon.client.host == "127.0.0.1"
    mon.close()


def test_monitor_requires_a_client_or_host():
    with pytest.raises(ValueError):
        sc.HSeriesSNMPMonitor()


def test_parse_json_value_is_defensive():
    assert sc.parse_json_value('{"a":1}') == {"a": 1}
    assert sc.parse_json_value(BIZ_ID_ERROR) is None
    assert sc.parse_json_value("not json") is None
    assert sc.parse_json_value(None) is None
    assert sc.parse_json_value(42) is None
    assert sc.parse_json_value(b'{"a":1}') is None


def test_parse_fans_and_psus_are_defensive():
    for bad in (None, "", "[]", BIZ_ID_ERROR, '{"not":"a list"}',
                '[1,2,3]', "garbage"):
        assert sc.parse_fans(bad) == []
        assert sc.parse_psus(bad) == []
    # Missing keys yield None fields, not a crash and not a false "ok".
    partial = sc.parse_fans('[{"fanId":1}]')
    assert partial[0]["status"] is None
    assert partial[0]["ok"] is None


def test_oid_constants_sit_under_the_enterprise_base():
    assert sc.ENTERPRISE_BASE == "1.3.6.1.4.1.319"
    assert sc.H_SERIES_BASE == "1.3.6.1.4.1.319.10.10"
    for oid in (sc.OID_DEVICE, sc.OID_INPUT, sc.OID_OUTPUT, sc.OID_SCREEN):
        assert oid.startswith(sc.H_SERIES_BASE + ".")


# ══ Read-only enforcement ══════════════════════════════════════════════════
#
# The point of this module is that it CANNOT write to the controller. These
# tests are the guard rail: they read snmp_client.py's own source so that a
# future SET path fails CI rather than shipping and locking the operator out
# of Companion again.


def _module_source():
    with open(sc.__file__, "r", encoding="utf-8") as handle:
        return handle.read()


def _code_tokens(source):
    """Tokens with comments and string literals (incl. docstrings) removed.

    Prose is allowed to *discuss* SET — indeed the module docstring must — so
    the grep has to look at executable code only.
    """
    tokens = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        tokens.append(token)
    return tokens


def test_module_defines_no_set_request_pdu():
    """0xA3 (SetRequest) must not appear as a literal anywhere in the code."""
    for token in _code_tokens(_module_source()):
        if token.type == tokenize.NUMBER:
            try:
                value = ast.literal_eval(token.string)
            except (ValueError, SyntaxError):
                continue
            assert value != 0xA3, (
                "SetRequest PDU tag 0xA3 found in snmp_client.py — this "
                "module is read-only by design"
            )


def test_module_defines_no_set_named_symbols():
    """No function, class or constant whose name announces a write path."""
    tree = ast.parse(_module_source())
    defined = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            defined.append(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.append(node.id)
    for name in defined:
        lowered = name.lstrip("_").lower()
        assert not lowered.startswith("set"), \
            "snmp_client.py defines %r — no write path may exist" % name
        for banned in ("setrequest", "set_request", "write_oid", "snmpset"):
            assert banned not in name.lower()


def test_read_pdu_types_are_exactly_get_and_getnext():
    assert sc.PDU_GET == 0xA0
    assert sc.PDU_GET_NEXT == 0xA1
    assert sc.PDU_RESPONSE == 0xA2
    assert sc.READ_PDU_TYPES == (0xA0, 0xA1)


def test_build_request_refuses_every_non_read_pdu_type():
    for tag in range(0x100):
        built = sc.build_request(DEV + ".2", "public", 1, pdu_type=tag)
        if tag in sc.READ_PDU_TYPES:
            assert built is not None
        else:
            assert built is None, "PDU type 0x%02x must be refused" % tag


def test_public_api_exposes_no_write_methods():
    for obj in (sc, sc.SNMPClient, sc.HSeriesSNMPMonitor):
        for name in dir(obj):
            if name.startswith("__"):
                continue            # object.__setattr__ et al, not ours
            lowered = name.lstrip("_").lower()
            assert not lowered.startswith("set"), \
                "%r exposes %r" % (obj, name)


def test_client_only_ever_emits_read_pdus(h15):
    """Behavioural proof: exercise every entry point, inspect the wire.

    This is a read-only assertion and it must stay one. It failed while the
    `.30.5` field-table decode was half-landed, because `get_output_status()`
    raised a KeyError before the stub ever saw the traffic — a decoder bug
    wearing a read-only test's clothes. Fixed in the decoder, not here.
    """
    stub, client = h15
    monitor = sc.HSeriesSNMPMonitor(client)
    client.get(DEV + ".2")
    client.get_many([DEV + ".2", DEV + ".3"])
    client.get_next(DEV + ".2")
    client.walk(DEV)
    monitor.get_device_health()
    monitor.get_input_status()
    monitor.get_output_status()
    monitor.get_screens()
    monitor.get_identity()
    assert stub.pdu_types, "the stub saw no traffic at all"
    assert set(stub.pdu_types) <= {sc.PDU_GET, sc.PDU_GET_NEXT}


def test_module_docstring_states_the_read_only_contract():
    doc = sc.__doc__ or ""
    assert "1.3.6.1.4.1.319" in doc
    assert "READ-ONLY BY DESIGN" in doc
    assert "Companion" in doc
