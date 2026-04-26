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

    def test_read_register_accepts_port_kwarg(self):
        """Port kwarg must not raise (returns None when disconnected)."""
        dev = NovaStar_Device("dev1", "Test", "192.168.0.10")
        assert dev.read_register(0x0000000A, 0x5200, port=0x03) is None

    def test_read_register_card_accepts_port_kwarg(self):
        dev = NovaStar_Device("dev1", "Test", "192.168.0.10")
        assert dev.read_register_card(0x0000000A, 0x5200, 0, port=0x03) is None


class TestDeviceTypeDetection:
    """Test automatic device type detection from port number."""

    def test_vx1000_default_port(self):
        dev = NovaStar_Device("dev1", "VX1000", "192.168.0.10")
        assert dev.device_type == "vx1000"
        assert dev.state["device_type"] == "vx1000"

    def test_vx1000_explicit_port(self):
        dev = NovaStar_Device("dev1", "VX1000", "192.168.0.10", port=5200)
        assert dev.device_type == "vx1000"

    def test_h_series_port(self):
        dev = NovaStar_Device("dev1", "H-Series", "192.168.0.10", port=5203)
        assert dev.device_type == "h_series"
        assert dev.state["device_type"] == "h_series"

    def test_unknown_port_defaults_vx1000(self):
        dev = NovaStar_Device("dev1", "Test", "192.168.0.10", port=9999)
        assert dev.device_type == "vx1000"


class TestHSeriesState:
    """Test H-series state dict structure."""

    def test_ports_dict_exists(self):
        dev = NovaStar_Device("dev1", "H", "192.168.0.10", port=5203)
        assert "ports" in dev.state
        assert isinstance(dev.state["ports"], dict)
        assert dev.state["ports"] == {}

    def test_port_bitmask_default(self):
        dev = NovaStar_Device("dev1", "H", "192.168.0.10", port=5203)
        assert dev.state["port_bitmask"] == 0

    def test_active_ports_default(self):
        dev = NovaStar_Device("dev1", "H", "192.168.0.10", port=5203)
        assert dev.state["active_ports"] == []

    def test_vx1000_also_has_port_fields(self):
        """VX1000 state dict includes port fields for forward compat."""
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        assert "ports" in dev.state
        assert "port_bitmask" in dev.state
        assert "active_ports" in dev.state


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
