"""
NovaStar Device Manager
Manages TCP connections and polls controllers for monitoring data.
Uses threading (not asyncio) for Flask/SocketIO compatibility.
"""

import json
import logging
import os
import socket
import struct
import threading
import time
from datetime import datetime
from novastar_protocol import (
    TCP_PORT, H_TCP_PORT,
    build_clear_bit_errors,
    build_read, build_read_card, parse_response, parse_live_monitoring,
    parse_system_info, parse_nssd, decode_length,
    REG_SYSTEM_INFO, REG_FIRMWARE, REG_DEVICE_PORT1, REG_DEVICE_PORT2,
    REG_BRIGHTNESS, REG_GAMMA, REG_DATETIME, REG_VIDEO_STATUS,
    REG_LIVE_MONITOR,
    H_REG_VIDEO_STATUS, H_REG_FIRMWARE,
    H_REG_BRIGHTNESS, H_REG_GAMMA, H_REG_DATETIME, H_REG_DEVICE_ID,
    H_REG_CARD_FAULT, H_REG_BIT_ERRORS,
    H_MAX_PORTS, H_PORT_BITMASK_BITS,
    parse_h_port_bitmask, parse_h_card_link, parse_h_card_fault,
    parse_bit_errors,
)
from h_series_json import (
    HSeriesJSONClient, parse_device_details, parse_receiving_card,
    parse_slot_info,
)
from snmp_client import (
    DEFAULT_COMMUNITY as SNMP_COMMUNITY, SNMP_PORT, SNMPClient,
    HSeriesSNMPMonitor,
)

# Child of the app's 'novastar_monitor' logger so these records reach the
# handlers app.py installs, without device_manager importing app.
logger = logging.getLogger("novastar_monitor.device_manager")

# Per-install runtime state written by the R0155 enumeration script. It is
# gitignored (site-specific card inventory), so a fresh checkout won't have
# one — see _load_known_cards_from_snapshot. Module-level so tests can point
# it at a tmp_path.
SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "wall_live_snapshot.json")

# Samples kept per history series.
HISTORY_LIMIT = 300

# Default seconds between poll cycles.
#
# This was 10.0, copied from the official Bitfocus Companion novastar-splicer
# module. That module is a *control* surface: 10 s is how fast its UI needs to
# reflect a change an operator just made. This app is a monitor watching a wall
# during a live show, and it has no such requirement — the conditions it exists
# to catch (a card heating up, a supply rail sagging, a chain dropping) develop
# over minutes. Copying a control module's cadence was never a reasoned choice.
#
# With the per-card sweep gone (see PER_CARD_SWEEP docs below), a routine cycle
# is now three bounded SNMP walks — and nothing at all on the control protocol
# — so the interval no longer governs a burst. The conservative default still
# matters, because the app's job during a show is to be as close to invisible
# on the wire as monitoring allows, on any protocol. 30 s
# gives a worst-case 30 s detection latency on a thermal trend that takes
# minutes, at a third of the traffic. Operators who want it tighter can lower
# it in settings; the point is what happens when nobody thinks about it.
#
# Single source of truth: app.py imports this constant instead of declaring
# its own. The two used to disagree (2.0 here, 10.0 there), so any caller that
# constructed a DeviceManager without passing an interval polled live hardware
# five times faster than the value the settings UI advertised.
DEFAULT_POLL_INTERVAL = 30.0

# How long a cached topology read (R0400 / R0405 / R0300) stays good.
#
# Screen layout, output list and slot inventory change when an operator
# reconfigures the splicer, not while a show runs. Re-reading them every cycle
# was several datagrams per device per cycle spent re-learning a constant.
TOPOLOGY_REFRESH_INTERVAL = 600.0

# ── Per-card polling policy ────────────────────────────────────────────────
#
# A full per-card R0155 sweep of this wall is ~1374 reads. Batched at 8 per
# datagram that is still ~172 request datagrams fired back to back at a
# controller that is simultaneously driving a live show, every single cycle.
# That sustained burst is the second of the two suspects in the outage where
# the operator lost control of the wall from Bitfocus Companion while this app
# was polling (the first was the W0120 heartbeat, now removed).
#
# So per-card polling is no longer something that happens. Routine health
# monitoring belongs to the SNMP client (read-only, claims no controller role);
# JSON per-card reads are an on-demand detail view — `refresh_chain()` for one
# chain, `refresh_cards()` for an explicit address list. `refresh_all_cards()`
# still exists because SNMP cannot reach per-card data at all, but it is
# explicit, rate-limited, and reachable from no poll path.

# Most addresses a single on-demand refresh may touch. One chain of this wall
# is well under this; the cap exists so a caller that passes a bad list cannot
# accidentally recreate the full sweep by another name.
PER_CARD_SWEEP_LIMIT = 256

# Bit-error reads are per-card binary reads and hit the controller's request
# budget like any other: it answers roughly 150-200 then stops, and a probe it
# declines to answer is indistinguishable from an absent card. Same shape as
# the enumerator's pacing (H_SERIES_FINDINGS §6.6).
BIT_ERROR_READ_PACE = 0.15
BIT_ERROR_READ_BUDGET = 150
BIT_ERROR_READ_REST = 45.0

# Per-card binary reads answer only on this port.
# How long a per-card reading may be used to draw a conclusion. Reads are on
# demand, so without this an alert fires forever off a value from hours ago.
CARD_READING_MAX_AGE = 300.0

# Publish partial results every this many cards during a long per-card pass.
# Small enough that the wall visibly fills in, large enough that the state
# broadcast is not the expensive part of the read.
PER_CARD_PUBLISH_EVERY = 8

H_PER_CARD_PORT = 5201
PER_CARD_TIMEOUT = 1.5
PER_CARD_CONNECT_TIMEOUT = 5.0

# Minimum seconds between full sweeps, per device. Deliberately long: a full
# sweep is the exact traffic pattern that was running during the outage, so it
# is something an operator asks for occasionally and deliberately, never
# something that can end up back on a cadence.
FULL_SWEEP_MIN_INTERVAL = 300.0


# ── Routine health lives on SNMP ───────────────────────────────────────────
#
# H-series devices are polled for health over SNMPv2c (snmp_client.py), not
# over the JSON control protocol. The reason is the outage: the JSON path on
# UDP 6000 is the *control* plane, and this app talking on it is what took
# Bitfocus Companion — the operator's show-control surface — away mid-show.
# SNMP is a different protocol on a different port, purely observational, and
# the agent serves several managers at once, so nothing this module reads can
# take the desk away from anybody.
#
# It costs more datagrams than the single R0100 it replaces (three bounded
# walks, ~40 datagrams, 0.7 s measured against the live H15), and that is the
# trade being made deliberately: traffic moved off the control plane is worth
# more than traffic minimised on it.
#
# Consecutive SNMP misses before a device that has NEVER answered on SNMP is
# left alone. Same shape and the same reasoning as _json_max_fails: a device
# with the agent switched off, or on firmware below the V2.0.0.0 the SNMP spec
# requires, must not cost a timeout every single cycle forever. Once SNMP has
# answered once, transient misses never disable it.
SNMP_MAX_FAILS = 3


# ── Global stop ────────────────────────────────────────────────────────────
#
# One switch that silences this app on the wire without killing the process.
# During the outage the only way the operator had to stop the monitor touching
# the wall was to kill it, mid-show. There must be a way that does not involve
# losing the dashboard, the alert history and the log.
#
# Module-level rather than an attribute of one DeviceManager, because "stop
# talking to the hardware" has to mean *everything*: the poll threads here, the
# on-demand card refreshes called from request threads, and the SNMP client
# being added alongside this one. One flag, one place to look.
#
# Setting it stops new requests being issued. It does not tear down sockets or
# interrupt a read already in flight — those finish on their existing timeouts
# (worst case a couple of seconds) rather than being aborted, because yanking a
# socket out from under a thread mid-read is how you get a hang instead of a
# stop.
_CONTACT_HALTED = threading.Event()


def halt_device_contact(reason=None):
    """Stop every outbound request to every controller, process-wide.

    Idempotent. Poll threads keep running and keep serving their last known
    state; they simply stop putting packets on the wire.
    """
    already = _CONTACT_HALTED.is_set()
    _CONTACT_HALTED.set()
    if not already:
        logger.warning("Device contact HALTED%s — no further requests will be "
                       "sent to any controller until resumed",
                       f": {reason}" if reason else "")
    return True


def resume_device_contact():
    """Allow outbound requests again. Idempotent."""
    was_halted = _CONTACT_HALTED.is_set()
    _CONTACT_HALTED.clear()
    if was_halted:
        logger.warning("Device contact resumed")
    return True


def device_contact_halted():
    """True while the global stop is engaged.

    Every code path that is about to touch hardware — in this module, in the
    SNMP client, anywhere — checks this first.
    """
    return _CONTACT_HALTED.is_set()


class HaltAwareSNMPClient(SNMPClient):
    """An SNMPClient that honours the global stop before every request.

    snmp_client.py deliberately imports nothing from this module — it is a
    standalone, dependency-free codec that has to stay testable on its own —
    so the halt check cannot live inside it. It lives here instead, on the one
    method that puts a datagram on the wire: `_exchange` is the sole caller of
    `sendto` in that module, and both public reads (`get_many`, `get_next`)
    and therefore both derived ones (`get`, `walk`) funnel through it.
    tests/test_device_manager.py asserts that chokepoint against
    snmp_client.py's own source, so a future read path that bypassed
    `_exchange` would fail the suite rather than quietly escape the stop.

    The whole point of the stop being module-level was that it covers every
    transport. A halted SNMP request returns None, which the client's callers
    already treat as "no reading" — nothing above this needs to know why.
    """

    def _exchange(self, oids, pdu_type):
        if device_contact_halted():
            return None
        return super()._exchange(oids, pdu_type)


def _snapshot(value):
    """Deep-copy the plain-JSON parts of a state tree.

    Poll threads rebuild their working state; request threads hand the result
    straight to `jsonify` / `socketio.emit`. Serializing a dict another thread
    is mutating raises `RuntimeError: dictionary changed size during
    iteration`, so what we publish is always a private copy nobody will touch
    again. Only dicts and lists are recursed — every leaf in the state tree is
    an immutable scalar.
    """
    if isinstance(value, dict):
        return {k: _snapshot(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_snapshot(v) for v in value]
    return value


def _int_or_none(value):
    """Coerce a device-reported number, or None if it isn't one.

    R0405 fields arrive straight off the wire; a missing key and a string are
    both possible, and neither may become a confident 0.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rect(iface):
    """(x, y, width, height) of one screenInterface, or None if incomplete."""
    x = _int_or_none(iface.get("x"))
    y = _int_or_none(iface.get("y"))
    w = _int_or_none(iface.get("width"))
    h = _int_or_none(iface.get("height"))
    if None in (x, y, w, h):
        return None
    return (x, y, w, h)


def summarize_screen(screen_id, screen):
    """Reduce one R0405 answer to the facts the wall view needs.

    `screen` is one entry of `state["screen_outputs"]`: `name`, `size`,
    `mosaic` and the raw `screenInterfaces` list.

    The thing this exists to get right is what "outputs" means. The observed
    wall answers with 16 screenInterfaces — but they are four sender cards
    (slots 20/22/28/30) each driving the SAME four 960x2160 columns of a
    3840x2160 canvas. Counting 16 as "active ports", or tiling the canvas 16
    ways, describes a wall four times the real one. So the interfaces are
    grouped by `slotId` and the geometry each slot covers is compared: when
    every slot covers the same set of rectangles, that is redundancy, and the
    number of independently driven regions is what the operator wants.

    `isCardOnline` comes back null on this hardware, so which of the four
    sender cards is actually driving is NOT knowable here. `card_online_known`
    says so explicitly rather than letting a caller infer "all four live".
    `covers_canvas` is three-valued for exactly the same reason — see the
    comment on it below.
    """
    screen = screen or {}
    interfaces = screen.get("screenInterfaces")
    if not isinstance(interfaces, list):
        interfaces = []

    size = screen.get("size") if isinstance(screen.get("size"), dict) else {}
    canvas_w = _int_or_none(size.get("width"))
    canvas_h = _int_or_none(size.get("height"))
    canvas = ({"width": canvas_w, "height": canvas_h}
              if canvas_w is not None and canvas_h is not None else None)

    mosaic_raw = screen.get("mosaic") if isinstance(screen.get("mosaic"), dict) else {}
    mosaic_row = _int_or_none(mosaic_raw.get("row"))
    mosaic_col = _int_or_none(mosaic_raw.get("column"))
    mosaic = ({"row": mosaic_row, "column": mosaic_col}
              if mosaic_row is not None or mosaic_col is not None else None)

    # Computed once, and deliberately not from `canvas` truthiness alone: a
    # `size` of {"width": 0, "height": 0} decodes to a perfectly well-formed
    # canvas dict whose area is 0, and every slot with no rectangles at all
    # would then "cover" it exactly (0 == 0). Zero area is mangled data, not a
    # canvas, so it is treated the same as R0405 having omitted `size`.
    canvas_area = (canvas["width"] * canvas["height"]) if canvas else None

    by_slot = {}
    online_flags = []
    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        slot = _int_or_none(iface.get("slotId"))
        entry = by_slot.setdefault(slot, {"interface_ids": [], "rects": []})
        iid = _int_or_none(iface.get("interfaceId"))
        if iid is not None:
            entry["interface_ids"].append(iid)
        rect = _rect(iface)
        if rect is not None:
            entry["rects"].append(rect)
        if iface.get("isCardOnline") is not None:
            online_flags.append(bool(iface.get("isCardOnline")))

    slots = []
    for slot in sorted(by_slot, key=lambda s: (s is None, s)):
        entry = by_slot[slot]
        rects = entry["rects"]
        covered = sum(w * h for (_, _, w, h) in rects)
        slots.append({
            "slot": slot,
            "outputs": len(entry["interface_ids"]) or len(rects),
            "interface_ids": sorted(entry["interface_ids"]),
            "regions": [{"x": x, "y": y, "width": w, "height": h}
                        for (x, y, w, h) in sorted(rects)],
            # Whether this slot's outputs, together, cover the whole canvas.
            # True for every slot on the observed wall — that IS the redundancy.
            #
            # THREE states, not two, and for the same reason as
            # `card_online_known` a few lines below: this used to be
            # `bool(canvas and rects and ...)`, which collapsed "we could not
            # check" into "this slot does not cover the canvas". Those are
            # different facts and they lead somewhere different. A slot that
            # genuinely covers only part of the canvas means the wall is NOT
            # redundant and losing that sender card takes a region of the
            # screen with it — worth telling an operator. A missing `size`
            # block (R0405 omitted it, or `width`/`height` came back
            # unparseable, both of which this decoder already tolerates
            # everywhere else) means nothing at all about redundancy, and
            # rendering it as "does not cover" invents a coverage gap on a
            # wall that may well be fully mirrored. None says "unknown" so a
            # caller can render "—" from an `is None` test, the way every
            # other unknown in this module is rendered.
            "covers_canvas": (None if not canvas_area or not rects
                              else covered == canvas_area),
        })

    all_rects = [r for entry in by_slot.values() for r in entry["rects"]]
    distinct_regions = len(set(all_rects))

    # Redundant when there is more than one slot and they all cover exactly the
    # same rectangles. Anything else (different regions per slot, ragged data)
    # falls through to "not redundant" and is reported at face value.
    region_sets = [frozenset(entry["rects"]) for entry in by_slot.values()
                   if entry["rects"]]
    redundant = (len(region_sets) > 1 and len(set(region_sets)) == 1)

    outputs_total = sum(s["outputs"] for s in slots)
    return {
        "screen_id": screen_id,
        "name": screen.get("name"),
        "canvas": canvas,
        "mosaic": mosaic,
        # Every physical output connection R0405 listed.
        "outputs_total": outputs_total,
        # Independently driven regions of the canvas — the honest "active
        # outputs" figure. Falls back to the raw total when the geometry is
        # missing, rather than reporting a confident 0.
        "active_outputs": distinct_regions or outputs_total,
        "distinct_regions": distinct_regions,
        "sender_slots": sorted(s["slot"] for s in slots if s["slot"] is not None),
        "slot_count": len(slots),
        "slots": slots,
        "redundant": redundant,
        # How many sender cards drive the same regions. 1 means "no redundancy
        # detected", never "one card confirmed driving".
        "redundancy_factor": len(region_sets) if redundant else 1,
        # False when isCardOnline was null on every interface, as it is here.
        "card_online_known": bool(online_flags),
    }


def wall_topology(state):
    """Live wall topology from a published device state, or None.

    This is the wall's identity as the processor reports it THIS poll — name,
    canvas, mosaic, outputs, sender slots. It is deliberately derived fresh
    from `state["screen_outputs"]` on every call and never merged with, or
    defaulted from, anything on disk: a stored file cannot know that the
    operator rebuilt the wall this morning.
    """
    if not isinstance(state, dict):
        return None
    screen_outputs = state.get("screen_outputs")
    if not isinstance(screen_outputs, dict) or not screen_outputs:
        return None

    screens = []
    for sid, screen in screen_outputs.items():
        if not isinstance(screen, dict):
            continue
        try:
            key = int(sid)
        except (TypeError, ValueError):
            key = sid
        screens.append(summarize_screen(key, screen))
    if not screens:
        return None

    screens.sort(key=lambda s: (not isinstance(s["screen_id"], int),
                                s["screen_id"] if isinstance(s["screen_id"], int) else 0))
    primary = screens[0]
    return {
        "source": "device",
        "screen_id": primary["screen_id"],
        "screen_name": primary["name"],
        "canvas": primary["canvas"],
        "mosaic": primary["mosaic"],
        "outputs_total": primary["outputs_total"],
        "active_outputs": primary["active_outputs"],
        "sender_slots": primary["sender_slots"],
        "slot_count": primary["slot_count"],
        "slots": primary["slots"],
        "redundant": primary["redundant"],
        "redundancy_factor": primary["redundancy_factor"],
        "card_online_known": primary["card_online_known"],
        "screens": screens,
        "screen_count": len(screens),
    }


def _blank_snmp_state():
    """The `snmp` block of device state before anything has been read.

    Every key is always present so the dashboard can render "—" from a
    `is None` test rather than having to know which fields a given firmware
    happens to expose. `available` is three-valued on purpose:

      None   nothing has been attempted yet (fresh device, or not H-series)
      True   the agent answered this cycle
      False  the agent did not answer

    and `unsupported` is the sticky version of that last one: the device has
    been asked SNMP_MAX_FAILS times and never once answered, so it is on
    firmware below V2.0.0.0 or has the agent switched off. The UI needs to
    tell "SNMP is off on this box" apart from "one poll timed out", because
    only the first is something an operator can go and fix.
    """
    return {
        "available": None,
        "unsupported": False,
        "read_at": None,
        "model": None,
        "firmware": None,
        "serial_number": None,
        "mac": None,
        "ip": None,
        "device_time": None,
        "temperature_status": None,
        "temperature_ok": None,
        "cpu_status": None,
        "fan_count": None,
        "psu_count": None,
        "fans": [],
        "psus": [],
        "failed_fans": [],
        "failed_psus": [],
        "disconnected_psus": [],
        "screens": {},
        "output": {},
    }


def _snmp_status_ok(value):
    """SNMP status field → True (0 = OK) / False (non-zero) / None (missing).

    Correct for the `Normal: 0` family ONLY — `.1.8` temperature, `.1.14` CPU,
    `.1.15` memory, `.1.16` fans, and the per-receiving-card statuses under
    `.30.7`. It is NOT a house rule. Polarity on this device is per-OID, and
    several fields are the other way round: `.1.11` genlock and `.1.13` system
    working status are `0: Not connected` / `0: Abnormal`, and the card-slot
    statuses `.20.2.1` / `.30.2.1` are `0: Abnormal` — see `_snmp_slot_ok`,
    which exists because this helper was applied to one of them.

    Kept local rather than imported from snmp_client, which exposes it as a
    private helper for its own decoders.
    """
    if value is None:
        return None
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return None


def _snmp_slot_ok(value):
    """Card-slot status (`.20.2.1` / `.30.2.1`) → True / False / None.

    INVERTED relative to `_snmp_status_ok`, which is the entire reason this is
    a separate function rather than a second caller. NovaStar's OID table
    gives the card-slot status as `0: Abnormal`, the opposite of the
    temperature / CPU / memory / fan statuses that are all `Normal: 0`.

    The table is the authority for that, and it is the ONLY authority. This
    docstring used to claim corroboration from the hardware: the `.30.3`
    output-card summary reads `{"SN":"","netPortCount":0,"status":1,
    "version":"0"}`, byte-identical on an H15 (V2.0.0.6) and an H2 (V2.2.0.0)
    while both were powered and driving a lit wall, so 1 looked like the value
    a healthy card reports. It is not evidence of that. NovaStar R&D, by email:
    that is the default summary returned for a slot with NO CARD FITTED, and we
    were querying input-side slot numbers — see `_snmp_output_is_populated`. The
    `status: 1` in it is the empty-slot default, not a healthy card's report,
    and neither device has yet answered this OID for a slot holding a card.

    Read through the `0 = OK` helper it was doing both halves of the wrong
    thing at once: a healthy 1 published `slot_ok: False`, which app.py turns
    into `Output card reports a fault (slot status 1)` — a CRITICAL, on every
    polling cycle, mid-show, on hardware that is fine — while a genuine 0
    (Abnormal) published `slot_ok: True` and said nothing at all.
    """
    if value is None:
        return None
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return None


# `_snmp_link_up()` used to live here, converting `.30.5.<n>` into "port n is
# linked". It is gone along with the entire ports-down alert. See
# `_apply_snmp_health` for why: `.30.5.x` is a field table describing ONE port,
# not an index over ports, so there was never a per-port link array to read.


# R0100 `cardType` for an output (sender) card. Input cards are 1 and empty
# slots are 0; both carry link blocks that mean something else.
OUTPUT_CARD_TYPE = 2


def _progress_reporter(callback, total, label):
    """Wrap a progress callback so a broken one cannot kill a read.

    Progress is cosmetic; the read is not. A callback that raises — a dead
    websocket, a client that vanished mid-sweep — must not abort a pass that
    takes minutes and cannot be retried without spending the controller's
    request budget all over again.
    """
    def report(done, phase, rest=None):
        if callback is None:
            return
        try:
            callback({"done": done, "total": total, "label": label,
                      "phase": phase, "rest_seconds": rest})
        except Exception:
            logger.debug("Progress callback failed", exc_info=True)
    return report


def _snmp_output_is_populated(card):
    """Whether the selected SNMP output slot is describing a real card.

    Returns False for the shape seen on the H15 at V2.0.0.6 and the H2 at
    V2.2.0.0: `.30.3` reads `{"SN":"","netPortCount":0,"status":1,"version":"0"}`
    on a lit wall passing traffic.

    That shape was read here as "the subtree is documented, present, and not
    populated" — a firmware that never fills it in. It is nothing of the kind.
    NovaStar R&D, by email: slot IDs are physical chassis positions with the
    input slots numbered first, and querying a slot with no card fitted returns
    exactly that default empty summary. We were reading slots 0..3, which on
    this chassis are not output positions at all — our H15's R0100 list puts its
    output cards at slots 20, 22, 24, 26, 28, 30, 32 and 34. So the emptiness is
    the documented answer to the question we asked, not a gap in the firmware.
    See the `.30.0` selector note in snmp_client for the full account and for
    why nothing here can re-ask with a better slot number: choosing a slot needs
    a SET, and the SNMP client is read-only by design.

    This function stays, and stays wired to the same caller, because the
    conclusion it draws is still right for the same practical reason: a summary
    with no serial, no version and zero ports is not describing a card we can
    judge, so the `slot_status` scalar arriving beside it is not a verdict about
    any card either. Any one piece of real card identity means a real card.

    Its only caller now is the `slot_ok` verdict. It used to take a `port_link`
    map as a second argument and cross-check it against the card's claimed port
    count; that argument is gone with the ports-down alert, because the map it
    checked never existed — see `_apply_snmp_health`. A positive `port_count`
    is therefore now taken at face value: it is a field the empty-slot summary
    leaves at 0, so a slot that reports one is a slot with a card in it.
    """
    if any(card.get(k) for k in ("serial_number", "firmware", "version",
                                 "sn")):
        return True
    port_count = card.get("port_count") or card.get("netPortCount")
    return isinstance(port_count, int) and not isinstance(port_count, bool) \
        and port_count > 0


class NovaStar_Device:
    """Represents a single NovaStar controller connection.

    State is double-buffered: the poll thread mutates a private draft
    (`self._draft`) and swaps a fresh snapshot into `self.state` when the
    cycle finishes, the same way demo_device does it. `self.state` is
    therefore always a complete, internally consistent dict that no thread
    will mutate — safe to serialize from a Flask request thread.
    """

    # Tells DeviceManager that `self.state` is already an immutable snapshot
    # and doesn't need a defensive copy before being serialized.
    publishes_state = True

    def __init__(self, device_id, name, ip, port=TCP_PORT,
                 snmp_community=SNMP_COMMUNITY, snmp_port=SNMP_PORT):
        self.device_id = device_id
        self.name = name
        self.ip = ip
        self.port = port
        self.sock = None
        self.connected = False
        self.lock = threading.Lock()
        self.seq = 0

        # Device type detection: H-series uses port 5203, VX1000 uses 5200
        self.device_type = "h_series" if port == H_TCP_PORT else "vx1000"

        # H-series JSON UDP client (port 6000). Used as the primary path for
        # H-series devices because it returns documented JSON instead of
        # reverse-engineered binary registers. Falls back to binary if the
        # JSON port doesn't respond within the timeout.
        self.json_client = (
            HSeriesJSONClient(ip, timeout=2.0)
            if self.device_type == "h_series" else None
        )
        # Tracks whether JSON UDP has worked at least once. If it never
        # responds we stop trying and stick with binary so we don't waste
        # 2s per poll cycle on dead UDP.
        self._json_ever_worked = False
        # Polls without a JSON response. Only gates the never-worked case —
        # see _json_should_try().
        self._json_consecutive_fails = 0
        self._json_max_fails = 3

        # H-series SNMPv2c monitor (UDP 161) — the routine health path. See
        # the policy note at the top of this module. Constructing it opens
        # nothing: SNMPClient creates its socket lazily on the first exchange,
        # so an H-series device that is never polled never touches the wire.
        # The client is the halt-aware subclass, so the global stop covers
        # this transport as well as the JSON and binary ones.
        self.snmp = (
            HSeriesSNMPMonitor(client=HaltAwareSNMPClient(
                ip, community=snmp_community, port=snmp_port))
            if self.device_type == "h_series" else None
        )
        # Same availability bookkeeping as the JSON path, and for the same
        # reason — see _snmp_should_try().
        self._snmp_ever_worked = False
        self._snmp_consecutive_fails = 0
        self._snmp_unavailable_logged = False
        # `_snmp_ports_linked` / `_snmp_expected_ports` used to live here: the
        # two halves of "which output ports SHOULD be linked right now", built
        # to suppress false alerts from the `.30.5` link map. Both are gone,
        # along with the alert and the link map, which was never a link map.
        # See `_apply_snmp_health`.

        # NOTE: there is deliberately no heartbeat thread here. See the
        # removal note in h_series_json.py — W0120 was implicated in the
        # outage that cost the operator control of the wall, and this monitor
        # never needed it.
        #
        # Cached (slot, port, card_id) inventory loaded from the snapshot.
        self._known_cards = None
        # Serializes on-demand per-card refreshes, which are called from
        # request threads rather than the poll thread.
        self._cards_lock = threading.Lock()
        # monotonic() of the last full sweep — enforces FULL_SWEEP_MIN_INTERVAL.
        self._last_full_sweep = None
        # (card_number, port, card_id) -> raw counter at the operator's zero
        # point. Empty means "show the controller's own cumulative counter".
        self._bit_error_baseline = {}
        self._percard_sock = None
        self._percard_seq = 0
        # Monotonic stamp for freshness decisions. The human-readable
        # `cards_read_at` is for display and is not a clock.
        self._cards_read_monotonic = None
        # kind -> {(card_number, port, card_id)} that did not answer last
        # pass and should be read first next time.
        self._unanswered = {}
        # PSU ids this process has observed connected at least once. See
        # `_psu_transitions`.
        self._psus_seen_connected = set()
        self._percard_pass_lock = threading.Lock()
        self._percard_pass_active = False
        # monotonic() of the last topology read; None means never.
        self._topology_read_at = None

        # Live state — `_draft` is the poll thread's working copy, `state`
        # (property) is the published snapshot.
        self._draft = {
            "device_id": device_id,
            "name": name,
            "ip": ip,
            "connected": False,
            "device_type": self.device_type,
            "last_poll": None,
            "poll_count": 0,
            "error": None,
            # Whether the global stop is engaged. Published with the rest of
            # the state so the dashboard can grey itself out and say WHY it
            # has stopped updating — during the outage the operator's only
            # signal that the monitor had gone quiet was that it had.
            "contact_halted": device_contact_halted(),
            # Routine health as read over SNMP. See _blank_snmp_state().
            "snmp": _blank_snmp_state(),
            "system_info": {},
            "device_info": {},
            # Unknown until a read succeeds — not "standby".
            "port2_active": None,
            # None, not 0. A confident "0%" on a lit wall is exactly the
            # false reading that moving brightness to R0401 was meant to fix;
            # the initialiser was still producing it before the first read,
            # and forever on a device that never answers.
            "brightness": None,
            "brightness_pct": None,
            "gamma": None,
            "datetime": "",
            "firmware_version": "",
            "live_monitoring": {},
            "video_status": {},
            "receiving_cards": [],
            # When the per-card readings were last taken. None until somebody
            # asks for them — they are an on-demand detail view now, so the UI
            # has to be able to say "not read yet" and "read at 20:14".
            "cards_read_at": None,
            # H-series port structure
            "ports": {},           # port_num -> {connected, card_count, cards}
            "port_bitmask": 0,     # raw bitmask from broadcast video status
            "active_ports": [],    # list of connected port numbers
            "sender_links": {},    # slot_id -> OPT/Ethernet link state
            "chain_breaks": [],    # suspected data breaks, from bit errors
            "history": {
                "temperature": [],
                "voltage": [],
                "timestamps": [],
            },
        }
        self._published = _snapshot(self._draft)

    # ── Published state ───────────────────────────────────

    @property
    def state(self):
        """The published state snapshot.

        Read-only by convention: writes land on a dict that the next
        `_publish()` replaces. Poll code must write to `self._draft`.
        """
        return self._published

    def _publish(self):
        # Freshness travels WITH the readings. Anything drawing a conclusion
        # from `receiving_cards` — an alert, a chart point — needs to know how
        # old they are, and a consumer that has only the state dict cannot ask
        # the device object.
        self._draft["cards_fresh"] = self.cards_are_fresh()
        """Swap a fresh snapshot of the draft into view (atomic rebind)."""
        self._published = _snapshot(self._draft)
        return self._published

    def set_error(self, message):
        """Record an error on the draft and publish it immediately."""
        self._draft["error"] = message
        self._publish()

    def publish_contact_state(self):
        """Re-publish so a halt/resume shows up without waiting for a poll.

        Called from the request thread that services `/api/halt`. A halted
        device stops polling, so nothing would otherwise republish and the
        dashboard would keep showing `contact_halted: false` for up to a full
        poll interval — on an emergency stop, mid-show. Best effort: the
        authority on whether contact is halted is `device_contact_halted()`,
        which the API answers from directly, so a failure here costs a stale
        flag on one device's state and nothing more. It is caught rather than
        raised because the operator's stop must not be able to 500.
        """
        try:
            self._draft["contact_halted"] = device_contact_halted()
            self._publish()
            return True
        except Exception:
            logger.exception("Could not republish contact state for %s", self.ip)
            return False

    # ── Connection ────────────────────────────────────────

    def connect(self):
        """Establish TCP connection."""
        if device_contact_halted():
            # The global stop means no packets, and a TCP connect is packets.
            return False
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.ip, self.port))
            self.connected = True
            self._draft["connected"] = True
            self._draft["error"] = None
            self._publish()
            return True
        except Exception as e:
            self.connected = False
            self._draft["connected"] = False
            self._draft["error"] = str(e)
            self._publish()
            return False

    def disconnect(self):
        """Close the TCP connection and release the JSON socket."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        # The JSON client holds a long-lived UDP socket of its own.
        if self.json_client:
            try:
                self.json_client.close()
            except Exception:
                pass
        # So does the SNMP client. Closing it sends nothing — SNMP has no
        # session to tear down, which is exactly why an abandoned poller
        # leaves nothing behind on the device.
        if self.snmp:
            try:
                self.snmp.close()
            except Exception:
                pass
        self.connected = False
        self._draft["connected"] = False
        self._publish()

    def read_register(self, reg_addr, reg_len, port=0x00):
        """Send a broadcast READ request and return the response payload."""
        if not self.connected or device_contact_halted():
            return None

        with self.lock:
            try:
                self.seq = (self.seq + 1) & 0xFFFF
                frame = build_read(self.seq, reg_addr, reg_len, port=port)
                self.sock.sendall(frame)
                # Use length-aware receive for large payloads
                data = self._recv_response(reg_len)
                result = parse_response(data)
                return result[1] if result else None
            except socket.timeout:
                return None
            except (ConnectionResetError, BrokenPipeError, OSError):
                self.connected = False
                self._draft["connected"] = False
                return None

    def read_register_card(self, reg_addr, reg_len, card_index, chain=0,
                           sender_card=0x00):
        """Send a per-card READ request targeting one card on one chain.

        `chain` is the 0-based daisy-chain index (byte[7], 0–15) and
        `card_index` the 0-based position of the card within that chain
        (byte[8]). `sender_card` is the 0-based sender card index in byte[5] —
        a *different* concept (H_SERIES_FINDINGS §6.5); every sender card is
        reached over the one connection to TCP 5201. Passing a chain number
        there was the pre-§6.5 mistake: every per-card read then addressed
        chain 0, so one chain's readings were attributed to all of them.
        VX1000 has a single sender card and a single chain, hence the defaults.
        """
        if not self.connected or device_contact_halted():
            return None

        with self.lock:
            try:
                self.seq = (self.seq + 1) & 0xFFFF
                frame = build_read_card(self.seq, reg_addr, reg_len,
                                        chain, card_index,
                                        sender_card=sender_card)
                self.sock.sendall(frame)
                data = self._recv_response(reg_len)
                result = parse_response(data)
                return result[1] if result else None
            except socket.timeout:
                return None
            except (ConnectionResetError, BrokenPipeError, OSError):
                self.connected = False
                self._draft["connected"] = False
                return None

    def _recv_response(self, reg_len):
        """Receive a response frame, reading enough bytes for the expected payload."""
        expected_payload = decode_length(reg_len)
        # header(18) + payload + checksum(2)
        expected_total = 18 + expected_payload + 2
        # Use a reasonable buffer: at least expected_total, but cap at 64KB
        buf_size = min(max(expected_total, 8192), 65536)
        return self.sock.recv(buf_size)

    def _ensure_tcp(self):
        """Bring the TCP socket up if it isn't already. Returns success."""
        return self.connected or self.connect()

    def poll(self):
        """Read all monitoring registers and publish the updated state."""
        # First gate, before anything else: the global stop. Checked here as
        # well as in DeviceManager's loop so a direct poll() — a test, a
        # future scheduler, a REPL — cannot get round it either.
        if device_contact_halted():
            return

        # H-series is polled over JSON UDP, which uses no TCP socket at all —
        # the binary fallback connects lazily instead. A busy or filtered
        # TCP 5203 therefore can't disable JSON monitoring, nor cost a 5 s
        # connect timeout every cycle on a device that answers fine on UDP.
        if self.device_type != "h_series" and not self._ensure_tcp():
            return

        now = datetime.now()
        try:
            self._draft["last_poll"] = now.strftime("%H:%M:%S")
            self._draft["poll_count"] += 1
            # We only get here when contact is allowed, so a cycle that runs
            # is itself proof the stop is clear.
            self._draft["contact_halted"] = False

            if self.device_type == "h_series":
                self._poll_h_series(now)
            else:
                self._poll_vx1000(now)
        finally:
            # Publish whatever we got, even if a read raised — the caller
            # (DeviceManager) reports the error separately.
            self._publish()

    # ── VX1000 Polling ────────────────────────────────────

    def _poll_vx1000(self, now):
        """Poll a VX1000-type controller (port 5200)."""
        # System Info
        data = self.read_register(*REG_SYSTEM_INFO)
        if data:
            info = parse_system_info(data)
            if info:
                self._draft["system_info"] = info

        # Firmware Version
        data = self.read_register(*REG_FIRMWARE)
        if data and len(data) >= 2:
            self._draft["firmware_version"] = f"{data[0]}.{data[1]}"

        # Device Info (NSSD) Port 1
        data = self.read_register(*REG_DEVICE_PORT1)
        if data:
            info = parse_nssd(data)
            if info:
                self._draft["device_info"] = info

        # Port 2 status
        data = self.read_register(*REG_DEVICE_PORT2)
        # None when the read failed. `bool(None)` is False, which the UI
        # rendered as a positive "○ Standby" — a statement about redundancy
        # state made from a timeout.
        self._draft["port2_active"] = (
            any(b != 0 for b in data[:10])
            if data and len(data) > 4 else None)

        # Common registers
        self._poll_common_registers()

        # Live Monitoring — broadcast read kept for backward compat; per-card reads below
        # are the authoritative source for temperature/voltage/link data.
        data = self.read_register(*REG_LIVE_MONITOR)
        if data:
            mon = parse_live_monitoring(data)
            if mon:
                self._draft["live_monitoring"] = mon

        # Video Status
        data = self.read_register(*REG_VIDEO_STATUS)
        if data and len(data) >= 20:
            first = data[0]
            if first == 0x1C:
                self._draft["video_status"] = {
                    "format": "receiving_card",
                    "signal_detected": bool(data[14]),
                    "input_valid": bool(data[15]),
                }
            elif first == 0x00 and len(data) > 22:
                self._draft["video_status"] = {
                    "format": "sending_card",
                    "timestamp": (
                        f"20{data[17]:02d}-{data[18]:02d}-{data[19]:02d} "
                        f"{data[20]:02d}:{data[21]:02d}:{data[22]:02d}"
                    ),
                }

        # Per-card live monitoring — query each receiving card directly.
        # A VX1000 has a single chain, so chain stays 0 and only the card
        # index varies.
        detected_count = self._draft.get("_detected_card_count", 0)
        scan_limit = detected_count if detected_count > 0 else 16
        cards = []
        for i in range(scan_limit):
            cdata = self.read_register_card(*REG_LIVE_MONITOR, i)
            mon = parse_live_monitoring(cdata) if cdata else None
            if mon and mon.get("online"):
                if detected_count == 0 and mon.get("card_count", 0) > 0:
                    detected_count = mon["card_count"]
                    self._draft["_detected_card_count"] = detected_count
                    scan_limit = detected_count
                cards.append({
                    "index": i,
                    "label": f"C{i + 1:02d}",
                    "online": True,
                    "answered": True,
                    "reading": "ok",
                    "temperature_c": mon["temperature_c"],
                    "voltage_v": mon["voltage_v"],
                    "link_status": mon["link_status"],
                    "link_raw": mon["link_raw"],
                    "firmware": mon["firmware"],
                    "mac_address": mon["mac_address"],
                })
                continue

            # THREE states, not two. This branch used to publish a flat
            # `online: False` for every way of not getting an answer, and
            # `read_register_card` returns None for a socket.timeout and for a
            # socket that has already been torn down by a ConnectionReset —
            # so a VX1000 whose TCP session dropped mid-cycle published every
            # one of its cards as OFFLINE. That is the same false-fault this
            # module already had to fix on the H-series JSON path (see
            # `_decode_cards`) and in `_merge_live_readings`: nobody can tell a
            # dark wall from a dead socket by looking at the dashboard, so the
            # dashboard must not claim to know.
            #
            #   cdata is None  the controller said nothing — timeout, or the
            #                  socket died. Says NOTHING about the card.
            #   undecodable    bytes came back but too few to parse. Also says
            #                  nothing about the card, just about the frame.
            #   absent         the controller answered and the live-monitoring
            #                  status byte has the present bit clear. THIS is
            #                  a real measurement: the sender card is telling
            #                  us there is no card reporting at that address,
            #                  which is also what the addresses past the end
            #                  of a short chain look like during the initial
            #                  16-deep discovery scan.
            #
            # Only the third is knowable, so only the third gets False. The
            # first two get None and are rendered as unknown; `reading` says
            # which so an operator can tell "the monitor lost the controller"
            # from "that slot is empty".
            if cdata is None:
                online, reading = None, "no_answer"
            elif mon is None:
                online, reading = None, "undecodable"
            else:
                online, reading = False, "absent"
            cards.append({"index": i, "label": f"C{i + 1:02d}",
                          "online": online, "answered": cdata is not None,
                          "reading": reading})
        if cards:
            self._draft["receiving_cards"] = cards
        self._update_aggregates(now, cards)

    # ── H-Series Polling ──────────────────────────────────

    def _json_should_try(self):
        """Whether to attempt the JSON UDP path this cycle.

        Once JSON has answered even once, transient failures never disable it
        — a few timed-out cycles on a busy network must not permanently
        downgrade a device to the binary path. The fail counter only gates the
        case where JSON has *never* worked (old firmware / filtered UDP),
        where retrying forever would burn a full timeout every cycle.
        """
        if not self.json_client or device_contact_halted():
            return False
        if self._json_ever_worked:
            return True
        return self._json_consecutive_fails < self._json_max_fails

    # ── SNMP health (the routine path) ─────────────────────

    def _snmp_should_try(self):
        """Whether to attempt an SNMP read this cycle.

        Mirrors `_json_should_try` deliberately, including its asymmetry: once
        the agent has answered even once, transient misses never disable it,
        because a couple of dropped datagrams on a busy network must not
        permanently downgrade a device to the control-protocol path. The fail
        counter only gates a device that has NEVER answered — agent switched
        off, or firmware below the V2.0.0.0 the SNMP spec requires — where
        retrying forever costs a timeout every cycle for nothing.
        """
        if not self.snmp or device_contact_halted():
            return False
        if self._snmp_ever_worked:
            return True
        return self._snmp_consecutive_fails < SNMP_MAX_FAILS

    def _poll_snmp(self):
        """Read chassis health, screens and output status over SNMP.

        Returns True if the agent answered, which is also this device's
        reachability verdict for the cycle. Three bounded walks; a device with
        no agent costs exactly one timeout, because `walk` stops at the first
        GETNEXT that goes unanswered.

        The input subtree (.20) is deliberately not read: nothing on the
        dashboard renders it, and an unread OID is a datagram not sent.
        """
        if not self._snmp_should_try():
            return False

        health, screens, output = {}, {}, {}
        try:
            health = self.snmp.get_device_health()
            # A dead agent still yields a well-formed dict of Nones, so
            # "answered" has to be judged on content. Identity or a decoded
            # fan/PSU array is enough; anything less is not a reading.
            answered = bool(health.get("model") or health.get("serial_number")
                            or health.get("fans") or health.get("psus"))
            if answered:
                screens = self.snmp.get_screens()
                output = self.snmp.get_output_status()
        except Exception:
            # snmp_client's house rule is that nothing reaches the caller as
            # an exception, and its tests enforce that. Caught anyway: a poll
            # thread that dies on one unexpected value takes the whole
            # device's monitoring with it, and this path runs unattended
            # during a show.
            logger.exception("SNMP read failed for %s", self.ip)
            answered = False

        if not answered:
            self._note_snmp_miss()
            return False

        self._snmp_ever_worked = True
        self._snmp_consecutive_fails = 0
        self._apply_snmp_health(health, screens, output)
        return True

    def _note_snmp_miss(self):
        """Record a cycle where SNMP produced nothing, and say so exactly once.

        `unsupported` only latches for a device that has never answered — for
        one that has, a miss is a miss and the next cycle may well succeed.
        """
        self._snmp_consecutive_fails += 1
        snmp = self._draft["snmp"]
        snmp["available"] = False
        unsupported = (not self._snmp_ever_worked
                       and self._snmp_consecutive_fails >= SNMP_MAX_FAILS)
        snmp["unsupported"] = unsupported
        if unsupported and not self._snmp_unavailable_logged:
            # Once per device, not once per cycle. A wall whose splicer has no
            # SNMP agent would otherwise write this line every poll interval
            # for the length of the show, and a log that scrolls is a log
            # nobody reads when something actually goes wrong.
            self._snmp_unavailable_logged = True
            logger.warning(
                "No SNMP answer from %s after %d attempts — routine health "
                "falls back to the JSON reachability read. SNMP needs "
                "firmware V2.0.0.0+ with the agent enabled.",
                self.ip, self._snmp_consecutive_fails)

    # ── PSU: alert on a transition, not on a state ─────────
    #
    # `iSignal 0` means "not connected to power" (NovaStar R&D, by email). On a
    # chassis with every bay populated that is a real fault. On one with spare
    # bays it is what an EMPTY BAY reports, and this field cannot tell the two
    # apart — so alerting on the absolute state would raise a CRITICAL every
    # polling cycle, for the lifetime of the show, about a bay that has never
    # had a supply in it.
    #
    # That is the same mistake as the SNMP "output port link lost" alert that
    # was removed from this file: a steady-state reading treated as an event.
    # A supply that was connected and then ISN'T is unambiguous, needs no
    # knowledge of how many bays are fitted, and is exactly the thing worth
    # waking somebody for mid-show. So the alert fires on the 1 -> 0 edge only.
    #
    # Per-process, deliberately: on restart nothing is "previously seen", so a
    # bay that is already at 0 stays quiet until it is observed connected. That
    # loses a supply that died before the monitor started, which is the safer
    # of the two failure modes — the alternative fires on every empty bay.

    def _psu_transitions(self, psus):
        """Supplies observed going connected -> not connected. Ids only."""
        dropped = []
        for psu in psus or []:
            pid = psu.get("power_id")
            if pid is None:
                continue
            connected = psu.get("connected")
            if connected is True:
                self._psus_seen_connected.add(pid)
            elif connected is False and pid in self._psus_seen_connected:
                dropped.append(pid)
        return sorted(dropped)

    def _apply_snmp_health(self, health, screens, output):
        """Publish one SNMP read into device state.

        Only `status` / `ok` fields become health verdicts. The per-fan
        `speed` and per-PSU `voltage` the device reports are not implemented
        over SNMP at all — NovaStar R&D, by email: "The device's fan speed and
        power supply voltage are not currently provided by the SNMP protocol.
        If you require this data, it needs to be customized." They read 0 on
        EVERY fan and EVERY supply because there is nothing behind them, not
        because a scale factor is missing; snmp_client surfaces them as
        `speed_raw` / `voltage_raw` and they are passed through here for
        display only. Nothing downstream may threshold them, and no firmware
        upgrade makes them worth revisiting.

        Thresholding a number whose meaning is not established has produced a
        wall of alerts on healthy hardware twice in this codebase already: the
        workStatus placeholder zeros, and the 4.7 V low-voltage floor that sat
        above the entire range these receiving cards actually report. This
        docstring used to cite "the masked voltage formula" as the second
        example. That was wrong twice over — the masked form `(raw & 0x7F) *
        0.1` is what NovaStar's H Series control protocol documents (§4.3.4 and
        §5.4.2, identical in V1.0.18 and V1.0.20), and the alerts it appeared
        to cause came from the threshold, not the decode.
        """
        # ── The output-port alert, and why there isn't one ──────────────────
        #
        # There used to be a CRITICAL here: "Output port N: link lost — this
        # port was carrying panels and is now reporting no link". It is gone,
        # removed rather than tuned, and it cannot come back in this form.
        #
        # It was built on a misreading of `.30.5.x`. That subtree was decoded
        # as `port_link[N]` — a map of port number → link state — with the
        # missing `.5.2` written off as "a gap is normal". NovaStar's official
        # OID table says it is a FIELD TABLE describing the ONE (slot, port)
        # selected by a `.30.4` SET, exactly like the sibling `.20.5.x` input
        # table this codebase already decodes correctly:
        #
        #   .30.5.1  link status of the Ethernet port
        #   .30.5.3  working status of the BACKUP port   0 inactive / 1 active
        #   .30.5.4  link status of the BACKUP port      0 not linked / 1 linked
        #
        # Our H15 answered `{1: 0, 3: 0, 4: 0}`. The old code read that as
        # "three of sixteen ports report no link" and raised a false CRITICAL
        # on a healthy, lit wall. Read correctly it says: primary link 0,
        # backup inactive, backup not linked — and the last two are exactly
        # what a wall with an idle backup is supposed to report. The alert was
        # never firing on a fault; it was firing on a healthy backup.
        #
        # Everything that existed only to defend that alert went with it:
        # `_snmp_link_up`, `_snmp_ports_linked`, `_expected_linked_ports()`
        # and the enumeration-snapshot seeding, plus the `ports_down` /
        # `ports_expected_linked` state keys and app.py's breach loop. They
        # were suppression machinery for false positives from a reading that
        # was wrong at the root — and none of them can be repaired, because
        # there is no per-port link array anywhere in this subtree to alert on.
        # Reinstating a per-port output alert needs a real per-port source
        # (`.30.4` SET-and-read, which a read-only client will not do, or the
        # per-card R0155 path that already backs `chain_breaks`).
        #
        # The three fields are still PUBLISHED, raw, under `output["port"]`,
        # so the data an operator can inspect is unchanged. Nothing derives a
        # verdict from them: which port they describe depends on a `.30.4`
        # selection we never make, so we do not know whose link status this is.
        port_fields = dict(output.get("port") or {})
        card = output.get("card") or {}

        # The slot verdict is gated on whether the selected `.30` slot holds a
        # card at all. A summary with no serial, no firmware and netPortCount 0
        # is the documented answer for a slot with nothing fitted (NovaStar
        # R&D, by email — see `_snmp_output_is_populated`), so the scalar
        # arriving beside it describes no card, and reading its 0 as "Abnormal"
        # would be a false CRITICAL of exactly the kind the removed port alert
        # turned out to be. None, not False: unknown is not an all-clear
        # either. The raw `slot_status` is published either way, so nothing is
        # hidden.
        slot_ok = (_snmp_slot_ok(card.get("slot_status"))
                   if _snmp_output_is_populated(card) else None)

        self._draft["snmp"] = {
            "available": True,
            "unsupported": False,
            "read_at": datetime.now().strftime("%H:%M:%S"),
            "model": health.get("model"),
            "firmware": health.get("firmware"),
            "serial_number": health.get("serial_number"),
            "mac": health.get("mac"),
            "ip": health.get("ip"),
            "device_time": health.get("device_time"),
            "temperature_status": health.get("temperature_status"),
            "temperature_ok": health.get("temperature_ok"),
            "cpu_status": health.get("cpu_status"),
            "fan_count": health.get("fan_count"),
            "psu_count": health.get("psu_count"),
            "fans": health.get("fans") or [],
            "psus": health.get("psus") or [],
            "failed_fans": health.get("failed_fans") or [],
            # These two now hold the same power ids by construction: PSU `ok`
            # is the `iSignal`-derived `connected` flag (snmp_client.parse_psus),
            # so "failed" here means the device reports the supply as NOT
            # CONNECTED TO POWER — NovaStar R&D, by email, on `.1.17`'s
            # `iSignal`: "0: not connected to power, 1: connected to power".
            # `disconnected_psus` is kept as the name that says what the value
            # means; `failed_psus` is the key app.py and app.js already read.
            #
            # This is a live alert path, not display-only. It used to be
            # unreachable — PSU `ok` was never False while it came from the
            # undocumented `status` field — and the comment here said "never
            # alarmed on", which is no longer true. `_snmp_breaches` raises a
            # CRITICAL per supply off `ok is False`; it fires only on a
            # documented `iSignal: 0`, never on an unexplained `status`.
            "failed_psus": health.get("failed_psus") or [],
            "disconnected_psus": health.get("disconnected_psus") or [],
            # The alertable subset: supplies watched go 1 -> 0. See
            # `_psu_transitions` for why the absolute state is not alertable.
            "dropped_psus": self._psu_transitions(health.get("psus")),
            "screens": {
                "screen_count": screens.get("screen_count"),
                # Named nothing, on purpose: the capture has five described
                # values for seven OIDs, and a guessed name on a monitoring
                # field gets read downstream as fact. The UI labels them by
                # suffix or not at all.
                "fields": screens.get("fields") or {},
            },
            "output": {
                "card_count": output.get("card_count"),
                "slot_status": card.get("slot_status"),
                "slot_ok": slot_ok,
                "firmware": card.get("firmware"),
                "serial_number": card.get("serial_number"),
                "port_count": card.get("port_count"),
                # The `.30.5.x` field table, raw and unjudged:
                # `link_status` / `backup_working` / `backup_link` for ONE
                # port, chosen by a `.30.4` SET we do not issue. Displayed,
                # never alarmed on — see the long note above. An absent key is
                # a field the walk did not return, which is unknown, not zero.
                "port": port_fields,
            },
        }

        # SNMP reports the device's real firmware ("V2.0.0.6"); the JSON path
        # could only offer the control protocol's version. Same field, better
        # source, so the dashboard header stops lying about what is installed.
        firmware = health.get("firmware")
        if firmware:
            self._draft["firmware_version"] = firmware

        # `active_ports` is NOT set from here any more. It used to be derived
        # from the `.30.5` "link map", which was a field table — so on the H15
        # it was publishing a list of ports built from `{link_status,
        # backup_working, backup_link}` keys read as port numbers. The JSON
        # `is_used` walk in `_apply_r0100()` and the video-status bitmask are
        # the two honest sources, and they are left to own the field alone.

    def _poll_h_series(self, now):
        """Poll an H-series controller.

        Three transports, in descending order of how much they can disturb the
        device:

        1. SNMPv2c (UDP 161) — read-only, claims no controller role, coexists
           with Companion. The routine health path.
        2. JSON UDP (port 6000) — the documented *control* protocol. Fallback
           reachability only, for a device with no SNMP agent.
        3. Binary (TCP 5203) — reverse-engineered, last resort.

        When SNMP answers, the control protocol gets nothing at all on a
        routine cycle. See `_poll_snmp` for why the R0100 that used to run
        here every cycle is no longer sent.
        """
        # ── SNMP first: the whole point of adding it ──────────────────
        if self._poll_snmp():
            # SNMP answered, so identity, firmware, chassis health, port link
            # state and therefore reachability are all covered. R0100 supplied
            # identity + reachability and nothing else routine (the topology
            # reads below are separately cached), so sending it as well would
            # be a control-plane datagram bought for no information at all.
            self._draft["connected"] = True
            self._draft["error"] = None
            self._refresh_topology()
            self._poll_screen_brightness()
            self._sample_history(now)
            return

        # ── No SNMP: fall back to the JSON reachability read ──────────
        # A device with the agent off or firmware below V2.0.0.0 must still be
        # monitored, so R0100 lives on down here rather than being deleted.
        if self._json_should_try():
            r0100 = self.json_client.get_device_details()
            if r0100 is not None:
                self._json_ever_worked = True
                self._json_consecutive_fails = 0
                self._poll_h_series_json(now, r0100)
                return
            self._json_consecutive_fails += 1
            if (self._json_consecutive_fails >= self._json_max_fails
                    and not self._json_ever_worked):
                self._draft["error"] = "JSON UDP timeout — falling back to binary"

        # ── Binary fallback (legacy path) ─────────────────────────────
        # Only this path needs the socket, so it's connected here rather than
        # up front — JSON never waits on TCP.
        if not self._ensure_tcp():
            return
        self._poll_h_series_binary(now)

    def _poll_h_series_json(self, now, r0100):
        """Populate state from the JSON UDP protocol.

        The fallback path for a device with no SNMP agent — on a device that
        answers SNMP this never runs, because `_poll_h_series` returns before
        it. Even here the cycle is deliberately tiny: the one R0100 the caller
        has already sent, and nothing else. Two things used to happen here
        that no longer do:

        · the W0120 heartbeat was started from here (removed — see the note in
          h_series_json.py), and
        · every cycle swept ~1374 receiving cards with R0155. Per-card reads
          are now on demand only (`refresh_chain` / `refresh_cards` /
          `refresh_all_cards`); routine health monitoring is SNMP's job.

        The topology reads (R0400 screen list, R0405 per-screen output info,
        R0300 output list) survive, but cached — they describe how the wall is
        wired, which does not change during a show, so re-reading them every
        cycle was several datagrams spent re-learning a constant. In steady
        state this method sends nothing at all.
        """
        # The device answered on UDP, so it is reachable regardless of what
        # TCP 5203 is doing. `self.connected` stays the TCP socket's own flag;
        # state["connected"] means "the controller is talking to us".
        self._draft["connected"] = True
        self._draft["error"] = None

        details = parse_device_details(r0100) or {}
        self._draft["device_info"] = {"source": "json_udp", **details}
        if details.get("proto_version"):
            self._draft["firmware_version"] = details["proto_version"]

        # Walk slots → interfaces, count online ports for high-level monitor.
        active_ports = []
        for slot in details.get("slots", []):
            for iface in slot.get("interfaces", []):
                if iface.get("is_used"):
                    active_ports.append(iface.get("interface_id"))
        self._draft["active_ports"] = sorted(p for p in active_ports if p is not None)

        self._refresh_topology()
        self._poll_screen_brightness()
        self._sample_history(now)

    def _sample_history(self, now):
        """Append one history point per poll cycle from the cards we have.

        The temperature and voltage charts were permanently empty on
        H-series: history is only appended by `_update_aggregates(
        record_history=True)`, and no H-series poll path reached it — the
        SNMP and JSON branches both return before it, and the binary fallback
        aggregates over `ports`, which the poll loop stopped populating.

        This samples the per-card readings the app already holds. It sends
        NOTHING: per-card reads stay on demand, so the series is a record of
        what was known at each cycle rather than a reason to poll the wall.
        A cycle with no readings records a gap, which is the honest shape for
        "nobody has read the cards recently".
        """
        cards = self._draft.get("receiving_cards") or []
        if not self.cards_are_fresh():
            # Readings exist but are old. Re-sampling them every cycle would
            # draw a flat line at values nobody has confirmed since — an
            # outage would render as "steady 40 C" rather than as a gap.
            cards = []
        self._update_aggregates(now, cards, record_history=True)

    def cards_are_fresh(self, max_age=CARD_READING_MAX_AGE):
        """Whether the per-card readings are recent enough to act on.

        Per-card reads are on demand, so `receiving_cards` can hold values
        from hours ago. Anything that draws a conclusion from them — a chart
        point, an alert — has to know that, otherwise a card that read 76 C
        during a sweep at 14:00 keeps raising CRITICAL long after it was
        fixed, powered down, or unplugged.
        """
        stamp = self._cards_read_monotonic
        if stamp is None:
            return False
        return (time.monotonic() - stamp) <= max_age

    def _poll_screen_brightness(self):
        """Read screen brightness from R0401. One datagram per cycle.

        Brightness is NOT cached with the topology: an operator changes it
        during a show (from Companion, here), so a value read once at startup
        would be wrong within minutes.

        This replaces reading binary register H_REG_BRIGHTNESS, which reports
        0 on H-series — the dashboard showed "0%" on a wall that was lit.
        R0401 reports 10 where the receiving cards report a raw 25 (25/255 =
        9.8%), so the screen value is already a percentage and the per-card
        value is 0-255. Two scales, and only the screen one is authoritative
        for "what is this wall set to".
        """
        if not self.json_client or device_contact_halted():
            return
        screen_id = self._primary_screen_id()
        if screen_id is None:
            return
        details = self.json_client.get_screen_details(screen_id)
        if not isinstance(details, dict):
            return
        raw = details.get("brightness")
        if not isinstance(raw, (int, float)):
            return
        self._draft["brightness_pct"] = round(float(raw), 1)
        # Keep the 0-255 field populated in the shape the UI already expects,
        # derived from the percentage rather than from a register that lies.
        self._draft["brightness"] = int(round(float(raw) * 255 / 100))
        self._draft["brightness_source"] = "R0401"

    def _primary_screen_id(self):
        """The screen whose brightness the dashboard reports.

        Multi-screen chassis are possible but this app has only ever seen one
        configured screen, so the lowest id is used rather than inventing a
        selection UI for a case that has not come up.
        """
        outputs = self._draft.get("screen_outputs") or {}
        ids = []
        for sid in outputs:
            try:
                ids.append(int(sid))
            except (TypeError, ValueError):
                continue
        if ids:
            return min(ids)
        screens = ((self._draft.get("screen_list") or {}).get("screens")
                   if isinstance(self._draft.get("screen_list"), dict) else None)
        for scr in (screens or []):
            if scr.get("screenId") is not None:
                return scr["screenId"]
        return None

    # ── Topology (cached) ──────────────────────────────────

    def _topology_is_stale(self):
        """Whether the cached topology is old enough to be worth re-reading."""
        if self._topology_read_at is None:
            return True
        return (time.monotonic() - self._topology_read_at) \
            >= TOPOLOGY_REFRESH_INTERVAL

    def refresh_topology(self, force=True):
        """Re-read screen/output topology (R0400, R0405 per screen, R0300).

        A handful of reads, not per-card, so this is cheap enough to expose
        directly — but it is still traffic, hence the global-stop check and
        the staleness gate the poll loop uses. `force=False` is the poll
        loop's call: read only if the cache has aged out.
        """
        if not self.json_client or device_contact_halted():
            return False
        if not force and not self._topology_is_stale():
            return False

        # R0400 — screen list (just names + IDs, not actual layout).
        screen_list = self.json_client.get_screen_list()
        if screen_list is not None:
            self._draft["screen_list"] = screen_list

        # R0405 — per-screen output info. This is where the real
        # topology lives: each output's pixel position, slot, interface,
        # and primary/backup pairing. Fan out one call per screen.
        screen_outputs = {}
        if isinstance(screen_list, dict):
            for scr in (screen_list.get("screens") or []):
                sid = scr.get("screenId")
                if sid is None:
                    continue
                info = self.json_client.get_screen_output_info(sid)
                if info is not None:
                    screen_outputs[sid] = {
                        "name": scr.get("name", f"Screen {sid}"),
                        "size": info.get("size"),
                        "mosaic": info.get("mosaic"),
                        "screenInterfaces": info.get("screenInterfaces", []),
                    }
        if screen_outputs:
            self._draft["screen_outputs"] = screen_outputs

        # R0300 — output list. Lightweight enumeration.
        output_list = self.json_client.get_output_list()
        if output_list is not None:
            self._draft["output_list"] = output_list

        # R0100 — for the per-slot OPT/Ethernet link blocks. This is the only
        # place that says whether a sender card is running fibre or copper,
        # and without it the wall map labels every chain "OPT n" including the
        # cards patched straight out of the Ethernet ports. It belongs here
        # rather than in the poll cycle because it describes wiring, which
        # changes when somebody re-patches, not during a show.
        #
        # On a device answering SNMP this is the ONLY R0100 sent — the poll
        # loop's was removed after the outage — so it is one cached datagram,
        # not a per-cycle cost.
        self._refresh_sender_links()

        self._topology_read_at = time.monotonic()
        return True

    def _refresh_sender_links(self):
        """Cache each output card's OPT / Ethernet link state from R0100.

        Then one R0102 per output card for its `linkstatus` — the cable and
        redundancy state NovaStar pointed at for detecting primary/backup
        switching. It lands on the same per-slot dict under
        `slot_link_status`, so nothing downstream has to change to keep
        working. That key is deliberately NOT called `link_status`: two
        different things already answer to that name — the binary per-card
        PRIMARY/BACKUP string, and the SNMP `.30.5.1` integer — and a third
        meaning under the same name would be misread.
        """
        r0100 = self.json_client.get_device_details()
        details = parse_device_details(r0100) if r0100 is not None else None
        if not details:
            return
        links = {}
        for slot in details.get("slots", []):
            slot_id = slot.get("slot_id")
            # cardType 2 is an output (sender) card; input cards have link
            # blocks too and they mean something else.
            if slot_id is None or slot.get("card_type") != OUTPUT_CARD_TYPE:
                continue
            out = slot.get("output_links") or {}
            if out.get("medium") is None and not out.get("opt") \
                    and not out.get("ethernet"):
                continue
            links[slot_id] = out
        if links:
            self._refresh_slot_link_status(links)
            self._draft["sender_links"] = links

    def _refresh_slot_link_status(self, links):
        """Merge R0102 `linkstatus` into each entry of `links`, in place.

        One datagram per output card, inside the topology refresh, so it
        inherits the 600 s cache rather than adding to the poll cycle. On a
        four-card chassis that is four datagrams every ten minutes.

        A slot that does not answer, or answers something this can't read, is
        left without the key rather than given a default — absent means "not
        reported", and the UI has to be able to tell that from "reported down".

        Connector 0 only. Whether `linkstatus` is one value per card or one per
        connector is unknown until a reply is captured; if it turns out to be
        per-connector this needs a sweep of 0..3 here.
        """
        for slot_id, entry in links.items():
            try:
                r0102 = self.json_client.get_slot_info(slot_id)
            except Exception:
                logger.debug("R0102 failed for slot %s", slot_id,
                             exc_info=True)
                continue
            parsed = parse_slot_info(r0102) if r0102 is not None else None
            if parsed and parsed.get("links"):
                entry["slot_link_status"] = parsed

    def _refresh_topology(self):
        """Poll-loop entry point: re-read topology only if the cache aged out."""
        return self.refresh_topology(force=False)

    # ── Per-card reads (ON DEMAND ONLY) ────────────────────
    #
    # Nothing below this line may be called from a poll path. See the
    # PER_CARD_SWEEP policy notes at the top of this module: the automatic
    # ~1374-card R0155 sweep is one of the two behaviours implicated in the
    # outage that cost the operator control of the wall mid-show.

    def known_cards(self):
        """The cached (slot, port, card_id) inventory, loaded on first use.

        Pure bookkeeping — reads the enumeration snapshot from disk, never the
        device. A UI can call this to find out what there is to inspect
        without sending anything.
        """
        if not self._known_cards:
            self._known_cards = self._load_known_cards_from_snapshot()
        return self._known_cards

    def known_chains(self):
        """Sorted (slot, port) pairs in the inventory — the inspectable units.

        This is what a "pick a chain to inspect" UI enumerates, and what
        `refresh_chain()` takes.
        """
        return sorted({(c["slot"], c["port"]) for c in self.known_cards()})

    def refresh_cards(self, addresses, limit=PER_CARD_SWEEP_LIMIT):
        """R0155 an explicit, bounded list of (slot, port, card_id) addresses.

        This is the on-demand detail view: the caller names exactly which
        cards it wants, the count is capped at `limit`, and the result is
        merged into the published `receiving_cards` so the dashboard shows the
        fresh readings for those cards and the (older) readings for the rest.

        Returns the refreshed card dicts. Empty if the global stop is engaged,
        if there is no JSON client, or if `addresses` is empty. Addresses over
        `limit` are dropped with a warning rather than silently sent — the cap
        exists so a bad caller cannot reconstruct the full sweep.
        """
        if not self.json_client or device_contact_halted():
            return []
        wanted = [tuple(a) for a in (addresses or [])]
        if not wanted:
            return []
        if len(wanted) > limit:
            logger.warning(
                "Per-card refresh for %s asked for %d addresses; capped at %d "
                "— per-card reads are an on-demand detail view, not a sweep",
                self.ip, len(wanted), limit)
            wanted = wanted[:limit]

        # Carry each address's identity fields (card_number, user_slot, …)
        # over from the inventory so a refreshed card still renders with its
        # operator-facing labels.
        by_address = {(c["slot"], c["port"], c["card_id"]): c
                      for c in self.known_cards()}
        entries = [by_address.get(a, {"slot": a[0], "port": a[1],
                                      "card_id": a[2]})
                   for a in wanted]
        return self._read_cards(entries)

    def refresh_chain(self, slot, port, limit=PER_CARD_SWEEP_LIMIT):
        """R0155 every known card on one (slot, port) chain.

        The bounded unit a future "inspect this chain" UI asks for. A chain is
        tens of cards, not ~1374, so this is a fraction of the traffic the
        automatic sweep produced and it only happens when somebody asks.
        """
        addresses = [(c["slot"], c["port"], c["card_id"])
                     for c in self.known_cards()
                     if c["slot"] == slot and c["port"] == port]
        if not addresses:
            logger.info("No known cards on %s chain (slot %s, port %s)",
                        self.ip, slot, port)
            return []
        return self.refresh_cards(addresses, limit=limit)

    def refresh_bit_errors(self, cards=None, limit=PER_CARD_SWEEP_LIMIT,
                           progress=None):
        """Read the per-card bit-error counter for an explicit card list.

        Bit errors are binary-only — R0155 has no equivalent — so this is the
        one reading the JSON path cannot supply, and until now the app had no
        way to read it at all: the wall map's "Bit Errors" mode was colouring
        cells from whatever the last enumeration happened to record, which on
        a wall left running is hours stale.

        Read-only and on demand, with the same protections as every other
        per-card path: refuses while contact is halted, caps the address list,
        and paces itself because the controller stops answering after a few
        hundred per-card reads (see H_SERIES_FINDINGS §6.6).

        `cards` is a list of dicts carrying `card_number`, `port` and
        `card_id` — the binary addressing, not R0155's (slot, port, card).
        Defaults to the whole known inventory, which is why the cap matters.

        Returns the list of `{card_number, port, card_id, bit_errors,
        bit_errors_saturated}` that answered.
        """
        if device_contact_halted():
            logger.warning("Bit-error read for %s refused: device contact is "
                           "halted", self.ip)
            return None
        wanted = list(cards if cards is not None else self.known_cards())
        wanted = [c for c in wanted if c.get("card_number") is not None
                  and c.get("port") is not None and c.get("card_id") is not None]
        if not wanted:
            return []
        if len(wanted) > limit:
            logger.warning(
                "Bit-error read for %s asked for %d cards; capped at %d",
                self.ip, len(wanted), limit)
            wanted = wanted[:limit]
        if not self._ensure_tcp():
            return []
        if not self._begin_percard_pass():
            logger.warning("%s: a per-card pass is already running; "
                           "refusing to start a second", self.ip)
            return "busy"
        wanted = self._prioritise_unanswered(wanted, "bit errors")

        results = []
        report = _progress_reporter(progress, len(wanted), "bit errors")
        for index, card in enumerate(wanted):
            if device_contact_halted():
                report(index, "halted")
                break
            if index and index % BIT_ERROR_READ_BUDGET == 0:
                # The controller stops answering after ~150 reads and needs
                # ~45 s of quiet. Announce it: a bar that simply stops for
                # three quarters of a minute reads as a hang, and an operator
                # mid-show will kill it and lose the whole pass.
                report(index, "resting", rest=BIT_ERROR_READ_REST)
                time.sleep(BIT_ERROR_READ_REST)
            report(index, "reading")
            # card_number is 1-based (what the operator calls it); byte[5] is
            # the 0-based sender card index.
            data = self._percard_read(
                *H_REG_BIT_ERRORS, card["port"], card["card_id"],
                card["card_number"] - 1)
            parsed = parse_bit_errors(data)
            present = bool(parsed and parsed.get("present"))
            entry = {
                "card_number": card["card_number"],
                "port": card["port"],
                "card_id": card["card_id"],
                "present": present,
            }
            if present:
                entry["bit_errors"] = parsed["errors"]
                entry["bit_errors_saturated"] = parsed["saturated"]
            else:
                # The count bytes are meaningless unless byte[0] says a card
                # answered, so no number is recorded — but the ADDRESS is,
                # because a card in the inventory that stops answering is the
                # whole point. See `detect_chain_breaks`.
                entry["bit_errors"] = None
                entry["bit_errors_saturated"] = None
            results.append(entry)
            if len(results) % PER_CARD_PUBLISH_EVERY == 0:
                self._flush_bit_errors(results)
                report(index + 1, "flush")
            time.sleep(BIT_ERROR_READ_PACE)

        report(len(wanted), "done")
        self._record_unanswered(results, "bit errors", wanted)
        self._draft["unanswered_cards"] = self.unanswered_count()
        self._merge_bit_errors(results)
        self._draft["bit_errors_read_at"] = datetime.now().isoformat()
        self._draft["chain_breaks"] = self.detect_chain_breaks()
        self._publish()
        self._end_percard_pass()
        return results

    def clear_device_bit_errors(self):
        """Send the one write this application makes: clear bit-error counters.

        THIS WRITES TO THE CONTROLLER. Everything else in this class reads.

        The frame is the one NovaLCT sends, reproduced byte-for-byte from
        `Bit error 4x clear erros.pcapng` (see `build_clear_bit_errors`). It
        is broadcast, so it clears every card on every chain of every sender
        card at once — there is no captured per-card variant and inventing one
        for a write is not worth the risk.

        Refuses while device contact is halted, and is never called from a
        poll path — it only runs when an operator asks. Returns True if the
        frame went out.

        The counter is cumulative and this discards it for everyone, not just
        for this dashboard. `set_bit_error_baseline()` is the non-destructive
        alternative and does not touch the device at all.
        """
        if device_contact_halted():
            logger.warning("Bit-error clear for %s refused: device contact is "
                           "halted", self.ip)
            return False
        if not self._ensure_tcp():
            logger.warning("Bit-error clear for %s refused: no TCP connection",
                           self.ip)
            return False
        with self.lock:
            try:
                self.seq = (self.seq + 1) & 0xFFFF
                self.sock.sendall(build_clear_bit_errors(self.seq))
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning("Bit-error clear for %s failed: %s",
                               self.ip, exc)
                self.connected = False
                self._draft["connected"] = False
                return False
        logger.info("Bit-error counters cleared on %s (device write)", self.ip)
        # The device counters are now zero, so any baseline we were holding
        # describes a number that no longer exists. Keeping it would subtract
        # a stale offset from fresh readings.
        self._bit_error_baseline = {}
        for card in self._draft.get("receiving_cards", []):
            if "bit_errors_raw" in card:
                card["bit_errors_raw"] = 0
            if card.get("bit_errors") is not None:
                card["bit_errors"] = 0
        self._draft["bit_errors_cleared_at"] = datetime.now().isoformat()
        self._publish()
        return True

    # ── Per-card binary connection ─────────────────────────
    #
    # Per-card reads do NOT go to the device's configured control port. They
    # only answer on TCP 5201, whichever port the operator added the device
    # with, and every sender card is reached over that one connection with
    # byte[5] selecting the card. Pointing per-card reads at the control
    # socket produced a clean "0 cards answered" — no error, just nothing.

    # ── Unanswered cards get priority next time ────────────
    #
    # A whole-wall read is capped and paced, and the controller stops
    # answering partway regardless. Reading the inventory in the same order
    # every time means the same tail is always the part that misses — those
    # cards are never covered, and the gap is invisible because each pass
    # looks like a normal partial success. Putting last pass's failures first
    # makes coverage converge across passes instead of stalling.

    # ── One per-card pass at a time ────────────────────────
    #
    # Two concurrent passes would interleave on the same TCP connection and
    # double the load on a controller that already stops answering after a
    # few hundred reads — so each would poison the other's results AND make
    # the budget run out twice as fast. They also both report progress, which
    # is why the bar appeared to jump backwards and forwards at random.

    def _begin_percard_pass(self):
        """Claim the per-card reader. False if a pass is already running."""
        with self._percard_pass_lock:
            if self._percard_pass_active:
                return False
            self._percard_pass_active = True
            return True

    def _end_percard_pass(self):
        with self._percard_pass_lock:
            self._percard_pass_active = False

    def percard_pass_running(self):
        return self._percard_pass_active

    def _prioritise_unanswered(self, cards, kind):
        """Reorder `cards` so the ones that failed last pass come first."""
        pending = self._unanswered.get(kind)
        if not pending:
            return list(cards)
        missed, rest = [], []
        for c in cards:
            key = (c.get("card_number"), c.get("port"), c.get("card_id"))
            (missed if key in pending else rest).append(c)
        if missed:
            logger.info("%s: retrying %d card(s) that did not answer last "
                        "pass, before the rest", self.ip, len(missed))
        return missed + rest

    def _record_unanswered(self, results, kind, attempted):
        """Remember which of `attempted` did not answer this pass.

        Only addresses actually attempted are updated — a pass that stopped
        early (halt, or the cap) must not clear the record for cards it never
        reached, or their turn at the front of the queue is lost.
        """
        answered = set()
        failed = set()
        for r in results:
            key = (r["card_number"], r["port"], r["card_id"])
            if r.get("present") or r.get("answered"):
                answered.add(key)
            else:
                failed.add(key)
        pending = self._unanswered.setdefault(kind, set())
        pending -= answered
        pending |= failed
        # Anything attempted but absent from results was not reached.
        reached = answered | failed
        for c in attempted:
            key = (c.get("card_number"), c.get("port"), c.get("card_id"))
            if key not in reached:
                pending.add(key)

    def unanswered_count(self, kind=None):
        """How many cards are queued for a retry. Published in device state."""
        if kind is not None:
            return len(self._unanswered.get(kind) or ())
        return {k: len(v) for k, v in self._unanswered.items()}

    def _percard_connect(self):
        if self._percard_sock is not None:
            return self._percard_sock
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(PER_CARD_CONNECT_TIMEOUT)
            sock.connect((self.ip, H_PER_CARD_PORT))
            sock.settimeout(PER_CARD_TIMEOUT)
        except OSError as exc:
            logger.warning("Per-card connection to %s:%d failed: %s",
                           self.ip, H_PER_CARD_PORT, exc)
            return None
        self._percard_sock = sock
        return sock

    def _percard_close(self):
        if self._percard_sock is not None:
            try:
                self._percard_sock.close()
            except OSError:
                pass
            self._percard_sock = None

    def _percard_read(self, register, reg_len, chain, card_index, sender_card):
        """One per-card binary read on TCP 5201. Returns payload or None."""
        sock = self._percard_connect()
        if sock is None:
            return None
        self._percard_seq = (self._percard_seq + 1) & 0xFFFF
        frame = build_read_card(self._percard_seq, register, reg_len,
                                chain, card_index, sender_card=sender_card)
        try:
            sock.sendall(frame)
            head = self._percard_recv_exactly(sock, 18)
            if head is None:
                self._percard_close()
                return None
            payload_len = decode_length(struct.unpack(">H", head[16:18])[0])
            if payload_len < 0 or payload_len > 65535:
                self._percard_close()
                return None
            rest = self._percard_recv_exactly(sock, payload_len + 2)
            if rest is None:
                self._percard_close()
                return None
        except OSError:
            self._percard_close()
            return None
        parsed = parse_response(head + rest)
        if not parsed or parsed[0] != register:
            # A mismatched register means the stream is out of step; never
            # treat somebody else's answer as evidence about this card.
            self._percard_close()
            return None
        return parsed[1]

    @staticmethod
    def _percard_recv_exactly(sock, count):
        buf = bytearray()
        while len(buf) < count:
            try:
                chunk = sock.recv(count - len(buf))
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def refresh_live_readings(self, cards=None, limit=PER_CARD_SWEEP_LIMIT,
                              progress=None):
        """Read temperature, voltage and link state per card over BINARY.

        This is the one that covers the whole wall. R0155 is rate-limited hard
        enough that a 286-card readings pass answered 36 and went quiet for
        the rest — which is how the dashboard ended up showing temperatures
        for 13% of the wall and presenting the average of them as "the wall
        temperature".

        Register 0x0000000A returns presence, temperature, voltage AND link
        status in a single read per card, and byte[0] distinguishes a real
        card (0x80) from an address past the end of a chain (bit 0x40 set).
        An absent address still answers, repeating the previous card's
        readings behind that flag, so the flag is not optional: ignoring it
        invents a plausible temperature for every empty address on the wall.

        Same protections as every other per-card path: refuses while contact
        is halted, caps the list, and paces itself against the controller's
        request budget.

        Returns the list of readings that answered.
        """
        if device_contact_halted():
            logger.warning("Live reading for %s refused: device contact is "
                           "halted", self.ip)
            return None
        wanted = list(cards if cards is not None else self.known_cards())
        wanted = [c for c in wanted if c.get("card_number") is not None
                  and c.get("port") is not None and c.get("card_id") is not None]
        if not wanted:
            return []
        if len(wanted) > limit:
            logger.warning("Live reading for %s asked for %d cards; capped "
                           "at %d", self.ip, len(wanted), limit)
            wanted = wanted[:limit]
        if not self._begin_percard_pass():
            logger.warning("%s: a per-card pass is already running; "
                           "refusing to start a second", self.ip)
            return "busy"
        wanted = self._prioritise_unanswered(wanted, "readings")

        results = []
        report = _progress_reporter(progress, len(wanted), "temperature & voltage")
        for index, card in enumerate(wanted):
            if device_contact_halted():
                report(index, "halted")
                break
            if index and index % BIT_ERROR_READ_BUDGET == 0:
                # The controller stops answering after ~150 reads and needs
                # ~45 s of quiet. Announce it: a bar that simply stops for
                # three quarters of a minute reads as a hang, and an operator
                # mid-show will kill it and lose the whole pass.
                report(index, "resting", rest=BIT_ERROR_READ_REST)
                time.sleep(BIT_ERROR_READ_REST)
            report(index, "reading")
            data = self._percard_read(
                *REG_LIVE_MONITOR, card["port"], card["card_id"],
                card["card_number"] - 1)
            mon = parse_live_monitoring(data)
            entry = {
                "card_number": card["card_number"],
                "port": card["port"],
                "card_id": card["card_id"],
                "answered": mon is not None,
                "present": bool(mon and mon.get("present")),
            }
            if entry["present"]:
                entry.update({
                    "temp_c": mon["temperature_c"],
                    "temperature_c": mon["temperature_c"],
                    "voltage_v": mon["voltage_v"],
                    "link_status": mon["link_status"],
                })
            results.append(entry)
            # Publish as we go. A whole-wall pass is minutes long, and holding
            # every reading until the end means the operator watches a bar
            # crawl while the wall they are trying to diagnose shows nothing
            # new — and learns nothing at all if they stop it early or contact
            # is halted partway. Each flush is a complete, usable partial
            # result: the cards read so far are live, the rest are untouched.
            if len(results) % PER_CARD_PUBLISH_EVERY == 0:
                self._flush_live_readings(results)
                report(index + 1, "flush")
            time.sleep(BIT_ERROR_READ_PACE)

        report(len(wanted), "done")
        self._record_unanswered(results, "readings", wanted)
        self._draft["unanswered_cards"] = self.unanswered_count()
        self._merge_live_readings(results)
        self._draft["cards_read_at"] = datetime.now().strftime("%H:%M:%S")
        self._cards_read_monotonic = time.monotonic()
        self._publish()
        self._end_percard_pass()
        return results

    def _flush_live_readings(self, results):
        """Publish the readings taken so far, mid-pass.

        Merging is idempotent per card, so re-merging the growing list each
        time is correct and much simpler than tracking a delta. The read
        stamp is set here too, otherwise a partial pass would publish live
        readings that the freshness gate still calls stale.
        """
        self._merge_live_readings(results)
        self._draft["cards_read_at"] = datetime.now().strftime("%H:%M:%S")
        self._cards_read_monotonic = time.monotonic()
        self._publish()

    def _flush_bit_errors(self, results):
        """Publish the bit-error counters taken so far, mid-pass.

        Chain-break detection runs on every flush rather than only at the end:
        a break is exactly what somebody triggering this read is looking for,
        and making them wait out the remaining cards to be told about it is
        the wrong way round.
        """
        self._merge_bit_errors(results)
        self._draft["bit_errors_read_at"] = datetime.now().isoformat()
        self._draft["chain_breaks"] = self.detect_chain_breaks()
        self._publish()

    def _merge_live_readings(self, results):
        """Fold binary per-card readings into the published card list.

        A card that did not answer, or answered with the absent flag, is left
        as UNKNOWN — `online: None` — never False. The binary walk that built
        the inventory already proved these cards exist; one quiet read is not
        evidence a panel went away, and writing False for it is what put "250
        panels offline" on a lit wall.
        """
        if not results:
            return
        published = self._draft.setdefault("receiving_cards", [])
        index = {(c.get("card_number"), c.get("port"), c.get("card_id")): c
                 for c in published}
        # The binary read is the primary source of per-card state, so it
        # CREATES the entries rather than only updating ones R0155 happened to
        # have made first. Before this, a chain read produced 22 answers that
        # merged into an empty list and vanished without an error.
        identity = {(c.get("card_number"), c.get("port"), c.get("card_id")): c
                    for c in self.known_cards()}
        for r in results:
            key = (r["card_number"], r["port"], r["card_id"])
            card = index.get(key)
            if card is None:
                card = dict(identity.get(key, {}))
                card.update({"card_number": key[0], "port": key[1],
                             "card_id": key[2]})
                card.setdefault("label",
                                f"P{key[1] + 1}C{key[2] + 1:02d}")
                published.append(card)
                index[key] = card
            fresh = r
            if not fresh["present"]:
                card["online"] = None
                card["reading"] = ("no_answer" if not fresh["answered"]
                                   else "absent")
                for key in ("temp_c", "temperature_c", "voltage_v"):
                    card[key] = None
                continue
            card["online"] = True
            card["reading"] = "ok"
            card["temp_c"] = fresh["temp_c"]
            card["temperature_c"] = fresh["temperature_c"]
            card["voltage_v"] = fresh["voltage_v"]
            card["link_status"] = fresh["link_status"]
            card["read_at"] = self._draft.get("cards_read_at")

    def _merge_bit_errors(self, results):
        """Fold fresh counters into the published card list, minus baseline."""
        if not results:
            return
        by_key = {(r["card_number"], r["port"], r["card_id"]): r
                  for r in results}
        for card in self._draft.get("receiving_cards", []):
            key = (card.get("card_number"), card.get("port"),
                   card.get("card_id"))
            fresh = by_key.get(key)
            if not fresh:
                continue
            card["bit_error_present"] = fresh["present"]
            if not fresh["present"]:
                card["bit_errors_raw"] = None
                card["bit_errors"] = None
                card["bit_errors_saturated"] = None
                continue
            card["bit_errors_raw"] = fresh["bit_errors"]
            card["bit_errors"] = self._apply_bit_error_baseline(
                key, fresh["bit_errors"])
            card["bit_errors_saturated"] = fresh["bit_errors_saturated"]

    def detect_chain_breaks(self, cards=None):
        """Locate the break point on each chain. Two signatures, both real.

        Both were produced deliberately on a live wall by pulling the cable at
        panel 12 of a 22-panel chain, and they look nothing alike.

        **A — the backup is carrying the chain.** Every panel still answers.
        Nothing goes offline. The only trace is the error counter::

            panels  1-11 : 0
            panel  12    : 2     <- the cable that came out
            panels 13-22 : 2     <- everything downstream

        Errors propagate because each card repeats to the next, so the first
        non-zero card is the break and the run must stay non-zero to the end.
        A single noisy card with clean cards after it is one bad card, not a
        break, and reporting it as one sends someone to the wrong cable.

        **B — nothing is carrying it.** The chain simply stops::

            panels  1-11 : present, 0 errors
            panels 12-22 : no answer at all

        This one is why the inventory matters. "No answer" is also what an
        empty address looks like, and what a throttled controller looks like
        (H_SERIES_FINDINGS §6.6) — so a short chain is only a break when we
        KNOW the chain is longer. We do: the enumeration recorded 22 panels
        there, and a control chain probed in the same pass still answered all
        22, which rules out the controller having stopped talking to us.

        `at_head` covers a chain whose very first panel is already bad or
        missing: there is no clean prefix, so the break is at or before the
        head rather than at panel 1.

        Returns break dicts, most panels affected first.
        """
        source = cards if cards is not None else self._draft.get(
            "receiving_cards", [])
        chains = {}
        for card in source:
            key = (card.get("card_number"), card.get("port"))
            if None in key or card.get("card_id") is None:
                continue
            chains.setdefault(key, []).append(card)

        breaks = []
        for (card_number, port), members in chains.items():
            members = sorted(members, key=lambda c: c["card_id"])
            found = self._chain_break(members)
            if not found:
                continue
            found.update({"card_number": card_number, "port": port})
            breaks.append(found)
        breaks.sort(key=lambda b: b["affected"], reverse=True)
        return breaks

    @staticmethod
    def _chain_break(members):
        """One chain's verdict, or None. `members` sorted by card_id."""
        # ── Signature B: known cards that stopped answering ──────────────
        # Only cards that were actually probed this pass can testify; a card
        # with no `bit_error_present` key was never asked.
        probed = [c for c in members if "bit_error_present" in c]
        if probed:
            missing = [c for c in probed if not c["bit_error_present"]]
            if missing and len(missing) < len(probed):
                first_gap = next(i for i, c in enumerate(probed)
                                 if not c["bit_error_present"])
                tail = probed[first_gap:]
                # Contiguous to the end of the chain. A hole in the middle
                # with live cards after it is not a severed run.
                if all(not c["bit_error_present"] for c in tail):
                    return {
                        "signature": "no_answer",
                        "at_head": first_gap == 0,
                        "break_card_id": tail[0]["card_id"],
                        "break_panel": tail[0]["card_id"] + 1,
                        "clean_before": first_gap,
                        "affected": len(tail),
                        "bit_errors": None,
                        "detail": (f"{len(tail)} of {len(probed)} panels on "
                                   f"this chain stopped answering; the "
                                   f"enumeration recorded them as present"),
                    }

        # ── Signature A: errors propagating downstream ───────────────────
        counts = [(c["card_id"], c.get("bit_errors")) for c in members]
        counts = [(cid, n) for cid, n in counts if isinstance(n, int)]
        if not counts or all(n == 0 for _, n in counts):
            return None
        first_bad = next(i for i, (_, n) in enumerate(counts) if n)
        downstream = counts[first_bad:]
        if not all(n for _, n in downstream):
            return None
        return {
            "signature": "bit_errors",
            "at_head": first_bad == 0,
            "break_card_id": counts[first_bad][0],
            "break_panel": counts[first_bad][0] + 1,
            "clean_before": first_bad,
            "affected": len(downstream),
            "bit_errors": counts[first_bad][1],
            "detail": (f"panels {counts[first_bad][0] + 1} onward are all "
                       f"reporting bit errors; everything before is clean"),
        }

    def _apply_bit_error_baseline(self, key, raw):
        """Counter value with the operator's zero point subtracted.

        The controller's counter is cumulative and only NovaLCT is known to
        reset it, so "clear" here means "start counting from now" rather than
        writing to the device. Floors at zero: a raw value below the baseline
        means the device-side counter was reset behind our back, and the
        honest reading of that is zero, not a negative count.
        """
        base = self._bit_error_baseline.get(key)
        if base is None:
            return raw
        if raw < base:
            return 0
        return raw - base

    def set_bit_error_baseline(self, results=None):
        """Zero the displayed counters WITHOUT touching the device.

        Records the current raw counter for each card as its new zero point.
        `results` defaults to the last values read. Returns the number of
        cards baselined.

        This is deliberately not a device write. Clearing the controller's own
        counter needs a command we have not captured, and this app has no
        write path at all — see the module header.
        """
        source = results
        if source is None:
            source = [{"card_number": c.get("card_number"),
                       "port": c.get("port"),
                       "card_id": c.get("card_id"),
                       "bit_errors": c.get("bit_errors_raw",
                                           c.get("bit_errors"))}
                      for c in self._draft.get("receiving_cards", [])]
        count = 0
        for r in source:
            key = (r.get("card_number"), r.get("port"), r.get("card_id"))
            raw = r.get("bit_errors")
            if None in key or not isinstance(raw, int):
                continue
            self._bit_error_baseline[key] = raw
            count += 1
        self._rebase_published_bit_errors()
        return count

    def clear_bit_error_baseline(self):
        """Forget the zero point and show the controller's raw counters."""
        self._bit_error_baseline = {}
        self._rebase_published_bit_errors()

    def _rebase_published_bit_errors(self):
        for card in self._draft.get("receiving_cards", []):
            raw = card.get("bit_errors_raw")
            if not isinstance(raw, int):
                continue
            key = (card.get("card_number"), card.get("port"),
                   card.get("card_id"))
            card["bit_errors"] = self._apply_bit_error_baseline(key, raw)
        self._publish()

    def bit_error_baseline_size(self):
        return len(self._bit_error_baseline)

    def refresh_all_cards(self):
        """R0155 the entire known inventory. Explicit and rate-limited.

        Kept only because SNMP cannot reach per-card data without a SET
        selector, so this stays the one read-only route to a whole-wall
        picture. It is exempt from `PER_CARD_SWEEP_LIMIT` (a full sweep is the
        point) and therefore *not* exempt from the rate limit: at most one
        every FULL_SWEEP_MIN_INTERVAL seconds, with no force override, because
        an override is how this ends up back on a cadence.

        Returns None if the sweep was refused (global stop engaged, or too
        soon after the last one) — distinct from `[]`, which means it ran and
        there was nothing to read. No poll path calls this.
        """
        if device_contact_halted():
            logger.warning("Full card sweep for %s refused: device contact is "
                           "halted", self.ip)
            return None
        now = time.monotonic()
        if self._last_full_sweep is not None:
            waited = now - self._last_full_sweep
            if waited < FULL_SWEEP_MIN_INTERVAL:
                logger.warning(
                    "Full card sweep for %s refused: %.0fs since the last one, "
                    "minimum is %.0fs", self.ip, waited, FULL_SWEEP_MIN_INTERVAL)
                return None

        entries = self.known_cards()
        if not entries:
            return []
        self._last_full_sweep = now
        logger.info("Full R0155 sweep of %d cards on %s (on demand)",
                    len(entries), self.ip)
        return self._read_cards(entries)

    def _read_cards(self, entries):
        """Batch-read R0155 for `entries` and merge the results into state.

        Shared by every on-demand path. Batched, not one blocking round trip
        per card: a per-card read that times out on every silent card once
        pushed a full-inventory pass past 14 minutes. Responses come back
        positionally aligned with the addresses, with None where the device
        stayed silent.
        """
        if not entries or not self.json_client or device_contact_halted():
            return []

        with self._cards_lock:
            addresses = [(c["slot"], c["port"], c["card_id"]) for c in entries]
            # Spend the controller's request budget the same way the binary
            # per-card path does. Without this, a full-inventory R0155 sweep
            # sent ~1374 addresses in one go, got answers for the first ~150
            # and silence for the rest — and every one of those silences was
            # then published as a card state.
            responses = []
            for start in range(0, len(addresses), BIT_ERROR_READ_BUDGET):
                if start:
                    time.sleep(BIT_ERROR_READ_REST)
                window = addresses[start:start + BIT_ERROR_READ_BUDGET]
                responses.extend(
                    self.json_client.get_receiving_cards_batch(window))
            refreshed = self._decode_cards(entries, responses)
            # Per-card readings no longer refresh on a cadence, so how old one
            # is stopped being obvious. Stamp each card and the device: a
            # reading from twenty minutes ago rendered next to a live one,
            # with nothing to tell them apart, is its own kind of wrong.
            read_at = datetime.now().strftime("%H:%M:%S")
            for card in refreshed:
                card["read_at"] = read_at
            self._merge_cards(refreshed)
            self._draft["cards_read_at"] = read_at
            self._cards_read_monotonic = time.monotonic()
            # Aggregates only — no history sample. History is a per-cycle time
            # series and an on-demand read happens whenever somebody clicks,
            # so appending here would put unevenly spaced points on a chart
            # whose x-axis assumes a fixed cadence.
            self._update_aggregates(None, self._draft["receiving_cards"],
                                    record_history=False)
            self._publish()
        return refreshed

    def _decode_cards(self, entries, responses):
        """Turn raw R0155 replies into card dicts, aligned with `entries`."""
        refreshed = []
        for entry, response in zip(entries, responses):
            # Shared decoder — the schema detection and the temp/voltage
            # scalings live in h_series_json.parse_receiving_card(),
            # calibrated against real captured R0155 replies. Never re-derive
            # them here.
            card = parse_receiving_card(response)
            if not card or not card.get("online"):
                # Silent, failed ack, or workStatus != 0. In the last case the
                # device DID answer, with temp/volt/brightness all 0 — those
                # are placeholders for a card that is not reporting, so the
                # entry carries no readings at all. Rebuilt from `entry` on
                # every read, so a card that goes quiet cannot keep publishing
                # the values it had while it was up.
                # THREE states, not two. `card is None` means the controller
                # said nothing — which on this hardware is overwhelmingly the
                # request budget running out (§6.6), not a panel going away.
                # Calling that "offline" is what put "250 panels offline" on a
                # fully lit wall. Only a card that ANSWERED and reported
                # workStatus != 0 is knowably not reporting.
                answered = card is not None
                refreshed.append({
                    **entry,
                    "online": False if answered else None,
                    "answered": answered,
                    "reading": "not_reporting" if answered else "no_answer",
                    "reporting": bool(card and card.get("reporting")),
                    "work_status": card.get("work_status") if card else None,
                })
                continue
            refreshed.append({
                **entry,
                "online": True,
                "answered": True,
                "reading": "ok",
                "reporting": True,
                "work_status": card.get("work_status"),
                # Both temperature names for compatibility — the top-stats
                # aggregator uses temperature_c; the live device-tree
                # renderer reads temp_c.
                "temp_c": card["temp_c"],
                "temperature_c": card["temperature_c"],
                "voltage_v": card["voltage_v"],
                "brightness": card["brightness"],
                "primary_power_ok": card["primary_power_ok"],
                "backup_power_ok": card["backup_power_ok"],
                # Device-reported extras (present on the centi schema only,
                # None on the older byte schema). temp_limit_c is the
                # controller's own limit for this card; the status flags are
                # its verdict on its own readings.
                "temp_limit_c": card.get("temp_limit_c"),
                "temp_status_ok": card.get("temp_status_ok"),
                "volt_status_ok": card.get("volt_status_ok"),
                "mcu_version": card.get("mcu_version"),
                "fpga_version": card.get("fpga_version"),
            })
        return refreshed

    def _merge_cards(self, refreshed):
        """Fold freshly-read cards into the published `receiving_cards`.

        A refresh now covers a subset (one chain, a hand-picked list), so it
        updates the cards it read and leaves the rest of the list alone rather
        than replacing it — otherwise inspecting one chain would blank every
        other card on the dashboard. Caller holds `self._cards_lock`.

        The list is rebound, never mutated in place: another thread may be
        snapshotting it for serialization, and it must see the whole old list
        or the whole new one.
        """
        if not refreshed:
            return
        merged = {(c.get("slot"), c.get("port"), c.get("card_id")): c
                  for c in (self._draft.get("receiving_cards") or [])}
        for card in refreshed:
            merged[(card.get("slot"), card.get("port"),
                    card.get("card_id"))] = card
        self._draft["receiving_cards"] = [
            merged[key] for key in sorted(
                merged, key=lambda k: tuple(-1 if p is None else p for p in k))
        ]

    def _load_known_cards_from_snapshot(self):
        """Load the (slot, port, card_id) list from wall_live_snapshot.json.

        The snapshot is per-install runtime state produced by the R0155
        enumeration script and is gitignored, so a fresh checkout simply
        doesn't have one. Log when it's missing or unusable: without it there
        is nothing to poll and the wall renders empty, which is otherwise
        indistinguishable from "every card is offline". Unknown top-level keys
        (e.g. `captured_at`) are ignored.
        """
        path = SNAPSHOT_PATH
        if not os.path.exists(path):
            logger.warning(
                "No receiving-card snapshot at %s — per-card monitoring for "
                "%s stays idle until the R0155 enumeration is run",
                path, self.ip)
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError) as e:
            logger.error("Receiving-card snapshot %s is unreadable: %s", path, e)
            return []

        cards = []
        for c in (snap.get("cards") or []):
            if c.get("slot") is None or c.get("port") is None \
                    or c.get("card_id") is None:
                continue
            cards.append({
                "slot": c["slot"],
                "port": c["port"],
                "card_id": c["card_id"],
                "opt": c.get("opt"),
                "card_number": c.get("card_number"),
                "user_slot": c.get("user_slot"),
                "port_on_opt": c.get("port_on_opt"),
            })
        if not cards:
            logger.warning("Receiving-card snapshot %s lists no usable cards", path)
            return []

        captured_at = snap.get("captured_at")
        logger.info("Loaded %d receiving cards from %s%s", len(cards), path,
                    f" (captured {captured_at})" if captured_at else "")
        return cards

    # ── REMOVED: _ensure_heartbeat(). Do not reintroduce it. ───────────────
    #
    # This started a daemon thread that sent W0120 every 3 s for the life of
    # the process, on its own socket so nothing could slow it down. While it
    # was running the operator lost control of the wall from Bitfocus
    # Companion, their show-control surface; killing this app gave it back.
    # A read-only monitor must not claim the attached-controller role, and it
    # gained nothing by doing so. The client method is gone too — see the
    # matching note in h_series_json.py.

    def _poll_h_series_binary(self, now):
        """Legacy binary protocol path. Used as fallback only.

        A sender card addresses 16 chains (§6.5), each with its own daisy
        chain of receiving cards (up to 91 at 60×120 resolution).

        Device-level reads only. This path used to sweep a chain's worth of
        per-card registers every cycle (three or four reads per card, plus a
        probe of an undiscovered chain) — the same wall, the same show, just
        over TCP instead of UDP, so it falls under the same rule as the JSON
        sweep: per-card reads happen when somebody asks. `_poll_h_port()` is
        still here and still correct; it is simply no longer on a timer.
        """
        # Firmware
        data = self.read_register(*H_REG_FIRMWARE)
        if data and len(data) >= 2:
            self._draft["firmware_version"] = f"{data[0]}.{data[1]}"

        # Common registers (brightness, gamma, datetime)
        self._poll_common_registers()

        # Broadcast video status — port connectivity bitmask
        bitmask_ports = []
        data = self.read_register(*H_REG_VIDEO_STATUS)
        if data and len(data) >= 32:
            # The bitmask is a single byte, so it only measures the first 8
            # chains. Chains above that are absent from the map rather than
            # reported False — they get discovered by probing, below.
            port_map = parse_h_port_bitmask(data)
            self._draft["port_bitmask"] = data[31]
            bitmask_ports = sorted(p for p, connected in port_map.items() if connected)
            self._draft["video_status"] = {
                "format": "h_series",
                "port_bitmask": f"0x{data[31]:02X}",
                "measured_port_count": len(bitmask_ports),
            }

        # Device identity (NSSD)
        data = self.read_register(*H_REG_DEVICE_ID)
        if data:
            info = parse_nssd(data)
            if info:
                self._draft["device_info"] = info

        # Active chains = what the bitmask measured, plus any chain an earlier
        # cycle actually found cards on (that's how chains 9-16 stay in the
        # rotation once discovered, since no bitmask bit covers them).
        active_ports = self._active_chains(bitmask_ports)
        self._draft["active_ports"] = active_ports

        # NOT DONE HERE: the per-chain card sweep and the undiscovered-chain
        # probe. Both used to run every cycle from this point
        # (`_select_ports_to_poll` / `_next_unprobed_port` feeding
        # `_poll_h_port`). Call `_poll_h_port(port_num)` from an on-demand
        # handler instead — the helpers are kept so that handler has something
        # to build on.

        # Re-derive: a chain an earlier on-demand inspection found cards on
        # stays active.
        self._draft["active_ports"] = self._active_chains(bitmask_ports)
        if isinstance(self._draft.get("video_status"), dict):
            self._draft["video_status"]["active_port_count"] = \
                len(self._draft["active_ports"])

        # Flatten per-port cards into receiving_cards for backward compatibility
        all_cards = []
        for port_num in sorted(self._draft["ports"].keys()):
            port_data = self._draft["ports"][port_num]
            all_cards.extend(port_data.get("cards", []))
        self._draft["receiving_cards"] = all_cards

        self._update_aggregates(now, all_cards)

    def _active_chains(self, bitmask_ports):
        """Chains the bitmask reported, unioned with chains we found cards on."""
        discovered = [p for p, d in self._draft["ports"].items()
                      if d.get("card_count", 0) > 0]
        return sorted(set(bitmask_ports) | set(discovered))

    def _select_ports_to_poll(self, active_ports):
        """Pick a batch of chains: all of them, or one round-robin slice.

        No longer called from the poll loop — kept for an on-demand handler
        that wants to walk the binary path's chains a slice at a time.
        """
        if not active_ports:
            return []
        total_known_cards = sum(
            self._draft["ports"].get(p, {}).get("card_count", 0)
            for p in active_ports
        )
        if total_known_cards <= 30 or len(active_ports) <= 2:
            # Small system: poll all ports every cycle
            return list(active_ports)
        # Large system: round-robin one port per cycle
        rr_idx = self._draft.get("_h_port_rr", 0) % len(active_ports)
        self._draft["_h_port_rr"] = rr_idx + 1
        return [active_ports[rr_idx]]

    def _next_unprobed_port(self):
        """Return the next bitmask-invisible chain to probe, or None.

        parse_h_port_bitmask only measures H_PORT_BITMASK_BITS chains, but a
        sender card addresses H_MAX_PORTS of them (§6.5). The rest are found
        by sending per-card reads at them — one chain per call, once each;
        after that they appear in active_ports if any card answered.

        No longer called from the poll loop: probing is per-card traffic, so
        it is now something an on-demand discovery action drives.
        """
        probed = self._draft.setdefault("_probed_ports", [])
        for port_num in range(H_PORT_BITMASK_BITS + 1, H_MAX_PORTS + 1):
            if port_num not in probed:
                probed.append(port_num)
                return port_num
        return None

    def _poll_h_port(self, port_num):
        """Poll receiving cards on a single H-series output port.

        `port_num` is the 1-based port/chain number used by the UI and the
        broadcast bitmask; the wire frame wants the 0-based chain index in
        byte[7], so the conversion happens once, here. The `port` kwarg of
        read_register_card is the OPT group (byte[5]) and is deliberately left
        at its 0x00 default — it is not the chain (§6.5).

        Per-card `online` is three-valued here, matching `_decode_cards` and
        `_merge_live_readings` — see the comment on the read below. This path
        is on-demand-only today, which is exactly why it has to be right: it
        is the template the next binary-transport handler will be copied from.
        """
        chain = port_num - 1  # 1-based port number → 0-based chain index

        port_state = self._draft["ports"].setdefault(port_num, {
            "connected": False,
            "card_count": 0,
            "cards": [],
        })

        # Scan cards on this chain. Use detected count if known,
        # otherwise probe up to 16 to discover the actual count.
        detected_key = f"_h_port_{port_num}_cards"
        detected_count = self._draft.get(detected_key, 0)
        scan_limit = detected_count if detected_count > 0 else 16
        consecutive_offline = 0

        cards = []
        for i in range(scan_limit):
            # Read video status for this card on this chain — gives the
            # 7-bit data-path link health (data break detection).
            vdata = self.read_register_card(*H_REG_VIDEO_STATUS, i, chain=chain)
            link_info = (parse_h_card_link(vdata)
                         if vdata and len(vdata) >= 2 else None)

            # THREE states, not two. `online` used to default to False and
            # only ever be raised to True, so a card whose video-status read
            # TIMED OUT was published identically to a card that answered and
            # reported zero connected data paths. The first is "we don't
            # know", the second is a genuine data break — the thing this read
            # exists to find — and an operator who cannot tell them apart
            # either ignores real breaks or chases imaginary ones.
            #
            # A timeout is not rare on this transport and is not evidence
            # about the panel: the controller answers roughly 150-200 per-card
            # binary reads and then simply stops (H_SERIES_FINDINGS §6.6), so
            # on any wall worth monitoring the tail of a sweep is silence from
            # a working controller. Reading that silence as "no link" is the
            # binary-transport twin of the bug that put "250 panels offline"
            # on a fully lit wall via the JSON path.
            #
            #   no_answer    nothing came back (timeout, or the socket died).
            #   undecodable  bytes came back, too short or malformed to parse.
            #   no_link      the card ANSWERED, with 0 of N data paths
            #                connected. A real, reportable fault.
            #   ok           answered with at least one path connected.
            if link_info is None:
                online = None
                reading = "no_answer" if not vdata else "undecodable"
            else:
                connected_paths, total_paths = link_info
                online = connected_paths > 0
                reading = "ok" if online else "no_link"

            card_online = online is True
            card_info = {
                "index": i,
                "label": f"P{port_num}C{i + 1:02d}",
                "port": port_num,
                "chain": chain,
                "online": online,
                "answered": link_info is not None,
                "reading": reading,
            }
            if link_info is not None:
                card_info.update({
                    "link_paths": f"{connected_paths}/{total_paths}",
                    "link_raw": vdata[1],
                })

            if card_online:
                consecutive_offline = 0

                # NO per-card temp/voltage here. REG_LIVE_MONITOR (0x0000000A)
                # is the VX1000 live-monitoring register and it is NOT per-card
                # on H-series: it answers for every (chain, card) address with
                # a free-running counter, whether a card is there or not.
                # Reading it here produced a plausible temperature and voltage
                # for every address on the wall, including empty ones.
                # H-series per-card temp/voltage comes from JSON R0155
                # (see h_series_json.parse_receiving_card).

                # Per-card fault flag (separate from data-break / link health).
                # 0 = no fault; non-zero codes are an active alarm whose
                # specific semantics aren't decoded yet.
                fdata = self.read_register_card(*H_REG_CARD_FAULT, i,
                                                chain=chain)
                fault = parse_h_card_fault(fdata)
                if fault:
                    card_info["fault"] = fault["fault"]
                    card_info["fault_code"] = fault["code"]

                # Per-card bit-error counter — the only continuous
                # data-integrity signal, and binary-only (no JSON equivalent).
                # 0xFFFF is the counter's ceiling, not a literal count.
                bdata = self.read_register_card(*H_REG_BIT_ERRORS, i,
                                                chain=chain)
                bits = parse_bit_errors(bdata)
                if bits:
                    card_info["bit_errors"] = bits["errors"]
                    card_info["bit_errors_saturated"] = bits["saturated"]
                    card_info["present"] = bits["present"]
            else:
                # Deliberately counts UNKNOWN alongside knowably-offline. This
                # is a traffic bound, not a health verdict: the discovery scan
                # has to stop somewhere, and three silent addresses in a row
                # is as good a stopping point as three empty ones. It costs
                # nothing to be wrong here — the chain is simply re-read next
                # time somebody asks — whereas publishing those cards as
                # offline would be a claim. The claim is the part that had to
                # change; the stopping rule can stay pessimistic.
                consecutive_offline += 1
                # During initial scan, stop after 3 consecutive offline cards
                if detected_count == 0 and consecutive_offline >= 3:
                    break

            cards.append(card_info)

        # Detect card count: last online card index + 1
        online_indices = [c["index"] for c in cards if c.get("online")]
        if online_indices and detected_count == 0:
            detected_count = max(online_indices) + 1
            self._draft[detected_key] = detected_count

        # Only keep cards up to the detected count
        if detected_count > 0:
            cards = cards[:detected_count]

        port_state["cards"] = cards
        port_state["card_count"] = len([c for c in cards if c.get("online")])
        # How many of those cards we have no answer for. Without this the
        # chain-level rollup hides the per-card fix it sits on top of: a chain
        # of 22 cards that all timed out and a chain of 22 cards that all
        # answered "no link" both report card_count 0.
        answered_cards = [c for c in cards if c.get("online") is not None]
        port_state["cards_unknown"] = len(cards) - len(answered_cards)
        # A probed chain with nothing on it is not "connected" — but a chain
        # nothing answered on is not "not connected" either, it is unknown.
        # Same three states as the cards it is derived from, because the
        # rollup of a set of unknowns is an unknown, not a False.
        port_state["connected"] = (
            True if port_state["card_count"] > 0
            else (False if answered_cards else None))

    # ── Common Registers & Aggregates ─────────────────────

    def _poll_common_registers(self):
        """Read registers shared between VX1000 and H-series."""
        # Brightness. On VX1000 the register works and is 0-255. On H-series
        # H_REG_BRIGHTNESS (0x06000000) reads 0 on a wall that is lit, which
        # is what put "0%" on the dashboard, so the value there comes from
        # R0401 instead (`_poll_screen_brightness`). Never let this overwrite
        # a percentage that a working source already supplied.
        if self.device_type == "h_series":
            if self._draft.get("brightness_source") != "R0401":
                data = self.read_register(*H_REG_BRIGHTNESS)
                if data and len(data) >= 1:
                    self._draft["brightness"] = data[0]
                    self._draft["brightness_pct"] = round(data[0] / 255 * 100, 1)
                    self._draft["brightness_source"] = "H_REG_BRIGHTNESS"
        else:
            data = self.read_register(*REG_BRIGHTNESS)
            if data and len(data) >= 1:
                raw = data[0]
                self._draft["brightness"] = raw
                self._draft["brightness_pct"] = round(raw / 255 * 100, 1)
                self._draft["brightness_source"] = "REG_BRIGHTNESS"

        # Gamma
        reg = H_REG_GAMMA if self.device_type == "h_series" else REG_GAMMA
        data = self.read_register(*reg)
        if data and len(data) >= 2:
            import struct
            self._draft["gamma"] = struct.unpack(">H", data[:2])[0]

        # Date/Time
        reg = H_REG_DATETIME if self.device_type == "h_series" else REG_DATETIME
        data = self.read_register(*reg)
        if data and len(data) >= 6:
            self._draft["datetime"] = (
                f"20{data[0]:02d}-{data[1]:02d}-{data[2]:02d} "
                f"{data[4]:02d}:{data[5]:02d}"
            )

    def _update_aggregates(self, now, cards, record_history=True):
        """Update live_monitoring aggregates and history from card list.

        `record_history=False` (used by the on-demand per-card reads) updates
        the aggregates but appends no history sample: the history arrays are a
        time series whose chart assumes a fixed cadence, and an on-demand read
        happens whenever an operator clicks. `now` is then unused and may be
        None.

        A card that answered R0155 with `workStatus != 0` is NOT reporting:
        its temp/volt zeros are placeholders. Such a card is excluded twice
        over — the parser marks it `online: False` and leaves its readings
        None — because a single 0.0 dragged into the mean or the max is
        indistinguishable from a real measurement once it is averaged.

        `online: None` — the card was never asked, or did not answer — is a
        third state and is excluded from the mean by the same truthiness test
        that excludes an offline card. That is correct for the aggregate (an
        unknown card contributes no measurement) but it is NOT correct to let
        it disappear, because `coverage_total - coverage_read` would then read
        as "cards that are down". `coverage_unknown` separates the two, so a
        caller can say "36 of 286 read, 0 down, 250 unknown" instead of
        implying 250 dead panels on a lit wall.
        """
        if not cards:
            return
        online_cards = [c for c in cards
                        if c.get("online") and c.get("reporting") is not False]
        unknown_cards = [c for c in cards if c.get("online") is None]

        temps = [c["temperature_c"] for c in online_cards
                 if c.get("temperature_c") is not None]
        volts = [c["voltage_v"] for c in online_cards
                 if c.get("voltage_v") is not None]

        # Every field is REPLACED, not merged, and a field with nothing behind
        # it this pass is None. `update()` on a dict that is only ever added to
        # meant a wall where every card had stopped answering kept publishing
        # `online: True` and its last healthy temperature indefinitely — a
        # false all-clear that survives the fault it should be reporting. A
        # partial read is just as bad in miniature: a fresh card_count next to
        # last hour's temperature_max_c reads as one measurement.
        #
        # `coverage` is what makes the aggregate honest: a mean over 36 of 286
        # cards is not "the wall temperature", and the UI needs to be able to
        # say so.
        agg = {
            "card_count": len(online_cards) if cards else None,
            "online": bool(online_cards),
            "coverage_read": len(online_cards),
            "coverage_total": len(cards),
            # Cards whose state this pass is UNKNOWN — never asked, timed out,
            # or answered with a frame that would not decode. Not part of
            # coverage_read (they supplied no measurement) and deliberately
            # not lumped in with the offline remainder either.
            "coverage_unknown": len(unknown_cards),
            "temperature_c": (round(sum(temps) / len(temps), 1)
                              if temps else None),
            "temperature_max_c": max(temps) if temps else None,
            "voltage_v": (round(sum(volts) / len(volts), 2)
                          if volts else None),
            # The minimum is the one that finds a single failing supply; the
            # mean cannot. `app._device_breaches` has always read
            # `voltage_min_v` and nothing ever wrote it, so the device-level
            # low-voltage alert could not fire at all.
            "voltage_min_v": min(volts) if volts else None,
        }

        self._draft["live_monitoring"].update(agg)

        if not record_history:
            return

        # History — exactly one sample per series per cycle so the three
        # arrays stay index-aligned; the chart maps timestamps[i] to
        # temperature[i]/voltage[i] positionally. A series with no reading
        # this cycle records None rather than skipping, which would shift
        # every later point onto the wrong label.
        hist = self._draft["history"]
        hist["temperature"].append(agg.get("temperature_c"))
        hist["voltage"].append(agg.get("voltage_v"))
        hist["timestamps"].append(now.strftime("%H:%M:%S"))
        for key in ("temperature", "voltage", "timestamps"):
            if len(hist[key]) > HISTORY_LIMIT:
                del hist[key][:-HISTORY_LIMIT]


class DeviceManager:
    """Manages multiple NovaStar devices and their polling threads."""

    def __init__(self, poll_interval=DEFAULT_POLL_INTERVAL):
        self.devices = {}
        self.poll_interval = poll_interval
        self._threads = {}
        # device_id -> Event. Set to tell that device's poll thread to exit;
        # the thread captures its own Event, so replacing a device_id can
        # never leave two threads polling the same hardware.
        self._stop_events = {}
        self._running = False
        self._on_update = None  # Callback: fn(device_id, state)
        self._on_error = None   # Callback: fn(device_id, error_info)

    def set_callbacks(self, on_update=None, on_error=None):
        self._on_update = on_update
        self._on_error = on_error

    # ── Global stop ───────────────────────────────────────
    #
    # Thin handles onto the module-level flag, so a caller holding a manager
    # (app.py, a route, the console) has an obvious place to reach for it.
    # They delegate rather than duplicate: the flag is process-wide because
    # the SNMP client checks the same one.

    def halt(self, reason=None):
        """Stop all device contact, everywhere, without stopping the app.

        Poll threads stay alive and keep serving the last known state; they
        just stop sending. Unlike `stop()`, this is reversible — `resume()`
        picks polling straight back up.

        The flag is set FIRST and the state republished afterwards: this is an
        emergency stop pressed during a show, so the only thing that must
        happen promptly is that packets stop. Refreshing the dashboard is a
        courtesy that happens next, and cannot delay or fail the stop.
        """
        result = halt_device_contact(reason)
        self._republish_contact_state()
        return result

    def resume(self):
        """Undo `halt()`."""
        result = resume_device_contact()
        self._republish_contact_state()
        return result

    def _republish_contact_state(self):
        """Push the new halt flag into every device's published state."""
        for dev in list(self.devices.values()):
            refresh = getattr(dev, "publish_contact_state", None)
            if callable(refresh):
                refresh()

    @staticmethod
    def is_halted():
        """Whether device contact is currently halted."""
        return device_contact_halted()

    def add_device(self, device_id, name, ip, port=TCP_PORT):
        """Add a device to monitor, replacing anything already on that id."""
        # Stop a thread left over from a previous device with this id before
        # starting a new one, or both would poll the same controller.
        self._signal_stop(device_id)
        dev = NovaStar_Device(device_id, name, ip, port)
        self.devices[device_id] = dev

        if self._running:
            self._start_device_thread(device_id)

        return dev

    def remove_device(self, device_id):
        """Stop monitoring a device and tear down its polling thread."""
        # Signal first: deleting the dict entry alone used to leave the thread
        # reconnecting and firing callbacks forever.
        self._signal_stop(device_id)
        dev = self.devices.pop(device_id, None)
        if dev:
            dev.disconnect()

    def get_state(self, device_id=None):
        """Get current state for one or all devices (safe to serialize)."""
        if device_id:
            dev = self.devices.get(device_id)
            return self._safe_state(dev) if dev else None
        return {did: self._safe_state(dev) for did, dev in self.devices.items()}

    def get_all_states(self):
        """Get all device states as a list (safe to serialize)."""
        return [self._safe_state(dev) for dev in self.devices.values()]

    def start(self):
        """Start polling all devices."""
        self._running = True
        for device_id in list(self.devices):
            self._start_device_thread(device_id)

    def stop(self, timeout=5.0):
        """Stop all polling, disconnect, and wait for the threads to exit."""
        self._running = False
        for event in list(self._stop_events.values()):
            event.set()
        for dev in list(self.devices.values()):
            dev.disconnect()
        # Join with a shared deadline: a thread blocked in a socket read
        # shouldn't be able to hang shutdown.
        deadline = time.monotonic() + timeout
        for t in list(self._threads.values()):
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        self._threads.clear()
        self._stop_events.clear()

    # ── Internals ─────────────────────────────────────────

    @staticmethod
    def _safe_state(dev):
        """Return a state dict that's safe to serialize on another thread.

        NovaStar_Device publishes an immutable snapshot (see its `state`
        property) and can be handed out directly. Anything else — the demo
        device, for instance — gets a defensive copy so `jsonify` never walks
        a dict a poll thread is mutating.
        """
        state = dev.state
        if getattr(dev, "publishes_state", False):
            return state
        return _snapshot(state)

    @staticmethod
    def _record_error(dev, message):
        """Record a poll-loop exception on the device's state."""
        setter = getattr(dev, "set_error", None)
        if callable(setter):
            setter(message)
        else:
            dev.state["error"] = message

    def _signal_stop(self, device_id):
        """Tell a device's poll thread to exit and forget about it."""
        event = self._stop_events.pop(device_id, None)
        if event:
            event.set()
        self._threads.pop(device_id, None)

    def _start_device_thread(self, device_id):
        """Start a polling thread for a device."""
        self._signal_stop(device_id)
        stop = threading.Event()
        self._stop_events[device_id] = stop

        def poll_loop():
            # `stop` is captured by value and the device is re-fetched every
            # iteration: removing or replacing the device ends this thread
            # even if a new one has since started under the same id.
            while self._running and not stop.is_set():
                dev = self.devices.get(device_id)
                if dev is None:
                    break
                # The global stop, checked before every cycle. The thread
                # keeps looping (so resume() is instant and needs no thread
                # restart) but never calls poll(). poll() checks the same flag
                # itself — this one just avoids the call entirely.
                if device_contact_halted():
                    stop.wait(self.poll_interval)
                    continue
                try:
                    dev.poll()
                    if self._on_update:
                        self._on_update(device_id, self._safe_state(dev))
                except Exception as e:
                    self._record_error(dev, str(e))
                    if self._on_error:
                        self._on_error(device_id, {"error": str(e)})
                # Interruptible sleep so stop()/remove_device() take effect
                # immediately instead of after a full poll interval.
                stop.wait(self.poll_interval)

        t = threading.Thread(target=poll_loop, daemon=True, name=f"poll-{device_id}")
        self._threads[device_id] = t
        t.start()
