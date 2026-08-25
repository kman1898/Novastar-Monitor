"""Tests for device_manager.py — device state and polling logic."""
import ast
import inspect
import json
import socket
import textwrap
import threading
import time
from datetime import datetime

import pytest

import device_manager
import novastar_protocol as proto
import snmp_client as sc
from device_manager import NovaStar_Device, DeviceManager


@pytest.fixture(autouse=True)
def _contact_not_halted():
    """The global stop is process-wide state — never leak it between tests."""
    device_manager.resume_device_contact()
    yield
    device_manager.resume_device_contact()


@pytest.fixture(autouse=True)
def _no_site_snapshot(tmp_path, monkeypatch):
    """No test may read the operator's real wall_live_snapshot.json.

    That file is per-install runtime state and gitignored, so a fresh checkout
    does not have one and the suite has to pass without it either way. A test
    that read it would be asserting about whichever 286-card wall was last
    enumerated on this machine, and would change its verdict when the operator
    re-runs the enumeration.

    (The SNMP poll path no longer consults the inventory at all —
    `_expected_linked_ports()` went with the removed ports-down alert — so this
    now matters only to the on-demand per-card reads. It stays autouse: the
    fixture is cheap and the failure mode it prevents is a test that passes on
    one machine and fails on another.) Tests that want an inventory set
    `_known_cards` explicitly or point SNAPSHOT_PATH at their own tmp_path file
    (see TestSnapshotLoading), both of which still work.
    """
    monkeypatch.setattr(device_manager, "SNAPSHOT_PATH",
                        str(tmp_path / "no-such-snapshot.json"))


# ── Test doubles (no sockets are ever opened) ─────────────


class StubJSONClient:
    """Stand-in for HSeriesJSONClient. Records calls, talks to nothing.

    `commands` is the ordered list of protocol commands the device would have
    received, which is what the "a routine poll sends almost nothing" tests
    assert on.
    """

    def __init__(self, responses=None, screen_list=None, brightness=None):
        # {(slot, port, card_id): r0155_response_dict}
        self.responses = responses or {}
        self.batch_calls = []
        self.single_calls = []
        self.commands = []
        self.screen_list = screen_list
        self.brightness = brightness
        self.closed = False

    def get_device_details(self, device_id=0):
        self.commands.append("R0100")
        return {"cmd": "R0100", "slotList": []}

    def get_screen_list(self):
        self.commands.append("R0400")
        return self.screen_list

    def get_screen_output_info(self, screen_id):
        self.commands.append("R0405")
        return None

    def get_screen_details(self, screen_id):
        self.commands.append("R0401")
        if self.brightness is None:
            return None
        return {"screenId": screen_id, "brightness": self.brightness,
                "ack": "Ok"}

    def get_output_list(self):
        self.commands.append("R0300")
        return None

    def get_receiving_cards_batch(self, addresses, **kwargs):
        addresses = [tuple(a) for a in addresses]
        self.batch_calls.append(addresses)
        self.commands.extend(["R0155"] * len(addresses))
        return [self.responses.get(a) for a in addresses]

    def get_receiving_card(self, slot_id, port_id, card_id):
        self.single_calls.append((slot_id, port_id, card_id))
        self.commands.append("R0155")
        return self.responses.get((slot_id, port_id, card_id))

    def close(self):
        self.closed = True


def snmp_fans(failed=(), count=10):
    """Fan list as snmp_client decodes it, with `failed` ids flagged.

    Built by running the real parser over the real payload shape, so a change
    to parse_fans() surfaces here rather than being papered over by a
    hand-written dict. Note `speed: 0` on every fan — that is what a HEALTHY
    running wall reports.
    """
    return sc.parse_fans(json.dumps([
        {"fanId": i, "speed": 0, "status": 1 if i in failed else 0}
        for i in range(count)]))


def snmp_psus(failed=(), count=4):
    """PSU list as snmp_client decodes it.

    NOTE: the power field on `.1.17` is `iSignal`, not `status`. NovaStar R&D,
    by email: "Regarding the device power status, please use the iSignal field.
    Meaning: Power status (0: not connected to power, 1: connected to power)."
    The documented polarity is correct; it just describes a key their own
    example for that OID leaves out. `failed` here therefore drops `iSignal` to
    0 and leaves `status` at the 0 every device we have reports, so nothing
    downstream can pass these tests by reading `status`.
    """
    return sc.parse_psus(json.dumps([
        {"iSignal": 0 if i in failed else 1, "powerId": i, "status": 0,
         "voltage": 0}
        for i in range(count)]))


def snmp_health(model="H15", firmware="V2.0.0.6", temperature_status=0,
                fans=None, psus=None):
    """A get_device_health() return value, keys exactly as snmp_client emits."""
    fans = snmp_fans() if fans is None else fans
    psus = snmp_psus() if psus is None else psus
    return {
        "device_time": "2026-08-08 17:32:18",
        "model": model, "firmware": firmware,
        "serial_number": "16081800D74C0000", "mac": "00:12:34:56:78:9A",
        "ip": "192.168.0.10", "arm_version": "V1.0.0.9",
        "temperature_status": temperature_status,
        "temperature_ok": sc._status_ok(temperature_status),
        "fan_count": len(fans), "psu_count": len(psus),
        "fans": fans, "psus": psus, "summary": {"cpuStatus": 0},
        "cpu_status": 0,
        "failed_fans": [f["fan_id"] for f in fans if f["ok"] is False],
        "failed_psus": [p["power_id"] for p in psus if p["ok"] is False],
        "disconnected_psus": [p["power_id"] for p in psus
                              if p["connected"] is False],
        "extra": {},
    }


# What a walk of a device with no SNMP agent produces: a well-formed dict of
# nothing at all. This is the shape the "did it answer?" test has to survive.
SNMP_SILENT = {name: None for name in
               ("device_time", "model", "firmware", "serial_number", "mac",
                "ip", "arm_version", "temperature_status", "temperature_ok",
                "cpu_status", "fan_count", "psu_count", "summary")}
SNMP_SILENT.update({"fans": [], "psus": [], "failed_fans": [],
                    "failed_psus": [], "extra": {}})


class StubSNMPMonitor:
    """Stand-in for HSeriesSNMPMonitor. Records calls, opens no socket.

    Defaults to a device with no agent — the state every H-series test that
    predates SNMP is implicitly asserting about, and the safe default for a
    suite that must never put a packet on the wire.
    """

    def __init__(self, health=None, screens=None, output=None):
        self.health = health if health is not None else dict(SNMP_SILENT)
        self.screens = screens if screens is not None else {"screen_count": None,
                                                            "fields": {},
                                                            "extra": {}}
        self.output = output if output is not None else {
            "card_count": None, "summary": None, "card": {}, "port": {},
            "extra": {}}
        self.calls = []
        self.closed = False

    def get_device_health(self):
        self.calls.append("health")
        return self.health

    def get_screens(self):
        self.calls.append("screens")
        return self.screens

    def get_output_status(self):
        self.calls.append("output")
        return self.output

    def get_input_status(self):
        self.calls.append("input")
        return {}

    def close(self):
        self.closed = True


def snmp_output(port=None, slot_status=1, card_count=8, port_count=4):
    """A get_output_status() return value.

    `slot_status` defaults to the HEALTHY value, which for the card-slot OIDs
    (.20.2.1 / .30.2.1) is 1 — NovaStar's table gives 0 as Abnormal there, the
    inverse of the Normal: 0 fields. Both devices in the fleet report the same
    1 in the `.30.3` summary while running normally.

    `port` is the `.30.5.x` FIELD table for one Ethernet port, not a map of
    port number → link state. It used to be `port_link={1: 1, 3: 1, 4: 0}`
    here, which is what our H15 answers — read as port numbers that said
    "three ports down" on a healthy wall. Read as fields it is the default
    below: primary linked, backup idle.
    """
    return {
        "card_count": card_count,
        "summary": {"portCount": port_count},
        "card": {"slot_status": slot_status, "firmware": "2.0.0.6",
                 "serial_number": "16081800D74C0000", "port_count": port_count},
        "port": ({"link_status": 1, "backup_working": 0, "backup_link": 0}
                 if port is None else port),
        "extra": {},
    }


def snmp_screens(count=2):
    return {"screen_count": count,
            "fields": {"1": "CIRCUIT MOM", "2": 3840, "3": 2160},
            "extra": {}}


class RecordingUDPSocket:
    """A UDP socket that records datagrams instead of sending them.

    Used where a REAL SNMPClient is under test: the suite runs on a machine
    wired to a live LED wall, so a test that reaches a socket must reach this
    one.
    """

    def __init__(self):
        self.sent = []

    def settimeout(self, _timeout):
        pass

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        return len(data)

    def recvfrom(self, _bufsize):
        raise socket.timeout()

    def close(self):
        pass


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
    """A byte-schema R0155 reply (field names from the older capture)."""
    return {
        "deviceId": 0, "slotId": slot, "portId": port, "recvCardId": card_id,
        "power0Status": 0, "power1Status": 0, "brightness": 127,
        "temp": temp, "voltage": voltage, "cmd": "R0155", "ack": "Ok",
    }


def _r0155_centi(slot=20, port=0, card_id=0, temp=3700, volt=440,
                 work_status=0):
    """A centi-schema R0155 reply, as captured off 192.168.0.10.

    `work_status=1` reproduces an absent card exactly as the wall reports it:
    it answers, with temp / volt / brightness all zeroed and voltStatus 2.
    """
    absent = work_status != 0
    return {
        "deviceId": 0, "slotId": slot, "portId": port, "recvCardId": card_id,
        "mcuVersion": "V4.5.1.81", "fpgaVersion": "V4.5.1.81",
        "workStatus": work_status,
        "tempStatus": 2 if absent else 0, "temp": 0 if absent else temp,
        "tempMax": 70,
        "voltStatus": 2 if absent else 0, "volt": 0 if absent else volt,
        "power0Status": 0, "power1Status": 0,
        "brightness": 0 if absent else 25, "cmd": "R0155", "ack": "Ok",
    }


def _h_device(json_client=None, cards=None, snmp=None):
    """An H-series device wired to stub clients — no I/O, on any transport.

    Both clients are replaced, not just the JSON one: NovaStar_Device now
    builds a real SNMP client in __init__, and the address these tests use is
    the operator's live wall.
    """
    dev = NovaStar_Device("dev1", "H", "192.168.0.10", port=proto.H_TCP_PORT)
    dev.json_client = json_client if json_client is not None else StubJSONClient()
    dev.snmp = snmp if snmp is not None else StubSNMPMonitor()
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

    def test_read_register_card_accepts_sender_card_kwarg(self):
        dev = NovaStar_Device("dev1", "Test", "192.168.0.10")
        assert dev.read_register_card(0x0000000A, 0x5200, 0,
                                      sender_card=0x03) is None


def _live_monitor_payload(status=0x80, temp_raw=88, volt_raw=170,
                          card_count=4, link_raw=1):
    """A REG_LIVE_MONITOR reply, laid out as parse_live_monitoring reads it.

    `status=0x80` is a card that is there. Anything without that bit set is
    the sender card telling us there is no card reporting at that address —
    including every address past the end of a short chain during the 16-deep
    discovery scan (see novastar_protocol.live_monitor_present).
    """
    data = bytearray(26)
    data[0] = status
    data[1] = temp_raw
    data[3] = volt_raw
    data[11] = max(card_count - 1, 0)   # zero-indexed on the wire
    data[12] = link_raw
    data[13] = 1
    data[14], data[15] = 4, 5           # firmware 4.5
    data[17] = 1
    return bytes(data)


class TestVX1000PerCardReadIsThreeValued:
    """A VX1000 card that did not answer is UNKNOWN, never OFFLINE.

    `read_register_card` returns None for a socket.timeout AND for a socket
    torn down by a ConnectionReset — so before this, one dropped TCP session
    published every card on the wall as offline. On a monitor watching a live
    show that is a false fault on every panel at once, and the operator's only
    way to check it is to walk out and look at the wall.
    """

    def _dev(self, per_card):
        """A VX1000 whose device-level reads all go quiet, with `per_card`
        deciding what each card address answers.

        `_ensure_tcp` is stubbed because `poll()` returns early for a non-
        H-series device without a socket — without this the test opens a real
        TCP connection to 192.168.0.10 and only passes on a machine that can
        reach the operator's wall. It passed here, silently, until the
        processor was unplugged.
        """
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        dev._ensure_tcp = lambda: True
        dev.connected = True
        dev.read_register = lambda *a, **k: None
        dev.read_register_card = lambda reg, length, i, **k: per_card(i)
        dev.poll()
        return dev

    def test_a_timed_out_card_is_unknown_not_offline(self):
        dev = self._dev(lambda i: None)
        cards = dev.state["receiving_cards"]
        assert len(cards) == 16                 # the discovery scan depth
        assert all(c["online"] is None for c in cards)
        assert all(c["reading"] == "no_answer" for c in cards)
        assert all(c["answered"] is False for c in cards)
        # The claim that matters: nothing anywhere says a card is off.
        assert not any(c["online"] is False for c in cards)

    def test_a_card_the_device_says_is_absent_is_still_offline(self):
        """The real negative survives — an answered 'not present' is a fact.

        The sender card answers an empty address with a well-formed payload
        whose presence bit is clear. That is a measurement, and it is what
        bounds the discovery scan, so it must keep reading False.
        """
        dev = self._dev(lambda i: _live_monitor_payload(status=0x00))
        cards = dev.state["receiving_cards"]
        assert all(c["online"] is False for c in cards)
        assert all(c["reading"] == "absent" for c in cards)
        assert all(c["answered"] is True for c in cards)

    def test_a_truncated_reply_is_unknown_not_offline(self):
        """Bytes arrived but the frame was too short to decode.

        parse_live_monitoring needs 20 bytes and returns None below that. A
        short frame says something about the read, nothing about the panel.
        """
        dev = self._dev(lambda i: b"\x80\x58")
        cards = dev.state["receiving_cards"]
        assert all(c["online"] is None for c in cards)
        assert all(c["reading"] == "undecodable" for c in cards)

    def test_a_healthy_card_still_reads_online(self):
        dev = self._dev(lambda i: _live_monitor_payload())
        cards = dev.state["receiving_cards"]
        assert all(c["online"] is True for c in cards)
        assert all(c["reading"] == "ok" for c in cards)
        # raw 170 & 0x7F = 42, units of 0.1 V → 4.2 V. Vendor doc §4.3.4.
        assert cards[0]["voltage_v"] == 4.2

    def test_a_chain_that_half_answers_reports_only_what_it_knows(self):
        """The realistic failure: the socket dies partway through the scan.

        Cards 0-1 answered before the drop; 2-15 are unknown. A dashboard must
        show two online, fourteen unknown and ZERO offline — the shape that
        tells an operator to check the monitor's connection, not the wall.
        """
        dev = self._dev(lambda i: _live_monitor_payload(card_count=0)
                        if i < 2 else None)
        cards = dev.state["receiving_cards"]
        assert [c["online"] for c in cards[:2]] == [True, True]
        assert all(c["online"] is None for c in cards[2:])
        assert not any(c["online"] is False for c in cards)
        lm = dev.state["live_monitoring"]
        assert lm["coverage_read"] == 2
        assert lm["coverage_unknown"] == 14
        assert lm["coverage_total"] == 16

    def test_unknown_cards_are_not_counted_as_offline_in_the_aggregate(self):
        """`coverage_total - coverage_read` is not an offline count.

        Reading it as one is how "250 panels offline" got onto a lit wall.
        With coverage_unknown published, a caller can subtract the reads it
        never got before drawing that conclusion.
        """
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        dev._update_aggregates(datetime.now(), [
            {"online": True, "temperature_c": 40.0, "voltage_v": 5.1},
            {"online": None},
            {"online": None},
            {"online": False},
        ])
        lm = dev._draft["live_monitoring"]
        assert lm["coverage_read"] == 1
        assert lm["coverage_unknown"] == 2
        assert lm["coverage_total"] == 4
        # One genuinely offline card, not three.
        assert lm["coverage_total"] - lm["coverage_read"] \
            - lm["coverage_unknown"] == 1
        # An unknown card contributes no number to the mean either.
        assert lm["temperature_c"] == 40.0


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
        """A caller that omits the interval must not poll faster than the
        value the settings UI advertises — the signature default used to be
        2.0 while app.DEFAULT_POLL_INTERVAL was 10.0.

        The 10.0 itself was copied from a Companion *control* module, whose
        cadence exists to make a UI feel responsive. This is a monitor running
        during a live show and the conditions it watches for take minutes, so
        the unattended default is deliberately slower.
        """
        mgr = DeviceManager()
        assert mgr.poll_interval == device_manager.DEFAULT_POLL_INTERVAL
        assert device_manager.DEFAULT_POLL_INTERVAL == 30.0
        assert device_manager.DEFAULT_POLL_INTERVAL >= 10.0

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


class TestHSeriesBinaryCardLinkIsThreeValued:
    """_poll_h_port: a card that timed out is UNKNOWN, not "no link".

    On demand only today, which is exactly why it has to be right — it is the
    template the next binary handler gets copied from.

    Silence on this transport is expected and is not about the panel: the
    controller answers roughly 150-200 per-card binary reads and then simply
    stops (H_SERIES_FINDINGS §6.6), so the tail of any real chain walk is
    quiet from a perfectly healthy controller. A card that ANSWERS with zero
    connected data paths is the genuine data break this read exists to find,
    and the two must never render the same.
    """

    def _dev(self, per_card):
        dev = NovaStar_Device("dev1", "H", "192.168.0.10", port=proto.H_TCP_PORT)
        dev.read_register_card = \
            lambda reg, length, i, chain=0, **k: per_card(reg, i)
        dev._poll_h_port(1)
        # _poll_h_port writes the draft; nothing publishes until the cycle ends.
        return dev._draft["ports"][1]

    def test_a_timed_out_card_is_unknown_not_a_data_break(self):
        port = self._dev(lambda reg, i: None)
        cards = port["cards"]
        assert cards, "expected the scan to have probed something"
        assert all(c["online"] is None for c in cards)
        assert all(c["reading"] == "no_answer" for c in cards)
        assert not any(c["online"] is False for c in cards)

    def test_a_card_reporting_zero_paths_is_a_real_fault(self):
        """0x00 on byte[1] = every data path down. That IS the break."""
        port = self._dev(lambda reg, i: b"\x1c\x00")
        cards = port["cards"]
        assert all(c["online"] is False for c in cards)
        assert all(c["reading"] == "no_link" for c in cards)
        assert all(c["link_paths"] == "0/7" for c in cards)
        assert all(c["answered"] is True for c in cards)

    def test_a_healthy_card_reports_all_seven_paths(self):
        port = self._dev(lambda reg, i: b"\x1c\x7f")
        cards = port["cards"]
        assert all(c["online"] is True for c in cards)
        assert all(c["reading"] == "ok" for c in cards)
        assert all(c["link_paths"] == "7/7" for c in cards)

    def test_a_truncated_reply_is_unknown_not_offline(self):
        """One byte back is not a link measurement."""
        port = self._dev(lambda reg, i: b"\x1c")
        assert all(c["online"] is None for c in port["cards"])
        assert all(c["reading"] == "undecodable" for c in port["cards"])

    def test_a_chain_that_goes_quiet_partway_keeps_the_answers_it_got(self):
        """The §6.6 signature: the controller answers, then stops.

        Cards 0-1 answered healthy, then the request budget ran out. The chain
        must read as two online and the rest unknown — reporting the tail as
        a data break sends someone hunting a cable that is fine.
        """
        port = self._dev(lambda reg, i: b"\x1c\x7f" if i < 2 else None)
        cards = port["cards"]
        assert [c["online"] for c in cards[:2]] == [True, True]
        assert all(c["online"] is None for c in cards[2:])
        assert not any(c["online"] is False for c in cards)
        assert port["cards_unknown"] == len(cards) - 2

    def test_a_chain_nobody_answered_on_is_unknown_not_disconnected(self):
        """The rollup of a set of unknowns is an unknown.

        `connected: False` here would let a chain-level view report the chain
        as dark when all we actually know is that we could not reach it.
        """
        port = self._dev(lambda reg, i: None)
        assert port["connected"] is None
        assert port["card_count"] == 0
        assert port["cards_unknown"] == len(port["cards"])

    def test_a_chain_that_answered_with_nothing_on_it_is_disconnected(self):
        """A probed chain with no panels is still a knowable False."""
        port = self._dev(lambda reg, i: b"\x1c\x00")
        assert port["connected"] is False
        assert port["card_count"] == 0
        assert port["cards_unknown"] == 0


class TestOnDemandCardRefresh:
    """The per-card refresh that produces every number on the dashboard.

    On demand only now — see TestPerCardPollingIsOnDemand for the rule; these
    tests cover the decoding it does when somebody does ask for it.
    """

    def test_voltage_uses_vendor_formula(self):
        """raw 170 → 4.2 V: lower 7 bits, units of 0.1 V.

        NovaStar's H Series Video Wall Splicers Control Protocol §4.3.4 and
        §5.4.2 (same wording in V1.0.18 and V1.0.20) state the encoding
        outright, with 172 → 4.4 V as the worked example. 170 & 0x7F = 42.

        This test previously asserted 5.1 V and, explicitly, `!= 4.2` — the
        old `raw * 0.03` form. That form was adopted here to keep cards above
        a 4.7 V low-voltage alarm, on the assumption that a wall reading below
        the alarm had to be a decode bug. It was the alarm that was wrong:
        these receiving cards run at roughly 4.2 V and the floor is now 3.8 V.
        """
        client = StubJSONClient({(20, 0, 0): _r0155(voltage=170)})
        dev = _h_device(client, [{"slot": 20, "port": 0, "card_id": 0}])
        card = dev.refresh_all_cards()[0]
        assert card["voltage_v"] == 4.2
        assert card["voltage_v"] != 5.1
        # Above the app's current 3.8 V low-voltage floor (DEFAULT_VOLTAGE_MIN).
        assert card["voltage_v"] > 3.8

    def test_voltage_range_of_the_real_wall_reads_healthy(self):
        """The wall's raw 165-173 decodes to the 3.7-4.5 V band it really is.

        Masked (vendor doc §4.3.4 / §5.4.2) these are 37-45 in units of 0.1 V.
        The old `raw * 0.03` reading of the same bytes was 4.95-5.19 V, which
        is where the "healthy 5 V rail" story in this repo came from; it was
        an artifact of leaving bit 7 in the number. The centi-schema firmware
        on the same wall reports 4.10-4.40 V, which is the band the masked
        form lands in and the unmasked one misses by about 0.9 V.
        """
        addresses = [(20, 0, i) for i in range(9)]
        client = StubJSONClient({
            addr: _r0155(card_id=addr[2], voltage=raw)
            for addr, raw in zip(addresses, range(165, 174))
        })
        dev = _h_device(client, [
            {"slot": s, "port": p, "card_id": c} for s, p, c in addresses])
        volts = [c["voltage_v"] for c in dev.refresh_all_cards()]
        assert min(volts) == 3.7      # 165 & 0x7F = 37
        assert max(volts) == 4.5      # 173 & 0x7F = 45
        # Still clear of the 3.8 V floor except at the very bottom of the
        # band, which is why that floor is where it is and not any higher.
        assert all(v >= 3.7 for v in volts)

    def test_temperature_and_power_flags(self):
        client = StubJSONClient({(20, 0, 0): _r0155(temp=88)})
        dev = _h_device(client, [{"slot": 20, "port": 0, "card_id": 0}])
        card = dev.refresh_all_cards()[0]
        assert card["temp_c"] == 44.0
        assert card["temperature_c"] == 44.0
        assert card["primary_power_ok"] is True
        assert card["backup_power_ok"] is True
        assert card["brightness"] == 127

    def test_silent_card_is_unknown_and_keeps_identity(self):
        client = StubJSONClient({})   # device answers nothing
        dev = _h_device(client, [
            {"slot": 20, "port": 0, "card_id": 7, "card_number": 8}])
        card = dev.refresh_all_cards()[0]
        # Silence is not offline: on this controller it is usually the
        # request budget, and the card was proven present by the enumeration.
        assert card["online"] is None
        assert card["answered"] is False
        assert card["reading"] == "no_answer"
        assert card["card_id"] == 7
        assert card["card_number"] == 8

    def test_uses_one_batched_read_not_one_call_per_card(self):
        addresses = [(20, 0, i) for i in range(20)]
        client = StubJSONClient({
            addr: _r0155(card_id=addr[2]) for addr in addresses})
        dev = _h_device(client, [
            {"slot": s, "port": p, "card_id": c} for s, p, c in addresses])
        cards = dev.refresh_all_cards()
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
        cards = dev.refresh_all_cards()
        assert [c["card_id"] for c in cards] == [0, 1, 2]
        assert cards[0]["temp_c"] == 40.0
        assert cards[1]["online"] is None
        assert cards[2]["temp_c"] == 50.0


class TestOnDemandCardRefreshCentiFirmware:
    """The firmware the operator's wall actually runs."""

    def _dev(self, responses, cards):
        return _h_device(StubJSONClient(responses), cards)

    def test_reporting_card_decodes_at_the_centi_scale(self):
        dev = self._dev({(20, 0, 10): _r0155_centi(card_id=10)},
                        [{"slot": 20, "port": 0, "card_id": 10}])
        card = dev.refresh_all_cards()[0]
        assert card["online"] is True
        assert card["temp_c"] == 37.0          # 3700 / 100, not 1850
        assert card["temperature_c"] == 37.0
        assert card["voltage_v"] == 4.4        # 440 / 100
        assert card["brightness"] == 25
        assert card["temp_limit_c"] == 70
        assert card["mcu_version"] == "V4.5.1.81"
        assert card["fpga_version"] == "V4.5.1.81"

    def test_absent_card_is_offline_with_no_readings(self):
        """workStatus 1: the device answers, but with placeholders."""
        dev = self._dev({(20, 0, 11): _r0155_centi(card_id=11, work_status=1)},
                        [{"slot": 20, "port": 0, "card_id": 11,
                          "card_number": 3}])
        card = dev.refresh_all_cards()[0]
        assert card["online"] is False
        assert card["reporting"] is False
        assert card["work_status"] == 1
        # Identity survives; not a single reading does.
        assert card["card_id"] == 11 and card["card_number"] == 3
        for key in ("temp_c", "temperature_c", "voltage_v", "brightness",
                    "primary_power_ok", "backup_power_ok"):
            assert card.get(key) is None, f"{key} leaked a placeholder"

    def test_absent_cards_cannot_move_the_aggregates(self):
        """One reporting card among many absent ones sets every aggregate."""
        addresses = [(20, 0, i) for i in range(10)]
        responses = {addr: _r0155_centi(card_id=addr[2], work_status=1)
                     for addr in addresses}
        responses[(20, 0, 0)] = _r0155_centi(card_id=0)
        dev = self._dev(responses, [{"slot": s, "port": p, "card_id": c}
                                    for s, p, c in addresses])
        cards = dev.refresh_all_cards()
        dev._update_aggregates(datetime.now(), cards)
        lm = dev._draft["live_monitoring"]
        assert lm["card_count"] == 1
        assert lm["temperature_c"] == 37.0
        assert lm["temperature_max_c"] == 37.0
        # A 0.0 dragged into the mean would show up here as 0.44.
        assert lm["voltage_v"] == 4.4

    def test_a_wall_of_absent_cards_records_nothing(self):
        addresses = [(20, 0, i) for i in range(5)]
        dev = self._dev(
            {addr: _r0155_centi(card_id=addr[2], work_status=1)
             for addr in addresses},
            [{"slot": s, "port": p, "card_id": c} for s, p, c in addresses])
        cards = dev.refresh_all_cards()
        dev._update_aggregates(datetime.now(), cards)
        lm = dev._draft["live_monitoring"]
        # Not empty any more, but nothing is CLAIMED: every reading is None
        # and `online` is False. The old behaviour returned early and left the
        # previous cycle's healthy numbers in place, so a wall that had gone
        # entirely dark kept publishing its last good temperature.
        assert lm["online"] is False
        assert lm["temperature_c"] is None
        assert lm["temperature_max_c"] is None
        assert lm["voltage_v"] is None
        assert lm["voltage_min_v"] is None
        assert lm["coverage_read"] == 0 and lm["coverage_total"] == 5
        # The gap IS recorded — one timestamp with None readings — so the
        # chart draws a break where the wall went quiet instead of joining a
        # line straight across it as though nothing happened.
        hist = dev._draft["history"]
        assert len(hist["timestamps"]) == 1
        assert hist["temperature"] == [None]
        assert hist["voltage"] == [None]

    def test_both_schemas_survive_the_same_refresh(self):
        """Mixed fleet: each reply is decoded by its own schema.

        Worth reading the two voltages together. The byte-schema card decodes
        to 4.2 V (170 & 0x7F = 42, units of 0.1 V) and the centi-schema card
        to 4.4 V (440 / 100) — the same rail, from two firmwares that encode
        it differently. Under the old `raw * 0.03` byte formula the first card
        read 5.1 V, nearly a volt apart from its neighbour, and that
        disagreement is a second, independent reason to trust the masked form
        the vendor documents in §4.3.4 / §5.4.2.
        """
        dev = self._dev({
            (20, 0, 0): _r0155(card_id=0, temp=88, voltage=170),
            (20, 0, 1): _r0155_centi(card_id=1),
        }, [{"slot": 20, "port": 0, "card_id": 0},
            {"slot": 20, "port": 0, "card_id": 1}])
        old, new = dev.refresh_all_cards()
        assert old["temp_c"] == 44.0 and old["voltage_v"] == 4.2
        assert new["temp_c"] == 37.0 and new["voltage_v"] == 4.4
        assert abs(old["voltage_v"] - new["voltage_v"]) < 0.5


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
        assert dev.refresh_all_cards() == []


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

    def test_no_online_cards_records_a_gap_not_a_silence(self):
        """A cycle where nothing answered is itself information. Skipping it
        left the previous sample as the newest point on the chart, so an
        outage rendered as a flat line at the last healthy value."""
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        dev._update_aggregates(datetime.now(), [{"online": False}])
        hist = dev._draft["history"]
        assert len(hist["timestamps"]) == 1
        assert hist["temperature"] == [None]
        assert dev._draft["live_monitoring"]["online"] is False


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


# ── Outage regressions ────────────────────────────────────
#
# Everything below exists because a monitoring tool took a production wall's
# show control away from the operator. These are tripwires, not coverage.


class TestNoHeartbeat:
    """W0120 is gone. Nothing may start a thread to send it again."""

    def test_polling_starts_no_thread_at_all(self):
        dev = _h_device(StubJSONClient())
        before = threading.active_count()
        for _ in range(3):
            dev.poll()
        assert threading.active_count() <= before
        assert not [t for t in threading.enumerate()
                    if t.name.startswith("hb-")]

    def test_device_carries_no_heartbeat_machinery(self):
        dev = _h_device(StubJSONClient())
        for attr in ("_heartbeat_thread", "_heartbeat_stop",
                     "_ensure_heartbeat"):
            assert not hasattr(dev, attr), f"{attr} came back"

    def test_the_device_class_never_spawns_a_thread(self):
        """A per-device background thread is how the heartbeat existed."""
        source = inspect.getsource(NovaStar_Device)
        assert "Thread(" not in source

    def test_the_json_client_has_no_heartbeat_to_call(self):
        import h_series_json
        assert not hasattr(h_series_json.HSeriesJSONClient, "heartbeat")


class TestPerCardPollingIsOnDemand:
    """A poll cycle must never sweep receiving cards."""

    def _polled(self, **kwargs):
        client = StubJSONClient(**kwargs)
        dev = _h_device(client, [{"slot": 20, "port": 0, "card_id": i}
                                 for i in range(50)])
        dev.poll()
        return dev, client

    def test_a_routine_poll_reads_no_cards(self):
        _, client = self._polled()
        assert client.batch_calls == []
        assert client.single_calls == []
        assert "R0155" not in client.commands

    def test_a_settled_poll_cycle_sends_one_command(self):
        """Topology is cached, so steady state is R0100 and nothing else."""
        dev, client = self._polled()
        client.commands.clear()
        dev.poll()
        dev.poll()
        assert client.commands == ["R0100", "R0100"]

    def test_topology_is_read_once_then_cached(self):
        _, client = self._polled(screen_list={"screens": [{"screenId": 1}]})
        # R0401 is NOT topology and is deliberately not cached with it — the
        # operator changes brightness during a show, so it is read every cycle.
        # The second R0100 is the cached wiring read (OPT vs Ethernet per
        # sender card), not a poll-loop reachability read.
        assert client.commands == ["R0100", "R0400", "R0405", "R0300",
                                   "R0100", "R0401"]

    def test_topology_is_re_read_once_the_cache_ages_out(self, monkeypatch):
        dev, client = self._polled()
        monkeypatch.setattr(device_manager, "TOPOLOGY_REFRESH_INTERVAL", 0.0)
        client.commands.clear()
        dev.poll()
        assert "R0400" in client.commands

    def test_the_binary_fallback_polls_no_chains_either(self):
        """Same wall, same show — TCP per-card reads are per-card reads."""
        dev = _h_device()
        dev.read_register = lambda *a, **k: None
        dev._poll_h_port = lambda port_num: pytest.fail(
            "the poll loop must not sweep a chain")
        dev._poll_h_series_binary(datetime.now())

    def test_no_h_series_poll_path_calls_a_card_read(self):
        """Belt and braces: the sweep can't come back by another name.

        Parsed rather than grepped so the prose explaining the removal (which
        names the very methods it removed) doesn't trip the assertion.
        """
        banned = {"refresh_cards", "refresh_chain", "refresh_all_cards",
                  "_read_cards", "_poll_h_port", "read_register_card"}
        for method in (NovaStar_Device._poll_h_series,
                       NovaStar_Device._poll_h_series_json,
                       NovaStar_Device._poll_h_series_binary):
            tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
            called = {n.func.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)}
            assert not (called & banned), \
                f"{method.__name__} calls {called & banned}"


class TestBoundedOnDemandRefresh:
    """What replaced the sweep: explicit, bounded, operator-driven reads."""

    def _dev(self):
        inventory = [{"slot": 20, "port": p, "card_id": c}
                     for p in (0, 1) for c in range(4)]
        client = StubJSONClient({
            (20, p, c): _r0155_centi(slot=20, port=p, card_id=c)
            for p in (0, 1) for c in range(4)})
        return _h_device(client, inventory), client

    def test_refresh_chain_reads_only_that_chain(self):
        dev, client = self._dev()
        cards = dev.refresh_chain(20, 1)
        assert len(cards) == 4
        assert client.batch_calls == [[(20, 1, c) for c in range(4)]]

    def test_refresh_chain_of_an_unknown_chain_sends_nothing(self):
        dev, client = self._dev()
        assert dev.refresh_chain(99, 0) == []
        assert client.batch_calls == []

    def test_known_chains_needs_no_traffic(self):
        """A 'pick a chain' UI can enumerate without touching the device."""
        dev, client = self._dev()
        assert dev.known_chains() == [(20, 0), (20, 1)]
        assert client.commands == []

    def test_an_explicit_address_list_is_honoured(self):
        dev, client = self._dev()
        cards = dev.refresh_cards([(20, 0, 2), (20, 1, 3)])
        assert [c["card_id"] for c in cards] == [2, 3]
        assert client.batch_calls == [[(20, 0, 2), (20, 1, 3)]]

    def test_an_oversized_request_is_capped(self):
        dev, client = self._dev()
        dev.refresh_cards([(20, 0, i) for i in range(500)], limit=8)
        assert len(client.batch_calls[0]) == 8

    def test_the_default_cap_is_far_below_a_full_wall(self):
        assert device_manager.PER_CARD_SWEEP_LIMIT < 1374

    def test_a_partial_refresh_keeps_the_other_cards_on_the_dashboard(self):
        dev, client = self._dev()
        dev.refresh_chain(20, 0)
        dev.refresh_chain(20, 1)
        published = dev.state["receiving_cards"]
        assert len(published) == 8
        assert {(c["port"], c["card_id"]) for c in published} == \
            {(p, c) for p in (0, 1) for c in range(4)}

    def test_readings_carry_the_time_they_were_taken(self):
        """Without a cadence, "how old is this?" stops being obvious."""
        dev, _ = self._dev()
        assert dev.state["cards_read_at"] is None      # nothing asked yet
        cards = dev.refresh_chain(20, 0)
        assert all(c["read_at"] for c in cards)
        assert dev.state["cards_read_at"] == cards[0]["read_at"]

    def test_an_on_demand_read_adds_no_history_sample(self):
        """History is a fixed-cadence series; a click is not a cycle."""
        dev, _ = self._dev()
        dev.refresh_chain(20, 0)
        dev.refresh_chain(20, 0)
        assert dev._draft["history"]["timestamps"] == []
        # It does still update the aggregates it just measured.
        assert dev._draft["live_monitoring"]["card_count"] == 4

    def test_a_full_sweep_is_rate_limited(self):
        dev, client = self._dev()
        assert dev.refresh_all_cards() is not None
        # Refused, not merely deduped — None distinguishes it from "ran and
        # found nothing".
        assert dev.refresh_all_cards() is None
        assert len(client.batch_calls) == 1

    def test_the_full_sweep_rate_limit_cannot_be_overridden(self):
        """No force flag: a bypass is how this ends up back on a timer."""
        params = inspect.signature(NovaStar_Device.refresh_all_cards).parameters
        assert list(params) == ["self"]
        assert device_manager.FULL_SWEEP_MIN_INTERVAL >= 60.0


class TestGlobalStop:
    """One switch that silences the app on the wire, without killing it."""

    def test_halt_and_resume_flip_the_flag(self):
        assert device_manager.device_contact_halted() is False
        device_manager.halt_device_contact("showtime")
        assert device_manager.device_contact_halted() is True
        device_manager.resume_device_contact()
        assert device_manager.device_contact_halted() is False

    def test_a_halted_device_polls_nothing(self):
        dev = _h_device(StubJSONClient())
        device_manager.halt_device_contact()
        dev.poll()
        assert dev.json_client.commands == []
        assert dev.state["poll_count"] == 0

    def test_a_halted_device_refuses_every_on_demand_read(self):
        client = StubJSONClient()
        dev = _h_device(client, [{"slot": 20, "port": 0, "card_id": 0}])
        device_manager.halt_device_contact()
        assert dev.refresh_cards([(20, 0, 0)]) == []
        assert dev.refresh_chain(20, 0) == []
        assert dev.refresh_all_cards() is None
        assert dev.refresh_topology() is False
        assert client.commands == []

    def test_a_halted_device_opens_no_tcp_socket(self):
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        device_manager.halt_device_contact()
        assert dev.connect() is False
        assert dev.sock is None

    def test_a_halted_device_puts_no_binary_frame_on_the_wire(self):
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        dev.sock = FakeSocket()
        dev.connected = True
        device_manager.halt_device_contact()
        assert dev.read_register(*proto.REG_SYSTEM_INFO) is None
        assert dev.read_register_card(*proto.REG_LIVE_MONITOR, 0) is None
        assert dev.sock.sent == []

    def test_the_poll_loop_stops_and_restarts_without_a_restart(self):
        """The operator gets the wall back mid-show, and the app back after."""
        mgr = DeviceManager(poll_interval=0.01)
        dev = FakeDevice("fake")
        mgr.devices["fake"] = dev
        mgr._running = True
        mgr._start_device_thread("fake")
        try:
            deadline = time.monotonic() + 2.0
            while dev.polls < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert dev.polls >= 2, "poll thread never ran"

            mgr.halt("show in progress")
            time.sleep(0.1)
            settled = dev.polls
            time.sleep(0.15)
            assert dev.polls == settled, "kept polling while halted"
            # The thread is still alive — resume must not need a restart.
            assert any(t.is_alive() for t in mgr._threads.values())

            mgr.resume()
            deadline = time.monotonic() + 2.0
            while dev.polls <= settled and time.monotonic() < deadline:
                time.sleep(0.01)
            assert dev.polls > settled, "did not resume"
        finally:
            mgr.stop(timeout=2.0)

    def test_a_halted_device_makes_no_snmp_call(self):
        """The stop covers the new transport as well as the old ones."""
        snmp = StubSNMPMonitor(health=snmp_health())
        dev = _h_device(StubJSONClient(), snmp=snmp)
        device_manager.halt_device_contact()
        dev.poll()
        assert snmp.calls == []
        assert dev._snmp_should_try() is False

    def test_halt_blocks_a_real_snmp_client_before_it_sends(self):
        """The gate is in the client, not just in the poll path.

        Exercises the real HaltAwareSNMPClient with a socket that records
        instead of sending — the on-demand and future callers reach the client
        directly, so the stop has to hold there too.
        """
        client = device_manager.HaltAwareSNMPClient("192.168.0.10")
        sock = RecordingUDPSocket()
        client._sock = sock

        device_manager.halt_device_contact("showtime")
        assert client.get(sc.OID_DEVICE + ".2") is None
        assert client.get_next(sc.OID_DEVICE) is None
        assert client.walk(sc.OID_DEVICE) == []
        assert sock.sent == [], "a datagram left while contact was halted"

        # And it is a gate, not a break: resuming lets exactly one datagram out
        # (the socket then times out, which is not this test's business).
        device_manager.resume_device_contact()
        client.get(sc.OID_DEVICE + ".2")
        assert len(sock.sent) == 1

    def test_the_halt_gate_sits_on_the_only_method_that_sends(self):
        """`_exchange` is the whole surface, so gating it gates everything.

        snmp_client.py cannot import this module (it is standalone by design),
        so the halt check lives in a subclass here. That only covers every
        request while `_exchange` remains the sole place a datagram is
        emitted — asserted against snmp_client's own source, the same way its
        read-only contract is.
        """
        import snmp_client

        senders = set()
        for node in ast.walk(ast.parse(inspect.getsource(snmp_client))):
            if not isinstance(node, ast.FunctionDef):
                continue
            for call in ast.walk(node):
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("sendto", "sendall", "send")):
                    senders.add(node.name)
        assert senders == {"_exchange"}, \
            f"snmp_client sends from {senders} — the halt gate covers _exchange"

    def test_the_manager_handles_delegate_to_the_process_wide_flag(self):
        """SNMP checks the same flag, so a manager must not own a private one."""
        mgr = DeviceManager()
        mgr.halt()
        assert device_manager.device_contact_halted() is True
        assert DeviceManager.is_halted() is True
        assert DeviceManager().is_halted() is True     # a *different* manager
        mgr.resume()
        assert device_manager.device_contact_halted() is False


# ── SNMP: the routine health path ─────────────────────────
#
# Per-card polling is gone, so this is what a poll cycle now IS. These tests
# use a stub monitor rather than a stub socket: snmp_client has its own
# 1200-line suite for the wire format, and what matters here is that the
# device publishes what the monitor returns, degrades quietly when it returns
# nothing, and never puts the control protocol back on a cadence.


class TestSNMPHealthPolling:
    def _live(self, **kwargs):
        """A device whose SNMP agent answers."""
        snmp = StubSNMPMonitor(health=snmp_health(**kwargs),
                               screens=snmp_screens(),
                               output=snmp_output())
        client = StubJSONClient()
        return _h_device(client, snmp=snmp), client, snmp

    def test_a_poll_publishes_the_snmp_health_block(self):
        dev, _, _ = self._live()
        dev.poll()
        snmp = dev.state["snmp"]
        assert snmp["available"] is True
        assert snmp["unsupported"] is False
        assert snmp["read_at"]
        assert snmp["model"] == "H15"
        assert snmp["firmware"] == "V2.0.0.6"
        assert snmp["serial_number"] == "16081800D74C0000"
        assert snmp["temperature_ok"] is True
        assert snmp["fan_count"] == 10 and len(snmp["fans"]) == 10
        assert snmp["psu_count"] == 4 and len(snmp["psus"]) == 4
        assert snmp["failed_fans"] == [] and snmp["failed_psus"] == []
        assert snmp["screens"]["screen_count"] == 2
        assert snmp["screens"]["fields"]["1"] == "CIRCUIT MOM"

    def test_per_fan_and_per_psu_status_reaches_state(self):
        """Fan 3 abnormal and PSU 1 not connected to power, end to end.

        A supply with `iSignal: 0` is the device's own documented claim that it
        has no power (NovaStar R&D, by email), so it is a real state and not an
        abstention. `failed_psus` and `disconnected_psus` carry the same id —
        PSU `ok` IS the iSignal-derived `connected` flag — and both are
        asserted so a future change that decouples them shows up here.
        """
        dev, _, _ = self._live(fans=snmp_fans(failed={3}),
                               psus=snmp_psus(failed={1}))
        dev.poll()
        snmp = dev.state["snmp"]
        assert snmp["failed_fans"] == [3]
        assert snmp["failed_psus"] == [1]
        assert dev.state["snmp"]["disconnected_psus"] == [1]
        assert [f["ok"] for f in snmp["fans"]].count(False) == 1
        assert snmp["fans"][3]["ok"] is False
        assert snmp["psus"][1]["ok"] is False
        assert snmp["psus"][1]["connected"] is False
        assert snmp["psus"][1]["i_signal"] == 0
        # And it is not `status` doing the work: every supply here reports the
        # same `status: 0`, healthy and unhealthy alike.
        assert all(p["status"] == 0 for p in snmp["psus"])
        assert snmp["psus"][0]["ok"] is True

    def test_the_placeholder_zeros_are_carried_but_never_renamed(self):
        """`speed` and `voltage` read 0 on a HEALTHY wall — no unit, ever.

        They stay in state for display, under names that cannot be mistaken
        for a measurement. A `speed_rpm` or `voltage_v` here would be read as
        fact by the next thing that touches it.
        """
        dev, _, _ = self._live()
        dev.poll()
        snmp = dev.state["snmp"]
        for fan in snmp["fans"]:
            assert fan["speed_raw"] == 0
            assert fan["ok"] is True           # healthy, despite speed 0
            assert "speed_rpm" not in fan and "speed" not in fan
        for psu in snmp["psus"]:
            assert psu["voltage_raw"] == 0
            assert psu["ok"] is True
            assert "voltage_v" not in psu and "voltage" not in psu

    def test_snmp_supplies_the_real_firmware_and_the_output_card(self):
        """`active_ports` used to be asserted here too, derived from the `.30.5`
        "link map". It was a field table, so that list was built from field
        names read as port numbers — it is no longer set from SNMP at all."""
        dev, _, _ = self._live()
        dev.poll()
        # The real device firmware, not the control protocol's version.
        assert dev.state["firmware_version"] == "V2.0.0.6"
        assert dev.state["snmp"]["output"]["card_count"] == 8
        assert dev.state["snmp"]["output"]["slot_ok"] is True

    def test_the_healthy_card_slot_status_is_one_not_zero(self):
        """`.30.2.1` is `0: Abnormal` — the inverse of every Normal: 0 field.

        Read with the 0 = OK helper, the 1 that both the H15 and the H2 report
        while driving a lit wall became `slot_ok: False`, which app.py turns
        into a CRITICAL "Output card reports a fault" on every polling cycle.
        """
        dev, _, snmp = self._live()
        snmp.output = snmp_output(slot_status=1)
        dev.poll()
        assert dev.state["snmp"]["output"]["slot_status"] == 1
        assert dev.state["snmp"]["output"]["slot_ok"] is True

    def test_a_zero_card_slot_status_is_the_abnormal_one(self):
        dev, _, snmp = self._live()
        snmp.output = snmp_output(slot_status=0)
        dev.poll()
        assert dev.state["snmp"]["output"]["slot_status"] == 0
        assert dev.state["snmp"]["output"]["slot_ok"] is False

    def test_a_stub_output_subtree_yields_no_slot_verdict_either_way(self):
        """`.30` answers on both fleet devices with no serial, no firmware and
        netPortCount 0. Everything it emits from that state is a stub, so a
        stub 0 must not be published as a slot fault — and None, not True, so
        it is not a false all-clear either. The raw value still is published.
        """
        dev, _, snmp = self._live()
        snmp.output = {"card_count": 8, "summary": {},
                       "card": {"slot_status": 0}, "port": {}, "extra": {}}
        dev.poll()
        assert dev.state["snmp"]["output"]["slot_status"] == 0
        assert dev.state["snmp"]["output"]["slot_ok"] is None

    def test_an_absent_card_slot_status_stays_unknown(self):
        dev, _, snmp = self._live()
        snmp.output = snmp_output(slot_status=None)
        dev.poll()
        assert dev.state["snmp"]["output"]["slot_ok"] is None

    def test_a_settled_snmp_cycle_touches_the_control_protocol_not_at_all(self):
        """The whole point: routine monitoring leaves UDP 6000 alone.

        R0100 used to go out every cycle. With SNMP answering it is not sent,
        because SNMP already covers identity and reachability and a
        control-plane datagram bought for nothing is exactly what cost the
        operator the wall.
        """
        dev, client, snmp = self._live()
        dev.poll()                     # first cycle also reads topology
        client.commands.clear()
        dev.poll()
        dev.poll()
        assert client.commands == []
        assert "R0100" not in client.commands
        assert snmp.calls == ["health", "screens", "output"] * 3

    def test_topology_is_still_read_once_then_cached(self):
        """SNMP has no equivalent of R0405's per-screen output geometry."""
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output())
        client = StubJSONClient(screen_list={"screens": [{"screenId": 1}]})
        dev = _h_device(client, snmp=snmp)
        dev.poll()
        assert client.commands == ["R0400", "R0405", "R0300", "R0100",
                                   "R0401"]
        client.commands.clear()
        dev.poll()
        # Topology stays cached; brightness does not — it is a live value.
        assert client.commands == ["R0401"]

    def test_a_poll_still_reads_no_cards(self):
        """SNMP cannot reach per-card data, and must not tempt anyone to try."""
        dev, client, _ = self._live()
        dev._known_cards = [{"slot": 20, "port": 0, "card_id": i}
                            for i in range(50)]
        dev.poll()
        assert client.batch_calls == [] and client.single_calls == []

    def test_snmp_marks_the_device_reachable(self):
        dev, _, _ = self._live()
        dev.poll()
        assert dev.state["connected"] is True
        assert dev.state["error"] is None
        assert dev.state["contact_halted"] is False


class TestSNMPDegradesGracefully:
    """A device with no agent, or old firmware, must cost nothing but a miss."""

    def test_a_silent_agent_falls_back_to_the_json_read(self):
        client = StubJSONClient()
        dev = _h_device(client, snmp=StubSNMPMonitor())     # silent by default
        dev.poll()
        # R0100 first, then the (cached from here on) topology reads.
        assert client.commands[0] == "R0100"
        assert dev.state["snmp"]["available"] is False
        assert dev.state["connected"] is True               # JSON answered

    def test_a_silent_agent_does_not_break_the_poll_loop(self):
        dev = _h_device(StubJSONClient(), snmp=StubSNMPMonitor())
        for _ in range(5):
            dev.poll()
        assert dev.state["poll_count"] == 5

    def test_a_silent_agent_is_not_asked_forever(self):
        snmp = StubSNMPMonitor()
        dev = _h_device(StubJSONClient(), snmp=snmp)
        for _ in range(10):
            dev.poll()
        assert len(snmp.calls) == device_manager.SNMP_MAX_FAILS
        assert dev.state["snmp"]["unsupported"] is True

    def test_the_unsupported_warning_is_logged_once_not_every_cycle(self, caplog):
        dev = _h_device(StubJSONClient(), snmp=StubSNMPMonitor())
        with caplog.at_level("WARNING", logger="novastar_monitor.device_manager"):
            for _ in range(10):
                dev.poll()
        lines = [r for r in caplog.records if "No SNMP answer" in r.getMessage()]
        assert len(lines) == 1
        assert "V2.0.0.0" in lines[0].getMessage()

    def test_a_transient_miss_never_disables_a_working_agent(self):
        """Same asymmetry as the JSON path — a dropped datagram is not a verdict."""
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output())
        dev = _h_device(StubJSONClient(), snmp=snmp)
        dev.poll()
        snmp.health = dict(SNMP_SILENT)
        for _ in range(10):
            dev.poll()
        assert dev._snmp_should_try() is True
        assert dev.state["snmp"]["unsupported"] is False
        assert dev.state["snmp"]["available"] is False

    def test_a_miss_marks_the_block_stale_rather_than_wiping_it(self):
        """Keep the last good readings on screen, but say they are not live."""
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output())
        dev = _h_device(StubJSONClient(), snmp=snmp)
        dev.poll()
        snmp.health = dict(SNMP_SILENT)
        dev.poll()
        block = dev.state["snmp"]
        assert block["available"] is False
        assert block["model"] == "H15"          # last known, not blanked
        assert block["read_at"]

    def test_an_exception_from_the_client_is_contained(self):
        """snmp_client promises not to raise; the poll loop does not rely on it."""
        class ExplodingMonitor(StubSNMPMonitor):
            def get_device_health(self):
                raise RuntimeError("no route to host")

        client = StubJSONClient()
        dev = _h_device(client, snmp=ExplodingMonitor())
        dev.poll()                              # must not raise
        assert dev.state["poll_count"] == 1
        assert dev.state["snmp"]["available"] is False
        assert "R0100" in client.commands       # fell through to the fallback

    def test_a_vx1000_device_has_no_snmp_at_all(self):
        dev = NovaStar_Device("dev1", "VX", "192.168.0.10")
        assert dev.snmp is None
        assert dev.state["snmp"]["available"] is None

    def test_disconnect_closes_the_snmp_client(self):
        snmp = StubSNMPMonitor()
        dev = _h_device(StubJSONClient(), snmp=snmp)
        dev.disconnect()
        assert snmp.closed is True


class TestSNMPOutputPortFieldsAreDataNotAnAlert:
    """`.30.5.x` is a FIELD table for one port, not a per-port link map.

    This class replaces TestSNMPPortLink and TestPortsDownSurvivesARestart,
    which tested a `ports_down` CRITICAL built on the misreading. NovaStar's
    official OID table gives `.30.5.1` as the port's link status, `.30.5.3` as
    the BACKUP port's working status (0 inactive / 1 active) and `.30.5.4` as
    the BACKUP port's link status (0 not linked / 1 linked) — all describing
    the ONE (slot, port) selected by a `.30.4` SET.

    Our H15 answered `{1: 0, 3: 0, 4: 0}`. Decoded as port numbers that read
    "three of sixteen ports report no link" and raised "Output port 1: link
    lost" on a lit, healthy wall. Decoded as fields it reads "primary link 0,
    backup inactive, backup not linked" — and the last two are exactly what a
    wall with an idle backup is supposed to say. The alert had no per-port
    array to stand on, so it was removed rather than tuned.
    """

    def _dev(self, port=None, inventory=()):
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output(port=port))
        dev = _h_device(StubJSONClient(), snmp=snmp)
        dev._known_cards = list(inventory)
        return dev, snmp

    def test_the_fields_are_published_raw_under_port(self):
        """Removing the alert must not remove the data behind it."""
        dev, _ = self._dev()
        dev.poll()
        assert dev.state["snmp"]["output"]["port"] == {
            "link_status": 1, "backup_working": 0, "backup_link": 0}

    def test_the_h15_answer_produces_no_alert_at_all(self):
        """The exact live regression: `{1: 0, 3: 0, 4: 0}` off our H15.

        Every value is 0 and every one of them is fine — an unlit primary
        field plus an idle backup. Nothing in the published state may turn it
        into a fault, on this poll or any later one.
        """
        dev, _ = self._dev({"link_status": 0, "backup_working": 0,
                            "backup_link": 0})
        for _ in range(3):
            dev.poll()
        output = dev.state["snmp"]["output"]
        assert output["port"] == {"link_status": 0, "backup_working": 0,
                                  "backup_link": 0}
        # Nothing derived: no verdict key, no fault list, nothing app.py could
        # loop over to raise a breach. (The alerting half is asserted in
        # tests/test_app.py.)
        assert set(output) == {"card_count", "slot_status", "slot_ok",
                               "firmware", "serial_number", "port_count",
                               "port"}

    def test_the_retired_keys_are_gone_from_state(self):
        """`port_link` / `ports_down` / `ports_expected_linked` all described
        a port dimension that does not exist in this subtree. A consumer that
        still reads them should get a KeyError, not a stale empty list that
        looks like an all-clear."""
        dev, _ = self._dev()
        dev.poll()
        output = dev.state["snmp"]["output"]
        for retired in ("port_link", "ports_down", "ports_expected_linked"):
            assert retired not in output

    def test_a_field_missing_from_the_walk_is_absent_not_zero(self):
        """Three-valued state. `.30.5.2` has no row in NovaStar's table and was
        never observed; a firmware that also withholds `.5.3` must leave the
        key out rather than publish a 0 that reads as "backup inactive"."""
        dev, _ = self._dev({"link_status": 1})
        dev.poll()
        assert dev.state["snmp"]["output"]["port"] == {"link_status": 1}

    def test_the_helpers_that_served_the_alert_are_gone(self):
        """`_snmp_link_up` and `_expected_linked_ports` existed only to build
        and then defend `ports_down`. Both are removed; so is the per-process
        `_snmp_ports_linked` set and the enumeration-snapshot seeding it was
        paired with. Nothing may quietly reintroduce them."""
        assert not hasattr(device_manager, "_snmp_link_up")
        dev, _ = self._dev()
        assert not hasattr(dev, "_expected_linked_ports")
        assert not hasattr(dev, "_snmp_ports_linked")
        assert not hasattr(dev, "_snmp_expected_ports")

    def test_active_ports_is_not_derived_from_the_field_table(self):
        """`active_ports` used to be `sorted(p for p, up in port_link ...)`,
        i.e. a list of output ports built out of field names read as port
        numbers. The JSON `is_used` walk and the video-status bitmask are the
        honest sources and are left to own the field alone.
        """
        dev, _ = self._dev()
        dev.poll()
        assert dev.state["active_ports"] == []


class TestContactHaltedIsPublished:
    """The dashboard has to be able to say WHY it stopped updating."""

    def test_state_carries_the_flag(self):
        dev = _h_device(StubJSONClient())
        assert dev.state["contact_halted"] is False

    def test_halting_republishes_without_waiting_for_a_poll(self):
        mgr = DeviceManager()
        dev = _h_device(StubJSONClient())
        mgr.devices["dev1"] = dev

        mgr.halt("show in progress")
        assert mgr.get_state("dev1")["contact_halted"] is True
        mgr.resume()
        assert mgr.get_state("dev1")["contact_halted"] is False

    def test_a_device_without_the_hook_is_skipped(self):
        """The demo device has no publish_contact_state — halt must not care."""
        mgr = DeviceManager()
        mgr.devices["demo"] = FakeDevice("demo")
        mgr.halt()
        assert device_manager.device_contact_halted() is True


# ── Live wall topology (R0405) ─────────────────────────────


def _iface(slot, iface_id, x, width=960, height=2160, online=None):
    """One screenInterfaces entry, shaped like the observed R0405 answer."""
    return {"slotId": slot, "interfaceId": iface_id, "outputId": iface_id % 4,
            "x": x, "y": 0, "width": width, "height": height,
            "interfaceType": 2, "isCardOnline": online}


def _redundant_screen(name="CIRCUIT MOM"):
    """The wall as the processor actually reports it.

    Four sender cards (slots 20/22/28/30), sixteen output connections, and
    every slot independently covering the same 3840x2160 canvas in four
    960-wide columns.
    """
    slots = [20, 20, 20, 20, 22, 22, 22, 22, 28, 28, 28, 28, 30, 30, 30, 30]
    interfaces = [_iface(slot, i, (i % 4) * 960) for i, slot in enumerate(slots)]
    return {"name": name,
            "size": {"x": 0, "y": 0, "width": 3840, "height": 2160},
            "mosaic": {"row": 1, "column": 1},
            "screenInterfaces": interfaces}


class TestWallTopology:
    """R0405 → the wall's identity, with the redundancy understood."""

    def test_identity_comes_from_the_device(self):
        topo = device_manager.wall_topology(
            {"screen_outputs": {0: _redundant_screen()}})
        assert topo["source"] == "device"
        assert topo["screen_name"] == "CIRCUIT MOM"
        assert topo["canvas"] == {"width": 3840, "height": 2160}
        assert topo["mosaic"] == {"row": 1, "column": 1}
        assert topo["sender_slots"] == [20, 22, 28, 30]

    def test_sixteen_outputs_are_four_active_outputs(self):
        """The bug: 16 output connections are NOT 16 active ports."""
        topo = device_manager.wall_topology(
            {"screen_outputs": {0: _redundant_screen()}})
        assert topo["outputs_total"] == 16
        assert topo["active_outputs"] == 4
        assert topo["redundant"] is True
        assert topo["redundancy_factor"] == 4

    def test_each_slot_covers_the_whole_canvas(self):
        topo = device_manager.wall_topology(
            {"screen_outputs": {0: _redundant_screen()}})
        assert len(topo["slots"]) == 4
        assert all(s["covers_canvas"] for s in topo["slots"])
        assert all(s["outputs"] == 4 for s in topo["slots"])
        assert topo["slots"][0]["interface_ids"] == [0, 1, 2, 3]

    def test_covers_canvas_is_unknown_when_the_canvas_is(self):
        """No canvas means "could not check", NOT "does not cover".

        The wall is fully mirrored — every one of the four sender cards drives
        the whole 3840x2160 canvas — so losing one costs nothing. If R0405
        omits `size`, or `width`/`height` come back in a form this decoder
        cannot read (both of which it tolerates everywhere else), a False here
        describes that same mirrored wall as four slots that each cover only
        part of it: a redundancy gap invented out of a missing field.
        """
        screen = _redundant_screen()
        del screen["size"]
        topo = device_manager.wall_topology({"screen_outputs": {0: screen}})
        assert topo["canvas"] is None
        assert all(s["covers_canvas"] is None for s in topo["slots"])
        # Not a single False anywhere — an `is False` test downstream must
        # find nothing to report.
        assert not any(s["covers_canvas"] is False for s in topo["slots"])

    def test_covers_canvas_is_unknown_when_size_is_mangled(self):
        """A non-numeric width is missing data, not a zero-width wall."""
        screen = _redundant_screen()
        screen["size"] = {"width": "3840px", "height": 2160}
        topo = device_manager.wall_topology({"screen_outputs": {0: screen}})
        assert topo["canvas"] is None
        assert all(s["covers_canvas"] is None for s in topo["slots"])

    def test_a_zero_area_canvas_is_not_covered_by_nothing(self):
        """0x0 must not make every empty slot report full coverage.

        `covered == canvas_area` is 0 == 0 for a slot with no rectangles, so a
        mangled zero-size canvas would otherwise certify total coverage of a
        wall nobody has measured — a false all-clear built entirely out of
        zeros, which is the placeholder trap this module keeps falling into.
        """
        topo = device_manager.wall_topology({"screen_outputs": {0: {
            "name": "ZERO", "size": {"width": 0, "height": 0},
            "screenInterfaces": [{"slotId": 20, "interfaceId": 0}]}}})
        assert topo["slots"][0]["covers_canvas"] is None

    def test_covers_canvas_still_says_false_when_it_knows(self):
        """The real negative survives: a slot driving half the canvas.

        This is the case worth alerting on — that slot going down takes a
        region of the wall with it — and making the field three-valued must
        not have cost us the ability to say so.
        """
        interfaces = [_iface(20, 0, 0), _iface(20, 1, 960),
                      _iface(22, 2, 1920), _iface(22, 3, 2880)]
        topo = device_manager.wall_topology({"screen_outputs": {0: {
            "name": "SPLIT", "size": {"width": 3840, "height": 2160},
            "screenInterfaces": interfaces}}})
        assert all(s["covers_canvas"] is False for s in topo["slots"])

    def test_covers_canvas_is_true_on_the_observed_wall(self):
        """The positive is still a hard True, not a truthy value."""
        topo = device_manager.wall_topology(
            {"screen_outputs": {0: _redundant_screen()}})
        assert all(s["covers_canvas"] is True for s in topo["slots"])

    def test_null_is_card_online_is_reported_as_unknown(self):
        """isCardOnline comes back null here — it must not read as 'all four up'."""
        topo = device_manager.wall_topology(
            {"screen_outputs": {0: _redundant_screen()}})
        assert topo["card_online_known"] is False

    def test_is_card_online_when_the_device_does_report_it(self):
        screen = _redundant_screen()
        screen["screenInterfaces"][0]["isCardOnline"] = True
        topo = device_manager.wall_topology({"screen_outputs": {0: screen}})
        assert topo["card_online_known"] is True

    def test_distinct_regions_are_not_collapsed_when_there_is_no_redundancy(self):
        """Two slots tiling different halves is 4 real outputs, not 2."""
        interfaces = [_iface(20, 0, 0), _iface(20, 1, 960),
                      _iface(22, 2, 1920), _iface(22, 3, 2880)]
        topo = device_manager.wall_topology({"screen_outputs": {0: {
            "name": "SPLIT", "size": {"width": 3840, "height": 2160},
            "mosaic": {"row": 1, "column": 1},
            "screenInterfaces": interfaces}}})
        assert topo["outputs_total"] == 4
        assert topo["active_outputs"] == 4
        assert topo["redundant"] is False
        assert topo["redundancy_factor"] == 1

    def test_no_screen_outputs_is_none_not_a_guess(self):
        assert device_manager.wall_topology({}) is None
        assert device_manager.wall_topology({"screen_outputs": {}}) is None
        assert device_manager.wall_topology(None) is None

    def test_missing_geometry_degrades_to_unknown(self):
        """A screen with no size and no rectangles must not invent numbers."""
        topo = device_manager.wall_topology({"screen_outputs": {0: {
            "name": "PARTIAL", "screenInterfaces": [{"slotId": 20,
                                                     "interfaceId": 0}]}}})
        assert topo["canvas"] is None
        assert topo["mosaic"] is None
        assert topo["outputs_total"] == 1
        # No rectangles to distinguish, so it falls back to the raw count
        # rather than reporting a confident 0 active outputs.
        assert topo["active_outputs"] == 1
        # Not False: there is no canvas and no geometry, so whether this slot
        # covers the canvas is unknown, not answered in the negative.
        assert topo["slots"][0]["covers_canvas"] is None

    def test_junk_interfaces_are_skipped_not_fatal(self):
        topo = device_manager.wall_topology({"screen_outputs": {0: {
            "name": "JUNK", "size": {"width": 3840, "height": 2160},
            "screenInterfaces": ["nonsense", {"slotId": "x", "x": "y"},
                                 _iface(20, 0, 0)]}}})
        assert topo["screen_name"] == "JUNK"
        assert topo["sender_slots"] == [20]

    def test_multiple_screens_all_summarised_primary_first(self):
        topo = device_manager.wall_topology({"screen_outputs": {
            1: _redundant_screen("SECOND"),
            0: _redundant_screen("CIRCUIT MOM"),
        }})
        assert topo["screen_count"] == 2
        assert topo["screen_name"] == "CIRCUIT MOM"
        assert [s["name"] for s in topo["screens"]] == ["CIRCUIT MOM", "SECOND"]


class TestScreenBrightness:
    """H_REG_BRIGHTNESS reads 0 on H-series — the dashboard showed 0% on a lit
    wall. R0401 reports a percentage directly."""

    def test_brightness_comes_from_r0401(self):
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output())
        client = StubJSONClient(screen_list={"screens": [{"screenId": 0}]},
                                brightness=10)
        dev = _h_device(client, snmp=snmp)
        dev.poll()
        assert dev.state["brightness_pct"] == 10.0
        assert dev.state["brightness_source"] == "R0401"
        # The 0-255 field the UI already reads stays populated, derived from
        # the percentage rather than from the register that reports 0.
        assert dev.state["brightness"] == 26

    def test_a_silent_r0401_leaves_brightness_alone(self):
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output())
        client = StubJSONClient(screen_list={"screens": [{"screenId": 0}]},
                                brightness=None)
        dev = _h_device(client, snmp=snmp)
        dev.poll()
        assert dev.state.get("brightness_source") is None

    def test_brightness_is_not_read_while_contact_is_halted(self):
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output())
        client = StubJSONClient(screen_list={"screens": [{"screenId": 0}]},
                                brightness=10)
        dev = _h_device(client, snmp=snmp)
        device_manager.halt_device_contact("test")
        try:
            dev._poll_screen_brightness()
        finally:
            device_manager.resume_device_contact()
        assert "R0401" not in client.commands


class TestSenderLinkMedium:
    """Which sender cards run fibre and which run copper. Without this the
    wall map labels an Ethernet-patched card's chains "OPT n"."""

    OPT_SLOT = {'slotId': 20, 'cardType': 2,
                'lightstatus': {'link0': 2, 'link1': 2},
                'linkstatus': {f'link{i}': 0 for i in range(16)}}
    ETH_SLOT = {'slotId': 22, 'cardType': 2,
                'lightstatus': {'link0': 0, 'link1': 0},
                'linkstatus': dict({f'link{i}': 0 for i in range(16)},
                                   link0=1, link1=1)}
    INPUT_SLOT = {'slotId': 0, 'cardType': 1,
                  'lightstatus': {'link0': 1, 'link1': 1},
                  'linkstatus': {f'link{i}': 0 for i in range(16)}}

    class _Client(StubJSONClient):
        def __init__(self, slots):
            super().__init__(screen_list={"screens": [{"screenId": 0}]})
            self._slots = slots

        def get_device_details(self, device_id=0):
            self.commands.append("R0100")
            return {"cmd": "R0100", "slotList": self._slots}

    def _dev(self, slots):
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output())
        client = self._Client(slots)
        dev = _h_device(client, snmp=snmp)
        dev.poll()
        return dev, client

    def test_medium_is_recorded_per_slot(self):
        dev, _ = self._dev([self.OPT_SLOT, self.ETH_SLOT])
        links = dev.state["sender_links"]
        assert links[20]["medium"] == "opt"
        assert links[22]["medium"] == "ethernet"

    def test_input_cards_are_not_recorded_as_senders(self):
        """Input cards carry link blocks too and they mean something else."""
        dev, _ = self._dev([self.INPUT_SLOT, self.OPT_SLOT])
        assert set(dev.state["sender_links"]) == {20}

    def test_wiring_is_cached_with_the_topology_not_polled(self):
        dev, client = self._dev([self.OPT_SLOT])
        client.commands.clear()
        dev.poll()
        assert "R0100" not in client.commands

    def test_a_silent_r0100_leaves_the_cache_alone(self):
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output())

        class Silent(StubJSONClient):
            def get_device_details(self, device_id=0):
                self.commands.append("R0100")
                return None

        dev = _h_device(Silent(screen_list={"screens": [{"screenId": 0}]}),
                        snmp=snmp)
        dev.poll()
        assert dev.state["sender_links"] == {}

    def test_links_are_not_read_while_contact_is_halted(self):
        snmp = StubSNMPMonitor(health=snmp_health(), screens=snmp_screens(),
                               output=snmp_output())
        client = self._Client([self.OPT_SLOT])
        dev = _h_device(client, snmp=snmp)
        device_manager.halt_device_contact("test")
        try:
            dev.poll()
        finally:
            device_manager.resume_device_contact()
        assert client.commands == []


class TestBitErrorReadAndBaseline:
    """Bit errors are binary-only. Until now the wall map coloured cells from
    whatever the last enumeration recorded, which on a running wall is hours
    stale."""

    def _dev(self, counters):
        """counters: {(sender_byte, chain, card_index): raw_count}"""
        dev = _h_device()
        dev.connected = True
        dev._ensure_tcp = lambda: True
        dev._draft["receiving_cards"] = [
            {"card_number": 1, "port": 0, "card_id": 0, "slot": 20},
            {"card_number": 1, "port": 0, "card_id": 1, "slot": 20},
            {"card_number": 2, "port": 0, "card_id": 0, "slot": 22},
        ]

        # Per-card reads go over the dedicated TCP 5201 connection, not the
        # device's configured control socket.
        def fake_read(register, reg_len, chain, card_index, sender_card):
            raw = counters.get((sender_card, chain, card_index))
            if raw is None:
                return None
            return bytes([0x05, raw & 0xFF, (raw >> 8) & 0xFF])

        dev._percard_read = fake_read
        return dev

    CARDS = [
        {"card_number": 1, "port": 0, "card_id": 0},
        {"card_number": 1, "port": 0, "card_id": 1},
        {"card_number": 2, "port": 0, "card_id": 0},
    ]

    def test_counters_are_read_per_card(self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev({(0, 0, 0): 5, (0, 0, 1): 0, (1, 0, 0): 145})
        results = dev.refresh_bit_errors(self.CARDS)
        by = {(r["card_number"], r["card_id"]): r["bit_errors"]
              for r in results}
        assert by == {(1, 0): 5, (1, 1): 0, (2, 0): 145}

    def test_card_number_is_one_based_and_byte5_is_zero_based(self,
                                                              monkeypatch):
        """Card 2 must be read at byte[5]=1, not 2."""
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        seen = []
        dev = self._dev({(1, 0, 0): 7})
        inner = dev._percard_read

        def spy(register, reg_len, chain, card_index, sender_card):
            seen.append(sender_card)
            return inner(register, reg_len, chain, card_index, sender_card)

        dev._percard_read = spy
        dev.refresh_bit_errors([{"card_number": 2, "port": 0, "card_id": 0}])
        assert seen == [1]

    def test_absent_cards_report_no_count_but_are_still_recorded(
            self, monkeypatch):
        """An absent address returns the free-running counter, which decodes
        to a plausible count for a card that is not there — so no number is
        kept. The ADDRESS is, because a card in the inventory that stops
        answering is exactly the fault this app exists to catch."""
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev({})
        dev._percard_read = lambda *a, **k: bytes([0xD7, 0x56, 0x00])
        results = dev.refresh_bit_errors(self.CARDS)
        assert len(results) == 3
        assert all(r["present"] is False for r in results)
        assert all(r["bit_errors"] is None for r in results)

    def test_results_are_merged_into_the_published_cards(self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev({(0, 0, 0): 5, (0, 0, 1): 0, (1, 0, 0): 145})
        dev.refresh_bit_errors(self.CARDS)
        published = {(c["card_number"], c["card_id"]): c
                     for c in dev.state["receiving_cards"]}
        assert published[(2, 0)]["bit_errors"] == 145
        assert dev.state["bit_errors_read_at"]

    def test_baseline_zeroes_the_display_without_touching_the_device(
            self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        counters = {(0, 0, 0): 5, (0, 0, 1): 0, (1, 0, 0): 145}
        dev = self._dev(counters)
        dev.refresh_bit_errors(self.CARDS)
        assert dev.set_bit_error_baseline() == 3

        published = {(c["card_number"], c["card_id"]): c
                     for c in dev.state["receiving_cards"]}
        assert published[(2, 0)]["bit_errors"] == 0
        # The controller's own counter is untouched and still readable.
        assert published[(2, 0)]["bit_errors_raw"] == 145

    def test_new_errors_after_a_baseline_are_counted_from_zero(self,
                                                               monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev({(1, 0, 0): 145})
        dev.refresh_bit_errors([self.CARDS[2]])
        dev.set_bit_error_baseline()
        dev = self._rearm(dev, {(1, 0, 0): 152})
        dev.refresh_bit_errors([self.CARDS[2]])
        published = {(c["card_number"], c["card_id"]): c
                     for c in dev.state["receiving_cards"]}
        assert published[(2, 0)]["bit_errors"] == 7
        assert published[(2, 0)]["bit_errors_raw"] == 152

    def _rearm(self, dev, counters):
        def fake_read(register, reg_len, chain, card_index, sender_card):
            raw = counters.get((sender_card, chain, card_index))
            if raw is None:
                return None
            return bytes([0x05, raw & 0xFF, (raw >> 8) & 0xFF])
        dev._percard_read = fake_read
        return dev

    def test_a_device_side_reset_reads_as_zero_not_negative(self, monkeypatch):
        """Somebody cleared the counter in NovaLCT after we baselined."""
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev({(1, 0, 0): 145})
        dev.refresh_bit_errors([self.CARDS[2]])
        dev.set_bit_error_baseline()
        dev = self._rearm(dev, {(1, 0, 0): 0})
        dev.refresh_bit_errors([self.CARDS[2]])
        published = {(c["card_number"], c["card_id"]): c
                     for c in dev.state["receiving_cards"]}
        assert published[(2, 0)]["bit_errors"] == 0

    def test_clearing_the_baseline_restores_the_raw_counters(self,
                                                             monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev({(1, 0, 0): 145})
        dev.refresh_bit_errors([self.CARDS[2]])
        dev.set_bit_error_baseline()
        dev.clear_bit_error_baseline()
        published = {(c["card_number"], c["card_id"]): c
                     for c in dev.state["receiving_cards"]}
        assert published[(2, 0)]["bit_errors"] == 145
        assert dev.bit_error_baseline_size() == 0

    def test_a_halted_device_reads_nothing(self):
        dev = self._dev({(0, 0, 0): 5})
        device_manager.halt_device_contact("test")
        try:
            assert dev.refresh_bit_errors(self.CARDS) is None
        finally:
            device_manager.resume_device_contact()

    def test_the_address_list_is_capped(self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev({(0, 0, i): 1 for i in range(500)})
        cards = [{"card_number": 1, "port": 0, "card_id": i}
                 for i in range(500)]
        results = dev.refresh_bit_errors(cards, limit=10)
        assert len(results) == 10


class TestChainBreakDetection:
    """Both signatures were produced deliberately: the operator pulled the
    cable at panel 12 of a 22-panel chain, once while the backup carried the
    tail and once while nothing did."""

    def _chain(self, entries):
        """entries: list of (card_id, bit_errors, present_or_None)"""
        out = []
        for cid, errs, present in entries:
            card = {"card_number": 1, "port": 3, "card_id": cid,
                    "bit_errors": errs}
            if present is not None:
                card["bit_error_present"] = present
            out.append(card)
        return out

    def test_backup_carrying_signature(self):
        """Every panel answers; errors start at 12 and run to the end."""
        cards = self._chain(
            [(i, 0, True) for i in range(11)]
            + [(i, 2, True) for i in range(11, 22)])
        dev = _h_device()
        breaks = dev.detect_chain_breaks(cards)
        assert len(breaks) == 1
        b = breaks[0]
        assert b["signature"] == "bit_errors"
        assert b["break_panel"] == 12
        assert b["affected"] == 11
        assert b["clean_before"] == 11
        assert b["at_head"] is False

    def test_no_backup_signature(self):
        """The chain simply stops answering from panel 12."""
        cards = self._chain(
            [(i, 0, True) for i in range(11)]
            + [(i, None, False) for i in range(11, 22)])
        dev = _h_device()
        breaks = dev.detect_chain_breaks(cards)
        assert len(breaks) == 1
        b = breaks[0]
        assert b["signature"] == "no_answer"
        assert b["break_panel"] == 12
        assert b["affected"] == 11

    def test_a_healthy_chain_reports_nothing(self):
        cards = self._chain([(i, 0, True) for i in range(22)])
        assert _h_device().detect_chain_breaks(cards) == []

    def test_one_noisy_card_is_not_a_break(self):
        """A single card with errors and clean cards after it is one bad card.
        Calling it a break sends someone to the wrong end of a cable run."""
        cards = self._chain(
            [(i, 0, True) for i in range(5)]
            + [(5, 9, True)]
            + [(i, 0, True) for i in range(6, 22)])
        assert _h_device().detect_chain_breaks(cards) == []

    def test_a_hole_with_live_cards_after_it_is_not_a_break(self):
        cards = self._chain(
            [(i, 0, True) for i in range(5)]
            + [(5, None, False)]
            + [(i, 0, True) for i in range(6, 22)])
        assert _h_device().detect_chain_breaks(cards) == []

    def test_a_break_at_the_head_does_not_blame_panel_one(self):
        cards = self._chain([(i, 4, True) for i in range(22)])
        b = _h_device().detect_chain_breaks(cards)[0]
        assert b["at_head"] is True
        assert b["clean_before"] == 0

    def test_a_whole_chain_gone_is_not_reported_as_a_break(self):
        """Nothing answered at all. That is a dead port or a throttled
        controller, not a located break, and guessing panel 1 would be a lie."""
        cards = self._chain([(i, None, False) for i in range(22)])
        assert _h_device().detect_chain_breaks(cards) == []

    def test_unprobed_cards_do_not_testify(self):
        """A card with no presence key was never asked this pass."""
        cards = self._chain([(i, None, None) for i in range(22)])
        assert _h_device().detect_chain_breaks(cards) == []

    def test_chains_are_independent_and_worst_first(self):
        cards = (self._chain([(i, 0, True) for i in range(11)]
                             + [(i, 2, True) for i in range(11, 22)]))
        for c in self._chain([(i, 0, True) for i in range(8)]
                             + [(i, 3, True) for i in range(8, 12)]):
            c["port"] = 4
            cards.append(c)
        breaks = _h_device().detect_chain_breaks(cards)
        assert [b["port"] for b in breaks] == [3, 4]
        assert [b["affected"] for b in breaks] == [11, 4]


class TestTheSlotVerdictNeedsAPopulatedSubtree:
    """`.30` answers on both fleet devices with `{"SN":"","netPortCount":0,
    "status":1,"version":"0"}` while driving a lit wall. A subtree answering
    with stubs must not produce a verdict in EITHER direction — a stub 0 read
    as "slot Abnormal" is a false CRITICAL, and the gate returns None rather
    than True so it is not a false all-clear either.

    This used to gate the removed ports-down alert as well, and took the link
    map as a second argument to cross-check against the card's claimed port
    count. That argument is gone with the map, which was never a map.
    """

    def test_an_unpopulated_subtree_yields_no_verdict(self):
        assert device_manager._snmp_output_is_populated(
            {"serial_number": None, "firmware": None,
             "port_count": None}) is False

    def test_a_card_with_a_serial_is_trusted(self):
        assert device_manager._snmp_output_is_populated(
            {"serial_number": "003168010000008b"}) is True

    def test_a_positive_port_count_is_a_card_that_is_answering(self):
        """`netPortCount` is one of the fields the stub shape leaves at 0, so
        a card reporting a real one is a card whose scalars mean something."""
        assert device_manager._snmp_output_is_populated({"port_count": 16}) is True
        assert device_manager._snmp_output_is_populated({"port_count": 0}) is False
        # A bool is not a port count; `True > 0` must not sneak through.
        assert device_manager._snmp_output_is_populated(
            {"port_count": True}) is False


class TestReadProgressAndRetryQueue:
    """A whole-wall pass is 286 sequential reads with a 45 s pause partway.
    Without progress it looks like a hang; without a retry queue the same tail
    misses every time and is never covered."""

    def _dev(self, answering):
        dev = _h_device()
        dev.connected = True
        dev._ensure_tcp = lambda: True
        dev._draft["receiving_cards"] = []
        dev.known_cards = lambda: []

        def fake(register, reg_len, chain, card_index, sender_card):
            if (sender_card, chain, card_index) in answering:
                b = bytearray(82)
                b[0] = 0x80
                b[1] = 84
                b[3] = 170
                return bytes(b)
            return None
        dev._percard_read = fake
        return dev

    CARDS = [{"card_number": 1, "port": 0, "card_id": i} for i in range(4)]

    def test_progress_is_reported_for_each_card(self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        seen = []
        dev = self._dev({(0, 0, i) for i in range(4)})
        dev.refresh_live_readings(self.CARDS, progress=seen.append)
        assert [e["phase"] for e in seen][-1] == "done"
        assert seen[0]["total"] == 4
        assert [e["done"] for e in seen if e["phase"] == "reading"] == [0, 1, 2, 3]

    def test_a_broken_progress_callback_does_not_abort_the_read(self,
                                                                monkeypatch):
        """Progress is cosmetic; the read costs the controller's budget and
        cannot simply be retried."""
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev({(0, 0, i) for i in range(4)})

        def boom(_info):
            raise RuntimeError("socket gone")
        results = dev.refresh_live_readings(self.CARDS, progress=boom)
        assert len(results) == 4

    def test_cards_that_did_not_answer_are_read_first_next_pass(self,
                                                                monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        # Only cards 0 and 1 answer; 2 and 3 miss.
        dev = self._dev({(0, 0, 0), (0, 0, 1)})
        dev.refresh_live_readings(self.CARDS)
        assert dev.unanswered_count("readings") == 2

        order = []
        inner = dev._percard_read

        def spy(register, reg_len, chain, card_index, sender_card):
            order.append(card_index)
            return inner(register, reg_len, chain, card_index, sender_card)
        dev._percard_read = spy
        dev.refresh_live_readings(self.CARDS)
        assert order[:2] == [2, 3]

    def test_a_card_that_answers_leaves_the_retry_queue(self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev({(0, 0, 0), (0, 0, 1)})
        dev.refresh_live_readings(self.CARDS)
        assert dev.unanswered_count("readings") == 2
        dev._percard_read = self._dev({(0, 0, i) for i in range(4)})._percard_read
        dev.refresh_live_readings(self.CARDS)
        assert dev.unanswered_count("readings") == 0

    def test_cards_never_reached_stay_queued(self, monkeypatch):
        """A pass cut short by the cap must not clear cards it never tried —
        otherwise they lose their turn at the front of the queue."""
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev(set())
        dev.refresh_live_readings(self.CARDS, limit=2)
        # Only 2 were attempted, but the other 2 were never reached and must
        # not be silently forgotten.
        assert dev.unanswered_count("readings") == 2

    def test_the_two_read_kinds_keep_separate_queues(self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev(set())
        dev.refresh_live_readings(self.CARDS)
        assert dev.unanswered_count("readings") == 4
        assert dev.unanswered_count("bit errors") == 0


class TestPartialResultsArePublishedDuringAPass:
    """A whole-wall pass is minutes long. Holding every reading until the end
    means the wall shows nothing new while the operator watches a bar crawl —
    and nothing at all if they stop it early or halt contact partway."""

    def _dev(self):
        dev = _h_device()
        dev.connected = True
        dev._ensure_tcp = lambda: True
        dev._draft["receiving_cards"] = []
        dev.known_cards = lambda: []

        def fake(register, reg_len, chain, card_index, sender_card):
            b = bytearray(82)
            b[0] = 0x80
            b[1] = 84          # 42.0 C
            b[3] = 170         # 5.10 V
            return bytes(b)
        dev._percard_read = fake
        return dev

    def _cards(self, n):
        return [{"card_number": 1, "port": 0, "card_id": i} for i in range(n)]

    def test_readings_appear_before_the_pass_finishes(self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        monkeypatch.setattr(device_manager, "PER_CARD_PUBLISH_EVERY", 4)
        dev = self._dev()
        seen = []

        def watch(info):
            if info["phase"] == "flush":
                # State published mid-pass must already carry the readings.
                seen.append(len(dev.state["receiving_cards"]))
        dev.refresh_live_readings(self._cards(12), progress=watch)
        assert seen == [4, 8, 12]

    def test_a_flush_marks_the_readings_fresh(self, monkeypatch):
        """Otherwise a partial pass publishes live readings that the
        freshness gate still calls stale, and nothing alerts on them."""
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        monkeypatch.setattr(device_manager, "PER_CARD_PUBLISH_EVERY", 4)
        dev = self._dev()
        fresh_at_flush = []

        def watch(info):
            if info["phase"] == "flush":
                fresh_at_flush.append(dev.cards_are_fresh())
        dev.refresh_live_readings(self._cards(8), progress=watch)
        assert fresh_at_flush and all(fresh_at_flush)

    def test_a_halt_partway_keeps_what_was_already_read(self, monkeypatch):
        """The point of flushing: stopping early is not the same as learning
        nothing."""
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        monkeypatch.setattr(device_manager, "PER_CARD_PUBLISH_EVERY", 4)
        dev = self._dev()

        def watch(info):
            if info["phase"] == "flush" and info["done"] >= 4:
                device_manager.halt_device_contact("operator stop")
        try:
            dev.refresh_live_readings(self._cards(20), progress=watch)
        finally:
            device_manager.resume_device_contact()
        published = dev.state["receiving_cards"]
        assert 4 <= len(published) < 20
        assert all(c["temp_c"] == 42.0 for c in published)


class TestOnlyOnePerCardPassAtATime:
    """Two concurrent passes interleave on one TCP connection, spend the
    controller's request budget twice as fast, and report progress over each
    other — which made the progress bar jump backwards and forwards."""

    def _dev(self):
        dev = _h_device()
        dev.connected = True
        dev._ensure_tcp = lambda: True
        dev._draft["receiving_cards"] = []
        dev.known_cards = lambda: []
        dev._percard_read = lambda *a, **k: None
        return dev

    CARDS = [{"card_number": 1, "port": 0, "card_id": 0}]

    def test_a_second_pass_is_refused_while_one_runs(self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev()
        seen = []

        def watch(info):
            if info["phase"] == "reading" and not seen:
                seen.append(dev.refresh_bit_errors(self.CARDS))
        dev.refresh_live_readings(self.CARDS, progress=watch)
        assert seen == ["busy"]

    def test_the_reader_is_released_when_the_pass_finishes(self, monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev()
        dev.refresh_live_readings(self.CARDS)
        assert dev.percard_pass_running() is False
        # And a later pass is not refused.
        assert dev.refresh_live_readings(self.CARDS) != "busy"

    def test_a_refused_pass_does_not_release_the_running_one(self,
                                                             monkeypatch):
        monkeypatch.setattr(device_manager, "BIT_ERROR_READ_PACE", 0)
        dev = self._dev()
        states = []

        def watch(info):
            if info["phase"] == "reading":
                dev.refresh_bit_errors(self.CARDS)
                states.append(dev.percard_pass_running())
        dev.refresh_live_readings(self.CARDS, progress=watch)
        assert states == [True]


class TestPSUAlertsOnTransitionNotState:
    """`iSignal 0` means "not connected to power" (NovaStar R&D, by email).
    On a chassis with spare bays that is ALSO what an empty bay reports, and
    the field cannot tell them apart — so alerting on the state would raise a
    CRITICAL every polling cycle, all show, about a bay that never had a
    supply in it. Same mistake as the SNMP port-link alert that was deleted
    from this module. Only the 1 -> 0 edge is alertable."""

    def _dev(self):
        dev = _h_device()
        return dev

    def _psus(self, *signals):
        return [{"power_id": i, "connected": sig, "status": 0,
                 "i_signal": None if sig is None else int(bool(sig))}
                for i, sig in enumerate(signals)]

    def test_a_bay_that_was_never_connected_is_never_alerted_on(self):
        dev = self._dev()
        for _ in range(5):
            assert dev._psu_transitions(self._psus(True, False)) == []

    def test_a_supply_that_drops_is_alerted_on(self):
        dev = self._dev()
        assert dev._psu_transitions(self._psus(True, True)) == []
        assert dev._psu_transitions(self._psus(True, False)) == [1]

    def test_the_drop_keeps_reporting_while_it_stays_down(self):
        """It really is still down; the alert layer's own cooldown decides how
        often to say so."""
        dev = self._dev()
        dev._psu_transitions(self._psus(True, True))
        assert dev._psu_transitions(self._psus(True, False)) == [1]
        assert dev._psu_transitions(self._psus(True, False)) == [1]

    def test_a_supply_that_comes_back_stops_alerting(self):
        dev = self._dev()
        dev._psu_transitions(self._psus(True, True))
        assert dev._psu_transitions(self._psus(True, False)) == [1]
        assert dev._psu_transitions(self._psus(True, True)) == []

    def test_unknown_isignal_is_not_a_drop(self):
        dev = self._dev()
        dev._psu_transitions(self._psus(True, True))
        assert dev._psu_transitions(self._psus(True, None)) == []

    def test_a_wall_of_empty_bays_stays_silent_forever(self):
        """The failure this exists to prevent."""
        dev = self._dev()
        for _ in range(50):
            assert dev._psu_transitions(self._psus(True, False, False, False)) == []
