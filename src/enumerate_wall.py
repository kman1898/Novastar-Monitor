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

# Every binary per-card read goes to this one port, whichever sender card it
# targets; byte[5] of the frame selects the sender card. The `rqProMI` reply
# advertises further services on 5202/5203/5204 and they do accept connections,
# but they answer every read with an empty payload — treating them as one
# service per sender card is what made sender cards 2+ enumerate as empty.
BINARY_PORT = 5201

# TCP 5200 is the broadcast/main controller. Kept for --sender-cards, which
# still lets an operator address 5200+N explicitly if a future chassis needs it.
BINARY_BASE_PORT = 5200

# 2 OPT fibres per sender card, 8 ports each -> chains 0-7 are OPT 1,
# chains 8-15 are OPT 2. Confirmed against the field layout of the existing
# snapshot (opt = port // 8 + 1, port_on_opt = port % 8 + 1).
PORTS_PER_OPT = 8

# Output (sender) cards occupy every second chassis slot from 20: slot 20 is
# card 1, 22 is card 2, 24 is card 3 ... The operator-facing card number and
# the frame's byte[5] both follow from the slot, which is why the slot list is
# the right source for "which sender cards exist".
FIRST_OUTPUT_SLOT = 20
OUTPUT_SLOT_STRIDE = 2

# The user-facing slot label in the snapshot is one higher than the protocol
# slotId (20/22/24 -> 21/23/25). Derived from the pre-existing snapshot; see
# ASSUMPTIONS at the bottom of this module.
USER_SLOT_OFFSET = 1

DEFAULT_MAX_CARDS_PER_CHAIN = H_MAX_CARDS_PER_PORT   # 91
DEFAULT_CHAIN_COUNT = H_MAX_PORTS                    # 16

# Per-probe socket timeout. A probe that lands on a real card answers in well
# under a millisecond on a wired LAN, but the *empty* address at the end of a
# chain takes far longer — the controller has to wait out its own read to a
# card that isn't there. At 0.5 s every single chain on the test wall ended on
# a timeout rather than on the device's "no card here" answer, which makes
# every count a guess; at 1.5 s they end on a real answer. The cost is paid
# once per chain (at most chains x sender_cards).
DEFAULT_PROBE_TIMEOUT = 1.5
DEFAULT_CONNECT_TIMEOUT = 5.0

# Re-probes before declaring end-of-chain. A single dropped response would
# otherwise truncate a chain and silently under-report the wall, which is the
# exact failure mode this tool exists to avoid. Cheap: only paid at boundaries.
DEFAULT_BOUNDARY_RETRIES = 2

# Delay after every probe. The controller throttles under sustained polling: an
# unpaced sweep left it refusing R0155 entirely for ~45 s, and while throttled a
# probe times out, which the walk reads as end-of-chain. That is how a 22-card
# chain enumerated as 7 and then 9 on consecutive unpaced runs. At 0.15 s a
# 22-card chain re-read cleanly and repeatedly.
DEFAULT_PROBE_PACE = 0.15

# Pacing alone does not stop the degradation — it only slows its onset. When a
# probe goes silent even after `--retries`, the walk pauses this long and asks
# again rather than calling it the end of the chain. The controller recovers
# from a refusing state in roughly 40-50 s of quiet, but a single chain
# boundary is a much smaller ask than the full R0155 sweep that produced that
# figure, so start well below it.
DEFAULT_REST_SECONDS = 10.0
DEFAULT_SILENCE_RESTS = 2

# The controller answers roughly this many per-card reads before it starts
# refusing, and reacting after the fact is not good enough: once degraded it
# stayed degraded through 10 s rests for the remaining nine chains of a sweep,
# reporting 0 cards for each. So rest *before* the budget runs out.
#
# Measured on the live H15 (sender card 1, 0.15 s pace): correct answers for
# the first ~206 probes — chains 0-5, 228 cards, every boundary answered — then
# chain 6 truncated and every later chain went silent. The batched JSON R0155
# sweep is worse, not better: it managed 5 answers out of a 95-address port
# before going quiet, because 8 commands per datagram spends the budget faster.
DEFAULT_PROBE_BUDGET = 150
DEFAULT_BUDGET_REST = 45.0

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
    if not parsed.get("present"):
        # The error-count bytes are only meaningful when byte[0] says a card
        # answered. An absent address returns the same `?? 56 00` shape the
        # free-running counter produces, which decodes to a plausible-looking
        # 86 errors for a card that isn't there.
        return False, {}
    return True, {
        "bit_errors": parsed.get("errors"),
        "bit_errors_saturated": parsed.get("saturated"),
    }


def _presence_live_monitor(payload):
    """Presence rule for register 0x0000000A — presence AND readings in one.

    This register was previously written off as "not per-card on H-series"
    because every address answered. Every address does answer — but byte[0]
    says which of them is a real card: `0x80` present, bit `0x40` set for
    anything past the end of a chain. An absent address repeats the previous
    card's temperature and voltage behind that flag, which is why "the device
    returned a well-formed payload" is not a presence test.

    Verified twice. Against a chain known to hold exactly 22 panels, cards
    0-21 answered `0x80` and 22-25 answered `0xE0/0xE2/0xE4/0xE6`. And against
    a NovaLCT capture of a full monitoring pass, where this register was polled
    once for each of the wall's 286 cards and `byte[1] / 2` reproduced the
    operator's stated 36-43 C across all of them.

    This makes it the better probe register: one read per card yields
    presence, temperature, voltage and link status, where `biterr` yields
    presence and an error count and needs a second protocol for the rest.
    """
    parsed = parse_live_monitoring(payload)
    if not parsed:
        return False, {}
    if not parsed.get("present"):
        return False, {}
    temp_c = parsed.get("temperature_c")
    return True, {
        "temp_c": temp_c,
        "temperature_c": temp_c,
        "voltage_v": parsed.get("voltage_v"),
        "link_status": parsed.get("link_status"),
        "online": True,
        "reading": "ok",
    }


# name -> (register, wire length, presence/readings decoder)
PROBE_REGISTERS = {
    "biterr": (H_REG_BIT_ERRORS[0], H_REG_BIT_ERRORS[1], _presence_bit_errors),
    "live": (REG_LIVE_MONITOR[0], REG_LIVE_MONITOR[1], _presence_live_monitor),
}
# `live` is the default: one read per card returns presence, temperature,
# voltage and link status, where `biterr` returns presence and an error count
# and leaves the readings to a second protocol (R0155) that the controller
# rate-limits hard. `biterr` is still the right choice when hunting a data
# break, since the error counter is what locates one.
DEFAULT_PROBE_REGISTER = "live"


class RequestBudget:
    """How many reads the controller will answer before it needs a rest.

    The budget belongs to the **controller**, not to a connection or a sender
    card, so one instance is shared across every probe in a run. Reconnecting
    does not reset it — verified: sender card 2 opened a fresh connection to
    5201 after sender card 1's sweep and got nothing on any chain, while the
    same probe on a rested controller answered immediately.

    Resting proactively is the whole point. Reacting to silence is too late:
    once the controller is refusing, it stayed refusing through 10 s rests for
    the remaining nine chains of a sweep, reporting 0 cards for each.
    """

    def __init__(self, size=DEFAULT_PROBE_BUDGET, rest=DEFAULT_BUDGET_REST,
                 on_rest=None, sleep=time.sleep):
        self.size = size
        self.rest = rest
        self.on_rest = on_rest
        self._sleep = sleep
        self.spent = 0
        self.rests_taken = 0

    def spend(self, count=1):
        """Account for `count` requests, resting first if the budget is out."""
        if self.size and self.spent and self.spent + count > self.size:
            self.rest_now()
        self.spent += count

    def rest_now(self):
        """Rest unconditionally and start the budget over.

        Used before a verification re-walk, where the whole point is that the
        controller is rested — waiting only if the budget happens to be spent
        would leave the re-walk as unreliable as the sweep that prompted it.
        """
        self.rests_taken += 1
        if self.on_rest:
            self.on_rest(self.rests_taken, self.rest)
        self._sleep(self.rest)
        self.spent = 0


class ProbeResult:
    """Outcome of one per-card probe.

    `answered` separates the two very different ways `present` can be False:

      answered=True   the device said "no card here" — trustworthy boundary
      answered=False  the device said nothing at all — could be a real absence,
                      could be the throttle, could be a dropped packet

    A chain that ends on an unanswered probe is a chain whose length we are
    guessing at, and the walk says so rather than reporting the number as fact.
    """

    __slots__ = ("present", "readings", "answered")

    def __init__(self, present, readings=None, answered=None):
        self.present = present
        self.readings = readings or {}
        # Presence implies an answer; default only matters for absence.
        self.answered = present if answered is None else answered


class BinarySenderCardProbe:
    """Read-only per-card probe against one sender card's TCP service.

    One instance owns one TCP connection to `ip:tcp_port` and walks
    `(chain, card_index)` addresses on it for a single sender card, selected by
    `sender_card` (byte[5] of the read frame).

    `tcp_port` is 5201 for every sender card. The per-card services the
    `rqProMI` reply advertises on 5202/5203/5204 accept connections but answer
    every read with an empty payload, so they cannot be used to enumerate — the
    sender card is chosen by the frame's byte[5] instead. Verified on a
    two-sender H15: `sender_card=0` and `sender_card=1` return different cards
    for the same (chain, card) address over one connection to 5201.

    `pace` is the delay inserted after every probe. The controller throttles
    under sustained polling — after an unpaced sweep it stopped answering R0155
    entirely for ~45 s — and a throttled probe looks exactly like an absent
    card, which silently truncates chains.

    Only READ frames are ever sent.
    """

    def __init__(self, ip, tcp_port, timeout=DEFAULT_PROBE_TIMEOUT,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 register=DEFAULT_PROBE_REGISTER, sender_card=0,
                 pace=DEFAULT_PROBE_PACE, budget=None, sleep=time.sleep):
        self.ip = ip
        self.tcp_port = tcp_port
        self.sender_card = sender_card
        self.pace = pace
        self.budget = budget
        self._sleep = sleep
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
        for "the device answered but the card isn't there" — `ProbeResult
        .answered` tells the two apart, and the caller re-probes either way
        before believing a boundary.
        """
        # Spend the request budget, then rest before the controller starts
        # refusing — not after, because once it is refusing it stays that way
        # for the rest of the sweep and every remaining chain reads as empty.
        # The budget belongs to the controller, not to this connection, so it
        # is tracked in a `RequestBudget` shared across every sender card.
        if self.budget is not None:
            self.budget.spend(1)

        self.probe_count += 1
        if self._sock is None:
            self._resync()
            if self._sock is None:
                return ProbeResult(False, answered=False)

        if self.pace:
            self._sleep(self.pace)

        self._seq = (self._seq + 1) & 0xFFFF
        frame = build_read_card(self._seq, self.register, self.reg_length,
                                chain, card_index, sender_card=self.sender_card)
        try:
            self._sock.sendall(frame)
        except OSError:
            self._resync()
            return ProbeResult(False, answered=False)

        data = self._recv_frame()
        if data is None:
            self._resync()
            return ProbeResult(False, answered=False)

        parsed = parse_response(data)
        if not parsed:
            self._resync()
            return ProbeResult(False, answered=False)
        reg, payload = parsed
        if reg != self.register:
            # Somebody else's answer (or a stale one) — the stream is out of
            # step. Never treat a mismatched frame as evidence about this card.
            self._resync()
            return ProbeResult(False, answered=False)

        present, readings = self._decode(payload)
        return ProbeResult(present, readings, answered=True)


# ── Chain walking ──────────────────────────────────────────────────────────


def enumerate_chain(probe, chain, max_cards=DEFAULT_MAX_CARDS_PER_CHAIN,
                    retries=DEFAULT_BOUNDARY_RETRIES, reporter=None,
                    silence_rests=DEFAULT_SILENCE_RESTS,
                    rest_seconds=DEFAULT_REST_SECONDS, sleep=time.sleep,
                    status=None):
    """Walk one chain and return the list of cards actually on it.

    Chains are contiguous (§6.5), so the walk runs from card position 0 upward
    and stops at the first address the device reports as empty. That boundary
    probe is the one NovaLCT also sends — it is **not** counted. An empty chain
    therefore costs one probe and yields zero cards.

    A boundary is only trustworthy when the device **answered** it. There are
    two distinct ways it fails to, and both were observed truncating this wall:

      * the answer for an empty address takes longer than a present card's, so
        a short socket timeout reads "empty" as silence (fixed by
        DEFAULT_PROBE_TIMEOUT — at 0.5 s every chain ended on silence, at 1.5 s
        most end on a real answer);
      * the controller degrades over a long sweep and stops answering
        addresses it answered a minute earlier. Chain 6 of this wall has 22
        cards when probed rested and reported 9 when reached ~200 probes into
        a full sweep.

    So `retries` handles a dropped packet, and `silence_rests` handles the
    second case: after the retries are used up on a *silent* probe, the walk
    pauses `rest_seconds` and tries the whole retry set again, up to
    `silence_rests` times. Only silence that survives a rest is accepted as a
    boundary — and even then the count is reported as a lower bound rather than
    as fact, because it cannot be distinguished from a still-degraded device.

    Pass a dict as `status` to learn how the walk ended: it gets
    `ended_on_silence` set, which is what the caller needs to decide whether
    the count is worth re-checking.

    Returns a list of `(card_index, readings_dict)` in ascending order.
    """
    if status is not None:
        status["ended_on_silence"] = False
    cards = []
    for card_index in range(max_cards):
        result = None
        rests_used = 0
        while True:
            for attempt in range(retries + 1):
                result = probe.probe(chain, card_index)
                # Retries exist to survive a *dropped* response. Any answer —
                # "here I am" or "no card here" — is final, so stop asking.
                # On a controller that degrades under load, two pointless
                # probes per chain boundary are worth not sending.
                if result.answered:
                    if attempt and reporter and result.present:
                        reporter.detail(
                            f"    chain {chain} card {card_index}: answered on "
                            f"retry {attempt} (transient drop)")
                    break
            # An answered "no card here" is a real boundary; take it as read.
            if result.present or result.answered:
                break
            if rests_used >= silence_rests:
                break
            rests_used += 1
            if reporter:
                reporter.detail(
                    f"    chain {chain} card {card_index}: silent after "
                    f"{retries + 1} attempts — resting {rest_seconds}s "
                    f"({rests_used}/{silence_rests}) before deciding it is "
                    f"the end of the chain")
            sleep(rest_seconds)
        if not result.present:
            if not result.answered and status is not None:
                status["ended_on_silence"] = True
            if not result.answered and reporter:
                reporter.warn(
                    f"chain {chain} ended at card {card_index} on SILENCE, "
                    f"not on a 'no card here' answer — after "
                    f"{retries + 1} attempts and {rests_used} rest(s) of "
                    f"{rest_seconds}s. {len(cards)} is a LOWER BOUND: a "
                    f"degraded controller is indistinguishable from an empty "
                    f"address. Re-run --chains {chain} on its own to confirm.")
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
                                 reporter=None,
                                 silence_rests=DEFAULT_SILENCE_RESTS,
                                 rest_seconds=DEFAULT_REST_SECONDS,
                                 unresolved=None):
    """Enumerate every chain on one sender card.

    Chains that ended on silence rather than on the device's "no card here"
    are appended to `unresolved` as `(card_number, chain, count)` so the caller
    can re-check them from a rested controller. Returns a list of card dicts.
    """
    reporter = reporter or Reporter(0)
    cards = []
    for chain in chains:
        status = {}
        found = enumerate_chain(probe, chain, max_cards=max_cards,
                                retries=retries, reporter=reporter,
                                silence_rests=silence_rests,
                                rest_seconds=rest_seconds, status=status)
        for card_index, readings in found:
            cards.append(make_card_entry(card_number, slot, chain, card_index,
                                         readings))
        if status.get("ended_on_silence") and unresolved is not None:
            unresolved.append((card_number, chain, len(found)))
        reporter.info(
            f"  sender card {card_number} (byte[5]={probe.sender_card}) "
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
        # The binary walk only records a card it got a presence answer for
        # (byte[0] == 0x05), so this is a fact about the hardware, independent
        # of whether R0155 later says anything about it.
        "present": True,
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
    # Card number and slot are related by the chassis layout, so derive it
    # rather than zipping two sorted lists. Zipping was wrong the moment the
    # card numbers stopped being 1..N: on a wall with cards 1, 2, 5, 6 it
    # paired card 5 with slot 28's *position* in the list, not with slot 28.
    reported = set(topo_slots or [])
    for number in sorted(card_numbers):
        slot = FIRST_OUTPUT_SLOT + (number - 1) * OUTPUT_SLOT_STRIDE
        if not reported or slot in reported:
            slot_map[number] = slot
    for i, number in enumerate(sorted(card_numbers)):
        if slot_map.get(number) is None and i < len(topo_slots):
            slot_map[number] = topo_slots[i]
    missing = [n for n in card_numbers if slot_map.get(n) is None]
    if missing:
        reporter.warn(
            "no slotId known for sender card(s) "
            + ", ".join(str(n) for n in missing)
            + " — pass --slot-map (e.g. 1=20,2=22,3=24). Cards will be "
              "enumerated but carry slot=null and cannot be polled by R0155.")
    return slot_map


def attach_readings(client, cards, reporter=None, batch_size=None,
                    budget=None):
    """Fill per-card readings from R0155, in place. Returns a stats dict.

    Cards the device does not answer for are **kept** in the list — a card
    that exists but cannot be read is a monitoring finding, not a card to
    delete — but they are recorded as `online: None`, NOT `online: False`.

    That distinction is the whole point. The binary walk has already proved
    these cards are physically there (byte[0] == 0x05). R0155 going quiet for
    them means the JSON path did not answer, and on this controller the
    overwhelmingly likely reason is the request budget (§6.6): a 286-card
    readings pass answered 36 and then went silent. Writing `online: False`
    for the other 250 put "250 panels offline" on a dashboard for a wall where
    every panel was lit — which is precisely the false alarm this tool exists
    to avoid. Silence from one protocol is not evidence a panel is down.

    `reading` records which it was, so the UI can say "no reading" rather than
    inventing a state.

    `budget` is the same `RequestBudget` the binary walk spends. R0155 draws on
    the *same* controller allowance the per-card reads do — asking for 286
    cards in one go answered 36 of them and left the controller refusing R0155
    for the next ~45 s — so the readings pass rests on the same schedule
    instead of undoing the sweep's careful pacing at the last step.
    """
    reporter = reporter or Reporter(0)
    addressable = [c for c in cards if c.get("slot") is not None]
    stats = {"requested": len(addressable), "answered": 0,
             "silent": 0, "skipped": len(cards) - len(addressable)}
    if not addressable:
        return stats

    addresses = [card_address(c) for c in addressable]
    if budget is None:
        responses = client.get_receiving_cards_batch(
            addresses, batch_size=batch_size)
    else:
        chunk = max(1, budget.size or len(addresses))
        responses = []
        for start in range(0, len(addresses), chunk):
            window = addresses[start:start + chunk]
            budget.spend(len(window))
            responses.extend(client.get_receiving_cards_batch(
                window, batch_size=batch_size))
    per_chain_silent = {}
    for card, raw in zip(addressable, responses):
        parsed = parse_receiving_card(raw) if raw is not None else None
        if not parsed:
            # Unknown, not offline. The binary walk found this card; R0155
            # merely declined to talk about it.
            card["online"] = None
            card["reading"] = "no_answer"
            stats["silent"] += 1
            key = (card["card_number"], card["port"])
            per_chain_silent[key] = per_chain_silent.get(key, 0) + 1
            continue
        stats["answered"] += 1
        card["reading"] = "ok"
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
    # Which sender cards actually had receiving cards behind them. A backup
    # card answers nothing until it takes over, so "no cards found" is its
    # normal state, not a fault — and not evidence about its role either.
    populated = {c["card_number"] for c in cards}
    sender_cards = [
        {
            "card_number": number,
            "slot": slot_map.get(number),
            "user_slot": (None if slot_map.get(number) is None
                          else slot_map[number] + USER_SLOT_OFFSET),
            # NOT "primary". This H15 has four sender cards, two primary and
            # two backup, and nothing we can read distinguishes them — the
            # backups are simply silent on both protocols. Claiming "primary"
            # for all four states something we have not established; `None`
            # says we do not know, which is true.
            "role": None,
            "carrying_panels": number in populated,
        }
        for number in sorted(slot_map or {})
    ]
    if not sender_cards:
        for number in sorted(populated):
            sender_cards.append({"card_number": number, "slot": None,
                                 "user_slot": None, "role": None,
                                 "carrying_panels": True})

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


def slot_to_card_number(slot_id):
    """Chassis slot id -> the sender card number an operator uses.

    Output cards occupy every second slot from 20, so slot 20 is card 1, 22 is
    card 2, 24 is card 3 and so on. Confirmed against an operator who
    described their wall as "cards 1 and 2 primary, 5 and 6 backup" for a
    chassis whose R0405 reports slots 20, 22, 28 and 30 — which is cards
    1, 2, 5 and 6 under this mapping, and card 5 was then observed carrying
    the backup half of a deliberately broken chain.
    """
    if slot_id is None or slot_id < FIRST_OUTPUT_SLOT:
        return None
    offset = slot_id - FIRST_OUTPUT_SLOT
    if offset % OUTPUT_SLOT_STRIDE:
        return None
    return offset // OUTPUT_SLOT_STRIDE + 1


def _resolve_sender_cards(args, reporter):
    """Decide which sender cards to scan: `{card_number: sender_card_byte}`.

    Card numbers are 1-based (what the operator and the snapshot call them);
    the wire byte is 0-based, so card N maps to byte N-1.

    The card numbers come from the SLOTS the controller reports, not from the
    `rqProMI` port list. That distinction cost us the backups: the discovery
    reply advertises four services on 5201-5204, which read as "cards 1, 2, 3
    and 4" and made the enumerator scan byte[5] 0-3. The real chassis had
    cards 1, 2, 5 and 6 (slots 20, 22, 28, 30), so byte[5] 4 and 5 — the two
    backup cards — were never probed at all. A backup is silent while idle, so
    nothing about the result looked wrong.
    """
    if args.sender_cards:
        numbers = args.sender_cards
        reporter.info(f"sender cards (from --sender-cards): "
                      f"{', '.join(str(n) for n in numbers)}")
        return {n: n - 1 for n in numbers}

    slots = getattr(args, "_topology_slots", None) or []
    numbers = sorted({n for n in (slot_to_card_number(s) for s in slots)
                      if n is not None})
    if numbers:
        reporter.info(
            "sender cards from controller slots: "
            + ", ".join(f"{n} (slot {FIRST_OUTPUT_SLOT + (n - 1) * OUTPUT_SLOT_STRIDE}"
                        f", byte[5]={n - 1})" for n in numbers))
        return {n: n - 1 for n in numbers}

    reporter.info(f"discovering sender cards via {DISCOVERY_REQUEST.decode()} "
                  f"on udp {args.discovery_port}...")
    ports = discover_sender_card_ports(args.ip, timeout=args.discovery_timeout,
                                       port=args.discovery_port)
    if not ports:
        reporter.warn("no answer to the rqProMI discovery probe. Pass "
                      "--sender-cards 1,2,5,6 to scan explicitly.")
        return {}
    mapping = {}
    for tcp_port in ports:
        number = tcp_port - BINARY_BASE_PORT
        if number < 1:
            continue
        mapping[number] = number - 1
    reporter.warn(
        "falling back to the rqProMI port list for sender card numbers. It "
        "reports one service per card starting at 5201, which numbers them "
        "1..N — on a chassis with backup cards in higher slots those numbers "
        "are wrong and the backups will not be scanned. Pass --sender-cards.")
    reporter.info("discovered sender cards: "
                  + ", ".join(f"{n} (byte[5]={b}, tcp {BINARY_PORT})"
                              for n, b in sorted(mapping.items())))
    return mapping


def verify_silent_chains(unresolved, cards, sender_bytes, slot_map, factory,
                         budget, args, reporter):
    """Re-walk every chain that ended on silence, from a rested controller.

    Silence during a sweep is ambiguous — it is both what an unconfigured chain
    looks like and what a controller that has run out of request budget looks
    like. Re-walking one chain at a time after a full rest removes the
    ambiguity: on the test wall every one of ten silence-ended chains came back
    with a proper "no card here" answer, and one of them (OPT 1 port 7, read as
    20 during the sweep) came back with its true 22 cards.

    The re-walk keeps whichever result is longer. It can only ever add cards,
    never remove them, so a verification pass that itself gets throttled leaves
    the sweep's answer intact.

    Returns `(cards, probes_sent)`.
    """
    reporter.info(f"verifying {len(unresolved)} chain(s) that ended on "
                  f"silence, one at a time from a rested controller")
    by_address = {}
    for number, chain, count in unresolved:
        by_address[(number, chain)] = count

    added = 0
    probes_sent = 0
    for (number, chain), swept in sorted(by_address.items()):
        # A full rest before each one — this is the whole point of the pass.
        budget.rest_now()
        probe = factory(sender_bytes[number])
        try:
            probe.open()
        except OSError as exc:
            reporter.warn(f"verify sender card {number} chain {chain}: "
                          f"cannot connect ({exc}); keeping {swept}")
            continue
        try:
            status = {}
            found = enumerate_chain(
                probe, chain, max_cards=args.max_cards, retries=args.retries,
                reporter=reporter,
                silence_rests=getattr(args, "silence_rests",
                                      DEFAULT_SILENCE_RESTS),
                rest_seconds=getattr(args, "rest_seconds",
                                     DEFAULT_REST_SECONDS),
                status=status)
        finally:
            probes_sent += getattr(probe, "probe_count", 0)
            probe.close()

        verdict = "still silent" if status.get("ended_on_silence") else "settled"
        if len(found) > swept:
            slot = slot_map.get(number)
            cards = [c for c in cards
                     if not (c["card_number"] == number and c["port"] == chain)]
            for card_index, readings in found:
                cards.append(make_card_entry(number, slot, chain, card_index,
                                             readings))
            added += len(found) - swept
            reporter.info(f"  sender card {number} chain {chain:2d}: "
                          f"{swept} -> {len(found)} cards ({verdict})")
        else:
            reporter.info(f"  sender card {number} chain {chain:2d}: "
                          f"{len(found)} cards, confirms {swept} ({verdict})")

    if added:
        reporter.info(f"verification recovered {added} card(s) the sweep "
                      f"missed")
    return cards, probes_sent


def run_binary(args, reporter, slot_map_override, probe_factory=None):
    """Binary transport: authoritative per-chain walk over TCP 520N."""
    # Topology first: the slot list is what tells us which sender cards exist
    # and what their operator-facing numbers are. Discovery only knows how
    # many services answer, which numbers backups wrongly.
    json_client = None
    topology = {}
    if not args.no_json:
        json_client = HSeriesJSONClient(args.ip, port=args.json_port)
        topology = fetch_topology(json_client, reporter)
    args._topology_slots = topology.get("slots", [])

    sender_bytes = _resolve_sender_cards(args, reporter)
    if not sender_bytes:
        return None, "no sender cards to scan"

    slot_map = resolve_slot_map(sorted(sender_bytes),
                                topology.get("slots", []),
                                override=slot_map_override, reporter=reporter)

    tcp_port = getattr(args, "binary_port", BINARY_PORT)
    pace = getattr(args, "pace", DEFAULT_PROBE_PACE)
    budget = RequestBudget(
        size=getattr(args, "probe_budget", DEFAULT_PROBE_BUDGET),
        rest=getattr(args, "budget_rest", DEFAULT_BUDGET_REST),
        on_rest=lambda n, secs: reporter.info(
            f"  resting {secs:g}s to stay inside the controller's request "
            f"budget (rest {n})"))
    factory = probe_factory or (
        lambda sender_card: BinarySenderCardProbe(
            args.ip, tcp_port, timeout=args.timeout,
            connect_timeout=args.connect_timeout,
            register=args.probe_register, sender_card=sender_card,
            pace=pace, budget=budget))

    verify_silent = getattr(args, "verify_silent", True)
    # Mid-sweep rests are the weaker of the two remedies and they are not
    # additive: on the test wall they turned a 22-card chain into 20 (still
    # wrong, still needing verification) while costing 20 s on each of nine
    # empty chains that stayed silent regardless. The verification pass fixes
    # the same chains properly, so when it is enabled the sweep skips the rests
    # and runs fast. With --no-verify-silent they are the only defence left,
    # so they come back on.
    silence_rests = getattr(args, "silence_rests", None)
    if silence_rests is None:
        silence_rests = 0 if verify_silent else DEFAULT_SILENCE_RESTS
    rest_seconds = getattr(args, "rest_seconds", DEFAULT_REST_SECONDS)

    cards = []
    probes_sent = 0
    unresolved = []
    for number in sorted(sender_bytes):
        sender_card = sender_bytes[number]
        reporter.info(f"scanning sender card {number} "
                      f"(byte[5]={sender_card}) on tcp {tcp_port} "
                      f"(register {args.probe_register})")
        probe = factory(sender_card)
        try:
            probe.open()
        except OSError as exc:
            reporter.warn(f"sender card {number}: cannot connect to "
                          f"{args.ip}:{tcp_port} ({exc}) — SKIPPED. The "
                          f"total below excludes every card behind it.")
            continue
        try:
            cards.extend(enumerate_sender_card_binary(
                probe, number, slot_map.get(number), args.chains,
                max_cards=args.max_cards, retries=args.retries,
                reporter=reporter, silence_rests=silence_rests,
                rest_seconds=rest_seconds, unresolved=unresolved))
        finally:
            probes_sent += getattr(probe, "probe_count", 0)
            probe.close()

    if unresolved and verify_silent:
        cards, verify_probes = verify_silent_chains(
            unresolved, cards, sender_bytes, slot_map, factory, budget,
            args, reporter)
        probes_sent += verify_probes

    reading_stats = None
    walk_gave_readings = any(c.get("temp_c") is not None for c in cards)
    if walk_gave_readings:
        # The `live` probe register already returned temperature, voltage and
        # link status for every card it found, so the R0155 pass would re-ask
        # the same questions over a protocol the controller rate-limits far
        # harder — and spend the budget doing it. On a 286-card wall that pass
        # answered 36 and left the rest looking unread.
        reporter.info(f"skipping the R0155 pass — the {args.probe_register} "
                      f"walk already read temperature and voltage for "
                      f"{sum(1 for c in cards if c.get('temp_c') is not None)}"
                      f" of {len(cards)} cards")
    elif json_client is not None and not args.no_readings:
        reporter.info(f"reading R0155 state for {len(cards)} cards...")
        reading_stats = attach_readings(json_client, cards, reporter=reporter,
                                        budget=budget)
    if json_client is not None:
        json_client.close()

    snapshot = build_snapshot(
        args.ip, cards, slot_map, topology,
        transport="binary",
        meta={"probe_register": args.probe_register,
              "probes_sent": probes_sent,
              "chains_scanned": list(args.chains),
              "boundary_retries": args.retries,
              "silence_ended_chains": len(unresolved),
              "verified_silent_chains": bool(unresolved and verify_silent)})
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
                        help="binary presence register. live = 0x0000000A "
                             "(default) gives presence AND temperature, "
                             "voltage and link status in one read per card. "
                             "biterr = 0x4A010002 gives presence and the "
                             "bit-error counter instead.")
    parser.add_argument("--no-verify-silent", dest="verify_silent",
                        action="store_false", default=True,
                        help="skip the pass that re-walks silence-ended "
                             "chains one at a time from a rested controller. "
                             "That pass is what turns an ambiguous 'we "
                             "stopped asking' into a real count; without it a "
                             "22-card chain can be recorded as 20.")
    parser.add_argument("--probe-budget", type=int,
                        default=DEFAULT_PROBE_BUDGET,
                        help=f"per-card reads to send before pausing "
                             f"(default: {DEFAULT_PROBE_BUDGET}). The "
                             f"controller answers roughly this many before it "
                             f"starts refusing, and once refusing it stays "
                             f"that way — every later chain then reads as "
                             f"empty. 0 disables the budget.")
    parser.add_argument("--budget-rest", type=float,
                        default=DEFAULT_BUDGET_REST,
                        help=f"seconds to pause when the request budget runs "
                             f"out (default: {DEFAULT_BUDGET_REST}). The "
                             f"controller took 40-50s of quiet to recover in "
                             f"testing.")
    parser.add_argument("--silence-rests", type=int, default=None,
                        help=f"how many times to pause mid-sweep and re-ask "
                             f"before accepting SILENCE as the end of a chain. "
                             f"Default is 0 while the verification pass is on "
                             f"(it does the same job better) and "
                             f"{DEFAULT_SILENCE_RESTS} with "
                             f"--no-verify-silent. An answered 'no card here' "
                             f"is always taken at face value; this only "
                             f"applies to no answer at all.")
    parser.add_argument("--rest-seconds", type=float,
                        default=DEFAULT_REST_SECONDS,
                        help=f"seconds to pause per --silence-rests attempt "
                             f"(default: {DEFAULT_REST_SECONDS})")
    parser.add_argument("--binary-port", type=int, default=BINARY_PORT,
                        help=f"TCP port carrying per-card reads for ALL sender "
                             f"cards (default: {BINARY_PORT}). The sender card "
                             f"is selected by byte[5] of the frame, not by the "
                             f"port.")
    parser.add_argument("--pace", type=float, default=DEFAULT_PROBE_PACE,
                        help=f"seconds to wait after every probe "
                             f"(default: {DEFAULT_PROBE_PACE}). The controller "
                             f"throttles under sustained polling and a "
                             f"throttled probe is indistinguishable from an "
                             f"absent card. 0 disables pacing.")
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
        binary_port = getattr(args, "binary_port", BINARY_PORT)
        if args.sender_cards:
            targets = (f"{args.ip}:{binary_port} (sender cards "
                       f"{', '.join(str(n) for n in args.sender_cards)} "
                       f"selected by byte[5])")
        else:
            targets = (f"{args.ip}:{DISCOVERY_UDP_PORT}/udp (rqProMI "
                       f"discovery), then one TCP connection to "
                       f"{args.ip}:{binary_port} per sender card it reports")
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
# 5. Primary vs backup is NOT determined. The test chassis has four sender
#    cards, two primary and two backup, and the backups answer nothing on
#    either protocol while idle — R0155 is silent for their slots and a
#    per-card binary read reports every address absent. `role` is therefore
#    None, and `carrying_panels` records the one thing we did observe. An
#    earlier version claimed "primary" for every card, which was a guess
#    dressed up as data.

if __name__ == "__main__":
    sys.exit(main())
