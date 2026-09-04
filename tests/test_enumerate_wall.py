"""Tests for the wall enumeration tool.

Nothing here talks to a real device. The binary path is exercised against a
loopback TCP stub that speaks the NovaStar 0x55AA frame format, and the JSON
path against injected fake clients. Two of these tests exist specifically to
make an accidental run against production hardware impossible:
`TestConfirmationFlag` and `test_no_hardcoded_production_ip`.
"""

import json
import os
import socket
import struct
import threading
import time
from argparse import Namespace
from datetime import datetime

import pytest

import enumerate_wall as ew
from novastar_protocol import (
    H_REG_BIT_ERRORS,
    REG_LIVE_MONITOR,
    checksum,
    decode_length,
)


# ── Loopback TCP stub for the binary protocol ─────────────────────────────


class FakeSenderCard:
    """Threaded TCP server that answers NovaStar per-card READ frames.

    `chains` maps chain index -> number of cards on that chain. An address
    inside a chain is "present"; anything at or past the chain's length is
    absent, and the stub either answers with a not-present status byte
    (`silent_absent=False`, the fast default) or says nothing at all
    (`silent_absent=True`, which is how real hardware may behave — unknown, so
    both are tested).

    `drop` maps (chain, card) -> how many of the first responses for that
    address to swallow, used to simulate a transient packet loss mid-chain.
    """

    def __init__(self, chains, silent_absent=False, drop=None):
        self.chains = dict(chains)
        self.silent_absent = silent_absent
        self.drop = dict(drop or {})
        self.probes = []          # every (chain, card) the client asked about
        self.sender_cards_seen = []   # byte[5] of every request
        self.connections = 0
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- server loop --

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.connections += 1
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(0.3)
        try:
            while not self._stop.is_set():
                req = self._recv_exactly(conn, 20)
                if req is None:
                    return
                reply = self._respond(req)
                if reply is not None:
                    conn.sendall(reply)
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _recv_exactly(conn, count):
        buf = bytearray()
        while len(buf) < count:
            try:
                chunk = conn.recv(count - len(buf))
            except socket.timeout:
                return None
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    # -- protocol --

    def _respond(self, req):
        chain = req[7]
        card = req[8]
        sender_card = req[5]
        register = struct.unpack(">I", req[12:16])[0]
        length_field = struct.unpack(">H", req[16:18])[0]
        with self._lock:
            self.probes.append((chain, card))
            self.sender_cards_seen.append(sender_card)
            remaining = self.drop.get((chain, card), 0)
            if remaining:
                self.drop[(chain, card)] = remaining - 1
                return None

        present = card < self.chains.get(chain, 0)
        if not present and self.silent_absent:
            return None
        payload = self._payload(register, length_field, present)
        return self._frame(req, payload)

    @staticmethod
    def _payload(register, length_field, present):
        size = decode_length(length_field)
        if register == H_REG_BIT_ERRORS[0]:
            # byte[0] 0x05 = present (§6.5); bytes[1-2] = LE error count.
            body = bytes([0x05 if present else 0x00, 0x00, 0x00])
        elif register == REG_LIVE_MONITOR[0]:
            body = bytearray(size)
            # 0x80 present / 0xE0 past the end of the chain. Real hardware
            # still returns the previous card's readings behind the absent
            # flag, so the stub does too — a decoder that ignores byte[0]
            # must fail here.
            body[0] = 0x80 if present else 0xE0
            body[1] = 88            # 44.0 C
            body[3] = 170           # 5.10 V
            body[12] = 1            # PRIMARY
            body = bytes(body)
        else:
            body = bytes(size)
        return body[:size].ljust(size, b"\x00")

    @staticmethod
    def _frame(req, payload):
        # Response mirrors the request's 16 body bytes (register at [12:16],
        # length at [16:18]) so parse_response can read them back out.
        body = req[2:18] + payload
        return struct.pack(">H", 0xAA55) + body + struct.pack("<H",
                                                              checksum(body))

    # -- lifecycle --

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    def probes_for(self, chain):
        with self._lock:
            return [c for ch, c in self.probes if ch == chain]


def make_probe(server, register="biterr", timeout=0.15, pace=0):
    return ew.BinarySenderCardProbe("127.0.0.1", server.port, timeout=timeout,
                                    connect_timeout=1.0, register=register,
                                    pace=pace)


# ── Fake JSON client ──────────────────────────────────────────────────────


class FakeJSONClient:
    """Stands in for HSeriesJSONClient. Answers only for known addresses."""

    def __init__(self, known=(), screens=None, output_info=None,
                 power_status=0):
        self.known = set(known)
        self.screens = screens
        self.output_info = output_info
        # 0 is what the capture this fake reproduces actually sent. NovaStar
        # documents 0 = Fault / 1 = Normal, so a test that wants a supply
        # reported healthy has to ask for 1.
        self.power_status = power_status
        self.requested = []
        self.closed = False

    def get_receiving_cards_batch(self, addresses, batch_size=None):
        out = []
        for slot, port, card_id in addresses:
            self.requested.append((slot, port, card_id))
            if (slot, port, card_id) in self.known:
                out.append({
                    "cmd": "R0155", "deviceId": 0, "slotId": slot,
                    "portId": port, "recvCardId": card_id,
                    "power0Status": self.power_status,
                    "power1Status": self.power_status, "brightness": 127,
                    "temp": 88, "voltage": 170, "ack": "Ok",
                })
            else:
                out.append(None)
        return out

    def get_screen_list(self):
        return self.screens

    def get_screen_output_info(self, screen_id):
        return self.output_info

    def close(self):
        self.closed = True


# ── Chain boundary logic ──────────────────────────────────────────────────


class TestChainBoundary:
    """A chain of N cards must yield exactly N — never N+1 from the probe
    past the end, never N-1 from a dropped response."""

    def test_chain_of_n_yields_exactly_n(self):
        with FakeSenderCard({0: 5}) as server:
            with make_probe(server) as probe:
                cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=0)
        assert [c for c, _ in cards] == [0, 1, 2, 3, 4]

    def test_boundary_probe_is_sent_but_not_counted(self):
        with FakeSenderCard({0: 5}) as server:
            with make_probe(server) as probe:
                cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=0)
            probed = server.probes_for(0)
        assert len(cards) == 5
        # NovaLCT probes one card past the end to find the boundary; so do we,
        # and that probe must not become a panel.
        assert probed == [0, 1, 2, 3, 4, 5]

    def test_empty_chain_probed_once_yields_zero(self):
        with FakeSenderCard({0: 0}) as server:
            with make_probe(server) as probe:
                cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=0)
            probed = server.probes_for(0)
        assert cards == []
        assert probed == [0]

    def test_silent_absent_device_still_finds_the_boundary(self):
        # Real hardware may simply not answer for a card that isn't there.
        with FakeSenderCard({0: 3}, silent_absent=True) as server:
            with make_probe(server, timeout=0.05) as probe:
                cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=0,
                                           silence_rests=0)
        assert len(cards) == 3

    def test_silent_absent_empty_chain_yields_zero(self):
        with FakeSenderCard({0: 0}, silent_absent=True) as server:
            with make_probe(server, timeout=0.05) as probe:
                cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=0,
                                           silence_rests=0)
        assert cards == []

    def test_transient_drop_mid_chain_is_recovered_by_retry(self):
        # A single lost response at card 2 must not truncate a 6-card chain.
        with FakeSenderCard({0: 6}, silent_absent=True,
                            drop={(0, 2): 1}) as server:
            with make_probe(server, timeout=0.05) as probe:
                cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=2,
                                           silence_rests=0)
        assert len(cards) == 6

    def test_without_retries_a_dropped_response_under_reports(self):
        # Documents why --retries defaults to 2. --silence-rests would also
        # have caught this one; it is disabled here to isolate the retry.
        with FakeSenderCard({0: 6}, silent_absent=True,
                            drop={(0, 2): 1}) as server:
            with make_probe(server, timeout=0.05) as probe:
                cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=0,
                                           silence_rests=0)
        assert len(cards) == 2

    def test_silence_rests_recover_a_drop_that_outlasts_the_retries(self):
        # Same 6-card chain, but the drop survives every retry. Without a rest
        # the chain truncates to 2; with one it comes back whole.
        with FakeSenderCard({0: 6}, silent_absent=True,
                            drop={(0, 2): 3}) as server:
            with make_probe(server, timeout=0.05) as probe:
                slept = []
                cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=2,
                                           silence_rests=2, rest_seconds=9.0,
                                           sleep=slept.append)
        assert len(cards) == 6
        # One rest recovers card 2; this stub is also silent at the real
        # boundary (card 6), which spends the full allowance before the walk
        # accepts it.
        assert slept == [9.0, 9.0, 9.0]

    def test_max_cards_cap_warns_instead_of_silently_truncating(self):
        warnings = []
        reporter = ew.Reporter(0)
        reporter.warn = warnings.append
        with FakeSenderCard({0: 10}) as server:
            with make_probe(server) as probe:
                cards = ew.enumerate_chain(probe, 0, max_cards=4, retries=0,
                                           reporter=reporter)
        assert len(cards) == 4
        assert warnings and "may be longer" in warnings[0]

    def test_chains_are_independent(self):
        with FakeSenderCard({0: 2, 1: 0, 2: 4}) as server:
            with make_probe(server) as probe:
                counts = [len(ew.enumerate_chain(probe, ch, max_cards=91,
                                                 retries=0))
                          for ch in (0, 1, 2)]
        assert counts == [2, 0, 4]


class TestProbeRegisters:

    def test_bit_error_register_still_works_as_a_probe(self):
        with FakeSenderCard({0: 1}) as server:
            with make_probe(server) as probe:
                assert probe.register == H_REG_BIT_ERRORS[0]
                result = probe.probe(0, 0)
        assert result.present
        assert result.readings["bit_errors"] == 0

    def test_live_register_yields_readings(self):
        with FakeSenderCard({0: 1}) as server:
            with make_probe(server, register="live") as probe:
                result = probe.probe(0, 0)
        assert result.present
        assert result.readings["temp_c"] == 44.0
        # raw 170 & 0x7F = 42, units of 0.1 V → 4.2 V. Vendor doc §4.3.4.
        assert result.readings["voltage_v"] == 4.2
        assert result.readings["link_status"] == "PRIMARY"

    def test_unknown_register_rejected(self):
        with pytest.raises(ValueError):
            ew.BinarySenderCardProbe("127.0.0.1", 5201, register="nope")

    def test_live_register_reports_presence_from_byte0(self):
        """0x80 present; bit 0x40 set means nothing at this address. Verified
        on a chain known to hold exactly 22 panels: cards 0-21 answered 0x80
        and 22-25 answered 0xE0/0xE2/0xE4/0xE6 while repeating the last card's
        readings."""
        payload = bytearray(82)
        payload[0] = 0x80
        payload[1] = 84          # 42.0 C  (84 / 2)
        payload[3] = 170         # 4.20 V  (170 & 0x7F = 42, units of 0.1 V)
        payload[12] = 1
        present, readings = ew._presence_live_monitor(bytes(payload))
        assert present is True
        assert readings["temp_c"] == 42.0
        assert readings["voltage_v"] == 4.2
        assert readings["link_status"] == "PRIMARY"

    def test_an_address_past_the_end_of_a_chain_is_absent(self):
        for status in (0xC0, 0xE0, 0xE2, 0xE4, 0xE6):
            payload = bytearray(82)
            payload[0] = status
            payload[1] = 86      # the previous card's reading, repeated
            present, readings = ew._presence_live_monitor(bytes(payload))
            assert present is False, hex(status)
            # and crucially no readings, so nothing invents a 43 C panel
            # for an address with nothing on it
            assert readings == {}, hex(status)

    def test_live_is_the_default_because_it_also_yields_readings(self):
        assert ew.DEFAULT_PROBE_REGISTER == "live"

    def test_a_chain_walked_with_live_finds_the_right_length(self):
        """The end-to-end check the old refusal was standing in for: an
        absent address answers, so the walk must stop on byte[0], not on
        silence."""
        with FakeSenderCard({0: 5}) as server:
            with make_probe(server, register="live") as probe:
                cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=0,
                                           silence_rests=0)
        assert [c for c, _ in cards] == [0, 1, 2, 3, 4]
        assert all(r["temp_c"] == 44.0 for _, r in cards)


class TestSenderCardAddressing:
    """Every sender card is read over ONE connection to 5201; byte[5] selects
    which. The 5202/5203/5204 services the rqProMI reply advertises accept
    connections but answer reads with an empty payload."""

    def test_discovered_ports_map_to_zero_based_sender_card_bytes(self,
                                                                  monkeypatch):
        monkeypatch.setattr(ew, "discover_sender_card_ports",
                            lambda *a, **k: [5201, 5202, 5203, 5204])
        args = Namespace(ip="1.2.3.4", sender_cards=None,
                         discovery_port=3800, discovery_timeout=0.1)
        assert ew._resolve_sender_cards(args, ew.Reporter(0)) == {
            1: 0, 2: 1, 3: 2, 4: 3}

    def test_explicit_sender_cards_map_the_same_way(self):
        args = Namespace(ip="1.2.3.4", sender_cards=[1, 3])
        assert ew._resolve_sender_cards(args, ew.Reporter(0)) == {1: 0, 3: 2}

    def test_probe_writes_the_sender_card_into_byte5(self):
        with FakeSenderCard({0: 1}) as server:
            probe = ew.BinarySenderCardProbe(
                "127.0.0.1", server.port, timeout=0.15, connect_timeout=1.0,
                sender_card=2, pace=0)
            with probe:
                probe.probe(0, 0)
        assert server.sender_cards_seen == [2]

    def test_every_sender_card_uses_the_same_tcp_port(self, monkeypatch,
                                                      tmp_path):
        factory = _FakeProbeFactory({0: {0: 2}, 1: {0: 3}})
        monkeypatch.setattr(ew, "BinarySenderCardProbe", factory)
        code = ew.main(["10.0.0.9", "--yes-contact-hardware",
                        "--sender-cards", "1,2", "--chains", "0",
                        "--no-json", "--retries", "0", "-q",
                        "-o", str(tmp_path / "snap.json")])
        assert code == ew.EXIT_OK
        assert {p.tcp_port for p in factory.instances} == {ew.BINARY_PORT}
        assert sorted(p.sender_card for p in factory.instances) == [0, 1]


class TestSilenceIsNotABoundary:
    """An answered "no card here" ends a chain. Silence does not — silence is
    also what a degraded controller looks like, and accepting it is how a
    22-card chain enumerated as 9."""

    class _ScriptedProbe:
        """Answers from a script of ProbeResults per (chain, card)."""

        def __init__(self, script):
            self.script = {k: list(v) for k, v in script.items()}
            self.asked = []
            self.probe_count = 0
            self.sender_card = 0
            self.tcp_port = ew.BINARY_PORT

        def probe(self, chain, card):
            self.probe_count += 1
            self.asked.append((chain, card))
            queue = self.script.get((chain, card))
            if not queue:
                return ew.ProbeResult(False, answered=True)
            return queue.pop(0) if len(queue) > 1 else queue[0]

    def _present(self):
        return ew.ProbeResult(True, {"bit_errors": 0})

    def _silent(self):
        return ew.ProbeResult(False, answered=False)

    def _absent(self):
        return ew.ProbeResult(False, answered=True)

    def test_silence_that_recovers_after_a_rest_does_not_truncate(self):
        """Card 2 is silent through every retry, then answers after a rest.
        The chain must come back as 4 cards, not 2."""
        script = {
            (0, 0): [self._present()],
            (0, 1): [self._present()],
            (0, 2): [self._silent(), self._silent(), self._silent(),
                     self._present()],
            (0, 3): [self._present()],
            (0, 4): [self._absent()],
        }
        probe = self._ScriptedProbe(script)
        slept = []
        cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=2,
                                   silence_rests=2, rest_seconds=7.0,
                                   sleep=slept.append)
        assert [c for c, _ in cards] == [0, 1, 2, 3]
        assert slept == [7.0]

    def test_answered_absence_ends_the_chain_without_resting(self):
        script = {
            (0, 0): [self._present()],
            (0, 1): [self._absent()],
        }
        probe = self._ScriptedProbe(script)
        slept = []
        cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=2,
                                   silence_rests=2, rest_seconds=7.0,
                                   sleep=slept.append)
        assert [c for c, _ in cards] == [0]
        assert slept == []            # no rest paid for a real boundary
        assert probe.asked == [(0, 0), (0, 1)]   # and no retries either

    def test_persistent_silence_is_accepted_but_warned_as_a_lower_bound(self):
        script = {
            (0, 0): [self._present()],
            (0, 1): [self._silent()],
        }
        probe = self._ScriptedProbe(script)
        reporter = _CapturingReporter()
        slept = []
        cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=1,
                                   silence_rests=2, rest_seconds=3.0,
                                   reporter=reporter, sleep=slept.append)
        assert [c for c, _ in cards] == [0]
        assert slept == [3.0, 3.0]
        assert any("LOWER BOUND" in w for w in reporter.warnings)
        assert any("SILENCE" in w for w in reporter.warnings)

    def test_silence_rests_zero_accepts_the_first_silence(self):
        script = {(0, 0): [self._silent()]}
        probe = self._ScriptedProbe(script)
        slept = []
        cards = ew.enumerate_chain(probe, 0, max_cards=91, retries=0,
                                   silence_rests=0, rest_seconds=3.0,
                                   sleep=slept.append)
        assert cards == []
        assert slept == []

    def test_probe_timeout_default_covers_the_slow_empty_address(self):
        """An empty address answers far slower than an occupied one; at 0.5s
        every chain on the test wall ended on a timeout instead."""
        assert ew.DEFAULT_PROBE_TIMEOUT >= 1.0


class _CapturingReporter(ew.Reporter):
    def __init__(self):
        super().__init__(0)
        self.warnings = []

    def warn(self, message):
        self.warnings.append(message)


class TestVerifySilentChains:
    """Silence during a sweep is ambiguous. Re-walking those chains one at a
    time from a rested controller is what turns 20 back into the true 22."""

    def test_silence_ended_chains_are_reported_to_the_caller(self):
        probe = TestSilenceIsNotABoundary._ScriptedProbe({
            (0, 0): [ew.ProbeResult(True, {}, answered=True)],
            (0, 1): [ew.ProbeResult(False, answered=False)],
        })
        unresolved = []
        ew.enumerate_sender_card_binary(
            probe, 1, 20, [0], max_cards=91, retries=0, silence_rests=0,
            unresolved=unresolved)
        assert unresolved == [(1, 0, 1)]

    def test_answered_boundary_is_not_reported_as_unresolved(self):
        probe = TestSilenceIsNotABoundary._ScriptedProbe({
            (0, 0): [ew.ProbeResult(True, {}, answered=True)],
            (0, 1): [ew.ProbeResult(False, answered=True)],
        })
        unresolved = []
        ew.enumerate_sender_card_binary(
            probe, 1, 20, [0], max_cards=91, retries=0, silence_rests=0,
            unresolved=unresolved)
        assert unresolved == []

    def _verify_args(self):
        return Namespace(max_cards=91, retries=0, silence_rests=0,
                         rest_seconds=0.0, verify_silent=True)

    def test_a_longer_rested_result_replaces_the_swept_one(self):
        """The sweep saw 1 card; the rested re-walk sees 3."""
        rested = TestSilenceIsNotABoundary._ScriptedProbe({
            (0, 0): [ew.ProbeResult(True, {}, answered=True)],
            (0, 1): [ew.ProbeResult(True, {}, answered=True)],
            (0, 2): [ew.ProbeResult(True, {}, answered=True)],
            (0, 3): [ew.ProbeResult(False, answered=True)],
        })
        rested.open = lambda: rested
        rested.close = lambda: None
        swept_cards = [ew.make_card_entry(1, 20, 0, 0)]
        slept = []
        budget = ew.RequestBudget(size=5, rest=45.0, sleep=slept.append)
        cards, probes = ew.verify_silent_chains(
            [(1, 0, 1)], swept_cards, {1: 0}, {1: 20},
            lambda sender_card: rested, budget, self._verify_args(),
            ew.Reporter(0))
        assert sorted(c["card_id"] for c in cards) == [0, 1, 2]
        assert slept == [45.0]        # a full rest before the re-walk
        assert probes == 4

    def test_a_shorter_rested_result_never_removes_cards(self):
        """A verification pass that is itself throttled must not make things
        worse than the sweep already had them."""
        throttled = TestSilenceIsNotABoundary._ScriptedProbe({
            (0, 0): [ew.ProbeResult(False, answered=False)],
        })
        throttled.open = lambda: throttled
        throttled.close = lambda: None
        swept_cards = [ew.make_card_entry(1, 20, 0, i) for i in range(3)]
        budget = ew.RequestBudget(size=5, rest=0.0, sleep=lambda _s: None)
        cards, _ = ew.verify_silent_chains(
            [(1, 0, 3)], swept_cards, {1: 0}, {1: 20},
            lambda sender_card: throttled, budget, self._verify_args(),
            ew.Reporter(0))
        assert sorted(c["card_id"] for c in cards) == [0, 1, 2]

    def test_verification_only_touches_the_chain_it_re_walked(self):
        rested = TestSilenceIsNotABoundary._ScriptedProbe({
            (0, 0): [ew.ProbeResult(True, {}, answered=True)],
            (0, 1): [ew.ProbeResult(True, {}, answered=True)],
            (0, 2): [ew.ProbeResult(False, answered=True)],
        })
        rested.open = lambda: rested
        rested.close = lambda: None
        other = [ew.make_card_entry(1, 20, 5, i) for i in range(4)]
        swept_cards = [ew.make_card_entry(1, 20, 0, 0)] + other
        budget = ew.RequestBudget(size=5, rest=0.0, sleep=lambda _s: None)
        cards, _ = ew.verify_silent_chains(
            [(1, 0, 1)], swept_cards, {1: 0}, {1: 20},
            lambda sender_card: rested, budget, self._verify_args(),
            ew.Reporter(0))
        assert len([c for c in cards if c["port"] == 5]) == 4
        assert len([c for c in cards if c["port"] == 0]) == 2

    def test_a_connect_failure_keeps_the_swept_count(self):
        def failing_factory(sender_card):
            probe = TestSilenceIsNotABoundary._ScriptedProbe({})
            probe.open = lambda: (_ for _ in ()).throw(OSError("refused"))
            probe.close = lambda: None
            return probe

        swept_cards = [ew.make_card_entry(1, 20, 0, i) for i in range(2)]
        budget = ew.RequestBudget(size=5, rest=0.0, sleep=lambda _s: None)
        cards, _ = ew.verify_silent_chains(
            [(1, 0, 2)], swept_cards, {1: 0}, {1: 20}, failing_factory,
            budget, self._verify_args(), ew.Reporter(0))
        assert len(cards) == 2


class TestRequestBudget:
    """The controller answers ~150-200 per-card reads then starts refusing,
    and stays refusing. Rest before the budget runs out, not after."""

    def test_no_rest_before_the_budget_is_spent(self):
        slept = []
        b = ew.RequestBudget(size=3, rest=45.0, sleep=slept.append)
        for _ in range(3):
            b.spend()
        assert slept == []
        assert b.spent == 3

    def test_rest_when_the_next_request_would_exceed_the_budget(self):
        slept = []
        b = ew.RequestBudget(size=3, rest=45.0, sleep=slept.append)
        for _ in range(4):
            b.spend()
        assert slept == [45.0]
        assert b.spent == 1          # counter restarts after the rest
        assert b.rests_taken == 1

    def test_budget_is_shared_across_sender_cards(self):
        """A fresh connection does not reset the controller's budget: sender
        card 2 opened a new socket after card 1's sweep and got nothing."""
        slept = []
        b = ew.RequestBudget(size=4, rest=30.0, sleep=slept.append)
        p1 = ew.BinarySenderCardProbe("127.0.0.1", 5201, sender_card=0,
                                      pace=0, budget=b)
        p2 = ew.BinarySenderCardProbe("127.0.0.1", 5201, sender_card=1,
                                      pace=0, budget=b)
        assert p1.budget is p2.budget is b

    def test_size_zero_disables_resting(self):
        slept = []
        b = ew.RequestBudget(size=0, rest=45.0, sleep=slept.append)
        for _ in range(50):
            b.spend()
        assert slept == []

    def test_on_rest_is_notified(self):
        events = []
        b = ew.RequestBudget(size=2, rest=12.0, sleep=lambda _s: None,
                             on_rest=lambda n, secs: events.append((n, secs)))
        for _ in range(5):
            b.spend()
        assert events == [(1, 12.0), (2, 12.0)]

    def test_probe_spends_the_budget(self):
        slept = []
        b = ew.RequestBudget(size=2, rest=8.0, sleep=slept.append)
        with FakeSenderCard({0: 6}) as server:
            probe = ew.BinarySenderCardProbe(
                "127.0.0.1", server.port, timeout=0.15, connect_timeout=1.0,
                pace=0, budget=b, sleep=slept.append)
            with probe:
                for card in range(4):
                    probe.probe(0, card)
        assert slept == [8.0]        # one rest after the 2-probe budget
        assert probe.probe_count == 4

    def test_defaults_are_conservative(self):
        assert 0 < ew.DEFAULT_PROBE_BUDGET <= 200
        assert ew.DEFAULT_BUDGET_REST >= 40


class TestReadingsShareTheBudget:
    """R0155 draws on the same controller allowance the binary reads do:
    asking for 286 cards at once answered 36 and left it refusing for ~45 s."""

    def _cards(self, count):
        return [ew.make_card_entry(1, 20, 0, i) for i in range(count)]

    def test_readings_are_chunked_to_the_budget_with_rests(self):
        cards = self._cards(10)
        client = FakeJSONClient(known={(20, 0, i) for i in range(10)})
        slept = []
        budget = ew.RequestBudget(size=4, rest=45.0, sleep=slept.append)
        stats = ew.attach_readings(client, cards, budget=budget)
        assert stats["answered"] == 10
        assert slept == [45.0, 45.0]      # 4 + 4 + 2

    def test_without_a_budget_the_old_single_call_path_is_used(self):
        cards = self._cards(10)
        client = FakeJSONClient(known={(20, 0, i) for i in range(10)})
        stats = ew.attach_readings(client, cards)
        assert stats["answered"] == 10
        assert client.requested == [(20, 0, i) for i in range(10)]

    def test_every_address_is_still_requested_exactly_once(self):
        cards = self._cards(9)
        client = FakeJSONClient(known={(20, 0, i) for i in range(9)})
        budget = ew.RequestBudget(size=2, rest=0.0, sleep=lambda _s: None)
        ew.attach_readings(client, cards, budget=budget)
        assert client.requested == [(20, 0, i) for i in range(9)]


class TestSilenceRestsDefault:
    """Mid-sweep rests and the verification pass do the same job; running both
    just makes the sweep slow. Whichever one is active must be a real defence."""

    def _run(self, monkeypatch, tmp_path, *extra):
        seen = {}
        real = ew.enumerate_sender_card_binary

        def spy(*a, **kw):
            seen["silence_rests"] = kw.get("silence_rests")
            return real(*a, **kw)

        monkeypatch.setattr(ew, "enumerate_sender_card_binary", spy)
        monkeypatch.setattr(ew, "BinarySenderCardProbe",
                            _FakeProbeFactory({0: {0: 2}}))
        ew.main(["10.0.0.9", "--yes-contact-hardware", "--sender-cards", "1",
                 "--chains", "0", "--no-json", "--retries", "0", "-q",
                 "-o", str(tmp_path / "snap.json")] + list(extra))
        return seen["silence_rests"]

    def test_verification_on_means_no_mid_sweep_rests(self, monkeypatch,
                                                      tmp_path):
        assert self._run(monkeypatch, tmp_path) == 0

    def test_verification_off_restores_mid_sweep_rests(self, monkeypatch,
                                                       tmp_path):
        assert self._run(monkeypatch, tmp_path,
                         "--no-verify-silent") == ew.DEFAULT_SILENCE_RESTS

    def test_explicit_flag_wins_over_both(self, monkeypatch, tmp_path):
        assert self._run(monkeypatch, tmp_path, "--silence-rests", "5") == 5


class TestProbePacing:
    """The controller throttles under sustained polling, and a throttled probe
    is indistinguishable from an absent card — which truncates chains."""

    def test_pace_delays_between_probes(self):
        with FakeSenderCard({0: 3}) as server:
            probe = ew.BinarySenderCardProbe(
                "127.0.0.1", server.port, timeout=0.15, connect_timeout=1.0,
                pace=0.05)
            with probe:
                start = time.monotonic()
                for card in range(4):
                    probe.probe(0, card)
                elapsed = time.monotonic() - start
        assert elapsed >= 4 * 0.05

    def test_pace_zero_does_not_sleep(self):
        with FakeSenderCard({0: 3}) as server:
            probe = ew.BinarySenderCardProbe(
                "127.0.0.1", server.port, timeout=0.15, connect_timeout=1.0,
                pace=0)
            with probe:
                start = time.monotonic()
                for card in range(4):
                    probe.probe(0, card)
                elapsed = time.monotonic() - start
        assert elapsed < 0.5

    def test_default_pace_is_nonzero(self):
        assert ew.DEFAULT_PROBE_PACE > 0

    def test_probe_on_unreachable_port_is_absent_not_an_exception(self):
        # Closed port: connect fails, and the probe must report absent rather
        # than blowing up mid-sweep.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()
        probe = ew.BinarySenderCardProbe("127.0.0.1", dead_port, timeout=0.05,
                                         connect_timeout=0.2)
        assert probe.probe(0, 0).present is False
        probe.close()


class TestSenderCardEnumeration:

    def test_card_entries_have_the_snapshot_shape(self):
        with FakeSenderCard({0: 2, 8: 1}) as server:
            with make_probe(server) as probe:
                cards = ew.enumerate_sender_card_binary(
                    probe, card_number=2, slot=22, chains=[0, 8],
                    max_cards=91, retries=0)
        assert len(cards) == 3
        first = cards[0]
        assert first["card_number"] == 2
        assert first["slot"] == 22
        assert first["user_slot"] == 23
        assert first["opt"] == 1
        assert first["port"] == 0
        assert first["port_on_opt"] == 1
        assert first["card_id"] == 0
        # Chain 8 is the first port on OPT 2 — matches the existing snapshot's
        # (card 2, opt 2, port 8, port_on_opt 1) rows.
        opt2 = cards[-1]
        assert (opt2["opt"], opt2["port"], opt2["port_on_opt"]) == (2, 8, 1)

    def test_missing_slot_leaves_user_slot_none(self):
        entry = ew.make_card_entry(1, None, 0, 0)
        assert entry["slot"] is None and entry["user_slot"] is None


# ── rqProMI discovery ─────────────────────────────────────────────────────


class TestDiscoveryParsing:

    CAPTURE = ("rpProMI:App,0161 H_SUB_CARD@^^@5201 H_SUB_CARD@^^@5202 "
               "H_SUB_CARD@^^@5203")

    def test_parses_the_captured_reply(self):
        assert ew.parse_rqpromi_response(self.CAPTURE) == [5201, 5202, 5203]

    def test_accepts_bytes(self):
        assert ew.parse_rqpromi_response(self.CAPTURE.encode()) == \
            [5201, 5202, 5203]

    def test_broadcast_port_5200_is_not_a_sender_card(self):
        text = "rpProMI:App H_SUB_CARD@^^@5200 H_SUB_CARD@^^@5201"
        assert ew.parse_rqpromi_response(text) == [5201]

    def test_unrelated_lan_chatter_is_ignored(self):
        assert ew.parse_rqpromi_response("hello 5201 5202") == []
        assert ew.parse_rqpromi_response(b"\xff\xfe\x00") == []
        assert ew.parse_rqpromi_response(None) == []

    def test_loose_fallback_only_for_discovery_replies(self):
        assert ew.parse_rqpromi_response("rpProMI:App 5201 5203") == \
            [5201, 5203]

    def test_deduplicates_and_sorts(self):
        text = "rpProMI: H_SUB_CARD@^^@5203 H_SUB_CARD@^^@5201 " \
               "H_SUB_CARD@^^@5203"
        assert ew.parse_rqpromi_response(text) == [5201, 5203]


class TestDiscoveryOverLoopback:

    def test_discovers_ports(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        server.settimeout(1.0)
        port = server.getsockname()[1]
        seen = []

        def serve():
            try:
                data, addr = server.recvfrom(4096)
                seen.append(data)
                server.sendto(TestDiscoveryParsing.CAPTURE.encode(), addr)
            except OSError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            ports = ew.discover_sender_card_ports("127.0.0.1", timeout=1.0,
                                                  port=port)
        finally:
            thread.join(timeout=1.0)
            server.close()
        assert ports == [5201, 5202, 5203]
        assert seen == [ew.DISCOVERY_REQUEST]

    def test_no_answer_returns_empty(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        try:
            assert ew.discover_sender_card_ports("127.0.0.1", timeout=0.1,
                                                 port=port) == []
        finally:
            sock.close()


# ── Snapshot shape ────────────────────────────────────────────────────────


class TestSnapshotShape:

    def _snapshot(self):
        cards = [ew.make_card_entry(1, 20, 0, i) for i in range(3)]
        cards += [ew.make_card_entry(2, 22, 9, i) for i in range(2)]
        return ew.build_snapshot(
            "10.1.2.3", cards, {1: 20, 2: 22},
            {"screen_name": "COSMIC MEADOW",
             "screen_size": {"width": 11520, "height": 2160},
             "mosaic": {"row": 1, "column": 3}},
            transport="binary")

    def test_top_level_keys_match_the_app_contract(self):
        snap = self._snapshot()
        for key in ("device_ip", "screen_name", "screen_size", "mosaic",
                    "sender_cards", "cards", "captured_at"):
            assert key in snap

    def test_captured_at_is_iso8601_and_utc(self):
        snap = self._snapshot()
        parsed = datetime.fromisoformat(snap["captured_at"])
        assert parsed.utcoffset().total_seconds() == 0

    def test_sender_cards_block(self):
        """`role` is None, not "primary". The test chassis has two primary and
        two backup sender cards and nothing readable distinguishes them, so
        claiming "primary" for every card is a guess dressed up as data."""
        snap = self._snapshot()
        assert snap["sender_cards"] == [
            {"card_number": 1, "slot": 20, "user_slot": 21, "role": None,
             "carrying_panels": True},
            {"card_number": 2, "slot": 22, "user_slot": 23, "role": None,
             "carrying_panels": True},
        ]

    def test_a_sender_card_with_no_cards_is_recorded_as_not_carrying(self):
        """An idle backup finds nothing. That is its normal state, so it is
        recorded rather than dropped — and not called a fault."""
        cards = [ew.make_card_entry(1, 20, 0, 0)]
        snap = ew.build_snapshot("10.1.2.3", cards, {1: 20, 2: 22})
        by_number = {c["card_number"]: c for c in snap["sender_cards"]}
        assert by_number[1]["carrying_panels"] is True
        assert by_number[2]["carrying_panels"] is False
        assert by_number[2]["role"] is None

    def test_card_fields_match_the_existing_snapshot(self):
        snap = self._snapshot()
        required = {"card_number", "slot", "user_slot", "opt", "port",
                    "port_on_opt", "card_id"}
        for card in snap["cards"]:
            assert required <= set(card)

    def test_enumeration_block_records_total(self):
        snap = self._snapshot()
        assert snap["enumeration"]["total_cards"] == 5
        assert snap["enumeration"]["transport"] == "binary"

    def test_sender_cards_derived_from_cards_when_slot_map_empty(self):
        cards = [ew.make_card_entry(3, None, 0, 0)]
        snap = ew.build_snapshot("10.1.2.3", cards, {})
        assert snap["sender_cards"] == [
            {"card_number": 3, "slot": None, "user_slot": None,
             "role": None, "carrying_panels": True}]


# ── Atomic write ──────────────────────────────────────────────────────────


class TestAtomicWrite:

    def test_writes_and_reads_back(self, tmp_path):
        target = tmp_path / "snap.json"
        ew.write_snapshot(str(target), {"cards": [], "captured_at": "x"})
        assert json.loads(target.read_text())["captured_at"] == "x"

    def test_replaces_previous_content_completely(self, tmp_path):
        target = tmp_path / "snap.json"
        target.write_text(json.dumps({"cards": list(range(500))}))
        ew.write_snapshot(str(target), {"cards": [1]})
        assert json.loads(target.read_text()) == {"cards": [1]}

    def test_leaves_no_temp_files_behind(self, tmp_path):
        target = tmp_path / "snap.json"
        ew.write_snapshot(str(target), {"cards": []})
        assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]

    def test_failed_serialisation_leaves_the_old_file_intact(self, tmp_path):
        target = tmp_path / "snap.json"
        target.write_text('{"cards": ["original"]}')
        # A value json.dump cannot serialise blows up mid-write.
        with pytest.raises(TypeError):
            ew.write_snapshot(str(target), {"cards": [object()]})
        assert json.loads(target.read_text()) == {"cards": ["original"]}
        # ...and the half-written temp file is cleaned up.
        assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]

    def test_creates_missing_directory(self, tmp_path):
        target = tmp_path / "nested" / "dir" / "snap.json"
        ew.write_snapshot(str(target), {"cards": []})
        assert target.exists()


# ── R0155 readings pass ───────────────────────────────────────────────────


class TestAttachReadings:

    def test_readings_are_merged(self):
        cards = [ew.make_card_entry(1, 20, 0, i) for i in range(2)]
        client = FakeJSONClient(known={(20, 0, 0), (20, 0, 1)})
        stats = ew.attach_readings(client, cards)
        assert stats == {"requested": 2, "answered": 2, "silent": 0,
                         "skipped": 0}
        assert cards[0]["temp_c"] == 44.0
        # raw 170 & 0x7F = 42, units of 0.1 V → 4.2 V. Vendor doc §5.4.2.
        assert cards[0]["voltage_v"] == 4.2
        assert cards[0]["brightness"] == 127
        # The fixture reports 0 on both supplies, which is unknown rather than
        # healthy, and an unknown reading is not merged onto the card at all.
        assert cards[0].get("primary_power_ok") is None
        assert cards[0]["online"] is True

    def test_a_healthy_power_flag_is_merged(self):
        """1 is the vendor's Normal value and has to reach the card entry."""
        cards = [ew.make_card_entry(1, 20, 0, 0)]
        client = FakeJSONClient(known={(20, 0, 0)}, power_status=1)
        ew.attach_readings(client, cards)
        assert cards[0]["primary_power_ok"] is True
        assert cards[0]["backup_power_ok"] is True

    def test_unreadable_cards_are_kept_not_dropped(self):
        # The whole point: a card that exists but can't be read is a finding,
        # not a card to delete from the snapshot. It is recorded as UNKNOWN
        # rather than offline — the binary walk proved it is there, and R0155
        # going quiet is a statement about R0155, not about the panel.
        cards = [ew.make_card_entry(1, 20, 0, i) for i in range(3)]
        client = FakeJSONClient(known={(20, 0, 0)})
        stats = ew.attach_readings(client, cards)
        assert len(cards) == 3
        assert stats["answered"] == 1 and stats["silent"] == 2
        assert cards[1]["online"] is None
        assert cards[1]["reading"] == "no_answer"
        assert cards[2]["online"] is None

    def test_fully_silent_chain_warns_loudly(self):
        warnings = []
        reporter = ew.Reporter(0)
        reporter.warn = warnings.append
        cards = [ew.make_card_entry(1, 20, 4, i) for i in range(3)]
        ew.attach_readings(FakeJSONClient(known=set()), cards,
                           reporter=reporter)
        assert len(warnings) == 1
        assert "0 of 3" in warnings[0]
        assert "recvCardId" in warnings[0]

    def test_cards_without_a_slot_are_skipped_not_requested(self):
        cards = [ew.make_card_entry(1, None, 0, 0)]
        client = FakeJSONClient()
        stats = ew.attach_readings(client, cards)
        assert stats["skipped"] == 1 and stats["requested"] == 0
        assert client.requested == []


# ── JSON sweep (the known-lossy comparison path) ──────────────────────────


class TestJSONSweep:

    def test_finds_cards_past_an_internal_hole(self):
        # R0155 leaves holes inside a chain; stopping at the first silence
        # would truncate the chain at the hole.
        known = {(20, 0, i) for i in range(10)} - {(20, 0, 4)}
        client = FakeJSONClient(known=known)
        cards, holes = ew.enumerate_chain_json(client, 20, 0, max_cards=91,
                                               gap_tolerance=4)
        assert [c for c, _ in cards] == [0, 1, 2, 3, 5, 6, 7, 8, 9]
        assert holes == [4]

    def test_stops_after_gap_tolerance(self):
        client = FakeJSONClient(known={(20, 0, i) for i in range(3)})
        cards, holes = ew.enumerate_chain_json(client, 20, 0, max_cards=91,
                                               gap_tolerance=4)
        assert len(cards) == 3
        # Trailing silences are boundary probes, not holes.
        assert holes == []
        # Addresses go out a batch at a time, so the whole first chunk of 8 is
        # requested — but the sweep stops there instead of walking to
        # --max-cards.
        assert client.requested == [(20, 0, cid) for cid in range(8)]

    def test_empty_chain_yields_nothing(self):
        client = FakeJSONClient(known=set())
        cards, holes = ew.enumerate_chain_json(client, 20, 0, max_cards=91,
                                               gap_tolerance=4)
        assert cards == [] and holes == []


class TestTopology:

    def test_reads_screen_metadata(self):
        client = FakeJSONClient(
            screens={"screens": [{"screenId": 0, "name": "COSMIC MEADOW"}]},
            output_info={
                "size": {"width": 11520, "height": 2160},
                "mosaic": {"row": 1, "column": 3},
                "screenInterfaces": [{"slotId": 24}, {"slotId": 20},
                                     {"slotId": 22}, {"slotId": 20}],
            })
        topo = ew.fetch_topology(client)
        assert topo["screen_name"] == "COSMIC MEADOW"
        assert topo["screen_size"] == {"width": 11520, "height": 2160}
        assert topo["mosaic"] == {"row": 1, "column": 3}
        assert topo["slots"] == [20, 22, 24]

    def test_missing_screen_list_is_not_fatal(self):
        warnings = []
        reporter = ew.Reporter(0)
        reporter.warn = warnings.append
        topo = ew.fetch_topology(FakeJSONClient(screens=None),
                                 reporter=reporter)
        assert topo["screen_name"] is None
        assert warnings

    def test_slot_map_from_topology(self):
        assert ew.resolve_slot_map([1, 2, 3], [20, 22, 24]) == \
            {1: 20, 2: 22, 3: 24}

    def test_slot_map_override_wins(self):
        assert ew.resolve_slot_map([1], [20], override={1: 99}) == {1: 99}

    def test_missing_slots_warn(self):
        warnings = []
        reporter = ew.Reporter(0)
        reporter.warn = warnings.append
        assert ew.resolve_slot_map([1, 2], [20], reporter=reporter) == {1: 20}
        assert warnings and "--slot-map" in warnings[0]


# ── Summary ───────────────────────────────────────────────────────────────


class TestSummary:

    def test_reports_per_card_per_chain_and_total(self):
        cards = ([ew.make_card_entry(1, 20, 0, i) for i in range(4)]
                 + [ew.make_card_entry(1, 20, 9, i) for i in range(2)]
                 + [ew.make_card_entry(2, 22, 0, i) for i in range(3)])
        snap = ew.build_snapshot("10.1.2.3", cards, {1: 20, 2: 22})
        text = ew.summarise(snap, {"answered": 9, "silent": 0, "skipped": 0})
        assert "sender card 1 (slot 20): 6 cards" in text
        assert "sender card 2 (slot 22): 3 cards" in text
        assert "chain  0  (OPT 1 port 1):    4" in text
        assert "chain  9  (OPT 2 port 2):    2" in text
        assert "TOTAL CARDS       : 9" in text

    def test_scanned_but_empty_chains_are_shown_as_zero(self):
        cards = [ew.make_card_entry(1, 20, 0, 0)]
        snap = ew.build_snapshot("10.1.2.3", cards, {1: 20},
                                 meta={"chains_scanned": [0, 1, 2]})
        text = ew.summarise(snap)
        # "no panels here" must be distinguishable from "never looked".
        assert "chain  1  (OPT 1 port 2):    0" in text
        assert "chain  2  (OPT 1 port 3):    0" in text

    def test_holes_are_flagged_as_a_lower_bound(self):
        snap = ew.build_snapshot("10.1.2.3",
                                 [ew.make_card_entry(1, 20, 0, 0)], {1: 20})
        text = ew.summarise(snap, None, holes=[(1, 0, 4), (1, 0, 5)])
        assert "LOWER BOUND" in text
        assert "--transport binary" in text


# ── Confirmation flag / accidental-run protection ─────────────────────────


class TestConfirmationFlag:

    def _capture(self, argv, monkeypatch):
        """Run main() with both transports replaced by tripwires."""
        fired = []

        def tripwire(*a, **kw):
            fired.append(a)
            raise AssertionError("transport ran without confirmation")

        monkeypatch.setattr(ew, "run_binary", tripwire)
        monkeypatch.setattr(ew, "run_json", tripwire)
        monkeypatch.setattr(ew, "discover_sender_card_ports", tripwire)
        monkeypatch.setattr(ew, "write_snapshot", tripwire)

        class Buffer:
            def __init__(self):
                self.text = ""

            def write(self, s):
                self.text += s

            def flush(self):
                pass

        out, err = Buffer(), Buffer()
        code = ew.main(argv, stdout=out, stderr=err)
        return code, out.text, err.text, fired

    def test_refuses_to_run_without_the_flag(self, monkeypatch):
        code, out, _err, fired = self._capture(["10.9.9.9"], monkeypatch)
        assert code == ew.EXIT_NOT_CONFIRMED
        assert code != 0
        assert fired == []

    def test_prints_exactly_what_it_would_contact(self, monkeypatch):
        _code, out, _err, _fired = self._capture(
            ["10.9.9.9", "--sender-cards", "1,2,3"], monkeypatch)
        assert "10.9.9.9" in out
        # One TCP target for every sender card — they are distinguished by
        # byte[5] of the frame, not by port, so 5202/5203 must NOT appear.
        assert "10.9.9.9:5201" in out
        assert "5202" not in out and "5203" not in out
        assert "sender cards 1, 2, 3" in out
        assert "--yes-contact-hardware" in out
        assert "Nothing is written to the device" in out

    def test_plan_names_the_output_file(self, monkeypatch, tmp_path):
        target = str(tmp_path / "snap.json")
        _code, out, _err, _fired = self._capture(
            ["10.9.9.9", "-o", target], monkeypatch)
        assert target in out

    def test_ip_is_required(self):
        with pytest.raises(SystemExit):
            ew.build_arg_parser().parse_args([])

    def test_no_hardcoded_production_ip(self):
        """The live wall lives at 192.168.0.10. It must not appear anywhere in
        this module — no default, no example, nothing that could be fired by
        accident."""
        source = open(ew.__file__, encoding="utf-8").read()
        assert "192.168.0.10" not in source

    def test_ip_argument_has_no_default(self):
        action = next(a for a in ew.build_arg_parser()._actions
                      if a.dest == "ip")
        assert action.default in (None, )


# ── Argument parsing helpers ──────────────────────────────────────────────


class TestArgParsing:

    def test_int_list_ranges_and_commas(self):
        assert ew._parse_int_list("0-3") == [0, 1, 2, 3]
        assert ew._parse_int_list("1,2,3") == [1, 2, 3]
        assert ew._parse_int_list("0-2,5") == [0, 1, 2, 5]
        assert ew._parse_int_list("3,1,3") == [1, 3]

    def test_slot_map(self):
        assert ew._parse_slot_map("1=20,2=22,3=24") == {1: 20, 2: 22, 3: 24}

    def test_defaults(self):
        args = ew.build_arg_parser().parse_args(["10.0.0.1"])
        assert args.chains == list(range(16))
        assert args.transport == "binary"
        assert args.max_cards == ew.DEFAULT_MAX_CARDS_PER_CHAIN
        assert args.retries == ew.DEFAULT_BOUNDARY_RETRIES
        assert args.yes_contact_hardware is False


# ── End to end through main() ─────────────────────────────────────────────


class _FakeProbeFactory:
    """Drop-in for BinarySenderCardProbe backed by a chain map per sender card.

    Keyed on the 0-based `sender_card` byte, not on a TCP port: every sender
    card is read over the one connection to 5201 and selected by byte[5].
    """

    def __init__(self, per_sender_card):
        self.per_sender_card = per_sender_card
        self.instances = []

    def __call__(self, ip, tcp_port, timeout=None, connect_timeout=None,
                 register="biterr", sender_card=0, pace=0, budget=None):
        probe = _FakeProbe(tcp_port, sender_card,
                           self.per_sender_card.get(sender_card, {}))
        self.instances.append(probe)
        return probe


class _FakeProbe:
    def __init__(self, tcp_port, sender_card, chains):
        self.tcp_port = tcp_port
        self.sender_card = sender_card
        self.chains = chains
        self.probe_count = 0

    def open(self):
        return self

    def close(self):
        pass

    def probe(self, chain, card_index):
        self.probe_count += 1
        present = card_index < self.chains.get(chain, 0)
        # answered=True: this stub always replies, so an absence here is a real
        # boundary and must not trigger the silence rests.
        return ew.ProbeResult(present, {"bit_errors": 0} if present else {},
                              answered=True)


class TestEndToEnd:

    def _argv(self, tmp_path, *extra):
        return ["10.0.0.9", "--yes-contact-hardware", "--sender-cards", "1,2",
                "--chains", "0-2", "--no-json", "--retries", "0",
                "-q", "-o", str(tmp_path / "snap.json")] + list(extra)

    def test_writes_a_snapshot(self, monkeypatch, tmp_path, capsys):
        factory = _FakeProbeFactory({0: {0: 4, 1: 2}, 1: {2: 3}})
        monkeypatch.setattr(ew, "BinarySenderCardProbe", factory)
        code = ew.main(self._argv(tmp_path))
        assert code == ew.EXIT_OK

        snap = json.loads((tmp_path / "snap.json").read_text())
        assert len(snap["cards"]) == 9
        assert snap["device_ip"] == "10.0.0.9"
        assert datetime.fromisoformat(snap["captured_at"])
        assert snap["enumeration"]["transport"] == "binary"
        assert snap["enumeration"]["chains_scanned"] == [0, 1, 2]

        summary = capsys.readouterr().out
        assert "TOTAL CARDS       : 9" in summary
        # Slot is derived from the card number via the chassis layout
        # (card N is slot 20 + 2(N-1)), so it is known even with --no-json.
        assert "sender card 1 (slot 20): 6 cards" in summary

    def test_slot_map_flows_into_the_snapshot(self, monkeypatch, tmp_path):
        factory = _FakeProbeFactory({0: {0: 1}, 1: {0: 1}})
        monkeypatch.setattr(ew, "BinarySenderCardProbe", factory)
        code = ew.main(self._argv(tmp_path, "--slot-map", "1=20,2=22"))
        assert code == ew.EXIT_OK
        snap = json.loads((tmp_path / "snap.json").read_text())
        assert snap["sender_cards"] == [
            {"card_number": 1, "slot": 20, "user_slot": 21, "role": None,
             "carrying_panels": True},
            {"card_number": 2, "slot": 22, "user_slot": 23, "role": None,
             "carrying_panels": True},
        ]
        assert {c["slot"] for c in snap["cards"]} == {20, 22}

    def test_zero_cards_does_not_clobber_an_existing_snapshot(
            self, monkeypatch, tmp_path):
        target = tmp_path / "snap.json"
        target.write_text('{"cards": ["previous"]}')
        factory = _FakeProbeFactory({0: {}, 1: {}})
        monkeypatch.setattr(ew, "BinarySenderCardProbe", factory)
        code = ew.main(self._argv(tmp_path))
        assert code == ew.EXIT_ERROR
        assert json.loads(target.read_text()) == {"cards": ["previous"]}

    def test_run_binary_over_the_real_loopback_stub(self, tmp_path):
        """Full binary path including sockets and frame parsing."""
        with FakeSenderCard({0: 3, 1: 0, 2: 5}) as server:
            args = Namespace(
                ip="127.0.0.1", sender_cards=[1],
                binary_port=server.port, pace=0,
                chains=[0, 1, 2], max_cards=91, retries=0,
                timeout=0.15, connect_timeout=1.0, probe_register="biterr",
                no_json=True, no_readings=True, json_port=6000,
                discovery_port=3800, discovery_timeout=0.1)
            result, error = ew.run_binary(args, ew.Reporter(0), None)
        assert error is None
        snapshot, reading_stats, holes = result
        assert len(snapshot["cards"]) == 8
        assert reading_stats is None and holes is None
        assert snapshot["enumeration"]["probes_sent"] == 3 + 1 + 1 + 5 + 1

    def test_no_sender_cards_is_an_error_not_an_empty_snapshot(self, tmp_path):
        args = Namespace(
            ip="127.0.0.1", sender_cards=[], chains=[0], max_cards=1,
            retries=0, timeout=0.05, connect_timeout=0.2,
            probe_register="biterr", no_json=True, no_readings=True,
            json_port=6000, discovery_port=1, discovery_timeout=0.05)
        result, error = ew.run_binary(args, ew.Reporter(0), None)
        assert result is None
        assert "no sender cards" in error


class TestReadOnly:
    """The tool must never write device state."""

    def test_sends_no_w_commands(self):
        source = open(ew.__file__, encoding="utf-8").read()
        # W0120 is the JSON heartbeat — a write-class command. It must never
        # be called, even though the module docstring names it.
        assert ".heartbeat(" not in source
        assert "build_write" not in source

    def test_only_read_frames_are_built(self):
        source = open(ew.__file__, encoding="utf-8").read()
        assert "build_read_card(" in source
        for forbidden in ("sendto(b\"W", "'cmd': 'W", '"cmd": "W'):
            assert forbidden not in source


def test_default_output_is_next_to_the_app_source():
    path = ew.default_output_path()
    assert os.path.basename(path) == "wall_live_snapshot.json"
    assert os.path.dirname(path) == os.path.dirname(
        os.path.abspath(ew.__file__))


class TestAbsentAddressesCarryNoReadings:
    """An absent address answers with the same `?? 56 00` shape the
    free-running counter produces, which decodes to 86 bit errors for a card
    that is not there."""

    def test_absent_yields_no_readings(self):
        present, readings = ew._presence_bit_errors(bytes([0xD7, 0x56, 0x00]))
        assert present is False
        assert readings == {}

    def test_present_yields_the_count(self):
        present, readings = ew._presence_bit_errors(bytes([0x05, 0x91, 0x00]))
        assert present is True
        assert readings["bit_errors"] == 0x0091
        assert readings["bit_errors_saturated"] is False

    def test_a_clean_card_reads_zero_not_absent(self):
        present, readings = ew._presence_bit_errors(bytes([0x05, 0x00, 0x00]))
        assert present is True
        assert readings["bit_errors"] == 0


class TestSlotToCardNumber:
    """Output cards occupy every second slot from 20. Getting this wrong meant
    the two backup cards were never scanned: the rqProMI port list numbered
    them 1..4, so the enumerator probed byte[5] 0-3 while the real cards were
    1, 2, 5 and 6 — byte[5] 0, 1, 4 and 5."""

    def test_the_operator_confirmed_mapping(self):
        assert ew.slot_to_card_number(20) == 1
        assert ew.slot_to_card_number(22) == 2
        assert ew.slot_to_card_number(28) == 5
        assert ew.slot_to_card_number(30) == 6

    def test_intermediate_slots_are_the_cards_between(self):
        assert ew.slot_to_card_number(24) == 3
        assert ew.slot_to_card_number(26) == 4

    def test_odd_and_low_slots_are_not_output_cards(self):
        assert ew.slot_to_card_number(21) is None
        assert ew.slot_to_card_number(19) is None
        assert ew.slot_to_card_number(0) is None
        assert ew.slot_to_card_number(None) is None

    def test_sender_cards_come_from_slots_not_discovery_ports(self,
                                                              monkeypatch):
        """The four rqProMI services would have said 1, 2, 3, 4."""
        monkeypatch.setattr(ew, "discover_sender_card_ports",
                            lambda *a, **k: [5201, 5202, 5203, 5204])
        args = Namespace(ip="1.2.3.4", sender_cards=None,
                         discovery_port=3800, discovery_timeout=0.1,
                         _topology_slots=[20, 22, 28, 30])
        assert ew._resolve_sender_cards(args, ew.Reporter(0)) == {
            1: 0, 2: 1, 5: 4, 6: 5}

    def test_discovery_is_the_fallback_and_says_so(self):
        warnings = []
        reporter = ew.Reporter(0)
        reporter.warn = warnings.append
        args = Namespace(ip="127.0.0.1", sender_cards=None,
                         discovery_port=1, discovery_timeout=0.05,
                         _topology_slots=[])
        ew._resolve_sender_cards(args, reporter)
        assert warnings

    def test_slot_map_pairs_card_with_its_own_slot(self):
        """Zipping sorted card numbers against sorted slots put card 5 with
        slot 28's list position rather than with slot 28."""
        assert ew.resolve_slot_map([1, 2, 5, 6], [20, 22, 28, 30]) == {
            1: 20, 2: 22, 5: 28, 6: 30}


class TestSilentR0155IsNotOffline:
    """A 286-card readings pass answered 36 and then hit the request budget.
    Recording the other 250 as `online: False` put "250 panels offline" on a
    dashboard for a wall where every panel was lit."""

    def _cards(self, n):
        return [ew.make_card_entry(1, 20, 0, i) for i in range(n)]

    def test_a_silent_card_is_unknown_not_offline(self):
        cards = self._cards(3)
        client = FakeJSONClient(known={(20, 0, 0)})
        stats = ew.attach_readings(client, cards)
        assert stats == {"requested": 3, "answered": 1, "silent": 2,
                         "skipped": 0}
        assert cards[0]["online"] is True
        assert cards[1]["online"] is None
        assert cards[2]["online"] is None

    def test_silence_is_labelled_so_the_ui_need_not_guess(self):
        cards = self._cards(2)
        stats = ew.attach_readings(FakeJSONClient(known={(20, 0, 0)}), cards)
        assert stats["answered"] == 1
        assert cards[0]["reading"] == "ok"
        assert cards[1]["reading"] == "no_answer"

    def test_presence_survives_a_silent_reading(self):
        """The binary walk proved the card is there. R0155 declining to talk
        about it does not un-prove that."""
        cards = self._cards(2)
        ew.attach_readings(FakeJSONClient(known=set()), cards)
        assert all(c["present"] is True for c in cards)

    def test_a_card_the_walk_found_is_marked_present(self):
        assert ew.make_card_entry(1, 20, 0, 0)["present"] is True

    def test_silent_cards_carry_no_invented_readings(self):
        cards = self._cards(2)
        ew.attach_readings(FakeJSONClient(known=set()), cards)
        for c in cards:
            assert "temp_c" not in c and "voltage_v" not in c


class TestReadingsPassIsSkippedWhenRedundant:
    """The `live` walk already returns temperature and voltage per card. Doing
    an R0155 pass afterwards re-asks the same questions over the protocol the
    controller rate-limits hardest — and spends the budget doing it. On a
    286-card wall that pass answered 36."""

    def _args(self, tmp_path, **over):
        base = dict(ip="10.0.0.9", sender_cards=[1], binary_port=1,
                    pace=0, chains=[0], max_cards=91, retries=0,
                    timeout=0.05, connect_timeout=0.2,
                    probe_register="live", no_json=False, no_readings=False,
                    json_port=6000, discovery_port=3800,
                    discovery_timeout=0.05, verify_silent=False,
                    silence_rests=0, probe_budget=0, budget_rest=0)
        base.update(over)
        return Namespace(**base)

    def test_a_walk_that_returned_readings_skips_r0155(self, monkeypatch):
        client = FakeJSONClient(known={(20, 0, 0)})
        monkeypatch.setattr(ew, "HSeriesJSONClient", lambda *a, **k: client)
        monkeypatch.setattr(ew, "fetch_topology", lambda *a, **k: {"slots": [20]})
        monkeypatch.setattr(ew, "BinarySenderCardProbe",
                            _FakeProbeFactoryWithReadings({0: {0: 3}}))
        result, error = ew.run_binary(self._args(None), ew.Reporter(0), None)
        assert error is None
        # Not one R0155 was sent.
        assert client.requested == []

    def test_a_walk_with_no_readings_still_falls_back_to_r0155(self,
                                                               monkeypatch):
        client = FakeJSONClient(known={(20, 0, 0)})
        monkeypatch.setattr(ew, "HSeriesJSONClient", lambda *a, **k: client)
        monkeypatch.setattr(ew, "fetch_topology", lambda *a, **k: {"slots": [20]})
        monkeypatch.setattr(ew, "BinarySenderCardProbe",
                            _FakeProbeFactory({0: {0: 3}}))
        ew.run_binary(self._args(None, probe_register="biterr"),
                      ew.Reporter(0), None)
        assert client.requested   # the biterr walk carries no temps


class _FakeProbeFactoryWithReadings(_FakeProbeFactory):
    """Like the plain fake, but answers with temperature/voltage the way the
    `live` register does."""

    def __call__(self, ip, tcp_port, timeout=None, connect_timeout=None,
                 register="live", sender_card=0, pace=0, budget=None):
        probe = _FakeProbeWithReadings(tcp_port, sender_card,
                                       self.per_sender_card.get(sender_card, {}))
        self.instances.append(probe)
        return probe


class _FakeProbeWithReadings(_FakeProbe):
    def probe(self, chain, card_index):
        self.probe_count += 1
        present = card_index < self.chains.get(chain, 0)
        readings = ({"temp_c": 42.0, "voltage_v": 5.1, "online": True}
                    if present else {})
        return ew.ProbeResult(present, readings, answered=True)
