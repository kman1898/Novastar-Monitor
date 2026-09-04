"""H-series JSON UDP control protocol client (port 6000).

Modern H-series video wall splicers expose a documented JSON-over-UDP control
protocol on port 6000 alongside the legacy binary protocol on TCP 5203.
This client uses the JSON path because:

- Responses are structured JSON — no register reverse-engineering required
- Per-card monitoring uses direct (slot, port, card_id) addressing
- Topology is returned by the controller (no chain serpentine guessing)
- The schema is documented in
  "H Series Video Wall Splicers Control Protocol V1.0.19" (NovaStar PDF).

This is the preferred path for H-series devices. The binary protocol stays in
device_manager as a fallback if the JSON port doesn't respond (older firmware,
network filtering, etc.).

This client is strictly READ-ONLY: every command it can send is an `R....`
query. It used to also send the `W0120` keepalive; that was removed after the
outage described below and must not come back — see the note at the end of the
command section.

Performance notes (a full per-card R0155 sweep is ~1374 reads, which is why
device_manager no longer runs one on a timer):

- One persistent UDP socket is reused for all calls instead of a new socket
  per request.
- Multiple commands go out in a single datagram (`send_recv_batch`) — the wire
  format is already a JSON array, so this is native to the protocol.
- Timeouts are per call class: bulk per-card reads use `BULK_TIMEOUT`,
  topology calls (R0100/R0400/R0405) keep the longer `TOPOLOGY_TIMEOUT`.
"""

import json
import socket
import threading
import time

JSON_UDP_PORT = 6000

# ── Timeouts ───────────────────────────────────────────────────────────────
# Measured LAN round-trip to the splicer is sub-millisecond (same switch,
# wired GigE); the device-side lookup adds a few ms at most. The timeout only
# ever matters for a request that gets NO answer, and R0155 silently returns
# nothing for cards it can't reach — so every unreachable card costs a full
# timeout. 0.3 s is ~2 orders of magnitude above the observed RTT (plenty of
# headroom for a loaded controller) while capping the cost of a dead card at
# 0.3 s instead of 2 s.
BULK_TIMEOUT = 0.3           # R0155 per-card reads
# Topology/verbose calls (R0100, R0400, R0401, R0405) build large JSON
# documents (R0401 is ~17 KB, fragmented across several IP datagrams) and the
# controller can take a while to assemble them. Keep the historical 2 s.
TOPOLOGY_TIMEOUT = 2.0
DEFAULT_TIMEOUT = TOPOLOGY_TIMEOUT   # kept for callers that import the name

MAX_PAYLOAD = 65536

# ── Batching ───────────────────────────────────────────────────────────────
# An R0155 request object is ~55 B and its response object ~150 B. Sizing is
# driven by the *response*: 8 responses ≈ 1.2 KB, which still fits inside one
# 1500 B Ethernet frame (1472 B of UDP payload after IP+UDP headers). Staying
# under the MTU means no IP fragmentation, so a single lost fragment can't
# destroy a whole batch. Deliberately conservative — the win is already ~8x
# fewer round trips, and the firmware's per-datagram command limit is
# undocumented.
DEFAULT_BATCH_SIZE = 8

# The device handles all commands in one datagram together, so the replies to
# a batch come back essentially back-to-back (sub-ms RTT, and they usually
# share a single response datagram). Once the first reply lands, anything
# still missing after a quiet period of this length is a card that isn't
# answering at all, so we stop waiting instead of holding the whole batch
# hostage to the full timeout. 50 ms is ~50x the observed RTT.
BATCH_SETTLE = 0.05


class HSeriesJSONClient:
    """Thread-safe UDP client for the H-series JSON protocol.

    Holds one persistent socket for command/response traffic, behind one lock.
    A socket that errors out is closed and lazily recreated on the next call.

    The protocol is stateless request/response, so "persistent" here only
    means the file descriptor is reused — there is no connection to keep.
    """

    def __init__(self, ip, port=JSON_UDP_PORT, timeout=TOPOLOGY_TIMEOUT,
                 bulk_timeout=BULK_TIMEOUT, batch_size=DEFAULT_BATCH_SIZE):
        self.ip = ip
        self.port = port
        self.timeout = timeout              # topology / default call class
        self.bulk_timeout = bulk_timeout    # per-card reads
        self.batch_size = batch_size

        self._lock = threading.Lock()       # guards _sock
        self._sock = None

        # Flipped to True if the device answers a multi-command datagram but
        # its replies can't be correlated (see send_recv_batch). From then on
        # we degrade to one command per datagram — slower, but never wrong.
        self._batch_unsupported = False

    # ── Socket management ──────────────────────────────────────────────

    @staticmethod
    def _new_socket():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Left unconnected on purpose: a connect()ed UDP socket drops replies
        # that come from any other source port, and we have not verified that
        # the splicer always answers from :6000.
        return sock

    @staticmethod
    def _close(sock):
        try:
            sock.close()
        except Exception:
            pass

    def _get_socket(self):
        """Return the shared command socket, creating it if needed.

        Caller must hold `self._lock`.
        """
        if self._sock is None:
            self._sock = self._new_socket()
        return self._sock

    def _drop_socket(self):
        """Discard the command socket after an error. Caller holds the lock."""
        if self._sock is not None:
            self._close(self._sock)
            self._sock = None

    def close(self):
        """Release the socket. Safe to call more than once."""
        with self._lock:
            self._drop_socket()

    @staticmethod
    def _drain(sock):
        """Discard datagrams left over from an earlier timed-out request.

        UDP has no sequence numbers, so a late reply to a previous command
        would otherwise be read as the answer to the current one. Cheap
        insurance now that the socket is reused across calls.
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

    def _exchange(self, sock, cmd_objs, timeout, response_key, expected,
                  settle=None):
        """Send one datagram of N commands, collect replies until done.

        Returns `(matched, ordered)` where `matched` maps correlation key →
        response object and `ordered` is every response object seen, in
        arrival order. Keeps reading until `expected` distinct keys have been
        matched or the deadline passes, so it works whether the device answers
        with one array datagram or one datagram per command.

        `settle`, if given, caps the wait at that long after the *last* reply
        received — see BATCH_SETTLE. The hard `timeout` still applies when the
        device says nothing at all.

        Raises OSError to the caller, which is responsible for dropping the
        socket. Returns empty results on timeout / undecodable payloads.
        """
        payload = json.dumps(cmd_objs, separators=(",", ":")).encode("utf-8")
        self._drain(sock)
        sock.settimeout(timeout)
        sock.sendto(payload, (self.ip, self.port))

        matched = {}
        ordered = []
        hard_deadline = time.monotonic() + timeout
        deadline = hard_deadline
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, _ = sock.recvfrom(MAX_PAYLOAD)
            except socket.timeout:
                break
            try:
                parsed = json.loads(data.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            elements = parsed if isinstance(parsed, list) else [parsed]
            for element in elements:
                if not isinstance(element, dict):
                    continue
                ordered.append(element)
                key = response_key(element)
                if key is not None and key not in matched:
                    matched[key] = element
            if len(matched) >= expected:
                break
            if settle and ordered:
                deadline = min(hard_deadline, time.monotonic() + settle)
        return matched, ordered

    def send_recv(self, cmd_obj, timeout=None):
        """Send a single JSON command, return the response object or None.

        The wire format wraps the command in a JSON array `[{...}]`. The
        device responds with an array of the same shape; we unwrap and
        return the first element for convenience.

        `timeout` defaults to the topology/default call class (`self.timeout`);
        per-card readers pass `self.bulk_timeout`.
        """
        wait = self.timeout if timeout is None else timeout
        want = _cmd_key(cmd_obj)
        try:
            with self._lock:
                sock = self._get_socket()
                try:
                    matched, ordered = self._exchange(
                        sock, [cmd_obj], wait, _echo_key(want), 1)
                except (OSError, socket.error):
                    self._drop_socket()
                    return None
        except Exception:
            return None
        return _pick_single(matched, ordered, want)

    def send_recv_batch(self, cmd_objs, timeout=None,
                        request_key=None, response_key=None,
                        settle=BATCH_SETTLE):
        """Send N commands in one datagram; return N responses, aligned.

        The result list is positionally aligned with `cmd_objs`; entries the
        device didn't answer (R0155 silently drops unreachable cards) are None.

        Correlation is by command identity, not by position — the protocol has
        no sequence numbers, and the device may answer out of order, in one
        array or in several datagrams. `request_key`/`response_key` extract
        that identity from a request object and a response object; both
        default to the `cmd` field, which is only unique when the batch holds
        distinct commands. For N identical-shaped R0155 reads use
        `get_receiving_cards_batch`, which correlates on the echoed
        (slotId, portId, recvCardId) address instead.

        If the keys aren't unique (or aren't derivable) the batch is refused
        and the commands are sent one at a time — correct, just not faster.

        `settle` stops the wait that long after the last reply instead of
        holding the batch until `timeout`, so one dead card in a batch costs
        ~50 ms rather than the full timeout. Pass 0 to disable.
        """
        if not cmd_objs:
            return []
        request_key = request_key or _cmd_key
        response_key = response_key or _cmd_key
        wait = self.timeout if timeout is None else timeout

        keys = [request_key(c) for c in cmd_objs]
        ambiguous = any(k is None for k in keys) or len(set(keys)) != len(keys)
        if len(cmd_objs) == 1 or ambiguous or self._batch_unsupported:
            return [self.send_recv(c, timeout=wait) for c in cmd_objs]

        try:
            with self._lock:
                sock = self._get_socket()
                try:
                    matched, ordered = self._exchange(
                        sock, list(cmd_objs), wait, response_key, len(keys),
                        settle=settle)
                except (OSError, socket.error):
                    self._drop_socket()
                    return [None] * len(cmd_objs)
        except Exception:
            return [None] * len(cmd_objs)

        if ordered and not matched:
            # The device answered but nothing correlated — its replies don't
            # carry the fields we key on. Never guess by position; degrade to
            # single-command datagrams for the rest of this client's life.
            self._batch_unsupported = True
            return [self.send_recv(c, timeout=wait) for c in cmd_objs]
        return [matched.get(k) for k in keys]

    # ── Documented commands ────────────────────────────────────────────

    def get_device_details(self, device_id=0):
        """R0100 — full device descriptor (model, slots, interfaces).

        Returns a dict with at least: modelId, protoVersion, memory, status,
        slotList[]. Each slot has cardType, modelId, sn, interfaces[].
        """
        return self.send_recv({"cmd": "R0100", "param0": device_id},
                              timeout=self.timeout)

    def get_slot_info(self, slot_id, port_id=0, connector_id=0):
        """R0102 — per-slot info including per-port linkstatus.

        param0=device, param1=slot, param2=connector (0..3).
        Returns slot summary with link state for each output connector.
        """
        return self.send_recv({
            "cmd": "R0102",
            "param0": 0,
            "param1": slot_id,
            "param2": connector_id,
        })

    def get_connector_info(self, slot_id, connector_id):
        """R0103 — per-connector signal state."""
        return self.send_recv({
            "cmd": "R0103",
            "param0": 0,
            "param1": slot_id,
            "param2": connector_id,
        })

    @staticmethod
    def _r0155(slot_id, port_id, card_id):
        """Build one R0155 request. Card ID is split low/high (16-bit)."""
        return {
            "cmd": "R0155",
            "param0": slot_id,
            "param1": port_id,
            "param2": card_id & 0xFF,
            "param3": (card_id >> 8) & 0xFF,
        }

    def get_receiving_card(self, slot_id, port_id, card_id):
        """R0155 — per-card temperature/voltage/etc.

        Card ID is split low/high across param2/param3 (16-bit value).
        Uses the short bulk timeout: this is the per-card polling path, and
        the device answers nothing at all for cards it can't reach.
        """
        return self.send_recv(self._r0155(slot_id, port_id, card_id),
                              timeout=self.bulk_timeout)

    def get_receiving_cards_batch(self, addresses, batch_size=None,
                                  timeout=None, settle=BATCH_SETTLE):
        """R0155 for many cards, `batch_size` commands per datagram.

        `addresses` is an iterable of (slot_id, port_id, card_id) tuples; the
        return value is a list of raw R0155 response dicts (or None where the
        device stayed silent) positionally aligned with `addresses`.

        Correlation uses the address the device echoes back —
        (slotId, portId, recvCardId) — which is what makes batching N
        identical-shaped R0155 calls safe: replies may arrive out of order,
        split across datagrams, or not at all, and each one still lands in the
        right slot. Duplicate addresses inside a single chunk would be
        ambiguous, so `send_recv_batch` detects that and falls back to
        one-at-a-time for that chunk.

        Feed with `parse_receiving_card()` to get monitor-state dicts.
        """
        addresses = list(addresses)
        size = batch_size or self.batch_size
        wait = self.bulk_timeout if timeout is None else timeout
        results = []
        for start in range(0, len(addresses), size):
            chunk = addresses[start:start + size]
            cmds = [self._r0155(*addr) for addr in chunk]
            results.extend(self.send_recv_batch(
                cmds,
                timeout=wait,
                request_key=_r0155_request_key,
                response_key=_r0155_response_key,
                settle=settle,
            ))
        return results

    # ── Undocumented commands used by the official splicer Companion module ──
    # Source: github.com/bitfocus/companion-module-novastar-splicer (and
    # kman1898's fork). These aren't in the public PDF but the splicer module
    # has shipped with them for years, so they're effectively stable.

    def get_device_init_status(self):
        """R0118 — get_device_init_status. Returns `{rate: 0..100}`.

        Splicer module polls this until `rate === 100` before issuing
        any other commands (boot/init guard).
        """
        return self.send_recv({"cmd": "R0118", "param0": 0})

    def get_screen_list(self):
        """R0400 — get_screen_list. Lightweight enumeration of screens.

        Returns just (screenId, name, createTime) per screen. Use
        get_screen_output_info(screenId) to get the actual layout.
        Topology call — long timeout.
        """
        return self.send_recv({"cmd": "R0400", "param0": 0},
                              timeout=self.timeout)

    def get_screen_details(self, screen_id):
        """R0401 — get_screen_details for one screen.

        Returns the full screen descriptor: brightness, color, audio,
        HDR settings, image quality presets, outputMode, etc. This is
        a verbose call (~17 KB JSON); prefer R0405 if you only need
        the per-output layout positions. Topology call — long timeout.
        """
        return self.send_recv({"cmd": "R0401", "param0": screen_id},
                              timeout=self.timeout)

    def get_screen_output_info(self, screen_id):
        """R0405 — get_screen_output_info for one screen.

        **This is the topology endpoint we want.** Returns:
          - mosaic: { row, column } — how outputs are stitched
          - size:   { x, y, width, height } — total screen rect
          - screenInterfaces[]: each output's slotId, interfaceId,
            x, y, width, height, resolution, modelId.
        Primary + backup outputs share the same (x, y) but have
        different slotIds. Topology call — long timeout.
        """
        return self.send_recv({"cmd": "R0405", "param0": screen_id},
                              timeout=self.timeout)

    def get_output_list(self):
        """R0300 — get_output_list. Lightweight enumeration of outputs."""
        return self.send_recv({"cmd": "R0300", "param0": 0})

    def get_output_details(self, output_id):
        """R0301 — get_output_details. Per-output detail (timing, color, online)."""
        return self.send_recv({
            "cmd": "R0301",
            "param0": 0,
            "param1": output_id,
        })

    def get_input_list_simplify(self):
        """R0226 — get_input_list_simplify. Lightweight input enumeration."""
        return self.send_recv({"cmd": "R0226", "param0": 0})

    # ── REMOVED: the W0120 heartbeat. Do not reintroduce it. ───────────────
    #
    # There used to be a `heartbeat()` here sending `W0120` every 3 s on its
    # own socket, copied from the vendor's Bitfocus Companion *control* module
    # for the splicer. While this monitor was running, the operator lost
    # control of the wall from Companion — their actual show-control surface —
    # and killing this app restored it.
    #
    # W0120 is a write-class command whose plausible meaning in the control
    # module's context is "a controller is attached and it is me". A read-only
    # monitor has no business claiming that role, and nothing here ever needed
    # the ack: every reading this client produces comes from an `R....` query
    # that works with no keepalive at all. So the cost was a production outage
    # and the benefit was zero.
    #
    # Removed rather than disabled behind a flag, because a flag is something
    # somebody eventually turns on. If a future feature genuinely needs to
    # control the device, that is a separate, clearly-named control client —
    # not a keepalive smuggled into the monitoring path.


# ── Correlation keys ───────────────────────────────────────────────────────


def _cmd_key(obj):
    """Default correlation key: the `cmd` field both sides echo."""
    if not isinstance(obj, dict):
        return None
    return obj.get("cmd")


def _echo_key(want):
    """Correlation key for a single command: accept only its own echo.

    Because the socket is now shared across calls, a late reply to an earlier
    timed-out command can still be sitting in the receive queue. Keying on the
    echoed `cmd` (case-insensitively — the device has only ever been seen
    echoing the exact string, but the comparison costs nothing) means such a
    reply is discarded instead of being returned as this call's answer.
    """
    def key(obj):
        if not isinstance(obj, dict):
            return None
        got = obj.get("cmd")
        if got is None or want is None:
            return None
        if got == want:
            return want
        if isinstance(got, str) and isinstance(want, str) \
                and got.lower() == want.lower():
            return want
        return None
    return key


def _pick_single(matched, ordered, want):
    """Choose the response for a single-command exchange, or None."""
    if want in matched:
        return matched[want]
    # Some commands answer with a bare payload that doesn't echo `cmd` at
    # all; accept the first such object. Anything that echoes a *different*
    # command is somebody else's reply — never return it.
    for element in ordered:
        if "cmd" not in element:
            return element
    return None


def _r0155_request_key(cmd_obj):
    """(cmd, slot, port, card_id) from an outgoing R0155 request."""
    if not isinstance(cmd_obj, dict):
        return None
    low = cmd_obj.get("param2")
    high = cmd_obj.get("param3")
    if low is None or high is None:
        return None
    return ("R0155", cmd_obj.get("param0"), cmd_obj.get("param1"),
            (high << 8) | low)


def _r0155_response_key(resp):
    """(cmd, slot, port, card_id) from an R0155 response.

    The device echoes the address it was asked about as slotId / portId /
    recvCardId (recvCardId is the recombined 16-bit id), which is the only
    thing distinguishing otherwise identical R0155 replies.
    """
    if not isinstance(resp, dict) or resp.get("cmd") != "R0155":
        return None
    slot = resp.get("slotId")
    port = resp.get("portId")
    card = resp.get("recvCardId")
    if slot is None or port is None or card is None:
        return None
    return ("R0155", slot, port, card)


# ── Helpers for mapping JSON responses to monitor state ────────────────────


# An H-series output card has two OPT (fibre) ports and sixteen Ethernet
# ports. R0100 reports them in two separate blocks — `lightstatus` (2 entries)
# and `linkstatus` (16) — which is what makes "is this card on fibre or
# copper?" answerable. See `parse_output_links`.
OPT_PORTS_PER_CARD = 2
ETHERNET_PORTS_PER_CARD = 16


def parse_device_details(r0100):
    """Extract a minimal monitor-state-friendly dict from R0100."""
    if not isinstance(r0100, dict):
        return None
    slots = r0100.get("slotList", [])
    return {
        "name": r0100.get("name", ""),
        "model_id": r0100.get("modelId"),
        "proto_version": r0100.get("protoVersion"),
        "memory": r0100.get("memory"),
        "status": r0100.get("status"),
        "slot_count": len(slots),
        "slots": [{
            "slot_id": s.get("slotId"),
            "model_id": s.get("modelId"),
            "card_type": s.get("cardType"),
            "sn": s.get("sn", ""),
            "resolution": s.get("resolution", {}),
            "interfaces": [{
                "interface_id": i.get("interfaceId"),
                "interface_type": i.get("interfaceType"),
                "i_signal": i.get("iSignal"),
                "is_used": i.get("isUsed"),
                "over_load_state": i.get("overLoadState"),
            } for i in s.get("interfaces", [])],
            "output_links": parse_output_links(s),
        } for s in slots],
    }


def parse_output_links(slot):
    """Which of a sender card's outputs are OPT and which are Ethernet.

    R0100 carries two link structures per output card and they cover different
    hardware:

      `lightstatus`  {link0, link1}          the two OPT (fibre) ports
      `linkstatus`   {link0 .. link15}       the sixteen Ethernet ports

    Confirmed against a wall whose wiring was known independently: the card
    running fibre reported `lightstatus {2, 2}` with `linkstatus` all zero,
    and the card running copper straight out of the Ethernet ports reported
    `lightstatus {0, 0}` with `linkstatus link0/link1` non-zero and the rest
    zero. Each card's backup mirrored it. Nothing was told to the device to
    produce that — both structures agreed with the physical wiring on all four
    cards.

    So a non-zero entry means "this output is carrying something", and which
    structure it came from says whether that output is fibre or copper. That
    is the difference between "OPT 1 port 3" and "Ethernet port 3", which the
    wall map otherwise has to guess at.

    The *meaning of the value* is not decoded. Observed 1 and 2 on Ethernet
    and 2 on OPT, with no reading that distinguishes them reliably, so this
    reports `state` verbatim and claims only up/down. Do not invent a speed or
    a health grade from it.

    `senderInterfaceStatus` carries the same 16 Ethernet entries in list form;
    it is read as a fallback for firmware that omits `linkstatus`.
    """
    def _entries(block, count):
        out = []
        if isinstance(block, dict):
            for idx in range(count):
                out.append(block.get(f"link{idx}"))
        return out

    optical = _entries(slot.get("lightstatus"), OPT_PORTS_PER_CARD)
    ethernet = _entries(slot.get("linkstatus"), ETHERNET_PORTS_PER_CARD)

    if not any(v is not None for v in ethernet):
        by_id = {}
        for entry in (slot.get("senderInterfaceStatus") or []):
            if isinstance(entry, dict) and entry.get("id") is not None:
                by_id[entry["id"]] = entry.get("status")
        if by_id:
            ethernet = [by_id.get(i) for i in range(ETHERNET_PORTS_PER_CARD)]

    def _ports(states, medium):
        return [{"index": i, "medium": medium, "state": st,
                 "up": bool(st)}
                for i, st in enumerate(states) if st is not None]

    opt_ports = _ports(optical, "opt")
    eth_ports = _ports(ethernet, "ethernet")
    opt_up = [p for p in opt_ports if p["up"]]
    eth_up = [p for p in eth_ports if p["up"]]

    # What the card is actually wired with. Both non-empty is legal hardware,
    # so it gets its own answer rather than being forced into one or the other.
    if opt_up and eth_up:
        medium = "mixed"
    elif opt_up:
        medium = "opt"
    elif eth_up:
        medium = "ethernet"
    else:
        medium = None

    return {
        "medium": medium,
        "opt": opt_ports,
        "ethernet": eth_ports,
        "opt_up": [p["index"] for p in opt_up],
        "ethernet_up": [p["index"] for p in eth_up],
    }


# ── R0102 sender-card link status ──────────────────────────────────────────
#
# NovaStar, by email 2026-09-04, answering "how do we detect primary/backup
# link switching": R0102 reads the sender card's `linkstatus`, encoded
#
#     0: Network cable not connected
#     1: Network cable connected
#     2: Redundancy not set
#     3: Redundancy enabled
#
# That enum folds TWO independent facts into one number. 0 and 1 answer "is a
# cable plugged in"; 2 and 3 answer "is redundancy configured". A value tells
# you about one axis and says nothing about the other, so it cannot be reduced
# to a single boolean — `up = bool(state)` would make "redundancy not set"
# indistinguishable from "cable connected". Each axis gets its own tri-state.
#
# NOT YET SEEN ON THE WIRE. No R0102 reply has been captured from this
# hardware, so the reply *shape* below is defensive rather than observed: the
# field is read whether the device puts it at the top level, in a per-connector
# `{link0..linkN}` block, or in a list. The encoding is the vendor's; the
# container is a guess. Verify against hardware before anything alerts on it —
# see docs/NEXT_HARDWARE_SESSION.md.
#
# Also unsettled: whether "Redundancy enabled" means redundancy is CONFIGURED
# or that the card is CURRENTLY carrying the backup path. Those are very
# different answers to "are we running on backup right now", and the vendor's
# wording does not decide it. Nothing in this project treats 3 as live failover
# until that is confirmed.
LINK_STATUS_LABELS = {
    0: "cable_disconnected",
    1: "cable_connected",
    2: "redundancy_not_set",
    3: "redundancy_enabled",
}

# Which axis each value speaks to: (cable_connected, redundancy_enabled).
# None means "this value says nothing about that axis".
_LINK_STATUS_AXES = {
    0: (False, None),
    1: (True, None),
    2: (None, False),
    3: (None, True),
}


def decode_link_status(state):
    """One R0102 `linkstatus` value → what it actually claims.

    Returns a dict with the raw `state`, a `label`, and the two independent
    tri-states. An unrecognised value keeps its raw form and claims nothing —
    the same rule byte[12] of the binary live-monitoring register needed after
    "anything unrecognised is disconnected" labelled 124 healthy panels down.
    """
    raw = _maybe_int(state)
    cable, redundancy = _LINK_STATUS_AXES.get(raw, (None, None))
    return {
        "state": raw,
        "label": LINK_STATUS_LABELS.get(raw),
        "cable_connected": cable,
        "redundancy_enabled": redundancy,
    }


def parse_slot_info(r0102):
    """Extract sender-card link state from an R0102 reply.

    `links` is one decoded entry per connector the reply carries, in index
    order. A reply with a single top-level `linkstatus` yields one entry at
    index 0 — that is the shape the vendor's wording implies (one value per
    sender card), and the per-connector block is handled in case the device
    answers per port instead.

    The two summary booleans are deliberately conservative: True only if some
    connector says so outright, False only if every connector that speaks to
    that axis says otherwise, and None when nothing in the reply addresses it.
    """
    if not isinstance(r0102, dict):
        return None

    block = r0102.get("linkstatus")
    if isinstance(block, dict):
        states = []
        for idx in range(ETHERNET_PORTS_PER_CARD):
            if f"link{idx}" not in block:
                break
            states.append(block[f"link{idx}"])
    elif isinstance(block, list):
        states = list(block)
    elif block is None:
        states = []
    else:
        states = [block]

    links = [dict(decode_link_status(st), index=i)
             for i, st in enumerate(states)]

    def _summary(key):
        values = [link[key] for link in links if link[key] is not None]
        if not values:
            return None
        return any(values)

    return {
        "slot_id": r0102.get("slotId"),
        "connector_id": r0102.get("connectorId"),
        "links": links,
        "cable_connected": _summary("cable_connected"),
        "redundancy_enabled": _summary("redundancy_enabled"),
        "raw": r0102,
    }


# ── R0155 reply schemas ────────────────────────────────────────────────────
#
# Two different R0155 reply shapes have been seen on real hardware, and both
# are supported because both came off actual devices in this fleet.
#
# SCHEMA_BYTE — the original capture::
#
#     {"deviceId":0,"slotId":20,"portId":0,"recvCardId":0,
#      "power0Status":0,"power1Status":0,"brightness":127,
#      "temp":88,"voltage":170,"cmd":"R0155","ack":"Ok"}
#
# SCHEMA_CENTI — captured live off the operator's H-series, 2026-08-08::
#
#     {"deviceId":0,"slotId":20,"portId":0,"recvCardId":10,
#      "mcuVersion":"V4.5.1.81","fpgaVersion":"V4.5.1.81",
#      "workStatus":0,"tempStatus":0,"temp":3700,"tempMax":70,
#      "voltStatus":0,"volt":440,"power0Status":0,"power1Status":0,
#      "brightness":25,"cmd":"R0155","ack":"Ok"}
#
# The two disagree about BOTH the key names and the scaling of `temp`, so
# decoding one as the other is not a rounding error — 3700 read as a raw byte
# is 1850 °C, which is exactly the bogus reading this split was added to fix.
#
# Detection is by KEY PRESENCE, never by value range. The voltage key is the
# discriminator (`volt` vs `voltage`) because it is the one field that is
# named differently in the two captures and is present in both of them; a
# range test on `temp` would have no defensible cut-off (a byte-schema card at
# 44 °C reports 88, and nothing rules out a centi-schema card reporting 88 =
# 0.88 °C on a cold start). The extra centi-only keys act as a backstop for a
# reply that carries the status block but no voltage field.
#
# SCHEMA_CENTI IS NOW THE DOCUMENTED ONE. NovaStar sent a corrected R0155 field
# document on 2026-09-04 (shipping with H firmware V2.3.0.0), saying the
# published version "does indeed lack some information and has unit errors".
# The corrected page specifies `temp` in units of 0.01 °C (4200 = 42 °C) and
# `volt` in units of 0.01 V (480 = 4.8 V) — exactly the scalings inferred here.
# See docs/H_SERIES_FINDINGS.md §6.7.
#
# SCHEMA_BYTE is NOT that documentation error. It was captured off real
# hardware and names its field `voltage`, not `volt`, so it is a genuine second
# reply shape; its scalings remain inferred from that one capture. A third
# firmware could plausibly use a third encoding; if one shows up it needs its
# own schema, not a tweak to these.
SCHEMA_BYTE = "byte"      # temp / 2 → °C, (voltage & 0x7F) * 0.1 → V
SCHEMA_CENTI = "centi"    # temp / 100 → °C, volt / 100 → V

# Keys that only the centi-schema firmware has been observed to send. Used
# only as a fallback when neither voltage key is present.
_CENTI_ONLY_KEYS = ("workStatus", "tempStatus", "voltStatus", "tempMax")


def detect_receiving_card_schema(r0155):
    """Return SCHEMA_CENTI or SCHEMA_BYTE for an R0155 reply.

    `volt` is checked before `voltage`: no reply carrying `volt` has ever been
    seen from the byte-schema firmware, so if both somehow appear the newer
    shape wins. A reply with neither voltage key falls back to SCHEMA_BYTE,
    which is a compatibility default and not a claim about the wire format —
    every capture of either schema has included its own voltage key, so this
    only affects replies we have never seen.
    """
    if not isinstance(r0155, dict):
        return SCHEMA_BYTE
    if "volt" in r0155:
        return SCHEMA_CENTI
    if "voltage" in r0155:
        return SCHEMA_BYTE
    if any(k in r0155 for k in _CENTI_ONLY_KEYS):
        return SCHEMA_CENTI
    return SCHEMA_BYTE


def parse_receiving_card(r0155):
    """Extract per-card state from an R0155 response.

    Handles both reply schemas — see SCHEMA_BYTE / SCHEMA_CENTI above for the
    captures and the scaling each one implies.

    A card the controller can't reach produces no reply at all, so `None` in /
    `None` out is one offline signal. There are two more:

    · a non-"Ok" ack, and
    · `workStatus != 0` (centi schema only).

    `workStatus` is the important one. On the live wall every card with
    `workStatus == 1` reported `temp: 0`, `volt: 0`, `brightness: 0` and
    `voltStatus: 2`, while every `workStatus == 0` card reported plausible
    readings (temp 3600–3800, volt 410–430). Those zeros are PLACEHOLDERS for
    a card that is not reporting, not measurements of a card sitting at 0 °C
    on a dead 0 V rail. So a non-reporting card comes back with `online:
    False` and every reading — temperature, voltage, brightness AND both power
    flags — set to None rather than to the placeholder value. Nothing
    downstream may average, max, or alert on a number the device never
    measured, and a power flag on such a card must not read as a verdict on a
    supply in either direction.

    `workStatus` is vendor-documented as 0 = Normal, 1 = Abnormal, and only
    those two have been observed; anything non-zero is treated as not
    reporting.

    Returns keys in the shape device_manager already stores per card
    (temp_c / temperature_c / voltage_v / brightness / primary_power_ok /
    backup_power_ok / online) so it can be merged straight into a card entry.
    `primary_power_ok` / `backup_power_ok` keep those names for compatibility
    but carry supply 1 and supply 2 — NovaStar's corrected R0155 document names
    the fields that way, not primary/backup.
    """
    if not isinstance(r0155, dict):
        return None
    ack = r0155.get("ack")
    ack_ok = ack is None or str(ack).lower() == "ok"
    schema = detect_receiving_card_schema(r0155)

    # Absent on the byte schema, which has no work-status field at all. An
    # answering byte-schema card is therefore assumed to be reporting — that
    # is the behaviour it has always had, and there is no field to say
    # otherwise.
    work_status = _maybe_int(r0155.get("workStatus"))
    reporting = work_status is None or work_status == 0

    card = {
        "online": ack_ok and reporting,
        "reporting": reporting,
        "work_status": work_status,
        "ack": ack,
        "schema": schema,
        "slot": r0155.get("slotId"),
        "port": r0155.get("portId"),
        "card_id": r0155.get("recvCardId"),
        # Both temperature keys: the aggregator reads temperature_c, the
        # device-tree renderer reads temp_c. Present-but-None means "no
        # reading", which is what every consumer's `is not None` guard wants.
        "temp_c": None,
        "temperature_c": None,
        "temperature_raw": None,
        "voltage_v": None,
        "voltage_raw": None,
        "brightness": None,
        "primary_power_ok": None,
        "backup_power_ok": None,
        # The controller's own temperature limit for this card (centi schema
        # only; 70 on every card of the live wall). Read as whole °C, not
        # centi-°C: the centi scaling would make it 0.7 °C, which is not a
        # limit. More authoritative than the app's hardcoded default
        # threshold, though nothing alerts on it yet.
        "temp_limit_c": _maybe_int(r0155.get("tempMax")),
        # The device's own verdict on each reading, centi schema only. The
        # enum is now vendor-documented (R0155 field doc, 2026-09-04):
        # 0 = Normal, 1 = Alarm, 2 = Abnormal. Only 0 and 2 have been seen
        # here, and 2 only on cards that were also `workStatus: 1`.
        # Any non-zero is treated as "the device says this reading isn't
        # good". Surfaced, not acted on: a non-zero status on a card that IS
        # reporting has never been observed, and suppressing its reading could
        # hide a genuine over-temperature.
        "temp_status": _maybe_int(r0155.get("tempStatus")),
        "volt_status": _maybe_int(r0155.get("voltStatus")),
        "temp_status_ok": _status_ok(r0155.get("tempStatus")),
        "volt_status_ok": _status_ok(r0155.get("voltStatus")),
        # Per-card firmware (centi schema only) — a card whose versions differ
        # from the rest of its chain is worth spotting.
        "mcu_version": r0155.get("mcuVersion"),
        "fpga_version": r0155.get("fpgaVersion"),
        "raw": r0155,
    }

    if not card["online"]:
        # Not reporting (or a failed ack): every value in the reply is a
        # placeholder. Leave them all None.
        return card

    temp_raw = _maybe_int(r0155.get("temp"))
    if schema == SCHEMA_CENTI:
        volt_raw = _maybe_int(r0155.get("volt"))
        temp_c = decode_temp_centi(temp_raw)
        volt_v = decode_volt_centi(volt_raw)
    else:
        volt_raw = _maybe_int(r0155.get("voltage"))
        temp_c = decode_temp_byte(temp_raw)
        volt_v = decode_voltage_byte(volt_raw)

    card.update({
        "temp_c": temp_c,
        "temperature_c": temp_c,
        "temperature_raw": temp_raw,
        "voltage_v": volt_v,
        "voltage_raw": volt_raw,
        "brightness": r0155.get("brightness"),
        # Power status: vendor-documented 0 = Fault, 1 = Normal, for receiving
        # card power supply 1 (power0Status) and supply 2 (power1Status). Only
        # 1 is trusted; 0 reads as unknown rather than as a fault, because most
        # of a healthy wall reports it. See _power_status for why. Only ever
        # evaluated for a card that is actually reporting, so a non-reporting
        # card's placeholder zeros claim nothing at all.
        "primary_power_ok": _power_status(r0155.get("power0Status"), True),
        "backup_power_ok": _power_status(r0155.get("power1Status"), True),
        # The raw fields, kept so the meaning can be settled later without
        # re-reading the wall.
        "power0_status_raw": r0155.get("power0Status"),
        "power1_status_raw": r0155.get("power1Status"),
    })
    return card


def decode_temp_centi(v):
    """Centi-schema temp → °C: value / 100.

    Vendor-documented. NovaStar's corrected R0155 field document (2026-09-04):
    "Temperature value, in units of 0.01 degrees Celsius; for example, a value
    of 4200 represents a temperature of 42 degrees Celsius."

    Independently consistent with the live H-series capture that this decode
    was originally derived from: `temp` 3600–3800 across reporting cards →
    36.0–38.0 °C, matching a wall running normally. The same values under the
    byte schema's `/2` would be 1800–1900 °C.
    """
    if v is None:
        return None
    try:
        return round(int(v) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def decode_volt_centi(v):
    """Centi-schema volt → V: value / 100.

    Vendor-documented. NovaStar's corrected R0155 field document (2026-09-04):
    "Voltage value, in units of 0.01V; for example, a value of 480 represents a
    voltage of 4.8V."

    Agrees with the capture this was derived from: `volt` 410–440 on reporting
    cards → 4.10–4.40 V. Cards that were not reporting (`workStatus: 1`) all
    read 0, which is a placeholder and never reaches this function.
    """
    if v is None:
        return None
    try:
        return round(int(v) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def decode_temp_byte(b):
    """Byte-schema temp → °C. Per H-series PDF §5.4.2: byte / 2.

    Byte schema ONLY (a reply carrying `voltage`, not `volt`). The captured
    wall reads 80–110 raw across 1374 cards → 40–55 °C, matching the NovaLCT
    MonitorSite GUI. The centi-schema firmware uses decode_temp_centi().
    """
    if b is None:
        return None
    try:
        return int(b) / 2.0
    except (TypeError, ValueError):
        return None


def decode_voltage_byte(b):
    """Byte-schema voltage → volts: lower 7 bits, units of 0.1 V.

    NovaStar's H Series Video Wall Splicers Control Protocol says it outright
    in §4.3.4 and §5.4.2, identically in V1.0.18 and V1.0.20: "The lower 7 bits
    represent the voltage value, in units of 0.1V. For instance, a value of 172
    indicates a voltage of 4.4V."

    This used to be `raw * 0.03`, changed in this project on the reasoning that
    the masked form put every card under a 4.7 V alarm on a healthy wall. That
    reasoning was backwards — the threshold was wrong, not the formula.
    Receiving cards run around 4.2 V. The unmasked form also disagreed with the
    centi schema by roughly 0.9 V on the same hardware; the masked form agrees
    with it.

    Byte schema ONLY — the centi firmware uses decode_volt_centi().
    """
    if not isinstance(b, int):
        return None
    return round((b & 0x7F) * 0.1, 2)

def _maybe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _status_ok(v):
    """Status field → True (0 = OK) / False (non-zero) / None (missing).

    Shared by tempStatus / voltStatus. `0 = OK` is inferred from the captures,
    not from the PDF, and the non-zero values are only partly observed
    (temp/volt status: 2).

    Coerced through `_maybe_int` first, exactly as its twin
    `snmp_client._status_ok` does. The captured firmware sends these as JSON
    numbers, but every other numeric field in this module is coerced for a
    reason: this device family has been observed quoting numbers in JSON, and
    an uncoerced `"0" == 0` is False. That is a healthy card reported as a
    temperature or voltage FAULT — the string is the only thing wrong with it —
    and the fix for a false fault mid-show is somebody walking to the wall.

    A value that is not a number at all is unknown rather than a fault:
    `_maybe_int` gives None and so does this. Note the deliberate asymmetry
    with `_power_status` below, which never returns False at all; that is a
    different question (what non-zero MEANS) and is settled differently.
    """
    v = _maybe_int(v)
    if v is None:
        return None
    return v == 0


def _power_status(v, reporting):
    """`powerNStatus` → True (healthy) / None (unknown). Never False.

    POLARITY: **0 = Fault, 1 = Normal.** Documented by NovaStar twice — in the
    H Series Control Protocol §4.3.4 and again in the corrected R0155 field
    document of 2026-09-04, which also renames the fields: these are power
    supply **1 and 2**, not primary and backup.

    This project had it backwards for a long time, and the mistake is worth
    remembering because the evidence looked like it pointed the other way.
    Fifteen cards reported `power0Status: 1` AND `power1Status: 1` while
    reporting 41-42 °C and 4.0-4.1 V. That was read as "both supplies failed on
    a card that is plainly running, so 1 can't mean failed" — a sound
    observation attached to a backwards conclusion. Under the documented
    polarity those cards were reporting two healthy supplies all along.

    The mapping is deliberately asymmetric, because the OTHER direction is
    still unexplained: 21 of 36 cards on the 286-panel wall report 0 on both
    fields while lit and answering. Read literally that is a double supply
    failure on hardware that is working. Almost certainly it means a supply
    that is not fitted or not monitored — these panels are single-supply — but
    NovaStar has not confirmed it, so:

        1  → True   healthy, vendor-documented Normal
        0  → None   documented Fault, but seen across most of a healthy wall

    Nothing alerts on either value. Returning False for 0 would put a critical
    supply fault on the majority of a working wall.

    Both readings are only ever evaluated for a REPORTING card. On a
    non-reporting one every field is a placeholder, and a placeholder is not a
    measurement in either direction.
    """
    if v is None or not reporting:
        return None
    return True if v == 1 else None
