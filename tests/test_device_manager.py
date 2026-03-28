"""Tests for device_manager.py — device state and polling logic."""
import novastar_protocol as proto
from device_manager import NovaStar_Device, DeviceManager


class TestNovaStarDevice:
    def test_initial_state(self):
        dev = NovaStar_Device("dev1", "Test VX1000", "192.168.0.10")
        assert dev.state["device_id"] == "dev1"
        assert dev.state["name"] == "Test VX1000"
        assert dev.state["ip"] == "192.168.0.10"
        assert dev.state["connected"] is False
        assert dev.state["receiving_cards"] == []
        assert dev.state["history"]["temperature"] == []

    def test_default_port(self):
        dev = NovaStar_Device("dev1", "Test", "192.168.0.10")
        assert dev.port == proto.TCP_PORT

    def test_custom_port(self):
        dev = NovaStar_Device("dev1", "Test", "192.168.0.10", port=9999)
        assert dev.port == 9999

    def test_disconnect_clears_state(self):
        dev = NovaStar_Device("dev1", "Test", "192.168.0.10")
        dev.connected = True
        dev.state["connected"] = True
        dev.disconnect()
        assert dev.connected is False
        assert dev.state["connected"] is False
        assert dev.sock is None

    def test_read_register_when_disconnected(self):
        dev = NovaStar_Device("dev1", "Test", "192.168.0.10")
        assert dev.read_register(0x0000000A, 0x5200) is None

    def test_read_register_card_when_disconnected(self):
        dev = NovaStar_Device("dev1", "Test", "192.168.0.10")
        assert dev.read_register_card(0x0000000A, 0x5200, 0) is None


class TestDeviceManager:
    def test_add_device(self):
        mgr = DeviceManager()
        dev = mgr.add_device("dev1", "VX1000", "192.168.0.10")
        assert "dev1" in mgr.devices
        assert dev.name == "VX1000"

    def test_remove_device(self):
        mgr = DeviceManager()
        mgr.add_device("dev1", "VX1000", "192.168.0.10")
        mgr.remove_device("dev1")
        assert "dev1" not in mgr.devices

    def test_get_state_single(self):
        mgr = DeviceManager()
        mgr.add_device("dev1", "VX1000", "192.168.0.10")
        state = mgr.get_state("dev1")
        assert state["name"] == "VX1000"

    def test_get_state_missing(self):
        mgr = DeviceManager()
        assert mgr.get_state("nonexistent") is None

    def test_get_all_states(self):
        mgr = DeviceManager()
        mgr.add_device("dev1", "VX1000", "192.168.0.10")
        mgr.add_device("dev2", "MCTRL660", "192.168.0.11")
        states = mgr.get_all_states()
        assert len(states) == 2

    def test_poll_interval(self):
        mgr = DeviceManager(poll_interval=5.0)
        assert mgr.poll_interval == 5.0

    def test_callbacks(self):
        mgr = DeviceManager()
        called = {}
        mgr.set_callbacks(on_update=lambda did, s: called.update({"update": did}))
        assert mgr._on_update is not None
