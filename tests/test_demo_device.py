"""Tests for demo_device.py — simulated VX1000 data generation."""
import demo_device as demo


class TestGenerateDemoState:
    def test_returns_dict(self):
        state = demo.generate_demo_state()
        assert isinstance(state, dict)

    def test_has_14_receiving_cards(self):
        state = demo.generate_demo_state()
        assert len(state["receiving_cards"]) == 14

    def test_all_cards_online(self):
        state = demo.generate_demo_state()
        for card in state["receiving_cards"]:
            assert card["online"] is True

    def test_temperatures_in_range(self):
        state = demo.generate_demo_state()
        for card in state["receiving_cards"]:
            assert 40 <= card["temperature_c"] <= 70

    def test_voltages_in_range(self):
        state = demo.generate_demo_state()
        for card in state["receiving_cards"]:
            assert 4.5 <= card["voltage_v"] <= 6.0

    def test_device_info_populated(self):
        state = demo.generate_demo_state()
        assert state["device_info"]["header"] == "NSSD"
        assert state["system_info"]["device_type"] == "VX1000"

    def test_live_monitoring_aggregate(self):
        state = demo.generate_demo_state()
        lm = state["live_monitoring"]
        assert lm["online"] is True
        assert lm["card_count"] == 14
        assert lm["link_status"] == "PRIMARY"


class TestDemoDevice:
    def test_initial_state(self):
        dev = demo.DemoDevice()
        assert dev.device_id == "demo-vx1000"
        assert dev.connected is True

    def test_poll_updates_state(self):
        dev = demo.DemoDevice()
        dev.poll()
        assert dev.state["poll_count"] == 1
        assert len(dev.state["history"]["temperature"]) == 1

    def test_poll_accumulates_history(self):
        dev = demo.DemoDevice()
        for _ in range(5):
            dev.poll()
        assert len(dev.state["history"]["temperature"]) == 5
        assert len(dev.state["history"]["voltage"]) == 5
        assert len(dev.state["history"]["timestamps"]) == 5

    def test_disconnect(self):
        dev = demo.DemoDevice()
        dev.disconnect()
        assert dev.connected is False
        assert dev.state["connected"] is False

    def test_connect(self):
        dev = demo.DemoDevice()
        dev.disconnect()
        dev.connect()
        assert dev.connected is True
