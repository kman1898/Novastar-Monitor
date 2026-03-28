"""
NovaStar Monitor — Demo/Offline Device
Generates simulated VX1000 data for UI testing without live hardware.

Data is modeled after real Wireshark captures from a VX1000 with 14 receiving cards.
Temperature and voltage values drift realistically around capture baselines.
"""

import random
import time
import math
from datetime import datetime

# Baselines from actual VX1000 Wireshark captures
_CARD_TEMPS_BASELINE = [55, 56, 58, 57, 59, 57, 58, 58, 57, 58, 57, 56, 56, 54]
_CARD_VOLTS_BASELINE = [5.19, 5.19, 5.16, 5.16, 5.16, 5.19, 5.16, 5.19,
                        5.19, 5.19, 5.22, 5.22, 5.19, 5.22]
_CARD_COUNT = 14
_MAC_PREFIX = "32:54:76:98:"


def _drift(base, amplitude=1.5, period=120):
    """Sinusoidal drift around a baseline, plus small random noise."""
    t = time.time()
    return base + amplitude * math.sin(t / period) + random.uniform(-0.3, 0.3)


def generate_demo_state(poll_count=0):
    """Generate a complete device state dict matching NovaStar_Device.state format."""
    now = datetime.now()
    cards = []
    temps = []
    volts = []

    for i in range(_CARD_COUNT):
        temp = round(_drift(_CARD_TEMPS_BASELINE[i], amplitude=1.5, period=90 + i * 5), 1)
        volt = round(_drift(_CARD_VOLTS_BASELINE[i], amplitude=0.04, period=60 + i * 3), 2)
        temps.append(temp)
        volts.append(volt)

        cards.append({
            "index": i,
            "label": f"C{i + 1:02d}",
            "online": True,
            "temperature_c": temp,
            "voltage_v": volt,
            "link_status": "PRIMARY",
            "link_raw": 1,
            "firmware": "2.16",
            "mac_address": f"{_MAC_PREFIX}{i:02X}:0C",
        })

    avg_temp = round(sum(temps) / len(temps), 1)
    avg_volt = round(sum(volts) / len(volts), 2)
    max_temp = max(temps)

    # Simulate brightness that slowly changes
    brightness_raw = int(128 + 60 * math.sin(time.time() / 300))
    brightness_pct = round(brightness_raw / 255 * 100, 1)

    return {
        "device_id": "demo-vx1000",
        "name": "Demo VX1000",
        "ip": "192.168.0.100",
        "port": 5200,
        "connected": True,
        "last_poll": now.strftime("%H:%M:%S"),
        "poll_count": poll_count,
        "error": None,
        "system_info": {
            "hw_version": "3.7",
            "build_date": "2023-06-15",
            "device_type": "VX1000",
            "ethernet_ports": 2,
            "input_count": 4,
        },
        "device_info": {
            "header": "NSSD",
            "active": True,
            "model_code": "0x0058",
            "serial": "DEMO-001",
            "hw_revision": "Rev C",
        },
        "port2_active": True,
        "brightness": brightness_raw,
        "brightness_pct": brightness_pct,
        "gamma": 2200,
        "datetime": now.strftime("20%y-%m-%d %H:%M"),
        "firmware_version": "2.16",
        "live_monitoring": {
            "online": True,
            "temperature_c": avg_temp,
            "temperature_max_c": max_temp,
            "voltage_v": avg_volt,
            "card_count": _CARD_COUNT,
            "link_status": "PRIMARY",
            "link_raw": 1,
            "firmware": "2.16",
            "mac_address": "32:54:76:98:BA:0C",
            "scan_lines": 16,
        },
        "video_status": {
            "format": "sending_card",
            "timestamp": now.strftime("20%y-%m-%d %H:%M:%S"),
        },
        "receiving_cards": cards,
        "history": {
            "temperature": [],
            "voltage": [],
            "timestamps": [],
        },
    }


class DemoDevice:
    """A simulated NovaStar device that generates realistic monitoring data.

    Drop-in replacement for NovaStar_Device in the device manager.
    """

    DEVICE_ID = "demo-vx1000"

    def __init__(self):
        self.device_id = self.DEVICE_ID
        self.name = "Demo VX1000"
        self.ip = "192.168.0.100"
        self.port = 5200
        self.connected = True
        self._poll_count = 0
        self.state = generate_demo_state()

    def connect(self):
        self.connected = True
        self.state["connected"] = True
        return True

    def disconnect(self):
        self.connected = False
        self.state["connected"] = False

    def poll(self):
        """Generate fresh simulated data each poll cycle."""
        self._poll_count += 1
        new_state = generate_demo_state(self._poll_count)

        # Preserve history across polls
        hist = self.state["history"]
        lm = new_state["live_monitoring"]
        hist["temperature"].append(lm["temperature_c"])
        hist["voltage"].append(lm["voltage_v"])
        hist["timestamps"].append(datetime.now().strftime("%H:%M:%S"))
        for key in ("temperature", "voltage", "timestamps"):
            if len(hist[key]) > 300:
                hist[key] = hist[key][-300:]

        new_state["history"] = hist
        self.state = new_state
