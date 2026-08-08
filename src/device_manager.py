"""
NovaStar Device Manager
Manages TCP connections and polls controllers for monitoring data.
Uses threading (not asyncio) for Flask/SocketIO compatibility.
"""

import json
import logging
import os
import socket
import threading
import time
from datetime import datetime
from novastar_protocol import (
    TCP_PORT, H_TCP_PORT,
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

# Default seconds between poll cycles — the same cadence as the official
# Bitfocus Companion novastar-splicer module: fresh enough to feel live, light
# enough to run 24/7 without hammering the controller. Heartbeat (W0120) runs
# separately at 3s and is unaffected.
#
# Single source of truth: app.py imports this constant instead of declaring
# its own. The two used to disagree (2.0 here, 10.0 there), so any caller that
# constructed a DeviceManager without passing an interval polled live hardware
# five times faster than the value the settings UI advertised.
DEFAULT_POLL_INTERVAL = 10.0


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

    def __init__(self, device_id, name, ip, port=TCP_PORT):
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
        # Heartbeat thread — sends W0120 every 3s while the device is
        # connected. Matches the cadence of the official splicer Companion
        # module. Started lazily on first successful JSON poll.
        self._heartbeat_thread = None
        self._heartbeat_stop = threading.Event()
        # Cached (slot, port, card_id) inventory loaded from the snapshot.
        self._known_cards = None

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
            "system_info": {},
            "device_info": {},
            "port2_active": False,
            "brightness": 0,
            "brightness_pct": 0,
            "gamma": 0,
            "datetime": "",
            "firmware_version": "",
            "live_monitoring": {},
            "video_status": {},
            "receiving_cards": [],
            # H-series port structure
            "ports": {},           # port_num -> {connected, card_count, cards}
            "port_bitmask": 0,     # raw bitmask from broadcast video status
            "active_ports": [],    # list of connected port numbers
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
        """Swap a fresh snapshot of the draft into view (atomic rebind)."""
        self._published = _snapshot(self._draft)
        return self._published

    def set_error(self, message):
        """Record an error on the draft and publish it immediately."""
        self._draft["error"] = message
        self._publish()

    # ── Connection ────────────────────────────────────────

    def connect(self):
        """Establish TCP connection."""
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
        """Close TCP connection, stop the heartbeat, release the JSON sockets."""
        # Signal heartbeat thread to stop
        self._heartbeat_stop.set()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        # The JSON client holds two long-lived UDP sockets of its own.
        if self.json_client:
            try:
                self.json_client.close()
            except Exception:
                pass
        self.connected = False
        self._draft["connected"] = False
        self._publish()

    def read_register(self, reg_addr, reg_len, port=0x00):
        """Send a broadcast READ request and return the response payload."""
        if not self.connected:
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
                           port=0x00):
        """Send a per-card READ request targeting one card on one chain.

        `chain` is the 0-based daisy-chain index (byte[7], 0–15) and
        `card_index` the 0-based position of the card within that chain
        (byte[8]). `port` is the OPT group in byte[5] — a *different* concept,
        0x00 in all observed traffic (H_SERIES_FINDINGS §6.5). Passing a chain
        number as `port` was the pre-§6.5 mistake: every per-card read then
        addressed chain 0, so one chain's readings were attributed to all of
        them. VX1000 has a single chain, hence the 0 default.
        """
        if not self.connected:
            return None

        with self.lock:
            try:
                self.seq = (self.seq + 1) & 0xFFFF
                frame = build_read_card(self.seq, reg_addr, reg_len,
                                        chain, card_index, port=port)
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
        self._draft["port2_active"] = bool(
            data and len(data) > 4 and any(b != 0 for b in data[:10]))

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
            if cdata:
                mon = parse_live_monitoring(cdata)
                if mon and mon.get("online"):
                    if detected_count == 0 and mon.get("card_count", 0) > 0:
                        detected_count = mon["card_count"]
                        self._draft["_detected_card_count"] = detected_count
                        scan_limit = detected_count
                    cards.append({
                        "index": i,
                        "label": f"C{i + 1:02d}",
                        "online": True,
                        "temperature_c": mon["temperature_c"],
                        "voltage_v": mon["voltage_v"],
                        "link_status": mon["link_status"],
                        "link_raw": mon["link_raw"],
                        "firmware": mon["firmware"],
                        "mac_address": mon["mac_address"],
                    })
                    continue
            cards.append({"index": i, "label": f"C{i + 1:02d}", "online": False})
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
        if not self.json_client:
            return False
        if self._json_ever_worked:
            return True
        return self._json_consecutive_fails < self._json_max_fails

    def _poll_h_series(self, now):
        """Poll an H-series controller.

        Prefers the documented JSON UDP protocol on port 6000 over the
        reverse-engineered binary protocol on TCP 5203. Falls back to
        binary if JSON UDP doesn't respond and TCP can be brought up.
        """
        # ── Try JSON UDP first ────────────────────────────────────────
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

        Mimics the polling pattern used by the official Bitfocus splicer
        Companion module: per cycle, fire R0100 (device details), R0400
        (screen list — topology), R0300 (output list). Reactive fan-out to
        R0301 for per-output detail when needed.
        """
        # Start the heartbeat thread once we know the device is reachable.
        self._ensure_heartbeat()

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

        # Per-card monitoring via R0155 — refresh status of the known
        # (slot, port, card_id) tuples discovered by the enumeration
        # snapshot. Per poll we just hit the known cards; full
        # enumeration is a one-time operation done out-of-band.
        cards = self._refresh_known_cards()
        self._draft["receiving_cards"] = cards
        self._update_aggregates(now, cards)

    def _refresh_known_cards(self):
        """Read R0155 for each (slot, port, card_id) in our cached card list.

        Returns the updated list of cards with current temp/voltage/power
        status. If we don't have a cached list yet (first poll), tries to
        load it from `wall_live_snapshot.json` next to the source tree.
        """
        if not self.json_client:
            return self._draft.get("receiving_cards", []) or []

        # Lazy-load snapshot the first time
        if not self._known_cards:
            self._known_cards = self._load_known_cards_from_snapshot()

        # No cards known yet — return empty; the enumeration script must
        # be run separately to populate the snapshot
        if not self._known_cards:
            return []

        # Batched, not one blocking round trip per card: at ~1374 cards a
        # per-card read that times out on every silent card pushed a cycle
        # past 14 minutes against a 180 s interval, so cycles ran back to
        # back forever. Responses come back positionally aligned with the
        # addresses, with None where the device stayed silent.
        addresses = [(c["slot"], c["port"], c["card_id"])
                     for c in self._known_cards]
        responses = self.json_client.get_receiving_cards_batch(addresses)

        refreshed = []
        for entry, response in zip(self._known_cards, responses):
            # Shared decoder — the temp/voltage byte formulas live in
            # h_series_json.parse_receiving_card(), calibrated against a real
            # captured R0155 reply. Never re-derive them here.
            card = parse_receiving_card(response)
            if not card or not card.get("online"):
                refreshed.append({**entry, "online": False})
                continue
            refreshed.append({
                **entry,
                "online": True,
                # Both temperature names for compatibility — the top-stats
                # aggregator uses temperature_c; the live device-tree
                # renderer reads temp_c.
                "temp_c": card["temp_c"],
                "temperature_c": card["temperature_c"],
                "voltage_v": card["voltage_v"],
                "brightness": card["brightness"],
                "primary_power_ok": card["primary_power_ok"],
                "backup_power_ok": card["backup_power_ok"],
            })
        return refreshed

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

    def _ensure_heartbeat(self):
        """Start the W0120 heartbeat thread if it isn't already running."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        if not self.json_client:
            return
        self._heartbeat_stop.clear()
        stop = self._heartbeat_stop

        def heartbeat_loop():
            # Every 3s — matches splicer module cadence. The stop Event is
            # the only exit condition (disconnect() sets it); the heartbeat
            # rides on UDP and must not depend on the TCP socket's state.
            while not stop.is_set():
                try:
                    self.json_client.heartbeat()
                except Exception:
                    pass
                stop.wait(3.0)

        t = threading.Thread(target=heartbeat_loop, daemon=True,
                             name=f"hb-{self.device_id}")
        t.start()
        self._heartbeat_thread = t

    def _poll_h_series_binary(self, now):
        """Legacy binary protocol path. Used as fallback only.

        A sender card addresses 16 chains (§6.5), each with its own daisy
        chain of receiving cards (up to 91 at 60×120 resolution).
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

        # Poll each connected chain's cards. To avoid long poll cycles, we
        # round-robin one chain per poll unless the total card count is small
        # enough to poll all of them.
        ports_to_poll = self._select_ports_to_poll(active_ports)

        # Chains the bitmask can't see still exist (§6.5: 16 per sender card).
        # Probe one per cycle so discovery isn't silently capped at 8.
        probe = self._next_unprobed_port()
        if probe is not None and probe not in ports_to_poll:
            ports_to_poll.append(probe)

        for port_num in ports_to_poll:
            self._poll_h_port(port_num)

        # Re-derive after polling: a probed chain becomes active as soon as a
        # card on it answers.
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
        """Pick this cycle's chains: all of them, or one round-robin slice."""
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
        by sending per-card reads at them — one chain per poll cycle, once
        each; after that they appear in active_ports if any card answered.
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

            card_online = False
            card_info = {
                "index": i,
                "label": f"P{port_num}C{i + 1:02d}",
                "port": port_num,
                "chain": chain,
                "online": False,
            }

            if vdata and len(vdata) >= 2:
                link_info = parse_h_card_link(vdata)
                if link_info:
                    connected_paths, total_paths = link_info
                    card_online = connected_paths > 0
                    card_info.update({
                        "online": card_online,
                        "link_paths": f"{connected_paths}/{total_paths}",
                        "link_raw": vdata[1],
                    })

            if card_online:
                consecutive_offline = 0

                # Real per-card temp/voltage/link enum from the VX1000-style
                # live monitoring register — confirmed against NovaLCT GUI to
                # be the authoritative source on H-series too.
                lmdata = self.read_register_card(*REG_LIVE_MONITOR, i,
                                                 chain=chain)
                if lmdata:
                    mon = parse_live_monitoring(lmdata)
                    if mon:
                        card_info.update({
                            "temperature_c": mon["temperature_c"],
                            "voltage_v": mon["voltage_v"],
                            "link_status": mon["link_status"],
                            "firmware": mon["firmware"],
                            "mac_address": mon["mac_address"],
                        })

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
        # A probed chain with nothing on it is not "connected".
        port_state["connected"] = port_state["card_count"] > 0

    # ── Common Registers & Aggregates ─────────────────────

    def _poll_common_registers(self):
        """Read registers shared between VX1000 and H-series."""
        # Brightness
        reg = H_REG_BRIGHTNESS if self.device_type == "h_series" else REG_BRIGHTNESS
        data = self.read_register(*reg)
        if data and len(data) >= 1:
            raw = data[0]
            self._draft["brightness"] = raw
            self._draft["brightness_pct"] = round(raw / 255 * 100, 1)

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

    def _update_aggregates(self, now, cards):
        """Update live_monitoring aggregates and history from card list."""
        if not cards:
            return
        online_cards = [c for c in cards if c.get("online")]
        if not online_cards:
            return

        temps = [c["temperature_c"] for c in online_cards
                 if c.get("temperature_c") is not None]
        volts = [c["voltage_v"] for c in online_cards
                 if c.get("voltage_v") is not None]

        agg = {
            "card_count": len(online_cards),
            "online": True,
        }
        if temps:
            agg["temperature_c"] = round(sum(temps) / len(temps), 1)
            agg["temperature_max_c"] = max(temps)
        if volts:
            agg["voltage_v"] = round(sum(volts) / len(volts), 2)

        self._draft["live_monitoring"].update(agg)

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
        # reconnecting, heartbeating and firing callbacks forever.
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
