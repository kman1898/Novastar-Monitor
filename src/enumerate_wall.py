#!/usr/bin/env python3
"""Enumerate every receiving card on an H-series wall and write the snapshot.

This is the generator for `src/wall_live_snapshot.json` — the file the wall
view (`/api/wall_live`) and `device_manager._load_known_cards_from_snapshot()`
read to know which cards exist. It used to be produced by ad-hoc inline shell
during a debugging session; the file is per-install runtime state and is
gitignored, so this script is the only reproducible way to rebuild it after a
physical change to the wall.

Protocol sources
----------------
* `docs/H_SERIES_FINDINGS.md` §6.5 — the verified addressing model, decoded
  from `H series Bit errors detection.pcapng` + `H series More.pcapng` on a
  single-sender-card H2 rig whose known 245-panel count is reproduced exactly.
  §1 of the same document contains an *older, superseded* model; §6.5 wins.
* "H Series Video Wall Splicers Control Protocol V1.0.19" (NovaStar PDF) —
  the JSON-over-UDP commands used for topology and per-card readings.

Addressing model (§6.5)
-----------------------
* A chassis holds up to 3 **sender cards**, each of which is its own TCP
  service: 5201 / 5202 / 5203. TCP 5200 is the broadcast / main controller.
  The device advertises the list in its answer to a `rqProMI:` datagram on
  UDP 3800.
* Each sender card drives **16 chains** (2 OPT fibres x 8 ports).
* In the binary per-card read frame, `byte[7]` is the chain index (0-15) and
  `byte[8]` is the card position within that chain. See `build_read_card()`
  in `novastar_protocol.py`.
* Chains are **contiguous** runs of cards. NovaLCT finds the end of a chain by
  probing one card *past* the last real one, and probes an empty chain once.
  Those extra probes are boundary detection, not panels — this is why the
  `More.pcapng` capture looks like 277 cards on a rig that has 245.

Why the binary transport is the default
---------------------------------------
The JSON `R0155` path is a limited wrapper: it **silently returns nothing**
for cards it cannot reach, with no error and no distinction between "this
address is past the end of the chain" and "this card exists but I can't talk
to it". It undercounted the 1548-panel COSMIC MEADOW wall as 1374 (the card
count in the pre-existing snapshot). The binary path found the true count.

So enumeration (which cards exist) is done over the binary transport by
default, and the JSON transport is used only to decorate the result with
topology metadata and per-card readings. `--transport json` reproduces the old
R0155 sweep for comparison, and prints an explicit warning plus a count of
addresses that stayed silent — under-reporting is always surfaced, never
swallowed.

Safety
------
This tool is **read-only with respect to the device**. It sends only
`build_read` / `build_read_card` frames (binary READ requests) and `R0xxx`
JSON commands. It never sends a `W0xxx` command — not even the `W0120`
heartbeat — so it cannot alter device state. It also refuses to send a single
packet without an explicit `--yes-contact-hardware` flag, because the wall it
talks to is production hardware that is often live during a show.
"""

import argparse
import json
import os
import re
import socket
import struct
import sys
import tempfile
import time
from datetime import datetime, timezone

from novastar_protocol import (
    H_MAX_CARDS_PER_PORT,
    H_MAX_PORTS,
    H_REG_BIT_ERRORS,
    REG_LIVE_MONITOR,
    build_read_card,
    decode_length,
    parse_bit_errors,
    parse_live_monitoring,
    parse_response,
)
from h_series_json import (
    JSON_UDP_PORT,
    HSeriesJSONClient,
    parse_receiving_card,
)

# ── Wire constants ─────────────────────────────────────────────────────────

# §6.5: `rqProMI:` on UDP 3800 is answered with a list of the chassis' sender
# card services, e.g.
#   rpProMI:App,0161 H_SUB_CARD@^^@5201 H_SUB_CARD@^^@5202 H_SUB_CARD@^^@5203
DISCOVERY_UDP_PORT = 3800
DISCOVERY_REQUEST = b"rqProMI:"

# TCP 5200 is the broadcast/main controller; sender card N answers on 5200+N.
BINARY_BASE_PORT = 5200

# 2 OPT fibres per sender card, 8 ports each -> chains 0-7 are OPT 1,
# chains 8-15 are OPT 2. Confirmed against the field layout of the existing
# snapshot (opt = port // 8 + 1, port_on_opt = port % 8 + 1).
PORTS_PER_OPT = 8

# The user-facing slot label in the snapshot is one higher than the protocol
# slotId (20/22/24 -> 21/23/25). Derived from the pre-existing snapshot; see
# ASSUMPTIONS at the bottom of this module.
USER_SLOT_OFFSET = 1

DEFAULT_MAX_CARDS_PER_CHAIN = H_MAX_CARDS_PER_PORT   # 91
DEFAULT_CHAIN_COUNT = H_MAX_PORTS                    # 16

# Per-probe socket timeout. A probe that lands on a real card answers in well
# under a millisecond on a wired LAN; the timeout only ever costs us anything
# on the boundary probe at the end of each chain, of which there are at most
# chains x sender_cards (48 on a full chassis).
DEFAULT_PROBE_TIMEOUT = 0.5
DEFAULT_CONNECT_TIMEOUT = 5.0

# Re-probes before declaring end-of-chain. A single dropped response would
# otherwise truncate a chain and silently under-report the wall, which is the
# exact failure mode this tool exists to avoid. Cheap: only paid at boundaries.
DEFAULT_BOUNDARY_RETRIES = 2

# R0155 leaves holes inside a chain (it answers nothing for a card it cannot
# reach), so the JSON sweep cannot stop at the first silence the way the binary
# walk can. Keep going until this many consecutive addresses stay silent.
DEFAULT_JSON_GAP_TOLERANCE = 4

DEFAULT_SNAPSHOT_NAME = "wall_live_snapshot.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_CONFIRMED = 2


# ── Progress / reporting ───────────────────────────────────────────────────


class Reporter:
    """Progress output. Levels: 0 = quiet, 1 = normal, 2+ = verbose.

    Progress goes to stderr so stdout carries only the final summary; a full
    sweep of a 3-card chassis takes minutes and the operator needs to see that
    it is still alive.
    """

    def __init__(self, level=1, stream=None):
        self.level = level
        self.stream = stream if stream is not None else sys.stderr

    def _emit(self, text):
        print(text, file=self.stream)
        try:
            self.stream.flush()
        except Exception:
            pass

    def info(self, text):
        if self.level >= 1:
            self._emit(text)

    def detail(self, text):
        if self.level >= 2:
            self._emit(text)

    def warn(self, text):
        # Warnings survive --quiet: an under-report warning must never be
        # suppressed by a verbosity flag.
        self._emit("WARNING: " + text)


# ── Sender-card discovery (UDP 3800 rqProMI) ───────────────────────────────

# Matches the `H_SUB_CARD@^^@5201` tokens in an `rpProMI:` answer. The `@^^@`
# separator is taken verbatim from the §6.5 capture; the fallback pattern below
# catches a firmware that labels the service differently but still lists ports.
_SUBCARD_RE = re.compile(r"H_SUB_CARD\s*@\^\^@\s*(\d{2,5})", re.IGNORECASE)
_BARE_PORT_RE = re.compile(r"\b(52[0-9]{2})\b")


def parse_rqpromi_response(text):
    """Extract sender-card TCP ports from an `rpProMI:` reply.

    Returns a sorted list of distinct TCP ports, excluding the broadcast/main
    port 5200 (it is not a sender card). Returns `[]` for anything that does
    not look like a discovery answer — defensive because this is
    unauthenticated
    UDP and anything on the LAN can answer.
    """
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str):
        return []
    ports = [int(m) for m in _SUBCARD_RE.findall(text)]
    if not ports:
        # Only fall back to the loose pattern if the payload at least claims to
        # be a discovery reply, so random LAN chatter can't seed a port list.
        if "proMI" not in text and "ProMI" not in text:
            return []
        ports = [int(m) for m in _BARE_PORT_RE.findall(text)]
    valid = {p for p in ports if 1 <= p <= 65535 and p != BINARY_BASE_PORT}
    return sorted(valid)


def discover_sender_card_ports(ip, timeout=1.0, port=DISCOVERY_UDP_PORT):
    """Ask the chassis which sender-card TCP services it exposes.

    Unicast (not broadcast) to the operator-supplied IP: we already know which
    device we mean, and a broadcast would poke every controller on the LAN.
    Returns a sorted list of TCP ports, or `[]` if nothing usable answered.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(DISCOVERY_REQUEST, (ip, port))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            sock.settimeout(remaining)
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                return []
            ports = parse_rqpromi_response(data)
            if ports:
                return ports
    except OSError:
        return []
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ── Binary per-card probing ────────────────────────────────────────────────


def _presence_bit_errors(payload):
    """Presence rule for register 0x4A010002 (§6.5).

    byte[0] == 0x05 means "card present / responding". Any other status is
    treated as absent. Returns `(present, readings)`.
    """
    parsed = parse_bit_errors(payload)
    if not parsed:
        return False, {}
    return bool(parsed.get("present")), {
        "bit_errors": parsed.get("errors"),
        "bit_errors_saturated": parsed.get("saturated"),
    }


def _presence_live_monitor(payload):
    """Presence rule for register 0x0000000A (VX1000-style live monitoring).

    This register has no documented presence byte, so presence here is simply
    "the device returned a well-formed payload for this (chain, card) address".
    The upside is that the same probe yields temperature / voltage / link
    status, so a binary-only run still produces readings.
    """
    parsed = parse_live_monitoring(payload)
    if not parsed:
        return False, {}
    temp_c = parsed.get("temperature_c")
    return True, {
        "temp_c": temp_c,
        "temperature_c": temp_c,
        "voltage_v": parsed.get("voltage_v"),
        "link_status": parsed.get("link_status"),
    }


# name -> (register, wire length, presence/readings decoder)
PROBE_REGISTERS = {
    "biterr": (H_REG_BIT_ERRORS[0], H_REG_BIT_ERRORS[1], _presence_bit_errors),
    "live": (REG_LIVE_MONITOR[0], REG_LIVE_MONITOR[1], _presence_live_monitor),
}
DEFAULT_PROBE_REGISTER = "biterr"


class ProbeResult:
    """Outcome of one per-card probe."""

    __slots__ = ("present", "readings")

    def __init__(self, present, readings=None):
        self.present = present
        self.readings = readings or {}


class BinarySenderCardProbe:
    """Read-only per-card probe against one sender card's TCP service.

    One instance owns one TCP connection to `ip:tcp_port` (5201/5202/5203 per
    §6.5) and walks `(chain, card_index)` addresses on it.

    Only READ frames are ever sent.
    """

    def __init__(self, ip, tcp_port, timeout=DEFAULT_PROBE_TIMEOUT,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 register=DEFAULT_PROBE_REGISTER):
        self.ip = ip
        self.tcp_port = tcp_port
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        try:
            self.register, self.reg_length, self._decode = \
                PROBE_REGISTERS[register]
        except KeyError:
            raise ValueError(f"unknown probe register: {register!r}")
        self.register_name = register
        self.probe_count = 0
        self._sock = None
        self._seq = 0

    # ── connection ─────────────────────────────────────────────────────

    def open(self):
        self.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout)
        sock.connect((self.ip, self.tcp_port))
        sock.settimeout(self.timeout)
        self._sock = sock
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _resync(self):
        """Reconnect after a probe that went unanswered.

        The binary protocol has no usable response correlation on our side
        (`parse_response` only exposes the register), so a late reply to a
        timed-out probe would be read as the answer to the *next* probe — and
        since every timeout happens at a chain boundary, that stray reply would
        make the next chain look like it starts with a phantom card. Dropping
        and reopening the socket is the only way to be certain the stream is
        back in step. Boundaries are rare (<= 48 per chassis), so this is
        cheap.
        """
        try:
            self.open()
        except OSError:
            self._sock = None

    # ── framing ────────────────────────────────────────────────────────

    def _recv_frame(self):
        """Read exactly one response frame, or return None on timeout.

        Frame layout: 18 header bytes, then the payload whose size the length
        field at bytes[16:18] encodes, then a 2-byte checksum. Reading a
        length-derived number of bytes (rather than a single `recv`) keeps the
        stream aligned when the device coalesces frames into one TCP segment.
        """
        head = self._recv_exactly(18)
        if head is None:
            return None
        payload_len = decode_length(struct.unpack(">H", head[16:18])[0])
        # Defensive: a corrupt length field must not make us block on a read
        # for a payload that will never arrive.
        if payload_len < 0 or payload_len > 65535:
            return None
        rest = self._recv_exactly(payload_len + 2)
        if rest is None:
            return None
        return head + rest

    def _recv_exactly(self, count):
        buf = bytearray()
        while len(buf) < count:
            try:
                chunk = self._sock.recv(count - len(buf))
            except socket.timeout:
                return None
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    # ── probing ────────────────────────────────────────────────────────

    def probe(self, chain, card_index):
        """Probe one card address. Returns a ProbeResult (never raises).

        `ProbeResult.present` is False both for "the device said nothing" and
        for "the device answered but the card isn't there"; the caller treats
        both as a chain boundary and re-probes before believing it.
        """
        self.probe_count += 1
        if self._sock is None:
            self._resync()
            if self._sock is None:
                return ProbeResult(False)

        self._seq = (self._seq + 1) & 0xFFFF
        frame = build_read_card(self._seq, self.register, self.reg_length,
                                chain, card_index)
        try:
            self._sock.sendall(frame)
        except OSError:
            self._resync()
            return ProbeResult(False)

        data = self._recv_frame()
        if data is None:
            self._resync()
            return ProbeResult(False)

        parsed = parse_response(data)
        if not parsed:
            self._resync()
            return ProbeResult(False)
        reg, payload = parsed
        if reg != self.register:
            # Somebody else's answer (or a stale one) — the stream is out of
            # step. Never treat a mismatched frame as evidence about this card.
            self._resync()
            return ProbeResult(False)

        present, readings = self._decode(payload)
        return ProbeResult(present, readings)


# ── Chain walking ──────────────────────────────────────────────────────────


def enumerate_chain(probe, chain, max_cards=DEFAULT_MAX_CARDS_PER_CHAIN,
                    retries=DEFAULT_BOUNDARY_RETRIES, reporter=None):
    """Walk one chain and return the list of cards actually on it.

    Chains are contiguous (§6.5), so the walk runs from card position 0 upward
    and stops at the first address that does not answer. That non-answering
    address is the boundary probe NovaLCT also sends — it is **not** counted.
    An empty chain therefore costs exactly one probe (plus retries) and yields
    zero cards, which is the documented NovaLCT behaviour.

    `retries` re-probes a missing address before accepting it as the boundary.
    A single dropped response would otherwise truncate the chain and silently
    under-report the wall.

    Returns a list of `(card_index, readings_dict)` in ascending order.
    """
    cards = []
    for card_index in range(max_cards):
        result = None
        for attempt in range(retries + 1):
            result = probe.probe(chain, card_index)
            if result.present:
                if attempt and reporter:
                    reporter.detail(
                        f"    chain {chain} card {card_index}: answered on "
                        f"retry {attempt} (transient drop)")
                break
        if not result.present:
            break
        cards.append((card_index, result.readings))
    else:
        # Ran to max_cards without finding a boundary: the chain may be longer
        # than the configured cap, so say so rather than quietly truncating.
        if reporter:
            reporter.warn(
                f"chain {chain} still had cards at the --max-cards limit of "
                f"{max_cards}; the chain may be longer than reported")
    return cards


def enumerate_sender_card_binary(probe, card_number, slot, chains,
                                 max_cards=DEFAULT_MAX_CARDS_PER_CHAIN,
                                 retries=DEFAULT_BOUNDARY_RETRIES,
                                 reporter=None):
    """Enumerate every chain on one sender card.

    Returns a list of card dicts.
    """
    reporter = reporter or Reporter(0)
    cards = []
    for chain in chains:
        found = enumerate_chain(probe, chain, max_cards=max_cards,
                                retries=retries, reporter=reporter)
        for card_index, readings in found:
            cards.append(make_card_entry(card_number, slot, chain, card_index,
                                         readings))
        reporter.info(
            f"  sender card {card_number} (tcp {probe.tcp_port}) "
            f"chain {chain:2d}: {len(found):3d} cards")
    return cards


# ── Card entry construction ────────────────────────────────────────────────


def make_card_entry(card_number, slot, port, card_id, readings=None):
    """Build one `cards[]` entry in the snapshot's established shape.

    `port` is the chain index (0-based, 0-15). The OPT split is derived, not
    read from the device: chains 0-7 are OPT 1 and chains 8-15 are OPT 2, which
    is how the existing snapshot's `opt` / `port_on_opt` fields line up.
    """
    entry = {
        "card_number": card_number,
        "slot": slot,
        "user_slot": None if slot is None else slot + USER_SLOT_OFFSET,
        "opt": port // PORTS_PER_OPT + 1,
        "port": port,
        "port_on_opt": port % PORTS_PER_OPT + 1,
        "card_id": card_id,
    }
    if readings:
        entry.update(readings)
    return entry


def card_address(entry):
    """(slot, port, card_id) — the R0155 address for a card entry."""
    return (entry.get("slot"), entry.get("port"), entry.get("card_id"))


# ── JSON topology + readings ───────────────────────────────────────────────


def fetch_topology(client, reporter=None):
    """Read screen name / size / mosaic / sender-card slots over JSON UDP.

    Uses R0400 (screen list) then R0405 (per-screen output info), the same
    calls `device_manager` makes. Everything here is best-effort decoration:
    a chassis that doesn't answer the JSON port still produces a valid
    snapshot, just without these fields.
    """
    reporter = reporter or Reporter(0)
    topo = {
        "screen_name": None,
        "screen_size": None,
        "mosaic": None,
        "slots": [],
    }
    screen_list = client.get_screen_list()
    screens = []
    if isinstance(screen_list, dict):
        screens = screen_list.get("screens") or []
    if not screens:
        reporter.warn("R0400 returned no screens; snapshot will have no "
                      "screen name/size (enumeration is unaffected)")
        return topo

    # The wall is one screen in every capture we have. If a chassis ever drives
    # several, take the first and say so rather than silently merging them.
    if len(screens) > 1:
        reporter.warn(
            f"device reports {len(screens)} screens; using the first "
            f"({screens[0].get('name')!r}) for snapshot metadata")
    first = screens[0]
    topo["screen_name"] = first.get("name")

    screen_id = first.get("screenId")
    if screen_id is None:
        return topo
    info = client.get_screen_output_info(screen_id)
    if not isinstance(info, dict):
        reporter.warn(f"R0405 returned nothing for screen {screen_id}")
        return topo

    size = info.get("size") or {}
    if isinstance(size, dict) and ("width" in size or "height" in size):
        topo["screen_size"] = {
            "width": size.get("width"),
            "height": size.get("height"),
        }
    mosaic = info.get("mosaic")
    if isinstance(mosaic, dict):
        topo["mosaic"] = {"row": mosaic.get("row"),
                          "column": mosaic.get("column")}

    slots = []
    for iface in info.get("screenInterfaces") or []:
        if not isinstance(iface, dict):
            continue
        slot = iface.get("slotId")
        if isinstance(slot, int) and slot not in slots:
            slots.append(slot)
    topo["slots"] = sorted(slots)
    return topo


def resolve_slot_map(card_numbers, topo_slots, override=None, reporter=None):
    """Map sender-card number -> protocol slotId.

    Order of preference: an explicit `--slot-map`, then the slot ids R0405
    reported (ascending, paired with ascending card numbers). If neither is
    available the slot is `None` — the snapshot still enumerates correctly, but
    `device_manager` cannot issue R0155 for those cards, so we warn.
    """
    reporter = reporter or Reporter(0)
    if override:
        return dict(override)
    slot_map = {}
    for i, number in enumerate(sorted(card_numbers)):
        if i < len(topo_slots):
            slot_map[number] = topo_slots[i]
    missing = [n for n in card_numbers if slot_map.get(n) is None]
    if missing:
        reporter.warn(
            "no slotId known for sender card(s) "
            + ", ".join(str(n) for n in missing)
            + " — pass --slot-map (e.g. 1=20,2=22,3=24). Cards will be "
              "enumerated but carry slot=null and cannot be polled by R0155.")
    return slot_map


def attach_readings(client, cards, reporter=None, batch_size=None):
    """Fill per-card readings from R0155, in place. Returns a stats dict.

    Cards the device does not answer for are **kept** in the list with
    `online: False` — the whole point of this tool is that a card which exists
    but cannot be read is a monitoring finding, not a card to delete. The
    returned stats feed the summary so the operator sees the coverage.
    """
    reporter = reporter or Reporter(0)
    addressable = [c for c in cards if c.get("slot") is not None]
    stats = {"requested": len(addressable), "answered": 0,
             "silent": 0, "skipped": len(cards) - len(addressable)}
    if not addressable:
        return stats

    responses = client.get_receiving_cards_batch(
        [card_address(c) for c in addressable], batch_size=batch_size)
    per_chain_silent = {}
    for card, raw in zip(addressable, responses):
        parsed = parse_receiving_card(raw) if raw is not None else None
        if not parsed:
            card["online"] = False
            stats["silent"] += 1
            key = (card["card_number"], card["port"])
            per_chain_silent[key] = per_chain_silent.get(key, 0) + 1
            continue
        stats["answered"] += 1
        card["online"] = bool(parsed.get("online"))
        for key in ("temp_c", "voltage_v", "brightness",
                    "primary_power_ok", "backup_power_ok"):
            if parsed.get(key) is not None:
                card[key] = parsed[key]

    # A chain where *every* card is silent is the signature of a bad
    # assumption rather than 40 dead panels — most likely the binary card
    # position is not the same number R0155 calls recvCardId on this firmware.
    # Say so loudly; do not let it pass as "all these panels are offline".
    totals = {}
    for card in addressable:
        key = (card["card_number"], card["port"])
        totals[key] = totals.get(key, 0) + 1
    for key, silent in sorted(per_chain_silent.items()):
        if silent == totals.get(key):
            reporter.warn(
                f"sender card {key[0]} chain {key[1]}: 0 of {silent} "
                f"enumerated cards answered R0155. Either the whole chain is "
                f"unreachable over JSON, or the binary card position does not "
                f"equal the R0155 recvCardId on this firmware. Cards were "
                f"kept with online=false.")
    return stats


# ── JSON-transport enumeration (comparison / fallback path) ────────────────


def enumerate_chain_json(client, slot, port, max_cards, gap_tolerance,
                         reporter=None, batch_size=8):
    """R0155 sweep of one chain. Returns `(cards, holes)`.

    Unlike the binary walk this cannot stop at the first silence: R0155 leaves
    holes *inside* a chain for cards it cannot reach (the pre-existing snapshot
    has exactly that — chain 0 of sender card 1 spans card ids 0-40 but only
    contains 39 of them). So the sweep continues until `gap_tolerance`
    consecutive addresses are silent, and every silent address that sits before
    the last answering card is returned as a `hole` so the caller can report
    the
    under-count instead of hiding it.
    """
    reporter = reporter or Reporter(0)
    cards = []
    pending_silent = []
    holes = []
    consecutive = 0
    start = 0
    while start < max_cards and consecutive < gap_tolerance:
        chunk = list(range(start, min(start + batch_size, max_cards)))
        raws = client.get_receiving_cards_batch(
            [(slot, port, cid) for cid in chunk], batch_size=batch_size)
        for cid, raw in zip(chunk, raws):
            parsed = parse_receiving_card(raw) if raw is not None else None
            if parsed:
                # Everything silent before this card was a hole, not the end.
                holes.extend(pending_silent)
                pending_silent = []
                consecutive = 0
                cards.append((cid, {
                    "online": bool(parsed.get("online")),
                    "temp_c": parsed.get("temp_c"),
                    "voltage_v": parsed.get("voltage_v"),
                    "brightness": parsed.get("brightness"),
                    "primary_power_ok": parsed.get("primary_power_ok"),
                    "backup_power_ok": parsed.get("backup_power_ok"),
                }))
            else:
                pending_silent.append(cid)
                consecutive += 1
                if consecutive >= gap_tolerance:
                    break
        start += len(chunk)
    return cards, holes


# ── Snapshot assembly + atomic write ───────────────────────────────────────


def utc_timestamp():
    """ISO-8601 UTC timestamp, e.g. `2026-08-08T14:03:11+00:00`.

    The dashboard renders a staleness indicator from this field, so it must
    always be present and always be parseable by `datetime.fromisoformat`.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_snapshot(device_ip, cards, slot_map, topology=None,
                   transport=DEFAULT_PROBE_REGISTER, meta=None):
    """Assemble the snapshot document in the shape the app already reads."""
    topology = topology or {}
    sender_cards = [
        {
            "card_number": number,
            "slot": slot_map.get(number),
            "user_slot": (None if slot_map.get(number) is None
                          else slot_map[number] + USER_SLOT_OFFSET),
            # Every card the discovery step reports is a live output card.
            # Backup-card detection is not implemented (see ASSUMPTIONS).
            "role": "primary",
        }
        for number in sorted(slot_map or {})
    ]
    if not sender_cards:
        for number in sorted({c["card_number"] for c in cards}):
            sender_cards.append({"card_number": number, "slot": None,
                                 "user_slot": None, "role": "primary"})

    snapshot = {
        "device_ip": device_ip,
        "captured_at": utc_timestamp(),
        "screen_name": topology.get("screen_name"),
        "screen_size": topology.get("screen_size"),
        "mosaic": topology.get("mosaic"),
        "sender_cards": sender_cards,
        "cards": cards,
    }
    snapshot["enumeration"] = dict(meta or {})
    snapshot["enumeration"].setdefault("transport", transport)
    snapshot["enumeration"].setdefault("total_cards", len(cards))
    return snapshot


def write_snapshot(path, snapshot):
    """Serialise and replace `path` atomically.

    The app loads this file on every `/api/wall_live` request, so a crash or a
    full disk mid-write must never leave a half-written document behind. The
    JSON is fully serialised into a temp file in the *same directory* (so
    `os.replace` is a same-filesystem rename and therefore atomic), fsync'd,
    and only then moved into place.
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix="." + os.path.basename(path) + ".",
        suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


# ── Summary ────────────────────────────────────────────────────────────────


def summarise(snapshot, reading_stats=None, holes=None):
    """Render the operator summary: per sender card, per chain, total."""
    cards = snapshot.get("cards", [])
    meta = snapshot.get("enumeration", {}) or {}
    # Seed every scanned chain at 0 so an empty chain shows up as an explicit
    # "0" row. A chain that silently vanishes from the summary is the same
    # class of mistake as an under-count: the operator can't tell "no panels
    # here" from "never looked".
    scanned = list(meta.get("chains_scanned") or [])
    by_card = {}
    for card in cards:
        by_card.setdefault(card["card_number"],
                           {chain: 0 for chain in scanned})
        by_card[card["card_number"]].setdefault(card["port"], 0)
        by_card[card["card_number"]][card["port"]] += 1

    lines = []
    lines.append("")
    lines.append("Enumeration summary")
    lines.append("===================")
    lines.append(f"device            : {snapshot.get('device_ip')}")
    lines.append(f"screen            : {snapshot.get('screen_name')}")
    lines.append(f"captured_at       : {snapshot.get('captured_at')}")
    lines.append(f"transport         : "
                 f"{snapshot.get('enumeration', {}).get('transport')}")
    lines.append("")
    for number in sorted(by_card):
        chains = by_card[number]
        subtotal = sum(chains.values())
        slot = next((s.get("slot") for s in snapshot.get("sender_cards", [])
                     if s.get("card_number") == number), None)
        lines.append(f"sender card {number} (slot {slot}): {subtotal} cards")
        for port in sorted(chains):
            opt = port // PORTS_PER_OPT + 1
            on_opt = port % PORTS_PER_OPT + 1
            lines.append(f"    chain {port:2d}  (OPT {opt} port {on_opt}): "
                         f"{chains[port]:4d}")
        lines.append("")
    lines.append(f"TOTAL CARDS       : {len(cards)}")

    if reading_stats:
        lines.append(f"R0155 readings    : {reading_stats.get('answered', 0)} "
                     f"answered / {reading_stats.get('silent', 0)} silent"
                     + (f" / {reading_stats['skipped']} skipped (no slot)"
                        if reading_stats.get("skipped") else ""))
    if holes:
        lines.append(f"R0155 holes       : {len(holes)} address(es) inside a "
                     f"discovered chain returned nothing. R0155 silently "
                     f"drops "
                     f"cards it cannot reach, so this count is a LOWER BOUND "
                     f"on the real panel count — re-run with "
                     f"--transport binary for an authoritative sweep.")
    lines.append("")
    lines.append("Compare TOTAL CARDS against the panel count you know for "
                 "this wall before trusting the snapshot.")
    return "\n".join(lines)


# ── Orchestration ──────────────────────────────────────────────────────────


def _resolve_sender_cards(args, reporter):
    """Decide which sender cards (card_number -> tcp port) to scan."""
    if args.sender_cards:
        numbers = args.sender_cards
        reporter.info(f"sender cards (from --sender-cards): "
                      f"{', '.join(str(n) for n in numbers)}")
        return {n: BINARY_BASE_PORT + n for n in numbers}

    reporter.info(f"discovering sender cards via {DISCOVERY_REQUEST.decode()} "
                  f"on udp {args.discovery_port}...")
    ports = discover_sender_card_ports(args.ip, timeout=args.discovery_timeout,
                                       port=args.discovery_port)
    if not ports:
        reporter.warn("no answer to the rqProMI discovery probe. Pass "
                      "--sender-cards 1,2,3 to scan explicitly.")
        return {}
    mapping = {}
    for tcp_port in ports:
        number = tcp_port - BINARY_BASE_PORT
        if number < 1:
            continue
        mapping[number] = tcp_port
    reporter.info("discovered sender cards: "
                  + ", ".join(f"{n} (tcp {p})"
                              for n, p in sorted(mapping.items())))
    return mapping


def run_binary(args, reporter, slot_map_override, probe_factory=None):
    """Binary transport: authoritative per-chain walk over TCP 520N."""
    sender_ports = _resolve_sender_cards(args, reporter)
    if not sender_ports:
        return None, "no sender cards to scan"

    json_client = None
    topology = {}
    if not args.no_json:
        json_client = HSeriesJSONClient(args.ip, port=args.json_port)
        topology = fetch_topology(json_client, reporter)

    slot_map = resolve_slot_map(sorted(sender_ports),
                                topology.get("slots", []),
                                override=slot_map_override, reporter=reporter)

    factory = probe_factory or (
        lambda tcp_port: BinarySenderCardProbe(
            args.ip, tcp_port, timeout=args.timeout,
            connect_timeout=args.connect_timeout,
            register=args.probe_register))

    cards = []
    probes_sent = 0
    for number in sorted(sender_ports):
        tcp_port = sender_ports[number]
        reporter.info(f"scanning sender card {number} on tcp {tcp_port} "
                      f"(register {args.probe_register})")
        probe = factory(tcp_port)
        try:
            probe.open()
        except OSError as exc:
            reporter.warn(f"sender card {number}: cannot connect to "
                          f"{args.ip}:{tcp_port} ({exc}) — SKIPPED. The total "
                          f"below excludes every card behind it.")
            continue
        try:
            cards.extend(enumerate_sender_card_binary(
                probe, number, slot_map.get(number), args.chains,
                max_cards=args.max_cards, retries=args.retries,
                reporter=reporter))
        finally:
            probes_sent += getattr(probe, "probe_count", 0)
            probe.close()

    reading_stats = None
    if json_client is not None and not args.no_readings:
        reporter.info(f"reading R0155 state for {len(cards)} cards...")
        reading_stats = attach_readings(json_client, cards, reporter=reporter)
    if json_client is not None:
        json_client.close()

    snapshot = build_snapshot(
        args.ip, cards, slot_map, topology,
        transport="binary",
        meta={"probe_register": args.probe_register,
              "probes_sent": probes_sent,
              "chains_scanned": list(args.chains),
              "boundary_retries": args.retries})
    return (snapshot, reading_stats, None), None


def run_json(args, reporter, slot_map_override):
    """JSON transport: R0155 sweep. Known to under-report — see module docs."""
    reporter.warn(
        "--transport json uses R0155, which silently returns nothing for "
        "cards it cannot reach. It undercounted a known 1548-panel wall as "
        "1374. Treat the total as a LOWER BOUND.")
    client = HSeriesJSONClient(args.ip, port=args.json_port)
    topology = fetch_topology(client, reporter)

    numbers = args.sender_cards or sorted(
        range(1, len(topology.get("slots", [])) + 1)) or [1, 2, 3]
    slot_map = resolve_slot_map(numbers, topology.get("slots", []),
                                override=slot_map_override, reporter=reporter)

    cards = []
    all_holes = []
    for number in sorted(numbers):
        slot = slot_map.get(number)
        if slot is None:
            reporter.warn(f"sender card {number}: no slotId, cannot sweep "
                          f"over "
                          f"JSON — SKIPPED")
            continue
        for chain in args.chains:
            found, holes = enumerate_chain_json(
                client, slot, chain, args.max_cards, args.json_gap_tolerance,
                reporter=reporter)
            for card_id, readings in found:
                cards.append(make_card_entry(number, slot, chain, card_id,
                                             readings))
            all_holes.extend((number, chain, cid) for cid in holes)
            reporter.info(f"  sender card {number} chain {chain:2d}: "
                          f"{len(found):3d} cards"
                          + (f" ({len(holes)} silent holes)" if holes else ""))
    client.close()

    snapshot = build_snapshot(
        args.ip, cards, slot_map, topology,
        transport="json",
        meta={"chains_scanned": list(args.chains),
              "json_gap_tolerance": args.json_gap_tolerance,
              "silent_holes": len(all_holes),
              "count_is_lower_bound": True})
    return (snapshot, None, all_holes), None


# ── CLI ────────────────────────────────────────────────────────────────────


def _parse_int_list(text):
    """`0,1,2` or `0-15` or a mix -> sorted list of distinct ints."""
    values = set()
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk[1:]:
            lo, _, hi = chunk.partition("-")
            values.update(range(int(lo), int(hi) + 1))
        else:
            values.add(int(chunk))
    return sorted(values)


def _parse_slot_map(text):
    """`1=20,2=22,3=24` -> {1: 20, 2: 22, 3: 24}."""
    mapping = {}
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        number, _, slot = chunk.partition("=")
        mapping[int(number)] = int(slot)
    return mapping


def default_output_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        DEFAULT_SNAPSHOT_NAME)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="enumerate_wall",
        description="Enumerate every receiving card on an H-series wall and "
                    "write src/wall_live_snapshot.json.",
        epilog="This tool contacts LIVE LED hardware. It sends nothing until "
               "you pass --yes-contact-hardware.")
    # Positional and required: there is deliberately no default IP, so no
    # invocation can ever fire at a production controller by accident.
    parser.add_argument("ip", help="controller IP address (no default)")
    parser.add_argument("--yes-contact-hardware", action="store_true",
                        help="REQUIRED. Confirms you intend to send packets "
                             "to "
                             "the controller. Without it the tool prints what "
                             "it would contact and exits.")
    parser.add_argument("-o", "--output", default=None,
                        help=f"snapshot path (default: "
                             f"src/{DEFAULT_SNAPSHOT_NAME})")
    parser.add_argument("--transport", choices=("binary", "json"),
                        default="binary",
                        help="binary = authoritative per-chain walk over TCP "
                             "520N (default); json = R0155 sweep, known to "
                             "under-report")
    parser.add_argument("--sender-cards", type=_parse_int_list, default=None,
                        help="sender card numbers to scan, e.g. 1,2,3 "
                             "(default: discover via rqProMI on UDP 3800)")
    parser.add_argument("--chains", type=_parse_int_list,
                        default=list(range(DEFAULT_CHAIN_COUNT)),
                        help=f"chain indices to scan, e.g. 0-15 "
                             f"(default: 0-{DEFAULT_CHAIN_COUNT - 1})")
    parser.add_argument("--max-cards", type=int,
                        default=DEFAULT_MAX_CARDS_PER_CHAIN,
                        help=f"maximum card positions per chain "
                             f"(default: {DEFAULT_MAX_CARDS_PER_CHAIN})")
    parser.add_argument("--probe-register", choices=sorted(PROBE_REGISTERS),
                        default=DEFAULT_PROBE_REGISTER,
                        help="binary presence register: biterr = 0x4A010002 "
                             "(documented presence byte, default); live = "
                             "0x0000000A (also yields temp/voltage/link)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_PROBE_TIMEOUT,
                        help=f"per-probe timeout in seconds "
                             f"(default: {DEFAULT_PROBE_TIMEOUT})")
    parser.add_argument("--connect-timeout", type=float,
                        default=DEFAULT_CONNECT_TIMEOUT,
                        help=f"TCP connect timeout (default: "
                             f"{DEFAULT_CONNECT_TIMEOUT})")
    parser.add_argument("--retries", type=int,
                        default=DEFAULT_BOUNDARY_RETRIES,
                        help=f"re-probes before accepting a chain boundary "
                             f"(default: {DEFAULT_BOUNDARY_RETRIES})")
    parser.add_argument("--json-port", type=int, default=JSON_UDP_PORT,
                        help=f"JSON UDP port (default: {JSON_UDP_PORT})")
    parser.add_argument("--json-gap-tolerance", type=int,
                        default=DEFAULT_JSON_GAP_TOLERANCE,
                        help="--transport json only: consecutive silent card "
                             "ids before a chain is considered finished "
                             f"(default: {DEFAULT_JSON_GAP_TOLERANCE})")
    parser.add_argument("--discovery-port", type=int,
                        default=DISCOVERY_UDP_PORT,
                        help=f"rqProMI UDP port "
                             f"(default: {DISCOVERY_UDP_PORT})")
    parser.add_argument("--discovery-timeout", type=float, default=1.0,
                        help="rqProMI discovery timeout (default: 1.0)")
    parser.add_argument("--slot-map", type=_parse_slot_map, default=None,
                        help="explicit sender card -> slotId map, e.g. "
                             "1=20,2=22,3=24 (default: derived from R0405)")
    parser.add_argument("--no-json", action="store_true",
                        help="binary transport only: skip all JSON UDP calls "
                             "(no screen metadata, no R0155 readings)")
    parser.add_argument("--no-readings", action="store_true",
                        help="skip the R0155 readings pass; enumerate only")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="more progress detail (repeatable)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="progress off (warnings still print)")
    return parser


def describe_plan(args, output_path):
    """Exactly what the tool would contact, printed before anything is sent."""
    lines = ["", "This run would contact LIVE hardware:", ""]
    lines.append(f"  controller IP   : {args.ip}")
    if args.transport == "binary":
        if args.sender_cards:
            targets = ", ".join(f"{args.ip}:{BINARY_BASE_PORT + n}"
                                for n in args.sender_cards)
        else:
            targets = (f"{args.ip}:{DISCOVERY_UDP_PORT}/udp (rqProMI "
                       f"discovery), then one TCP connection per sender card "
                       f"it reports (5201-5203)")
        lines.append(f"  TCP targets     : {targets}")
    if args.transport == "json" or not args.no_json:
        lines.append(f"  UDP target      : {args.ip}:{args.json_port} "
                     f"(JSON control protocol)")
    lines.append(f"  transport       : {args.transport}")
    lines.append(f"  chains          : "
                 f"{args.chains[0]}-{args.chains[-1]} ({len(args.chains)})")
    lines.append(f"  max cards/chain : {args.max_cards}")
    cards = len(args.sender_cards or [1, 2, 3])
    lines.append(f"  worst-case probes: "
                 f"~{cards * len(args.chains) * args.max_cards}"
                 f" read requests")
    lines.append(f"  output          : {output_path}")
    lines.append("")
    lines.append("  Every request is a READ. "
                 "Nothing is written to the device.")
    lines.append("")
    lines.append("Re-run with --yes-contact-hardware to proceed.")
    return "\n".join(lines)


def main(argv=None, stdout=None, stderr=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    output_path = args.output or default_output_path()

    if not args.yes_contact_hardware:
        # Deliberately the very first thing after argument parsing: no socket
        # is created, no packet is sent, nothing is written.
        print(describe_plan(args, output_path), file=stdout)
        return EXIT_NOT_CONFIRMED

    if not args.chains:
        print("error: --chains selected no chains", file=stderr)
        return EXIT_ERROR

    level = 0 if args.quiet else 1 + args.verbose
    reporter = Reporter(level, stream=stderr)
    reporter.info(f"contacting {args.ip} ({args.transport} transport)")

    started = time.monotonic()
    if args.transport == "binary":
        result, error = run_binary(args, reporter, args.slot_map)
    else:
        result, error = run_json(args, reporter, args.slot_map)
    if error:
        print(f"error: {error}", file=stderr)
        return EXIT_ERROR

    snapshot, reading_stats, holes = result
    elapsed = time.monotonic() - started
    snapshot["enumeration"]["elapsed_seconds"] = round(elapsed, 1)

    if not snapshot["cards"]:
        reporter.warn("enumeration found ZERO cards. The snapshot was NOT "
                      "written — refusing to overwrite a good snapshot with "
                      "an empty one.")
        print(summarise(snapshot, reading_stats, holes), file=stdout)
        return EXIT_ERROR

    write_snapshot(output_path, snapshot)
    reporter.info(f"wrote {output_path} in {elapsed:.1f}s")
    print(summarise(snapshot, reading_stats, holes), file=stdout)
    return EXIT_OK


# ── ASSUMPTIONS a maintainer must verify against real hardware ─────────────
#
# 1. Sender card N answers on TCP 5200+N. Taken from the §6.5 rqProMI capture,
#    which listed 5201/5202/5203 in card order. Discovery returns the port list
#    but not which card each port belongs to; we assume ascending order.
# 2. The binary card position (byte[8]) is the same number R0155 calls
#    recvCardId. The pre-existing snapshot casts doubt on this: its chain 0
#    starts at card id 0 but chains 1-3 start at card id 5. If that offset is
#    real, `attach_readings` will find a chain where every card is silent and
#    warn about it (it never deletes cards on that basis).
# 3. Presence via bit-error register status byte 0x05. Documented in §6.5 for a
#    present card; behaviour for an *absent* address was never captured. The
#    walk therefore also treats "no answer at all" as absent, which is the
#    conservative reading in both directions.
# 4. `user_slot = slot + 1` and the OPT split at chain 8. Both derived from the
#    field values in the pre-existing snapshot, not from any protocol document.
# 5. Every enumerated sender card is a primary output card. Backup cards (O-6
#    in §1) are not distinguished; `role` is always "primary".

if __name__ == "__main__":
    sys.exit(main())
