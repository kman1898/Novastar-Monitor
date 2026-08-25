"""Read-only SNMPv2c client for NovaStar H-series controllers (UDP 161).

Schema source: "H Series SNMP Protocol Instructions V1.0" (NovaStar PDF).
Enterprise OID base **1.3.6.1.4.1.319**; everything this module reads lives
under 1.3.6.1.4.1.319.10.10.

WHY THIS EXISTS — the Companion lockout
---------------------------------------
The app used to monitor the splicer over the JSON control protocol on UDP 6000
(see h_series_json.py), including a `W0120` keepalive lifted from the vendor's
*control* module. That keepalive tells the device "a controller is attached",
and the H-series honours one controller at a time: while the monitor was
polling, the operator was **locked out of Bitfocus Companion** — their actual
show-control surface. A monitoring tool that can take the desk away from the
operator mid-show is worse than no monitoring tool.

SNMP fixes that at the transport level rather than by being careful:

- It is purely observational. Nothing in a GET claims the controller role.
- The agent serves multiple managers concurrently, so this client coexists with
  Companion, NovaLCT, and any NMS the venue already runs.
- There is no keepalive and no session — each GET is self-contained, so an
  abandoned poller leaves nothing behind on the device.

READ-ONLY BY DESIGN
-------------------
This module implements GetRequest and GetNextRequest **only**. There is no
write path, and adding one is not a small change to be made in passing: the
SetRequest PDU tag is not defined anywhere in this file, `build_request`
refuses any PDU type other than the two read ones, and
tests/test_snmp_client.py asserts all of that against this file's own source.
A SET capability that merely *exists* eventually gets called — by a well-meant
refactor, by a caller that "just needs to flip one field" — and the failure
mode is the outage this module was written to prevent. Keep it absent.

The consequence is that anything the device gates behind a SET selector is out
of scope here. Per-receiving-card work status, temperature and voltage
(.30.6 and .30.7.x) require .30.4 to be SET first with a slot/port/card
selector; without it the agent answers the literal string `ERROR: BizIdError`.
Those readings stay on the JSON path. See `is_error_value` — the agent reports
that failure as a perfectly well-formed OctetString, so it has to be caught by
value, not by SNMP status.

NO NEW DEPENDENCIES
-------------------
pysnmp is not installed, and the app ships as a PyInstaller bundle for macOS
*and* Windows, where the net-snmp CLI tools do not exist — so neither a new
dependency nor shelling out to `snmpget` is viable. Hence the small pure-Python
BER codec below. It covers exactly the ASN.1 subset SNMPv2c needs (sequence,
integer, octet string, OID, null, the application types the device actually
emits) and nothing else. It is not a general-purpose ASN.1 library and should
not grow into one.

House rule, same as h_series_json: never raise at the caller. Every entry point
returns None / an empty container on timeout, refusal, or garbage, and lets the
caller decide what a missing reading means.
"""

import json
import random
import socket
import struct
import threading
import time

SNMP_PORT = 161
DEFAULT_COMMUNITY = "public"

# SNMPv2c. The version field is the protocol version *minus one*, so v2c
# travels the wire as 1 (v1 would be 0). The device answers v2c.
SNMP_VERSION_2C = 1

# ── BER tags ───────────────────────────────────────────────────────────────
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30

# Application types. The H-series has been observed emitting Integer,
# OctetString and Opaque; the counter/gauge/timeticks tags are decoded too
# because they are standard on any agent and cost three lines.
TAG_IP_ADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_OPAQUE = 0x44
TAG_COUNTER64 = 0x46

# Context-specific exception markers an SNMPv2c agent returns in place of a
# value. These are "there is nothing here", not transport failures.
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82

# ── PDU types ──────────────────────────────────────────────────────────────
# Exactly three, and that is the whole point: two reads and their response.
# The SetRequest tag is deliberately absent — see the module docstring.
PDU_GET = 0xA0
PDU_GET_NEXT = 0xA1
PDU_RESPONSE = 0xA2

READ_PDU_TYPES = (PDU_GET, PDU_GET_NEXT)

# ── Timeouts and bounds ────────────────────────────────────────────────────
# The splicer is on the same wired GigE switch (sub-millisecond RTT measured on
# the JSON path), but an SNMP agent builds its answer from a live hardware
# query, so allow more headroom than the 0.3 s used for JSON bulk reads.
DEFAULT_TIMEOUT = 1.0

# Retransmissions, not attempts: 0 means one datagram per call. UDP SNMP
# normally retries, but this device has already been overloaded once by this
# app, and a missed poll costs one stale cycle whereas doubled traffic costs
# the operator. Callers that genuinely need a retry can pass retries=1.
DEFAULT_RETRIES = 0

# Hard ceiling on GETNEXT iterations in a single walk. An unbounded GETNEXT
# loop is the classic way to hammer an agent: point it at an unexpected subtree
# (or hit a firmware that answers the same OID forever) and it never stops. The
# largest subtree here is a few dozen varbinds, so 512 is ~10x headroom and
# still terminates in about a second of wall time.
MAX_WALK_VARBINDS = 512

MAX_PAYLOAD = 65536

# Values prefixed with this are the agent reporting a failure inside a
# successful response — see `is_error_value`.
ERROR_VALUE_PREFIX = "ERROR:"


class _Marker:
    """A named singleton for the SNMPv2c "no value here" varbind types."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name

    def __bool__(self):
        return False


NO_SUCH_OBJECT = _Marker("noSuchObject")
NO_SUCH_INSTANCE = _Marker("noSuchInstance")
END_OF_MIB_VIEW = _Marker("endOfMibView")

_EXCEPTION_MARKERS = {
    TAG_NO_SUCH_OBJECT: NO_SUCH_OBJECT,
    TAG_NO_SUCH_INSTANCE: NO_SUCH_INSTANCE,
    TAG_END_OF_MIB_VIEW: END_OF_MIB_VIEW,
}


# ══ BER primitives ═════════════════════════════════════════════════════════


def encode_length(n):
    """BER length octets, short form under 128 and long form above."""
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag, value):
    return bytes([tag]) + encode_length(len(value)) + value


def _int_octets(value):
    """Minimal two's-complement octets for a BER INTEGER.

    Grow the width until the value fits *signed*, which is what produces the
    leading 0x00 on positive values whose top bit is set — omit it and the
    agent reads the number as negative.
    """
    width = 1
    while True:
        try:
            return value.to_bytes(width, "big", signed=True)
        except OverflowError:
            width += 1


def encode_integer(value):
    return _tlv(TAG_INTEGER, _int_octets(int(value)))


def encode_octet_string(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _tlv(TAG_OCTET_STRING, bytes(value))


def encode_null():
    return _tlv(TAG_NULL, b"")


def encode_sequence(payload):
    return _tlv(TAG_SEQUENCE, payload)


def _base128(n):
    """Multi-byte base-128 OID subidentifier, high bit set on all but the last."""
    if n < 0x80:
        return bytes([n])
    chunks = []
    while n > 0:
        chunks.insert(0, n & 0x7F)
        n >>= 7
    return bytes([c | 0x80 for c in chunks[:-1]] + [chunks[-1]])


def encode_oid(oid):
    """Dotted OID string (or int sequence) → BER OID TLV, or None if invalid.

    The first two arcs are packed into a single octet as `40*a + b` — that is
    why 1.3.6.1.2.1... starts with 0x2b (43) rather than 0x01 0x03.
    """
    arcs = parse_oid(oid)
    if arcs is None or len(arcs) < 2:
        return None
    if arcs[0] > 2 or (arcs[0] < 2 and arcs[1] >= 40):
        return None
    body = _base128(arcs[0] * 40 + arcs[1])
    for arc in arcs[2:]:
        body += _base128(arc)
    return _tlv(TAG_OID, body)


def parse_oid(oid):
    """Normalise a dotted string / iterable of ints into a tuple of ints.

    Returns None rather than raising on anything that isn't a valid OID, so
    every caller can treat a bad OID the same way it treats a dead device.
    """
    if oid is None:
        return None
    if isinstance(oid, (tuple, list)):
        parts = list(oid)
    elif isinstance(oid, str):
        parts = oid.strip().strip(".").split(".")
    else:
        return None
    if not parts:
        return None
    arcs = []
    for part in parts:
        try:
            arc = int(part)
        except (TypeError, ValueError):
            return None
        if arc < 0:
            return None
        arcs.append(arc)
    return tuple(arcs)


def format_oid(arcs):
    """Tuple of ints → dotted string. None-safe."""
    if not arcs:
        return None
    return ".".join(str(a) for a in arcs)


def decode_oid(data):
    """BER OID *value* octets → tuple of ints, or None if malformed.

    The whole body is read as base-128 subidentifiers first, and only then is
    the leading one split back into two arcs. Doing it the other way round —
    treating byte[0] as `40*a + b` directly — is wrong whenever the packed
    value needs more than one octet, which happens for any OID under 2.x with
    a second arc of 40 or more. Nothing under 1.3.6.1.4.1.319 hits that, but a
    decoder that quietly mangles a whole OID class is not one to keep.
    """
    if not data:
        return None
    subids = []
    value = 0
    pending = False
    for byte in data:
        value = (value << 7) | (byte & 0x7F)
        pending = True
        if not byte & 0x80:
            subids.append(value)
            value = 0
            pending = False
    if pending or not subids:
        # Trailing continuation byte with nothing after it: truncated OID.
        return None
    head = subids[0]
    if head < 40:
        arcs = [0, head]
    elif head < 80:
        arcs = [1, head - 40]
    else:
        # First arc 2 is the only one whose second arc is unbounded.
        arcs = [2, head - 80]
    arcs.extend(subids[1:])
    return tuple(arcs)


def parse_tlv(data, offset=0):
    """Read one TLV at `offset`.

    Returns `(tag, value_bytes, next_offset)` or None if the header runs past
    the end of the buffer, the length is indefinite (illegal in BER, and the
    shape a fuzzer reaches for), or the declared length overruns the datagram.
    Every decode path in this module goes through here, so a truncated or
    hostile packet becomes None instead of an IndexError.
    """
    if not isinstance(data, (bytes, bytearray)):
        return None
    if offset < 0 or offset + 2 > len(data):
        return None
    tag = data[offset]
    index = offset + 1
    first = data[index]
    index += 1
    if first < 0x80:
        length = first
    elif first == 0x80:
        return None
    else:
        count = first & 0x7F
        if count > 4 or index + count > len(data):
            return None
        length = int.from_bytes(data[index:index + count], "big")
        index += count
    end = index + length
    if end > len(data):
        return None
    return tag, bytes(data[index:end]), end


def decode_integer(data):
    """BER INTEGER value octets → int, or None."""
    if not data:
        return None
    return int.from_bytes(data, "big", signed=True)


def decode_unsigned(data):
    """Counter/Gauge/TimeTicks value octets → int, or None.

    Unsigned on the wire even though BER would read a high top bit as
    negative, hence the separate decoder.
    """
    if not data:
        return None
    return int.from_bytes(data, "big", signed=False)


def decode_opaque(data):
    """Opaque value octets → float where the payload is a float, else bytes.

    net-snmp renders the device's framerate fields as `Opaque: Float: 60.0`.
    That is the widely-implemented (RFC 3416 era) convention of wrapping an
    IEEE-754 value inside Opaque with an application-specific tag:

        0x9f 0x78 0x04 <4 bytes big-endian float>
        0x9f 0x79 0x08 <8 bytes big-endian double>

    Anything else inside Opaque is returned as raw bytes rather than guessed
    at — a wrong framerate is worse than an undecoded one.
    """
    if not data:
        return None
    if len(data) >= 3 and data[0] == 0x9F:
        kind = data[1]
        parsed = parse_tlv(data, 1)
        if parsed is not None:
            _, payload, _ = parsed
            try:
                if kind == 0x78 and len(payload) == 4:
                    return struct.unpack(">f", payload)[0]
                if kind == 0x79 and len(payload) == 8:
                    return struct.unpack(">d", payload)[0]
            except struct.error:
                return bytes(data)
    return bytes(data)


def decode_octet_string(data):
    """OctetString → str when it is valid UTF-8, else the raw bytes.

    Every H-series value observed is ASCII text (JSON documents, versions,
    MAC/SN strings), but an agent may hand back a genuine byte blob and
    mangling it with errors="replace" would hide that. Strict decode, byte
    fallback.
    """
    try:
        return data.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return bytes(data)


def decode_value(tag, data):
    """One varbind value TLV → a Python value.

    Unknown tags come back as raw bytes: an unrecognised type is data we
    cannot interpret, not a reason to drop the varbind.
    """
    if tag in _EXCEPTION_MARKERS:
        return _EXCEPTION_MARKERS[tag]
    if tag == TAG_INTEGER:
        return decode_integer(data)
    if tag == TAG_OCTET_STRING:
        return decode_octet_string(data)
    if tag == TAG_NULL:
        return None
    if tag == TAG_OID:
        return format_oid(decode_oid(data))
    if tag in (TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS, TAG_COUNTER64):
        return decode_unsigned(data)
    if tag == TAG_IP_ADDRESS:
        if len(data) == 4:
            return ".".join(str(b) for b in data)
        return bytes(data)
    if tag == TAG_OPAQUE:
        return decode_opaque(data)
    return bytes(data)


# ══ SNMP message assembly ══════════════════════════════════════════════════


def build_request(oids, community=DEFAULT_COMMUNITY, request_id=None,
                  pdu_type=PDU_GET):
    """Encode a GetRequest or GetNextRequest datagram. Returns bytes or None.

    `pdu_type` is validated against READ_PDU_TYPES, which is the enforcement
    point for this module's read-only contract: there is no code path here
    that can emit a write PDU, whatever a caller passes in.

    Every varbind is sent with a NULL value — in a read PDU the value slot is
    a placeholder the agent overwrites in its response.
    """
    if pdu_type not in READ_PDU_TYPES:
        return None
    if isinstance(oids, (str, bytes)) or not hasattr(oids, "__iter__"):
        oids = [oids]
    oids = list(oids)
    if not oids:
        return None
    if request_id is None:
        request_id = new_request_id()

    varbinds = b""
    for oid in oids:
        encoded = encode_oid(oid)
        if encoded is None:
            return None
        varbinds += encode_sequence(encoded + encode_null())

    pdu_body = (encode_integer(request_id)
                + encode_integer(0)          # error-status, 0 in a request
                + encode_integer(0)          # error-index, 0 in a request
                + encode_sequence(varbinds))
    pdu = _tlv(pdu_type, pdu_body)
    return encode_sequence(
        encode_integer(SNMP_VERSION_2C)
        + encode_octet_string(community)
        + pdu
    )


def new_request_id():
    """A positive 31-bit request id.

    Kept inside the signed 32-bit range so it always encodes as a plain
    positive INTEGER, and randomised so a late reply to a previous, timed-out
    request can be told apart from this one's answer — the same stale-reply
    trap `_echo_key` guards against on the JSON path.
    """
    return random.randrange(1, 0x7FFFFFFF)


def parse_response(data, expect_request_id=None):
    """Decode a response datagram.

    Returns `{"request_id", "community", "error_status", "error_index",
    "varbinds": [(oid_str, value), ...]}` or None if the packet is not a
    well-formed SNMP response (or is an answer to somebody else's request).

    Structural checks are deliberately strict — everything arriving here came
    off a socket — but a varbind that fails to decode is skipped rather than
    condemning the whole packet, so one odd field cannot cost a whole poll.
    """
    outer = parse_tlv(data)
    if outer is None:
        return None
    tag, body, _ = outer
    if tag != TAG_SEQUENCE:
        return None

    field = parse_tlv(body, 0)
    if field is None or field[0] != TAG_INTEGER:
        return None
    version = decode_integer(field[1])
    offset = field[2]

    field = parse_tlv(body, offset)
    if field is None or field[0] != TAG_OCTET_STRING:
        return None
    community = decode_octet_string(field[1])
    offset = field[2]

    field = parse_tlv(body, offset)
    if field is None or field[0] != PDU_RESPONSE:
        # Not a response PDU. Requests echoed back, traps, and anything else
        # are not this client's business.
        return None
    pdu = field[1]

    numbers = []
    offset = 0
    for _ in range(3):                    # request-id, error-status, error-index
        field = parse_tlv(pdu, offset)
        if field is None or field[0] != TAG_INTEGER:
            return None
        numbers.append(decode_integer(field[1]))
        offset = field[2]
    request_id, error_status, error_index = numbers
    if expect_request_id is not None and request_id != expect_request_id:
        return None

    field = parse_tlv(pdu, offset)
    if field is None or field[0] != TAG_SEQUENCE:
        return None
    varbind_list = field[1]

    varbinds = []
    offset = 0
    while offset < len(varbind_list):
        field = parse_tlv(varbind_list, offset)
        if field is None:
            break
        vb_tag, vb_body, offset = field
        if vb_tag != TAG_SEQUENCE:
            continue
        name = parse_tlv(vb_body, 0)
        if name is None or name[0] != TAG_OID:
            continue
        arcs = decode_oid(name[1])
        if arcs is None:
            continue
        value_field = parse_tlv(vb_body, name[2])
        if value_field is None:
            continue
        varbinds.append((format_oid(arcs),
                         decode_value(value_field[0], value_field[1])))

    return {
        "version": version,
        "community": community,
        "request_id": request_id,
        "error_status": error_status,
        "error_index": error_index,
        "varbinds": varbinds,
    }


def is_error_value(value):
    """True if the agent packed a failure into an otherwise valid value.

    The H-series answers `ERROR: BizIdError` — a normal OctetString, in a
    response with error-status 0 — for every OID that needs a SET selector
    chosen first (.30.6, .30.7.x). SNMP considers that a successful read, so
    nothing below this function can catch it. Treated as absent data, because
    storing the string "ERROR: BizIdError" as a card temperature is exactly
    the class of bug this app has been bitten by before.
    """
    return isinstance(value, str) and value.strip().startswith(ERROR_VALUE_PREFIX)


def usable_value(value):
    """Normalise a decoded varbind value: absent things become None."""
    if isinstance(value, _Marker):
        return None
    if is_error_value(value):
        return None
    return value


def is_under(oid_arcs, root_arcs):
    """True if `oid_arcs` sits inside the `root_arcs` subtree (inclusive)."""
    if oid_arcs is None or root_arcs is None:
        return False
    return oid_arcs[:len(root_arcs)] == tuple(root_arcs)


# ══ Client ═════════════════════════════════════════════════════════════════


class SNMPClient:
    """Read-only SNMPv2c client. GET, multi-OID GET, and a bounded walk.

    One UDP socket is held for the client's lifetime behind a lock, matching
    HSeriesJSONClient: SNMP is stateless request/response, so "persistent"
    means only that the file descriptor is reused. A socket that errors out is
    closed and lazily recreated.

    Nothing here writes. See the module docstring.
    """

    def __init__(self, host, community=DEFAULT_COMMUNITY, port=SNMP_PORT,
                 timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES):
        self.host = host
        self.community = community
        self.port = port
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self._lock = threading.Lock()
        self._sock = None

    # ── Socket management ──────────────────────────────────────────────

    def _get_socket(self):
        """Return the shared socket, creating it if needed. Caller holds lock."""
        if self._sock is None:
            # Left unconnected: a connect()ed UDP socket silently drops replies
            # from any other source port, and we have not verified that every
            # agent answers from :161.
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return self._sock

    def _drop_socket(self):
        """Discard the socket after an error. Caller holds the lock."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def close(self):
        """Release the socket. Safe to call more than once."""
        with self._lock:
            self._drop_socket()

    @staticmethod
    def _drain(sock):
        """Discard datagrams left over from an earlier timed-out request.

        The request-id check in `parse_response` already rejects stale replies,
        but draining first means a backlog can't eat the deadline one packet at
        a time.
        """
        try:
            sock.settimeout(0)
            while True:
                try:
                    sock.recvfrom(MAX_PAYLOAD)
                except (BlockingIOError, socket.timeout):
                    return
        except OSError:
            return

    # ── Core exchange ──────────────────────────────────────────────────

    def _exchange(self, oids, pdu_type):
        """Send one read PDU, return the parsed response dict or None.

        Loops on receive rather than taking the first datagram: a stale reply
        (wrong request id) or a malformed packet is discarded and the wait
        continues until the deadline, instead of being reported as this call's
        answer or as a failure.
        """
        request_id = new_request_id()
        payload = build_request(oids, self.community, request_id, pdu_type)
        if payload is None:
            return None

        for _ in range(self.retries + 1):
            try:
                with self._lock:
                    sock = self._get_socket()
                    try:
                        self._drain(sock)
                        sock.settimeout(self.timeout)
                        sock.sendto(payload, (self.host, self.port))
                        deadline = time.monotonic() + self.timeout
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            sock.settimeout(remaining)
                            data, _addr = sock.recvfrom(MAX_PAYLOAD)
                            parsed = parse_response(data, request_id)
                            if parsed is not None:
                                return parsed
                    except socket.timeout:
                        continue
                    except (OSError, socket.error):
                        self._drop_socket()
                        continue
            except Exception:
                # Nothing reaches the caller as an exception, ever.
                return None
        return None

    # ── Public reads ───────────────────────────────────────────────────

    def get(self, oid):
        """GET one OID. Returns the decoded value, or None if unavailable.

        None covers every kind of "no reading": timeout, a non-zero
        error-status, noSuchObject/noSuchInstance, and an `ERROR:` payload.
        The caller decides what a missing value means — same contract as
        h_series_json.
        """
        values = self.get_many([oid])
        arcs = parse_oid(oid)
        key = format_oid(arcs) if arcs else None
        return values.get(key)

    def get_many(self, oids):
        """GET several OIDs in one PDU. Returns {oid_str: value}.

        OIDs the device could not answer are simply absent from the dict
        rather than present-and-None, so `in` is a real membership test. An
        empty dict means the whole exchange failed.
        """
        oids = list(oids or [])
        if not oids:
            return {}
        parsed = self._exchange(oids, PDU_GET)
        if parsed is None or parsed.get("error_status"):
            # A non-zero error-status invalidates the whole PDU: SNMPv2c may
            # still echo the varbinds, but with error-index pointing at one
            # that failed. Don't try to salvage a partial read.
            return {}
        result = {}
        for oid, value in parsed["varbinds"]:
            value = usable_value(value)
            if value is None:
                continue
            result[oid] = value
        return result

    def get_next(self, oid):
        """GETNEXT one OID. Returns `(oid_str, value)` or None.

        The value is returned as decoded — including `ERROR:` strings and the
        exception markers — because `walk` needs to see them to decide whether
        to keep going. Callers wanting a clean value should use `walk`.
        """
        parsed = self._exchange([oid], PDU_GET_NEXT)
        if parsed is None or parsed.get("error_status"):
            return None
        if not parsed["varbinds"]:
            return None
        return parsed["varbinds"][0]

    def walk(self, root_oid, max_varbinds=MAX_WALK_VARBINDS):
        """GETNEXT across a subtree. Returns `[(oid_str, value), ...]`.

        Bounded three ways, because a runaway GETNEXT loop against an
        unexpected agent is how a *monitoring* tool becomes a load generator:

        1. `max_varbinds` caps the iteration count outright.
        2. The walk stops as soon as the returned OID leaves `root_oid`'s
           subtree — GETNEXT past the last leaf legitimately hands back the
           next MIB object, which is somebody else's data.
        3. The OID must strictly increase each round. A firmware that answers
           GETNEXT with the same OID (or an earlier one) would otherwise spin
           forever at full speed.

        `ERROR:`-valued varbinds are omitted from the result but do not stop
        the walk: an OID that needs a SET selector is absent data sitting
        between OIDs that are readable, not the end of the subtree. Exception
        markers are omitted the same way; endOfMibView ends the walk.

        Returns [] on any failure — never raises, never partially-signals.
        """
        root = parse_oid(root_oid)
        if root is None:
            return []
        try:
            limit = int(max_varbinds)
        except (TypeError, ValueError):
            return []
        if limit <= 0:
            return []

        results = []
        cursor = root
        previous = None
        for _ in range(limit):
            step = self.get_next(format_oid(cursor))
            if step is None:
                break
            oid_str, value = step
            arcs = parse_oid(oid_str)
            if arcs is None:
                break
            if not is_under(arcs, root):
                break                     # left the subtree — done, not failed
            if previous is not None and arcs <= previous:
                break                     # agent isn't advancing; bail out
            if value is END_OF_MIB_VIEW:
                break
            previous = arcs
            cursor = arcs
            clean = usable_value(value)
            if clean is None:
                continue                  # ERROR:/noSuchInstance → absent data
            results.append((oid_str, clean))
        return results


# ══ H-series OID map ═══════════════════════════════════════════════════════
#
# All observed OIDs live under 1.3.6.1.4.1.319.10.10, verified by walk against
# an H15 running V2.0.0.6. Four subtrees:
#
#   .1   device        scalars + two embedded JSON arrays
#   .20  input cards   count, per-card scalars, JSON summary, signal block
#   .30  output cards  count, per-slot scalars, JSON summary, port field table
#   .40  screens       count, per-screen scalars
#
# Only the OIDs the capture pins down UNAMBIGUOUSLY are given names below.
# Where the capture lists fewer descriptions than OIDs (.20.2.1-.7 is seven
# OIDs for four described fields; .40.2.1-.7 is seven for five), the mapping
# is a guess, and a guessed name on a monitoring field is worse than no name —
# it gets read as fact downstream. Those are returned under "extra", keyed by
# their OID suffix, until somebody re-walks the device and pins them down.
#
# `.1.11`-`.1.15` used to be listed here as another such gap — "five OIDs
# against three described meanings". That was wrong, and it was wrong in the
# direction that matters: it treated documented fields as unknowable. NovaStar's
# official OID table describes all FIVE of them, one meaning per OID:
#
#   .1.11  Genlock status                 0: Not connected / 1: Connected
#   .1.12  Genlock frame rate
#   .1.13  System working status          0: Abnormal / 1: Normal
#   .1.14  CPU status                     Normal: 0 / Abnormal: 1
#   .1.15  Memory status                  Normal: 0 / Abnormal: 1
#
# Note that `.13` is the INVERSE of `.14` / `.15` — 0 is the bad value there
# and the good one two rows down. That is the same per-OID polarity trap that
# `.30.2.1` sprang on this codebase, and it is the reason these
# stay in "extra" as raw values rather than being decoded into `*_ok` booleans
# here: nothing in the app consumes them yet, and a verdict nobody asked for is
# a verdict nobody checks the polarity of. The meanings are recorded above so
# whoever does want them does not have to guess or re-derive.
#
# `.1.17` used to be named alongside `.30.2.1` as a polarity trap. It was never
# one. Its documented `0: Not connected / 1: Connected` is correct and always
# was — it describes the `iSignal` key in the JSON payload, which NovaStar's
# own example for this OID omits, so we spent two rounds applying it to the
# wrong key. Confirmed by NovaStar R&D, by email. See `parse_psus`.
#
# The JSON-valued OIDs (.1.0, .1.16, .1.17, .20.3, .30.3) carry their own key
# names on the wire, so they are decoded properly and are the preferred source
# for anything a scalar OID also reports.

ENTERPRISE_BASE = "1.3.6.1.4.1.319"
H_SERIES_BASE = ENTERPRISE_BASE + ".10.10"

OID_DEVICE = H_SERIES_BASE + ".1"
OID_INPUT = H_SERIES_BASE + ".20"
OID_OUTPUT = H_SERIES_BASE + ".30"
OID_SCREEN = H_SERIES_BASE + ".40"

# Device scalars. Suffix under OID_DEVICE → field name.
DEVICE_FIELDS = {
    "0": "summary_json",        # whole-device JSON descriptor
    "1": "device_time",         # "2026-08-08 17:32:18"
    "2": "model",               # "H15"
    "3": "firmware",            # "V2.0.0.6"
    "4": "serial_number",
    "5": "mac",
    "6": "ip",
    "7": "arm_version",
    "8": "temperature_status",  # 0 = normal
    "9": "fan_count",
    "10": "psu_count",
    "16": "fans_json",          # [{"fanId","speed","status"}] x10
    "17": "psus_json",          # [{"iSignal","powerId","status","voltage"}] x4
}

# ── The `.30.0` output-slot selector, and why our reads came back empty ────
#
# `.30.0` chooses WHICH output slot the rest of the `.30` subtree describes:
# `.30.2.x` scalars, the `.30.3` JSON summary, and (with `.30.4` on top of it)
# the per-port and per-card leaves. Nothing in this module writes it — we are
# read-only — so every read here describes whichever slot the device happens
# to have selected.
#
# Slot IDs are PHYSICAL CHASSIS POSITIONS, not a 0..N-1 index over the output
# cards. NovaStar R&D, by email: "The index of the slot ID starts from 0", but
# input slots are numbered first and output slots sit behind them, so `.30.1`
# answering 8 does NOT mean the valid selectors are 0..7. Their worked example
# is an H5 whose first output slot is 10, followed by 11, 12 and 13 (MVR).
#
# Querying a slot with no card fitted returns the default empty summary
# `{"SN":"","netPortCount":0,"status":1,"version":"0"}` — the exact bytes both
# our devices returned, on both firmwares. That is the documented answer for an
# empty slot, NOT an unpopulated subtree and NOT a firmware bug. We were
# feeding the selector 0..3, which on this chassis are input-side positions.
#
# UNVERIFIED, pending a hardware retest: our H15's R0100 slot list reports its
# output cards (cardType 2) at chassis slots 20, 22, 24, 26, 28, 30, 32 and 34,
# so those are very likely the numbers `.30.0` wants — and 20/22 are the two to
# try first. It is a strong hypothesis, not a fact: the wall has been off the
# network since the vendor answered, so nobody has put 20 into the selector and
# watched a real serial number come back. The same numbering almost certainly
# explains the `ERROR: BizIdError` on `.30.6` / `.30.7.x`, where we were
# sending outputSlotId 4 — not an output slot on this chassis at all.
#
# Behaviour here is unchanged either way. Setting the selector needs a SET PDU,
# which this module does not and must not have (see the module docstring); a
# slot-aware output read has to come from the JSON path or from a separate,
# explicitly-scoped tool.

# Output slot scalars: four described fields for four OIDs, in order. They
# describe the slot currently selected by `.30.0` — see above.
OUTPUT_CARD_FIELDS = {
    "1": "slot_status",
    "2": "firmware",
    "3": "serial_number",
    "4": "port_count",
}

# Input signal block: five described fields for five OIDs, in order.
INPUT_SIGNAL_FIELDS = {
    "1": "signal_status",
    "2": "width",
    "3": "height",
    "4": "framerate",           # Opaque: Float
    "5": "signal_type",
}

# The output-port FIELD table. `.30.5.2` was absent from the walk (the device
# answered .1, .3 and .4 only) and has no row in NovaStar's table either, so it
# is left unnamed rather than guessed at. See OUTPUT_PORT_FIELDS for the rest.
OID_OUTPUT_PORT = OID_OUTPUT + ".5"


def _suffix(oid, root):
    """The part of `oid` below `root`, dotted, or None if not underneath."""
    arcs = parse_oid(oid)
    root_arcs = parse_oid(root)
    if arcs is None or root_arcs is None or not is_under(arcs, root_arcs):
        return None
    return format_oid(arcs[len(root_arcs):]) or ""


def parse_json_value(value):
    """Decode an embedded JSON string value. None on anything unusable.

    Several OIDs carry a whole JSON document as an OctetString. `ERROR:`
    payloads and non-JSON text come back as None rather than as a string that
    happens to be in a JSON-shaped field.
    """
    value = usable_value(value)
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def parse_fans(value):
    """Fan array JSON → list of per-fan dicts. Always a list.

    Observed: 10 entries, every one `{"fanId":0..9,"speed":0,"status":0}`.

    `speed` is not implemented. NovaStar R&D, by email: "The device's fan speed
    and power supply voltage are not currently provided by the SNMP protocol.
    If you require this data, it needs to be customized." It is not an
    un-scaled tach value waiting for a decode and not something a later
    firmware fills in — it is a key with nothing behind it. Surfaced as
    `speed_raw`, never as a unit-bearing field, so nothing downstream can alarm
    on "fan stopped".

    `status` is the field to trust: NovaStar's OID table gives `.1.16` as
    `Normal: 0 / Abnormal: 1`, so 0 is genuinely OK here. Do NOT generalise it
    — polarity is per-OID on this device. `.1.17` is not the counter-example it
    used to be listed as (its documented `0: Not connected / 1: Connected`
    describes `iSignal`, a different field — see `parse_psus`), but `.1.13`,
    `.20.2.1`, `.30.2.1`, `.30.5.3` and `.30.5.4` all are: 0 is bad on those.
    """
    entries = parse_json_value(value)
    if not isinstance(entries, list):
        return []
    fans = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = _maybe_int(entry.get("status"))
        fans.append({
            "fan_id": _maybe_int(entry.get("fanId")),
            "speed_raw": _maybe_int(entry.get("speed")),
            "status": status,
            "ok": None if status is None else status == 0,
        })
    return fans


def parse_psus(value):
    """PSU array JSON → list of per-supply dicts. Always a list.

    Observed: 4 entries, `{"iSignal":1,"powerId":0..3,"status":0,"voltage":0}`.

    **`iSignal` is the power field, NOT `status`.** NovaStar R&D, by email:
    "Regarding the device power status, please use the iSignal field. Meaning:
    Power status (0: not connected to power, 1: connected to power)." The
    documented `.1.17` polarity — `0: Not connected / 1: Connected` — is
    therefore CORRECT; it describes `iSignal`, which is absent from the `.1.17`
    example in NovaStar's own OID table and is why we never found it. Both
    devices in this fleet report `iSignal: 1` on every supply, so the wall was
    connected and healthy the whole time we were arguing about it.

    This codebase got it wrong twice, in opposite directions, and that record
    is kept on purpose. First `status: 0` was read as OK on the fan OID's
    `Normal: 0` polarity — a false all-clear on the one thing an operator most
    needs to know about. Then the documented polarity was applied to `status`
    instead, which made a healthy wall report four unconnected supplies, and
    when the hardware contradicted that, the conclusion drawn was that
    NovaStar's table had the polarity "backwards". It did not. Both mistakes
    have the same root: reading a field whose meaning was never documented and
    inventing one for it. `status` on this OID is STILL undocumented — R&D
    confirmed `iSignal` and said nothing at all about `status` — so it is
    carried raw here and drives no verdict.

    Polarity is still per-OID on this device and must never be assumed:
    `.1.16` fans, `.1.8` temperature, `.1.14` CPU and `.1.15` memory are
    `Normal: 0`, while `.1.13` system working status, `.20.2.1` / `.30.2.1`
    card-slot status and `.30.5.3` / `.30.5.4` backup-port status are all
    "0 is bad".

    `voltage` is not implemented and never will read anything. NovaStar R&D,
    by email: "The device's fan speed and power supply voltage are not
    currently provided by the SNMP protocol. If you require this data, it needs
    to be customized." The key is emitted, always reads 0, and means nothing.
    It is surfaced as `voltage_raw` with no unit attached — there is nothing
    here to revisit on a later firmware.
    """
    entries = parse_json_value(value)
    if not isinstance(entries, list):
        return []
    psus = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = _maybe_int(entry.get("status"))
        i_signal = _maybe_int(entry.get("iSignal"))
        # The power verdict comes from `iSignal` — 1 connected, 0 not
        # connected to power — per NovaStar R&D, by email. See the docstring.
        connected = None if i_signal is None else i_signal == 1
        psus.append({
            "power_id": _maybe_int(entry.get("powerId")),
            # Raw and unjudged. R&D named `iSignal` as the power field and said
            # nothing about what `status` means on this OID, and this codebase
            # has twice invented a meaning for an undocumented value here and
            # been wrong. Published for display; nothing derives from it.
            "status": status,
            "connected": connected,
            "ok": connected,
            # Not provided over SNMP at all (R&D, by email — see the
            # docstring). Always 0, means nothing, carries no unit.
            "voltage_raw": _maybe_int(entry.get("voltage")),
            "i_signal": i_signal,
        })
    return psus


# `.30.5.x` — fields describing the ONE (slot, port) selected by a `.30.4`
# SET, not an index over ports. `.5.2` has no row in NovaStar's table and has
# never been observed; it is left unnamed rather than guessed at.
OUTPUT_PORT_FIELDS = {
    "1": "link_status",
    "3": "backup_working",
    "4": "backup_link",
}


class HSeriesSNMPMonitor:
    """Named, typed monitoring fields on top of the raw OID space.

    Callers ask for "device health" and get decoded fans, PSUs and statuses —
    including the JSON documents the device embeds in OctetStrings — instead of
    a pile of OIDs. Every method returns a dict (possibly with None values) or
    an empty container; nothing raises.

    Read-only, like everything else here: it owns an SNMPClient and can only
    ask it to GET and walk.
    """

    def __init__(self, client=None, host=None, community=DEFAULT_COMMUNITY,
                 port=SNMP_PORT, timeout=DEFAULT_TIMEOUT):
        if client is None:
            if host is None:
                raise ValueError("HSeriesSNMPMonitor needs a client or a host")
            client = SNMPClient(host, community=community, port=port,
                                timeout=timeout)
        self.client = client

    def close(self):
        self.client.close()

    # ── Device health ──────────────────────────────────────────────────

    def get_device_health(self):
        """Decode the .1 device subtree into named fields.

        One walk rather than N GETs: the subtree is ~15 leaves, and a walk
        also picks up any leaf this map doesn't name yet (it lands in
        "extra"). Returns a dict whose keys are always present, so callers can
        use `is not None` instead of `in`.

        `summary` is the parsed .1.0 JSON. It is the authoritative source for
        cpuStatus / fansCount / etc. because it carries the device's own key
        names.

        `.1.11`-`.1.15` stay in "extra" as raw values. This used to be because
        the capture "could not assign them to specific meanings" — five OIDs
        against three descriptions. That claim is disproved: NovaStar's table
        gives all five (.11 genlock, .12 genlock frame rate, .13 system working
        status, .14 CPU status, .15 memory status; see the OID map comment).
        They stay raw anyway, for a different and better reason — nothing
        downstream consumes them, `.13` is `0: Abnormal` while `.14`/`.15` are
        `Normal: 0`, and this module does not mint health verdicts nobody asked
        for on fields whose polarity alternates row by row.
        """
        values = dict(self.walk_subtree(OID_DEVICE))

        health = {name: None for name in DEVICE_FIELDS.values()}
        health.pop("summary_json", None)
        health.pop("fans_json", None)
        health.pop("psus_json", None)
        extra = {}

        for oid, value in values.items():
            suffix = _suffix(oid, OID_DEVICE)
            if suffix is None:
                continue
            name = DEVICE_FIELDS.get(suffix)
            if name is None:
                extra[suffix] = value
            elif not name.endswith("_json"):
                health[name] = value

        summary = parse_json_value(values.get(OID_DEVICE + ".0"))
        fans = parse_fans(values.get(OID_DEVICE + ".16"))
        psus = parse_psus(values.get(OID_DEVICE + ".17"))

        # Counts: prefer the scalar OID, fall back to the JSON array length.
        # They agreed in the capture (10 fans, 4 PSUs); if a future firmware
        # disagrees, the array is what the health verdict below iterates.
        fan_count = health.get("fan_count")
        psu_count = health.get("psu_count")

        health.update({
            "summary": summary if isinstance(summary, dict) else None,
            "fans": fans,
            "psus": psus,
            "fan_count": fan_count if fan_count is not None else (len(fans) or None),
            "psu_count": psu_count if psu_count is not None else (len(psus) or None),
            "cpu_status": _maybe_int((summary or {}).get("cpuStatus")),
            "temperature_ok": _status_ok(health.get("temperature_status")),
            # Fans/PSUs the device flags as not-OK. Empty list means "all
            # reporting OK"; it is distinct from an empty `fans` list, which
            # means the device told us nothing at all.
            "failed_fans": [f["fan_id"] for f in fans if f["ok"] is False],
            # PSU `ok` IS `connected` (parse_psus), so this list and
            # `disconnected_psus` below hold the same power ids by
            # construction. That is deliberate and is not a duplicate waiting
            # to be collapsed: `failed_psus` is the shape every consumer
            # already reads, and `disconnected_psus` names what the value
            # actually means — a supply NovaStar R&D confirmed (by email) the
            # device is reporting as not connected to power, via `iSignal 0`.
            #
            # Neither can fire on an undocumented value any more. Before the
            # vendor answer, PSU `ok` was derived from the never-documented
            # `status` field and both lists were guesses; now the only thing
            # that puts a supply in either is `iSignal: 0`, which has one
            # documented meaning. A supply that is present but reports some
            # unexpected `status` stays out of both lists entirely.
            "failed_psus": [p["power_id"] for p in psus if p["ok"] is False],
            "disconnected_psus": [p["power_id"] for p in psus
                                  if p["connected"] is False],
            "extra": extra,
        })
        return health

    # ── Cards, ports, screens ──────────────────────────────────────────

    def get_input_status(self):
        """Input-card subtree (.20): count, JSON summary, signal block.

        `.20.2.1`-`.20.2.7` are returned raw under "extra" — see the OID map
        comment for why they aren't named.
        """
        values = dict(self.walk_subtree(OID_INPUT))
        result = {
            "card_count": values.get(OID_INPUT + ".1"),
            "summary": parse_json_value(values.get(OID_INPUT + ".3")),
            "signal": {},
            "extra": {},
        }
        for oid, value in values.items():
            suffix = _suffix(oid, OID_INPUT)
            if suffix is None:
                continue
            if suffix in ("1", "3"):
                continue
            if suffix.startswith("5."):
                name = INPUT_SIGNAL_FIELDS.get(suffix[2:])
                if name:
                    result["signal"][name] = value
                    continue
            result["extra"][suffix] = value
        return result

    def get_output_status(self):
        """Output-card subtree (.30): count, per-slot fields, port field table.

        `port` is a dict of NAMED FIELDS (`link_status`, `backup_working`,
        `backup_link`) describing ONE Ethernet port, NOT a map of port number →
        link state. There is no per-port link array anywhere in this subtree.

        Only the readable part. `.30.6` / `.30.7.x` (per-receiving-card work
        status, temperature, voltage, FPGA/MCU version) need `.30.4` SET with a
        slot/port/card selector first and answer `ERROR: BizIdError` until then
        — out of scope for a read-only client, and dropped by the walk rather
        than reported as data. `.30.5.x` is selected by that same SET, so on a
        read-only client the fields describe whichever (slot, port) the device
        currently has selected, which we did not choose and cannot name. Values
        are passed through raw for that reason; no caller may turn them into a
        per-port verdict.
        """
        values = dict(self.walk_subtree(OID_OUTPUT))
        result = {
            "card_count": values.get(OID_OUTPUT + ".1"),
            "summary": parse_json_value(values.get(OID_OUTPUT + ".3")),
            "card": {},
            "port": {},
            "extra": {},
        }
        for oid, value in values.items():
            suffix = _suffix(oid, OID_OUTPUT)
            if suffix is None:
                continue
            if suffix in ("1", "3"):
                continue
            if suffix.startswith("2."):
                name = OUTPUT_CARD_FIELDS.get(suffix[2:])
                if name:
                    result["card"][name] = value
                    continue
            if suffix.startswith("5."):
                # A FIELD table, not a port index. This was decoded as
                # `port_link[N]` — i.e. "the link state of port N" — and the
                # missing `.5.2` was written off as "a gap is normal". It is
                # not a gap: NovaStar's table gives these as four different
                # fields describing ONE (slot, port) selected by the `.30.4`
                # SET, exactly like the sibling `.20.5.x` input table that
                # this module already decodes correctly.
                #
                #   .5.1  link status of the Ethernet port
                #   .5.3  working status of the BACKUP port  0 inactive / 1 active
                #   .5.4  link status of the BACKUP port     0 not linked / 1 linked
                #
                # The consequence of the old reading was a wall of nonsense:
                # our H15 answered `{1: 0, 3: 0, 4: 0}`, which came out as
                # "three of sixteen ports report no link" and produced a
                # CRITICAL "output port 1 link lost" on a healthy wall. Read
                # correctly it says primary link 0, backup inactive, backup
                # not linked — and the last two are exactly right for a wall
                # whose backup is idle.
                name = OUTPUT_PORT_FIELDS.get(suffix[2:])
                if name:
                    result["port"][name] = value
                else:
                    result["extra"][suffix] = value
                continue
            result["extra"][suffix] = value
        return result

    def get_screens(self):
        """Screen subtree (.40): count, plus the per-screen fields raw.

        `.40.2.1`-`.40.2.7` hold the screen name ("CIRCUIT MOM"), 3840x2160, a
        Float framerate and a brightness, but the capture has five described
        values for seven OIDs and no confirmed order, so they are returned
        under "fields" keyed by their OID suffix rather than named. The
        framerate arrives already decoded from Opaque Float.
        """
        values = dict(self.walk_subtree(OID_SCREEN))
        result = {
            "screen_count": values.get(OID_SCREEN + ".1"),
            "fields": {},
            "extra": {},
        }
        for oid, value in values.items():
            suffix = _suffix(oid, OID_SCREEN)
            if suffix is None or suffix == "1":
                continue
            if suffix.startswith("2."):
                result["fields"][suffix[2:]] = value
            else:
                result["extra"][suffix] = value
        return result

    # ── Plumbing ───────────────────────────────────────────────────────

    def walk_subtree(self, root, max_varbinds=MAX_WALK_VARBINDS):
        """Bounded walk of one subtree. Always a list of (oid, value)."""
        return self.client.walk(root, max_varbinds=max_varbinds)

    def get_identity(self):
        """Cheap single-PDU identity read: model, firmware, SN, MAC, IP.

        Useful as a reachability probe — five scalars in one datagram, no walk.
        """
        oids = [OID_DEVICE + "." + suffix
                for suffix in ("2", "3", "4", "5", "6")]
        values = self.client.get_many(oids)
        return {
            "model": values.get(OID_DEVICE + ".2"),
            "firmware": values.get(OID_DEVICE + ".3"),
            "serial_number": values.get(OID_DEVICE + ".4"),
            "mac": values.get(OID_DEVICE + ".5"),
            "ip": values.get(OID_DEVICE + ".6"),
        }


# ── Small shared helpers (same semantics as h_series_json) ─────────────────


def _maybe_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status_ok(value):
    """Status field → True (0 = OK) / False (non-zero) / None (missing).

    For the `Normal: 0` fields ONLY. Its single caller is
    `temperature_status` (`.1.8`), which NovaStar's OID table gives as
    `Normal: 0`, as it does `.1.14` CPU, `.1.15` memory, `.1.16` fans and the
    per-receiving-card statuses under `.30.7`.

    This is NOT the polarity every status field on this device family uses,
    and the sentence that used to say so here is what licensed this helper
    being pointed at `.30.2.1` in device_manager, where 0 means Abnormal and
    the verdict came out backwards in both directions. `.1.11` genlock and
    `.1.13` system working status are `0: Not connected` / `0: Abnormal` too.
    Polarity is per-OID; before adding a caller, look the OID up.
    """
    value = _maybe_int(value)
    if value is None:
        return None
    return value == 0
