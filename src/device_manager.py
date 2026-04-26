"""
NovaStar Device Manager
Manages TCP connections and polls controllers for monitoring data.
Uses threading (not asyncio) for Flask/SocketIO compatibility.
"""

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
    h_card_data_register,
    parse_h_port_bitmask, parse_h_card_link, parse_h_card_temperature,
)


class NovaStar_Device:
    """Represents a single NovaStar controller connection."""

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

        # Live state
        self.state = {
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

    def connect(self):
        """Establish TCP connection."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.ip, self.port))
            self.connected = True
            self.state["connected"] = True
            self.state["error"] = None
            return True
        except Exception as e:
            self.connected = False
            self.state["connected"] = False
            self.state["error"] = str(e)
            return False

    def disconnect(self):
        """Close TCP connection."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self.connected = False
        self.state["connected"] = False

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
                self.state["connected"] = False
                return None

    def read_register_card(self, reg_addr, reg_len, card_index, port=0x00):
        """Send a per-card READ request targeting a specific receiving card (0-based index)."""
        if not self.connected:
            return None

        with self.lock:
            try:
                self.seq = (self.seq + 1) & 0xFFFF
                frame = build_read_card(self.seq, reg_addr, reg_len, card_index,
                                        port=port)
                self.sock.sendall(frame)
                data = self._recv_response(reg_len)
                result = parse_response(data)
                return result[1] if result else None
            except socket.timeout:
                return None
            except (ConnectionResetError, BrokenPipeError, OSError):
                self.connected = False
                self.state["connected"] = False
                return None

    def _recv_response(self, reg_len):
        """Receive a response frame, reading enough bytes for the expected payload."""
        expected_payload = decode_length(reg_len)
        # header(18) + payload + checksum(2)
        expected_total = 18 + expected_payload + 2
        # Use a reasonable buffer: at least expected_total, but cap at 64KB
        buf_size = min(max(expected_total, 8192), 65536)
        return self.sock.recv(buf_size)

    def poll(self):
        """Read all monitoring registers and update state."""
        if not self.connected:
            if not self.connect():
                return

        now = datetime.now()
        self.state["last_poll"] = now.strftime("%H:%M:%S")
        self.state["poll_count"] += 1

        if self.device_type == "h_series":
            self._poll_h_series(now)
        else:
            self._poll_vx1000(now)

    # ── VX1000 Polling ────────────────────────────────────

    def _poll_vx1000(self, now):
        """Poll a VX1000-type controller (port 5200)."""
        # System Info
        data = self.read_register(*REG_SYSTEM_INFO)
        if data:
            info = parse_system_info(data)
            if info:
                self.state["system_info"] = info

        # Firmware Version
        data = self.read_register(*REG_FIRMWARE)
        if data and len(data) >= 2:
            self.state["firmware_version"] = f"{data[0]}.{data[1]}"

        # Device Info (NSSD) Port 1
        data = self.read_register(*REG_DEVICE_PORT1)
        if data:
            info = parse_nssd(data)
            if info:
                self.state["device_info"] = info

        # Port 2 status
        data = self.read_register(*REG_DEVICE_PORT2)
        self.state["port2_active"] = bool(data and len(data) > 4 and any(b != 0 for b in data[:10]))

        # Common registers
        self._poll_common_registers()

        # Live Monitoring — broadcast read kept for backward compat; per-card reads below
        # are the authoritative source for temperature/voltage/link data.
        data = self.read_register(*REG_LIVE_MONITOR)
        if data:
            mon = parse_live_monitoring(data)
            if mon:
                self.state["live_monitoring"] = mon

        # Video Status
        data = self.read_register(*REG_VIDEO_STATUS)
        if data and len(data) >= 20:
            first = data[0]
            if first == 0x1C:
                self.state["video_status"] = {
                    "format": "receiving_card",
                    "signal_detected": bool(data[14]),
                    "input_valid": bool(data[15]),
                }
            elif first == 0x00 and len(data) > 22:
                self.state["video_status"] = {
                    "format": "sending_card",
                    "timestamp": (
                        f"20{data[17]:02d}-{data[18]:02d}-{data[19]:02d} "
                        f"{data[20]:02d}:{data[21]:02d}:{data[22]:02d}"
                    ),
                }

        # Per-card live monitoring — query each receiving card directly.
        detected_count = self.state.get("_detected_card_count", 0)
        scan_limit = detected_count if detected_count > 0 else 16
        cards = []
        for i in range(scan_limit):
            cdata = self.read_register_card(*REG_LIVE_MONITOR, i)
            if cdata:
                mon = parse_live_monitoring(cdata)
                if mon and mon.get("online"):
                    if detected_count == 0 and mon.get("card_count", 0) > 0:
                        detected_count = mon["card_count"]
                        self.state["_detected_card_count"] = detected_count
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
            self.state["receiving_cards"] = cards
        self._update_aggregates(now, cards)

    # ── H-Series Polling ──────────────────────────────────

    def _poll_h_series(self, now):
        """Poll an H-series controller (port 5203).

        H-series has up to 15 output ports, each with its own chain
        of receiving cards (up to 91 at 60×120 resolution).
        """
        # Firmware
        data = self.read_register(*H_REG_FIRMWARE)
        if data and len(data) >= 2:
            self.state["firmware_version"] = f"{data[0]}.{data[1]}"

        # Common registers (brightness, gamma, datetime)
        self._poll_common_registers()

        # Broadcast video status — port connectivity bitmask
        data = self.read_register(*H_REG_VIDEO_STATUS)
        if data and len(data) >= 32:
            port_map = parse_h_port_bitmask(data)
            self.state["port_bitmask"] = data[31]
            self.state["active_ports"] = sorted(
                p for p, connected in port_map.items() if connected
            )
            self.state["video_status"] = {
                "format": "h_series",
                "port_bitmask": f"0x{data[31]:02X}",
                "active_port_count": len(self.state["active_ports"]),
            }

        # Device identity (NSSD)
        data = self.read_register(*H_REG_DEVICE_ID)
        if data:
            info = parse_nssd(data)
            if info:
                self.state["device_info"] = info

        # Poll each connected port's cards.
        # To avoid long poll cycles, we round-robin one port per poll
        # unless the total card count is small enough to poll all.
        active_ports = self.state.get("active_ports", [])
        if not active_ports:
            return

        # Determine which port(s) to poll this cycle
        total_known_cards = sum(
            self.state["ports"].get(p, {}).get("card_count", 0)
            for p in active_ports
        )
        if total_known_cards <= 30 or len(active_ports) <= 2:
            # Small system: poll all ports every cycle
            ports_to_poll = active_ports
        else:
            # Large system: round-robin one port per cycle
            rr_idx = self.state.get("_h_port_rr", 0) % len(active_ports)
            ports_to_poll = [active_ports[rr_idx]]
            self.state["_h_port_rr"] = rr_idx + 1

        for port_num in ports_to_poll:
            self._poll_h_port(port_num)

        # Flatten per-port cards into receiving_cards for backward compatibility
        all_cards = []
        for port_num in sorted(self.state["ports"].keys()):
            port_data = self.state["ports"][port_num]
            all_cards.extend(port_data.get("cards", []))
        self.state["receiving_cards"] = all_cards

        self._update_aggregates(now, all_cards)

    def _poll_h_port(self, port_num):
        """Poll receiving cards on a single H-series output port."""
        port_state = self.state["ports"].setdefault(port_num, {
            "connected": True,
            "card_count": 0,
            "cards": [],
        })
        port_state["connected"] = True

        # Scan cards on this port. Use detected count if known,
        # otherwise probe up to 16 to discover the actual count.
        detected_key = f"_h_port_{port_num}_cards"
        detected_count = self.state.get(detected_key, 0)
        scan_limit = detected_count if detected_count > 0 else 16
        consecutive_offline = 0

        cards = []
        for i in range(scan_limit):
            # Read video status for this card on this port
            cdata = self.read_register_card(*H_REG_VIDEO_STATUS, i, port=port_num)

            card_online = False
            card_info = {
                "index": i,
                "label": f"P{port_num}C{i + 1:02d}",
                "port": port_num,
                "online": False,
            }

            if cdata and len(cdata) >= 2:
                link_info = parse_h_card_link(cdata)
                if link_info:
                    connected_paths, total_paths = link_info
                    card_online = connected_paths > 0
                    card_info.update({
                        "online": card_online,
                        "link_paths": f"{connected_paths}/{total_paths}",
                        "link_raw": cdata[1],
                    })

            if card_online:
                consecutive_offline = 0
                # Read temperature from data channel 0
                tdata = self.read_register_card(*h_card_data_register(0), i,
                                                port=port_num)
                temp = parse_h_card_temperature(tdata)
                if temp is not None:
                    card_info["temperature_c"] = temp
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
            self.state[detected_key] = detected_count

        # Only keep cards up to the detected count
        if detected_count > 0:
            cards = cards[:detected_count]

        port_state["cards"] = cards
        port_state["card_count"] = len([c for c in cards if c.get("online")])

    # ── Common Registers & Aggregates ─────────────────────

    def _poll_common_registers(self):
        """Read registers shared between VX1000 and H-series."""
        # Brightness
        reg = H_REG_BRIGHTNESS if self.device_type == "h_series" else REG_BRIGHTNESS
        data = self.read_register(*reg)
        if data and len(data) >= 1:
            raw = data[0]
            self.state["brightness"] = raw
            self.state["brightness_pct"] = round(raw / 255 * 100, 1)

        # Gamma
        reg = H_REG_GAMMA if self.device_type == "h_series" else REG_GAMMA
        data = self.read_register(*reg)
        if data and len(data) >= 2:
            import struct
            self.state["gamma"] = struct.unpack(">H", data[:2])[0]

        # Date/Time
        reg = H_REG_DATETIME if self.device_type == "h_series" else REG_DATETIME
        data = self.read_register(*reg)
        if data and len(data) >= 6:
            self.state["datetime"] = (
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

        temps = [c["temperature_c"] for c in online_cards if "temperature_c" in c]
        volts = [c["voltage_v"] for c in online_cards if "voltage_v" in c]

        agg = {
            "card_count": len(online_cards),
            "online": True,
        }
        if temps:
            agg["temperature_c"] = round(sum(temps) / len(temps), 1)
            agg["temperature_max_c"] = max(temps)
        if volts:
            agg["voltage_v"] = round(sum(volts) / len(volts), 2)

        self.state["live_monitoring"].update(agg)

        # History (keep last 300 samples)
        hist = self.state["history"]
        if temps:
            hist["temperature"].append(agg["temperature_c"])
        if volts:
            hist["voltage"].append(agg["voltage_v"])
        hist["timestamps"].append(now.strftime("%H:%M:%S"))
        for key in ("temperature", "voltage", "timestamps"):
            if len(hist[key]) > 300:
                hist[key] = hist[key][-300:]


class DeviceManager:
    """Manages multiple NovaStar devices and their polling threads."""

    def __init__(self, poll_interval=2.0):
        self.devices = {}
        self.poll_interval = poll_interval
        self._threads = {}
        self._running = False
        self._on_update = None  # Callback: fn(device_id, state)
        self._on_error = None   # Callback: fn(device_id, error_info)

    def set_callbacks(self, on_update=None, on_error=None):
        self._on_update = on_update
        self._on_error = on_error

    def add_device(self, device_id, name, ip, port=TCP_PORT):
        """Add a device to monitor."""
        dev = NovaStar_Device(device_id, name, ip, port)
        self.devices[device_id] = dev

        if self._running:
            self._start_device_thread(device_id)

        return dev

    def remove_device(self, device_id):
        """Stop monitoring a device."""
        if device_id in self.devices:
            self.devices[device_id].disconnect()
            del self.devices[device_id]

    def get_state(self, device_id=None):
        """Get current state for one or all devices."""
        if device_id:
            dev = self.devices.get(device_id)
            return dev.state if dev else None
        return {did: dev.state for did, dev in self.devices.items()}

    def get_all_states(self):
        """Get all device states as a list."""
        return [dev.state for dev in self.devices.values()]

    def start(self):
        """Start polling all devices."""
        self._running = True
        for device_id in self.devices:
            self._start_device_thread(device_id)

    def stop(self):
        """Stop all polling and disconnect."""
        self._running = False
        for dev in self.devices.values():
            dev.disconnect()

    def _start_device_thread(self, device_id):
        """Start a polling thread for a device."""
        def poll_loop():
            dev = self.devices.get(device_id)
            while self._running and dev:
                try:
                    dev.poll()
                    if self._on_update:
                        self._on_update(device_id, dev.state)
                except Exception as e:
                    dev.state["error"] = str(e)
                    if self._on_error:
                        self._on_error(device_id, {"error": str(e)})
                time.sleep(self.poll_interval)

        t = threading.Thread(target=poll_loop, daemon=True, name=f"poll-{device_id}")
        t.start()
        self._threads[device_id] = t
