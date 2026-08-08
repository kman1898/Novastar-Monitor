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

Performance notes (the polling workload is ~1374 per-card R0155 reads/cycle):

- One persistent UDP socket is reused for all calls instead of a new socket
  per request (see `_bulk_socket`). The heartbeat has its own socket and lock
  so `W0120` never queues behind a batch of card reads.
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
BULK_TIMEOUT = 0.3           # R0155 per-card reads, W0120 heartbeat
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

    Holds one persistent socket for command/response traffic and a second one
    for the heartbeat, each behind its own lock. A socket that errors out is
    closed and lazily recreated on the next call.

    The protocol is stateless request/response, so "persistent" here only
    means the file descriptor is reused — there is no connection to keep.
    """

    def __init__(self, ip, port=JSON_UDP_PORT, timeout=TOPOLOGY_TIMEOUT,
                 bulk_timeout=BULK_TIMEOUT, batch_size=DEFAULT_BATCH_SIZE):
        self.ip = ip
        self.port = port
        self.timeout = timeout              # topology / default call class
        self.bulk_timeout = bulk_timeout    # per-card reads, heartbeat
        self.batch_size = batch_size

        self._lock = threading.Lock()       # guards _sock
        self._sock = None
        self._hb_lock = threading.Lock()    # guards _hb_sock (heartbeat only)
        self._hb_sock = None

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
        """Release both sockets. Safe to call more than once."""
        with self._lock:
            self._drop_socket()
        with self._hb_lock:
            if self._hb_sock is not None:
                self._close(self._hb_sock)
                self._hb_sock = None

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

    def heartbeat(self):
        """W0120 — device_heartbeat. Splicer module sends this every 3s.

        Ack-style keepalive: tells the device a controller is still connected.

        This is NOT fire-and-forget — it sends the command and then blocks on
        recvfrom for up to `bulk_timeout` (0.3 s) waiting for the ack,
        returning the parsed response or None if the device stayed silent.
        It runs on its own socket behind its own lock, so a heartbeat never
        queues behind a batch of per-card reads (and vice versa); the caller's
        3 s cadence is therefore never distorted by polling traffic.
        """
        cmd_obj = {"cmd": "W0120", "param0": 0}
        payload_wait = self.bulk_timeout
        try:
            with self._hb_lock:
                if self._hb_sock is None:
                    self._hb_sock = self._new_socket()
                sock = self._hb_sock
                try:
                    matched, ordered = self._exchange(
                        sock, [cmd_obj], payload_wait, _echo_key("W0120"), 1)
                except (OSError, socket.error):
                    self._close(sock)
                    self._hb_sock = None
                    return None
        except Exception:
            return None
        return _pick_single(matched, ordered, "W0120")


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
        } for s in slots],
    }


def parse_receiving_card(r0155):
    """Extract per-card state from an R0155 response.

    Field names are taken from a live capture off the H-series at
    192.168.0.10, not from the PDF (which documents the request but not the
    reply body)::

        {"deviceId":0,"slotId":20,"portId":0,"recvCardId":0,
         "power0Status":0,"power1Status":0,"brightness":127,
         "temp":88,"voltage":170,"cmd":"R0155","ack":"Ok"}

    `temp` and `voltage` are raw bytes — see decode_temp_byte() and
    decode_voltage_byte(). Power status bytes are 0 = OK, non-zero = fault
    (power0 = primary supply, power1 = backup supply). A card the controller
    can't reach produces no reply at all, so `None` in / `None` out is the
    offline signal; a reply with a non-"Ok" ack is treated as offline too.

    Returns keys in the shape device_manager already stores per card
    (temp_c / temperature_c / voltage_v / brightness / primary_power_ok /
    backup_power_ok / online) so it can be merged straight into a card entry.
    """
    if not isinstance(r0155, dict):
        return None
    ack = r0155.get("ack")
    temp_raw = _maybe_int(r0155.get("temp"))
    volt_raw = _maybe_int(r0155.get("voltage"))
    temp_c = decode_temp_byte(temp_raw)
    return {
        "online": ack is None or str(ack).lower() == "ok",
        "ack": ack,
        "slot": r0155.get("slotId"),
        "port": r0155.get("portId"),
        "card_id": r0155.get("recvCardId"),
        # Both temperature keys: the aggregator reads temperature_c, the
        # device-tree renderer reads temp_c.
        "temp_c": temp_c,
        "temperature_c": temp_c,
        "temperature_raw": temp_raw,
        "voltage_v": decode_voltage_byte(volt_raw),
        "voltage_raw": volt_raw,
        "brightness": r0155.get("brightness"),
        "primary_power_ok": _status_ok(r0155.get("power0Status")),
        "backup_power_ok": _status_ok(r0155.get("power1Status")),
        "raw": r0155,
    }


def decode_temp_byte(b):
    """Raw temp byte → °C. Per H-series PDF §5.4.2: byte / 2.

    Captured wall reads 80–110 raw across 1374 cards → 40–55 °C, matching
    the NovaLCT MonitorSite GUI.
    """
    if b is None:
        return None
    try:
        return int(b) / 2.0
    except (TypeError, ValueError):
        return None


def decode_voltage_byte(b):
    """Raw voltage byte → volts, using the vendor formula `raw * 0.03`.

    Same encoding as the binary live-monitoring register 0x0000000A byte[3]
    (`parse_voltage()` in novastar_protocol.py, calibrated against VX1000 and
    documented in docs/VX1000_Protocol_Analysis.md).

    NOT `(raw & 0x7F) / 10`. That variant was a guess and it is wrong: the
    1374-card capture spans raw 165–173, which is 4.95–5.19 V under the vendor
    formula (a healthy 5 V rail) but 3.7–4.5 V under the masked one — below
    the app's own 4.7 V low-voltage alarm threshold for every card on a wall
    that was running normally. The high bit is never clear in the captured
    data, so masking it off just silently subtracts 12.8 V-units.
    """
    if b is None:
        return None
    try:
        return round(int(b) * 0.03, 2)
    except (TypeError, ValueError):
        return None


def _maybe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _status_ok(v):
    """Power status byte: 0 = OK, non-zero = fault, missing = unknown."""
    if v is None:
        return None
    return v == 0
