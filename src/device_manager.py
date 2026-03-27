"""
NovaStar Device Manager
Manages TCP connections and polls controllers for monitoring data.
Uses threading (not asyncio) for Flask/SocketIO compatibility.
"""

import socket
import threading
import time
import json
from datetime import datetime
from novastar_protocol import (
    TCP_PORT, build_read, parse_response, parse_live_monitoring,
    parse_system_info, parse_nssd, per_card_address,
    REG_SYSTEM_INFO, REG_FIRMWARE, REG_DEVICE_PORT1, REG_DEVICE_PORT2,
    REG_BRIGHTNESS, REG_GAMMA, REG_DATETIME, REG_VIDEO_STATUS,
    REG_LIVE_MONITOR, REG_PORT_INFO, REG_CARD_CONFIG,
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

        # Live state
        self.state = {
            "device_id": device_id,
            "name": name,
            "ip": ip,
            "connected": False,
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

    def read_register(self, reg_addr, reg_len):
        """Send a READ request and return the response payload."""
        if not self.connected:
            return None

        with self.lock:
            try:
                self.seq = (self.seq + 1) & 0xFFFF
                frame = build_read(self.seq, reg_addr, reg_len)
                self.sock.sendall(frame)
                data = self.sock.recv(8192)
                result = parse_response(data)
                return result[1] if result else None
            except socket.timeout:
                return None
            except (ConnectionResetError, BrokenPipeError, OSError):
                self.connected = False
                self.state["connected"] = False
                return None

    def poll(self):
        """Read all monitoring registers and update state."""
        if not self.connected:
            if not self.connect():
                return

        now = datetime.now()
        self.state["last_poll"] = now.strftime("%H:%M:%S")
        self.state["poll_count"] += 1

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

        # Brightness
        data = self.read_register(*REG_BRIGHTNESS)
        if data and len(data) >= 1:
            raw = data[0]
            self.state["brightness"] = raw
            self.state["brightness_pct"] = round(raw / 255 * 100, 1)

        # Gamma
        data = self.read_register(*REG_GAMMA)
        if data and len(data) >= 2:
            import struct
            self.state["gamma"] = struct.unpack(">H", data[:2])[0]

        # Date/Time
        data = self.read_register(*REG_DATETIME)
        if data and len(data) >= 6:
            self.state["datetime"] = f"20{data[0]:02d}-{data[1]:02d}-{data[2]:02d} {data[4]:02d}:{data[5]:02d}"

        # Live Monitoring (temperature, voltage, cards, link)
        data = self.read_register(*REG_LIVE_MONITOR)
        if data:
            mon = parse_live_monitoring(data)
            if mon:
                self.state["live_monitoring"] = mon

                # Update history (keep last 300 samples)
                hist = self.state["history"]
                hist["temperature"].append(mon["temperature_c"])
                hist["voltage"].append(mon["voltage_v"])
                hist["timestamps"].append(now.strftime("%H:%M:%S"))
                max_hist = 300
                for key in ("temperature", "voltage", "timestamps"):
                    if len(hist[key]) > max_hist:
                        hist[key] = hist[key][-max_hist:]

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
                    "timestamp": f"20{data[17]:02d}-{data[18]:02d}-{data[19]:02d} {data[20]:02d}:{data[21]:02d}:{data[22]:02d}",
                }

        # Per-card scan (every 5th poll to avoid slowdown)
        card_count = self.state["live_monitoring"].get("card_count", 0)
        if card_count > 0 and self.state["poll_count"] % 5 == 1:
            cards = []
            for i in range(1, card_count + 1):
                addr = per_card_address(i)
                cdata = self.read_register(addr, 0x0001)
                has_data = bool(cdata and len(cdata) > 4 and any(b != 0 for b in cdata[:10]))
                cards.append({
                    "index": i,
                    "label": f"C{i:02d}",
                    "has_data": has_data,
                })
            self.state["receiving_cards"] = cards


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
