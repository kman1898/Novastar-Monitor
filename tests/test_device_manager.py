"""Tests for device_manager.py — device state and polling logic."""
import json
import socket
import threading
import time
from datetime import datetime

import device_manager
import novastar_protocol as proto
from device_manager import NovaStar_Device, DeviceManager


# ── Test doubles (no sockets are ever opened) ─────────────


class StubJSONClient:
    """Stand-in for HSeriesJSONClient. Records calls, talks to nothing."""

    def __init__(self, responses=None):
        # {(slot, port, card_id): r0155_response_dict}
        self.responses = responses or {}
        self.batch_calls = []
        self.single_calls = []
        self.closed = False

    def get_receiving_cards_batch(self, addresses, **kwargs):
        addresses = [tuple(a) for a in addresses]
        self.batch_calls.append(addresses)
        return [self.responses.get(a) for a in addresses]

    def get_receiving_card(self, slot_id, port_id, card_id):
        self.single_calls.append((slot_id, port_id, card_id))
        return self.responses.get((slot_id, port_id, card_id))

    def close(self):
        self.closed = True


class FakeDevice:
    """Minimal device for exercising DeviceManager's threading."""

    def __init__(self, device_id="fake"):
        self.device_id = device_id
        self.polls = 0
        self.disconnected = False
        self.state = {"device_id": device_id, "name": device_id, "error": None}

    def poll(self):
        self.polls += 1

    def disconnect(self):
        self.disconnected = True


class FakeSocket:
    """Captures the frames the device would have put on the wire."""

    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _bufsize):
        raise socket.timeout()

    def close(self):
        pass


def _r0155(slot=20, port=0, card_id=0, temp=88, voltage=170):
    """A real-shaped R0155 reply (field names from the 192.168.0.10 capture)."""
    return {
        "deviceId": 0, "slotId": slot, "portId": port, "recvCardId": card_id,
        "power0Status": 0, "power1Status": 0, "brightness": 127,
        "temp": temp, "voltage": voltage, "cmd": "R0155", "ack": "Ok",
    }


def _h_device(json_client=None, cards=None):
    """An H-series device wired to a stub JSON client — no I/O."""
    dev = NovaStar_Device("dev1", "H", "192.168.0.10", port=proto.H_TCP_PORT)
    dev.json_client = json_client if json_client is not None else StubJSONClient()
    if cards is not None:
        dev._known_cards = cards
    return dev


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

    def test_default_poll_interval_matches_the_shared_constant(self):
        """A caller that omits the interval must not poll 5x faster than the
        value the settings UI advertises — the signature default used to be
        2.0 while app.DEFAULT_POLL_INTERVAL was 10.0."""
        mgr = DeviceManager()
        assert mgr.poll_interval == device_manager.DEFAULT_POLL_INTERVAL
        assert device_manager.DEFAULT_POLL_INTERVAL == 10.0

    def test_app_reads_the_managers_default_not_its_own(self):
        """device_manager owns the constant; app imports it (it cannot import
        the other way round without a cycle)."""
        import app as appmod
        assert appmod.DEFAULT_POLL_INTERVAL is device_manager.DEFAULT_POLL_INTERVAL
        assert appmod.DEFAULT_SETTINGS['poll_interval'] == \
            device_manager.DEFAULT_POLL_INTERVAL

    def test_callbacks(self):
        mgr = DeviceManager()
        called = {}
        mgr.set_callbacks(on_update=lambda did, s: called.update({"update": did}))
        assert mgr._on_update is not None


class TestPerCardAddressing:
    """byte[7] = chain, byte[8] = card, byte[5] = OPT group (§6.5)."""

    def _frame(self, dev):
        assert dev.sock.sent, "expected a frame on the wire"
        return dev.sock.sent[-1]

    def _connected(self):
        dev = NovaStar_Device("dev1", "H", "192.168.0.10", port=proto.H_TCP_PORT)
        dev.sock = FakeSocket()
        dev.connected = True
        return dev

    def test_chain_and_card_land_in_separate_bytes(self):
        dev = self._connected()
        dev.read_register_card(*proto.REG_LIVE_MONITOR, 5, chain=3)
        frame = self._frame(dev)
        assert frame[6] == 0x01   # per-card marker
        assert frame[7] == 3      # chain index
        assert frame[8] == 5      # card position within the chain

    def test_chain_is_not_the_opt_group(self):
        """A chain must never be written to byte[5] — that's the OPT group."""
        dev = self._connected()
        dev.read_register_card(*proto.REG_LIVE_MONITOR, 0, chain=7)
        frame = self._frame(dev)
        assert frame[5] == 0x00
        assert frame[7] == 7

    def test_chain_defaults_to_zero(self):
        dev = self._connected()
        dev.read_register_card(*proto.REG_LIVE_MONITOR, 2)
        frame = self._frame(dev)
        assert frame[7] == 0
        assert frame[8] == 2

    def test_poll_h_port_converts_port_number_to_chain(self):
        """Port numbers are 1-based; the wire wants a 0-based chain index."""
        dev = NovaStar_Device("dev1", "H", "192.168.0.10", port=proto.H_TCP_PORT)
        calls = []

        def fake_read(reg, length, card_index, chain=0, port=0x00):
            calls.append({"chain": chain, "card": card_index, "opt": port})
            return None

        dev.read_register_card = fake_read
        dev._poll_h_port(5)

        assert calls, "expected per-card reads"
        assert all(c["chain"] == 4 for c in calls)   # port 5 → chain 4
        assert all(c["opt"] == 0x00 for c in calls)


class TestRefreshKnownCards:
    """The per-card refresh that produces every number on the dashboard."""

    def test_voltage_uses_vendor_formula(self):
        """raw 170 → 5.1 V, not the 4.2 V the masked /10 formula produced."""
        client = StubJSONClient({(20, 0, 0): _r0155(voltage=170)})
        dev = _h_device(client, [{"slot": 20, "port": 0, "card_id": 0}])
        card = dev._refresh_known_cards()[0]
        assert card["voltage_v"] == 5.1
        assert card["voltage_v"] != 4.2
        # Above the app's 4.7 V low-voltage alarm threshold.
        assert card["voltage_v"] > 4.7

    def test_voltage_range_of_the_real_wall_reads_healthy(self):
        """The wall's raw 165-173 must decode to a healthy 5 V rail."""
        addresses = [(20, 0, i) for i in range(9)]
        client = StubJSONClient({
            addr: _r0155(card_id=addr[2], voltage=raw)
            for addr, raw in zip(addresses, range(165, 174))
        })
        dev = _h_device(client, [
            {"slot": s, "port": p, "card_id": c} for s, p, c in addresses])
        volts = [c["voltage_v"] for c in dev._refresh_known_cards()]
        assert min(volts) == 4.95
        assert max(volts) == 5.19
        assert all(v > 4.7 for v in volts)

    def test_temperature_and_power_flags(self):
        client = StubJSONClient({(20, 0, 0): _r0155(temp=88)})
        dev = _h_device(client, [{"slot": 20, "port": 0, "card_id": 0}])
        card = dev._refresh_known_cards()[0]
        assert card["temp_c"] == 44.0
        assert card["temperature_c"] == 44.0
        assert card["primary_power_ok"] is True
        assert card["backup_power_ok"] is True
        assert card["brightness"] == 127

    def test_silent_card_is_offline_and_keeps_identity(self):
        client = StubJSONClient({})   # device answers nothing
        dev = _h_device(client, [
            {"slot": 20, "port": 0, "card_id": 7, "card_number": 8}])
        card = dev._refresh_known_cards()[0]
        assert card["online"] is False
        assert card["card_id"] == 7
        assert card["card_number"] == 8

    def test_uses_one_batched_read_not_one_call_per_card(self):
        addresses = [(20, 0, i) for i in range(20)]
        client = StubJSONClient({
            addr: _r0155(card_id=addr[2]) for addr in addresses})
        dev = _h_device(client, [
            {"slot": s, "port": p, "card_id": c} for s, p, c in addresses])
        cards = dev._refresh_known_cards()
        assert len(cards) == 20
        assert client.single_calls == []          # no per-card round trips
        assert len(client.batch_calls) == 1
        assert client.batch_calls[0] == addresses

    def test_results_stay_aligned_with_addresses(self):
        """A silent card in the middle must not shift later cards' readings."""
        client = StubJSONClient({
            (20, 0, 0): _r0155(card_id=0, temp=80),
            (20, 0, 2): _r0155(card_id=2, temp=100),
        })
        dev = _h_device(client, [
            {"slot": 20, "port": 0, "card_id": i} for i in range(3)])
        cards = dev._refresh_known_cards()
        assert [c["card_id"] for c in cards] == [0, 1, 2]
        assert cards[0]["temp_c"] == 40.0
        assert cards[1]["online"] is False
        assert cards[2]["temp_c"] == 50.0


class TestSnapshotLoading:
    def _write(self, tmp_path, monkeypatch, payload):
        path = tmp_path / "wall_live_snapshot.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(device_manager, "SNAPSHOT_PATH", str(path))
        return path

    def test_loads_card_addresses(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, {"cards": [
            {"card_number": 1, "slot": 20, "user_slot": 21, "opt": 1,
             "port": 0, "port_on_opt": 1, "card_id": 0},
            {"card_number": 2, "slot": 20, "port": 0, "card_id": 1},
        ]})
        dev = _h_device()
        cards = dev._load_known_cards_from_snapshot()
        assert len(cards) == 2
        assert cards[0]["slot"] == 20
        assert cards[0]["card_id"] == 0
        assert cards[0]["user_slot"] == 21
        assert cards[1]["user_slot"] is None

    def test_tolerates_captured_at(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, {
            "captured_at": "2026-08-08T12:00:00",
            "cards": [{"slot": 20, "port": 0, "card_id": 0}],
        })
        dev = _h_device()
        assert len(dev._load_known_cards_from_snapshot()) == 1

    def test_missing_snapshot_logs_and_returns_empty(self, tmp_path, monkeypatch,
                                                    caplog):
        monkeypatch.setattr(device_manager, "SNAPSHOT_PATH",
                            str(tmp_path / "nope.json"))
        dev = _h_device()
        with caplog.at_level("WARNING", logger="novastar_monitor.device_manager"):
            assert dev._load_known_cards_from_snapshot() == []
        assert "snapshot" in caplog.text.lower()

    def test_corrupt_snapshot_logs_and_returns_empty(self, tmp_path, monkeypatch,
                                                     caplog):
        path = tmp_path / "wall_live_snapshot.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(device_manager, "SNAPSHOT_PATH", str(path))
        dev = _h_device()
        with caplog.at_level("ERROR", logger="novastar_monitor.device_manager"):
            assert dev._load_known_cards_from_snapshot() == []
        assert "unreadable" in caplog.text.lower()

    def test_entries_missing_an_address_are_skipped(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, {"cards": [
            {"slot": 20, "port": 0, "card_id": 0},
            {"slot": 20, "port": 0},          # no card_id
        ]})
        dev = _h_device()
        assert len(dev._load_known_cards_from_snapshot()) == 1

    def test_refresh_returns_empty_without_a_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(device_manager, "SNAPSHOT_PATH",
                            str(tmp_path / "nope.json"))
        dev = _h_device()
        assert dev._refresh_known_cards() == []


class TestAggregates:
    def _cards(self):
        return [
            {"online": True, "temperature_c": 40.0, "voltage_v": 5.1},
            {"online": True, "temperature_c": 44.0, "voltage_v": 5.19},
            {"online": False},
        ]

    def test_averages_and_max_ignore_offline_cards(self):
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        dev._update_aggregates(datetime.now(), self._cards())
        lm = dev._draft["live_monitoring"]
        assert lm["card_count"] == 2
        assert lm["temperature_c"] == 42.0
        assert lm["temperature_max_c"] == 44.0
        assert lm["voltage_v"] == 5.14
        assert lm["online"] is True

    def test_history_arrays_stay_index_aligned(self):
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        # Cycle 1: full readings. Cycle 2: temperature only.
        dev._update_aggregates(datetime.now(), self._cards())
        dev._update_aggregates(datetime.now(),
                               [{"online": True, "temperature_c": 41.0}])
        hist = dev._draft["history"]
        assert len(hist["temperature"]) == len(hist["voltage"]) \
            == len(hist["timestamps"]) == 2
        # The missing voltage sample is a hole, not a missing element.
        assert hist["voltage"][1] is None
        assert hist["temperature"][1] == 41.0

    def test_history_is_trimmed_to_the_limit(self):
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        now = datetime.now()
        for _ in range(device_manager.HISTORY_LIMIT + 25):
            dev._update_aggregates(now, self._cards())
        hist = dev._draft["history"]
        for key in ("temperature", "voltage", "timestamps"):
            assert len(hist[key]) == device_manager.HISTORY_LIMIT

    def test_no_online_cards_records_nothing(self):
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        dev._update_aggregates(datetime.now(), [{"online": False}])
        assert dev._draft["history"]["timestamps"] == []


class TestPublishedState:
    """get_state / get_all_states must hand out something safe to serialize."""

    def test_state_is_a_snapshot_not_the_working_draft(self):
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        assert dev.state is not dev._draft
        before = dev.state
        dev._draft["receiving_cards"].append({"online": True})
        assert before["receiving_cards"] == []      # snapshot unaffected
        dev._publish()
        assert dev.state is not before
        assert len(dev.state["receiving_cards"]) == 1

    def test_nested_containers_are_copied(self):
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        published = dev.state
        assert published["history"] is not dev._draft["history"]
        assert published["history"]["temperature"] is not \
            dev._draft["history"]["temperature"]

    def test_get_all_states_is_serializable_while_state_churns(self):
        """json.dumps must never trip over a dict another thread is editing."""
        mgr = DeviceManager()
        dev = NovaStar_Device("dev1", "H", "192.168.0.10", port=proto.H_TCP_PORT)
        mgr.devices["dev1"] = dev

        stop = threading.Event()

        def churn():
            n = 0
            while not stop.is_set():
                n = (n + 1) % 40
                dev._draft["ports"] = {p: {"cards": [], "card_count": p}
                                       for p in range(n)}
                dev._draft["receiving_cards"] = [{"i": i} for i in range(n)]
                dev._publish()

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            for _ in range(300):
                json.dumps(mgr.get_all_states())
        finally:
            stop.set()
            t.join(timeout=2.0)

    def test_demo_style_device_gets_a_defensive_copy(self):
        mgr = DeviceManager()
        fake = FakeDevice("demo")
        mgr.devices["demo"] = fake
        handed_out = mgr.get_state("demo")
        assert handed_out == fake.state
        assert handed_out is not fake.state


class TestPollThreadLifecycle:
    def _running_manager(self, device_id="fake"):
        mgr = DeviceManager(poll_interval=0.01)
        dev = FakeDevice(device_id)
        mgr.devices[device_id] = dev
        mgr._running = True
        mgr._start_device_thread(device_id)
        return mgr, dev

    def _wait_for_polls(self, dev, count=2, timeout=2.0):
        deadline = time.monotonic() + timeout
        while dev.polls < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert dev.polls >= count, "poll thread never ran"

    def test_remove_device_stops_the_poll_thread(self):
        mgr, dev = self._running_manager()
        self._wait_for_polls(dev)

        mgr.remove_device("fake")
        assert dev.disconnected is True
        time.sleep(0.1)
        settled = dev.polls
        time.sleep(0.1)
        assert dev.polls == settled, "thread kept polling a removed device"

    def test_stop_joins_threads(self):
        mgr, dev = self._running_manager()
        self._wait_for_polls(dev)
        threads = list(mgr._threads.values())

        mgr.stop(timeout=2.0)
        assert mgr._running is False
        assert dev.disconnected is True
        assert all(not t.is_alive() for t in threads)
        assert mgr._threads == {}

    def test_readding_an_id_leaves_only_one_thread(self):
        mgr, first = self._running_manager()
        self._wait_for_polls(first)

        second = FakeDevice("fake")
        mgr.devices["fake"] = second
        mgr._start_device_thread("fake")
        self._wait_for_polls(second)

        # The original thread must have exited rather than double-polling.
        time.sleep(0.1)
        settled = first.polls
        time.sleep(0.1)
        assert first.polls == settled
        assert len(mgr._threads) == 1
        mgr.stop(timeout=2.0)

    def test_poll_errors_are_reported_not_fatal(self):
        mgr = DeviceManager(poll_interval=0.01)
        dev = FakeDevice("boom")

        def explode():
            dev.polls += 1
            raise RuntimeError("no route to host")

        dev.poll = explode
        mgr.devices["boom"] = dev
        errors = []
        mgr.set_callbacks(on_error=lambda did, info: errors.append(info))
        mgr._running = True
        mgr._start_device_thread("boom")
        self._wait_for_polls(dev)
        mgr.stop(timeout=2.0)

        assert errors and "no route to host" in errors[0]["error"]
        assert dev.state["error"] == "no route to host"


class TestJSONPathAvailability:
    def test_transient_failures_do_not_disable_json_forever(self):
        dev = _h_device()
        dev._json_ever_worked = True
        dev._json_consecutive_fails = 99
        assert dev._json_should_try() is True

    def test_json_is_abandoned_only_if_it_never_worked(self):
        dev = _h_device()
        dev._json_consecutive_fails = dev._json_max_fails
        assert dev._json_should_try() is False

    def test_json_runs_without_a_tcp_connection(self):
        """A busy/filtered TCP 5203 must not disable JSON UDP monitoring."""
        dev = _h_device()
        called = {}
        dev._ensure_tcp = lambda: (_ for _ in ()).throw(
            AssertionError("JSON must not wait on TCP"))
        dev.json_client.get_device_details = lambda: called.setdefault("hit", True)
        dev._poll_h_series_json = lambda now, r0100: called.setdefault("json", True)

        dev._poll_h_series(datetime.now())
        assert called.get("json") is True

    def test_poll_never_opens_tcp_for_a_working_json_device(self):
        dev = _h_device()
        dev._ensure_tcp = lambda: (_ for _ in ()).throw(
            AssertionError("JSON must not wait on TCP"))
        dev.json_client.get_device_details = lambda: {"cmd": "R0100"}
        dev._poll_h_series_json = lambda now, r0100: None
        dev.poll()
        assert dev.state["poll_count"] == 1

    def test_binary_fallback_is_skipped_without_tcp(self):
        dev = _h_device()
        dev.json_client.get_device_details = lambda: None
        dev._ensure_tcp = lambda: False
        dev._poll_h_series_binary = lambda now: (_ for _ in ()).throw(
            AssertionError("binary path needs TCP"))
        dev._poll_h_series(datetime.now())

    def test_disconnect_closes_the_json_client(self):
        dev = _h_device()
        dev.disconnect()
        assert dev.json_client.closed is True
        assert dev._heartbeat_stop.is_set() is True


class TestChainDiscovery:
    """The bitmask only measures 8 chains; discovery must not stop there."""

    def test_unmeasured_chains_are_probed_one_per_cycle(self):
        dev = _h_device()
        probes = [dev._next_unprobed_port() for _ in range(10)]
        assert probes[0] == proto.H_PORT_BITMASK_BITS + 1
        assert probes[proto.H_MAX_PORTS - proto.H_PORT_BITMASK_BITS - 1] == \
            proto.H_MAX_PORTS
        # Every bitmask-invisible chain gets probed exactly once, then None.
        assert probes[proto.H_MAX_PORTS - proto.H_PORT_BITMASK_BITS:] == \
            [None] * (10 - (proto.H_MAX_PORTS - proto.H_PORT_BITMASK_BITS))

    def test_discovered_chains_above_8_stay_active(self):
        dev = _h_device()
        dev._draft["ports"][12] = {"connected": True, "card_count": 7, "cards": []}
        assert 12 in dev._active_chains([1, 2])

    def test_empty_chains_are_not_active(self):
        dev = _h_device()
        dev._draft["ports"][12] = {"connected": False, "card_count": 0, "cards": []}
        assert dev._active_chains([1]) == [1]
