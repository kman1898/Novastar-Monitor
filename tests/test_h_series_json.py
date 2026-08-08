"""Tests for the H-series JSON UDP client.

These tests don't talk to a real device. They verify the wire-format
encoding (commands sent), response decoding, socket reuse / batching
behaviour, and the helpers that map JSON shapes into monitor state.
"""

import json
import socket
import threading
import h_series_json as hsj


# ── Loopback UDP server helper ────────────────────────────────────────────


class LoopServer:
    """Minimal UDP loopback server for exercising the client end-to-end.

    `handler(request_objs, sock, addr)` is called for every datagram received
    and may send zero or more replies. Every decoded request array is kept in
    `self.requests` and every client source address in `self.sources`.
    """

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.sources = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            self.sources.append(addr)
            try:
                self.requests.append(json.loads(data.decode("utf-8")))
            except ValueError:
                self.requests.append(None)
            try:
                self.handler(self.requests[-1], self._sock, addr)
            except OSError:
                return

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            self._sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def _send_json(sock, addr, obj):
    sock.sendto(json.dumps(obj).encode("utf-8"), addr)


def _r0155_reply(req, temp=88, voltage=170, ack="Ok"):
    """Build a realistic R0155 response for an R0155 request object."""
    card_id = (req.get("param3", 0) << 8) | req.get("param2", 0)
    return {
        "deviceId": 0,
        "slotId": req.get("param0"),
        "portId": req.get("param1"),
        "recvCardId": card_id,
        "power0Status": 0,
        "power1Status": 0,
        "brightness": 127,
        "temp": temp,
        "voltage": voltage,
        "cmd": "R0155",
        "ack": ack,
    }


# ── Wire-format encoding ──────────────────────────────────────────────────


class TestCommandEncoding:
    """The PDF requires JSON arrays wrapping command objects."""

    def _capture_payload(self, fn):
        """Run a client method against a UDP echo server, return the bytes sent."""
        captured = {}

        def server(sock):
            data, addr = sock.recvfrom(65536)
            captured["payload"] = data
            # Echo back a minimal valid response so the client doesn't time
            # out. Real replies echo the `cmd` they answer, and the client
            # correlates on it, so the stub has to as well.
            try:
                cmd = json.loads(data.decode("utf-8"))[0]["cmd"]
            except Exception:
                cmd = "OK"
            sock.sendto(json.dumps([{"cmd": cmd, "ack": "Ok"}]).encode(), addr)

        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        t = threading.Thread(target=server, args=(srv,), daemon=True)
        t.start()
        try:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=port, timeout=1.0)
            fn(client)
            client.close()
        finally:
            srv.close()
        t.join(timeout=1.0)
        return captured.get("payload", b"")

    def test_r0100_format(self):
        payload = self._capture_payload(lambda c: c.get_device_details(0))
        obj = json.loads(payload)
        assert obj == [{"cmd": "R0100", "param0": 0}]

    def test_r0155_card_id_split(self):
        # card_id=267 = 0x010B → param2=0x0B, param3=0x01
        payload = self._capture_payload(lambda c: c.get_receiving_card(3, 11, 267))
        obj = json.loads(payload)
        assert obj == [{
            "cmd": "R0155",
            "param0": 3,
            "param1": 11,
            "param2": 0x0B,
            "param3": 0x01,
        }]

    def test_r0102_format(self):
        payload = self._capture_payload(lambda c: c.get_slot_info(7, connector_id=2))
        obj = json.loads(payload)
        assert obj == [{"cmd": "R0102", "param0": 0, "param1": 7, "param2": 2}]

    # ── Undocumented commands (mirrored from splicer Companion module) ──

    def test_r0118_init_status(self):
        payload = self._capture_payload(lambda c: c.get_device_init_status())
        assert json.loads(payload) == [{"cmd": "R0118", "param0": 0}]

    def test_r0400_screen_list(self):
        payload = self._capture_payload(lambda c: c.get_screen_list())
        assert json.loads(payload) == [{"cmd": "R0400", "param0": 0}]

    def test_r0300_output_list(self):
        payload = self._capture_payload(lambda c: c.get_output_list())
        assert json.loads(payload) == [{"cmd": "R0300", "param0": 0}]

    def test_r0301_output_details(self):
        payload = self._capture_payload(lambda c: c.get_output_details(5))
        assert json.loads(payload) == [{"cmd": "R0301", "param0": 0, "param1": 5}]

    def test_r0226_input_simplify(self):
        payload = self._capture_payload(lambda c: c.get_input_list_simplify())
        assert json.loads(payload) == [{"cmd": "R0226", "param0": 0}]

    def test_w0120_heartbeat(self):
        payload = self._capture_payload(lambda c: c.heartbeat())
        assert json.loads(payload) == [{"cmd": "W0120", "param0": 0}]


# ── Send/recv with mocked UDP server ──────────────────────────────────────


class TestSendRecv:
    """Round-trip through a real UDP loopback to validate the full path."""

    def _serve_once(self, response_obj):
        """Start a one-shot UDP server that returns response_obj as JSON."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]

        def serve():
            try:
                data, addr = srv.recvfrom(65536)
                payload = json.dumps(response_obj).encode("utf-8")
                srv.sendto(payload, addr)
            except Exception:
                pass
            finally:
                srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        return port, t

    def test_round_trip_returns_first_array_element(self):
        port, _ = self._serve_once([{"cmd": "R0100", "modelId": 29965}])
        client = hsj.HSeriesJSONClient("127.0.0.1", port=port, timeout=1.0)
        result = client.get_device_details()
        client.close()
        assert result == {"cmd": "R0100", "modelId": 29965}

    def test_round_trip_handles_dict_response(self):
        # Some devices may return a single object instead of an array
        port, _ = self._serve_once({"cmd": "R0100", "modelId": 1})
        client = hsj.HSeriesJSONClient("127.0.0.1", port=port, timeout=1.0)
        result = client.get_device_details()
        client.close()
        assert result == {"cmd": "R0100", "modelId": 1}

    def test_timeout_returns_none(self):
        # No server listening — client should return None instead of raising
        client = hsj.HSeriesJSONClient("127.0.0.1", port=1, timeout=0.2)
        assert client.get_device_details() is None
        client.close()

    def test_invalid_json_returns_none(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]

        def serve():
            try:
                _, addr = srv.recvfrom(65536)
                srv.sendto(b"not json at all", addr)
            finally:
                srv.close()

        threading.Thread(target=serve, daemon=True).start()
        client = hsj.HSeriesJSONClient("127.0.0.1", port=port, timeout=0.3)
        assert client.get_device_details() is None
        client.close()

    def test_late_reply_to_previous_command_is_rejected(self):
        """A shared socket must not hand a stale answer to the next caller."""
        def handler(req, sock, addr):
            cmd = req[0]["cmd"]
            if cmd == "R0100":
                return  # deliberately silent → client times out
            # Late R0100 answer arrives interleaved with the R0300 answer.
            _send_json(sock, addr, [{"cmd": "R0100", "modelId": 999}])
            _send_json(sock, addr, [{"cmd": "R0300", "outputs": []}])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.3)
            assert client.get_device_details() is None
            assert client.get_output_list() == {"cmd": "R0300", "outputs": []}
            client.close()

    def test_pending_datagram_is_drained_before_send(self):
        """Junk queued on the shared socket is discarded, not returned."""
        import time

        def handler(req, sock, addr):
            _send_json(sock, addr, [{"cmd": req[0]["cmd"], "ack": "Ok"}])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.5)
            client.get_device_details()          # binds the shared socket
            local_port = client._sock.getsockname()[1]
            stray = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            stray.sendto(b'[{"cmd":"R0300","stale":true}]',
                         ("127.0.0.1", local_port))
            stray.close()
            time.sleep(0.05)
            # Without the pre-send drain this would return the stale object.
            assert client.get_output_list() == {"cmd": "R0300", "ack": "Ok"}
            client.close()

    def test_response_for_another_command_is_not_returned(self):
        def handler(req, sock, addr):
            _send_json(sock, addr, [{"cmd": "R9999", "junk": True}])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.2)
            assert client.get_device_details() is None
            client.close()

    def test_payload_without_cmd_echo_is_still_accepted(self):
        def handler(req, sock, addr):
            _send_json(sock, addr, [{"rate": 100}])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.3)
            assert client.get_device_init_status() == {"rate": 100}
            client.close()


# ── Persistent socket reuse ───────────────────────────────────────────────


class TestSocketReuse:
    """One socket for the whole client instead of one per request."""

    def _echo_server(self):
        def handler(req, sock, addr):
            _send_json(sock, addr, [{"cmd": req[0]["cmd"], "ack": "Ok"}])
        return LoopServer(handler)

    def test_same_socket_across_calls(self):
        with self._echo_server() as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.5)
            client.get_device_details()
            first = client._sock
            client.get_output_list()
            client.get_input_list_simplify()
            assert client._sock is first
            # Same source port every time → the socket really was reused.
            assert len({addr[1] for addr in srv.sources}) == 1
            client.close()

    def test_socket_recreated_after_error(self):
        with self._echo_server() as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.5)
            assert client.get_device_details() is not None
            # Simulate the socket going bad underneath us.
            client._sock.close()
            assert client.get_device_details() is None
            assert client._sock is None
            # Next call must transparently rebuild it.
            assert client.get_device_details() is not None
            client.close()

    def test_close_is_idempotent(self):
        with self._echo_server() as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.5)
            client.get_device_details()
            client.close()
            client.close()
            assert client._sock is None

    def test_heartbeat_uses_its_own_socket(self):
        with self._echo_server() as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.5)
            client.get_device_details()
            client.heartbeat()
            assert client._hb_sock is not None
            assert client._hb_sock is not client._sock
            assert len({addr[1] for addr in srv.sources}) == 2
            client.close()

    def test_heartbeat_does_not_block_on_bulk_lock(self):
        """W0120 must not queue behind a batch of card reads."""
        with self._echo_server() as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.5)
            with client._lock:          # pretend a card batch is in flight
                assert client.heartbeat() == {"cmd": "W0120", "ack": "Ok"}
            client.close()


# ── Batched commands (many per datagram) ──────────────────────────────────


class TestBatching:
    """N commands in one datagram, correlated by identity not position."""

    ADDRS = [(20, 0, 0), (20, 0, 1), (20, 0, 2), (20, 1, 0)]

    def test_single_datagram_carries_all_commands(self):
        def handler(req, sock, addr):
            _send_json(sock, addr, [_r0155_reply(c) for c in req])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           bulk_timeout=0.3, batch_size=8)
            results = client.get_receiving_cards_batch(self.ADDRS)
            client.close()

        assert len(srv.requests) == 1                 # one round trip, not 4
        assert len(srv.requests[0]) == 4
        assert [r["recvCardId"] for r in results] == [0, 1, 2, 0]
        assert [r["portId"] for r in results] == [0, 0, 0, 1]

    def test_out_of_order_replies_still_align(self):
        def handler(req, sock, addr):
            _send_json(sock, addr, [_r0155_reply(c) for c in reversed(req)])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           bulk_timeout=0.3)
            results = client.get_receiving_cards_batch(self.ADDRS)
            client.close()

        assert [(r["slotId"], r["portId"], r["recvCardId"]) for r in results] \
            == self.ADDRS

    def test_replies_split_across_datagrams(self):
        """Device may answer one datagram per command instead of one array."""
        def handler(req, sock, addr):
            for c in req:
                _send_json(sock, addr, [_r0155_reply(c)])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           bulk_timeout=0.3)
            results = client.get_receiving_cards_batch(self.ADDRS)
            client.close()

        assert all(r is not None for r in results)
        assert [r["recvCardId"] for r in results] == [0, 1, 2, 0]

    def test_dropped_cards_become_none_in_place(self):
        """R0155 silently answers nothing for unreachable cards."""
        def handler(req, sock, addr):
            keep = [c for c in req if c["param2"] != 1]   # card_id 1 is dead
            _send_json(sock, addr, [_r0155_reply(c) for c in keep])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           bulk_timeout=0.2)
            results = client.get_receiving_cards_batch(self.ADDRS)
            client.close()

        assert results[1] is None
        assert [r for r in results if r is not None].__len__() == 3
        assert results[0]["recvCardId"] == 0 and results[2]["recvCardId"] == 2

    def test_partial_batch_returns_after_quiet_period(self):
        """One dead card must not hold its batch until the full timeout."""
        import time

        def handler(req, sock, addr):
            keep = [c for c in req if c["param2"] != 1]
            _send_json(sock, addr, [_r0155_reply(c) for c in keep])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           bulk_timeout=2.0)
            start = time.monotonic()
            results = client.get_receiving_cards_batch(self.ADDRS,
                                                       settle=0.05)
            elapsed = time.monotonic() - start
            client.close()

        assert results[1] is None
        assert elapsed < 0.5          # not the 2.0 s hard timeout

    def test_silent_device_still_waits_the_full_timeout(self):
        """The quiet period only starts once something has come back."""
        import time
        with LoopServer(lambda req, sock, addr: None) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           bulk_timeout=0.4)
            start = time.monotonic()
            results = client.get_receiving_cards_batch(self.ADDRS)
            elapsed = time.monotonic() - start
            client.close()

        assert results == [None] * 4
        assert elapsed >= 0.35

    def test_chunking_respects_batch_size(self):
        def handler(req, sock, addr):
            _send_json(sock, addr, [_r0155_reply(c) for c in req])

        addrs = [(20, 0, i) for i in range(10)]
        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           bulk_timeout=0.3, batch_size=4)
            results = client.get_receiving_cards_batch(addrs)
            client.close()

        assert [len(r) for r in srv.requests] == [4, 4, 2]
        assert [r["recvCardId"] for r in results] == list(range(10))

    def test_default_batch_size_is_conservative(self):
        # Sized to keep the *response* datagram under a 1500 B MTU.
        assert 1 < hsj.DEFAULT_BATCH_SIZE <= 16

    def test_duplicate_keys_fall_back_to_single_sends(self):
        """Ambiguous batches are refused, never guessed by position."""
        def handler(req, sock, addr):
            _send_json(sock, addr, [{"cmd": "R0300", "n": len(req)}])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.3)
            cmds = [{"cmd": "R0300", "param0": 0}, {"cmd": "R0300", "param0": 0}]
            results = client.send_recv_batch(cmds, timeout=0.3)
            client.close()

        assert len(srv.requests) == 2                  # one command each
        assert all(len(r) == 1 for r in srv.requests)
        assert results == [{"cmd": "R0300", "n": 1}] * 2

    def test_distinct_commands_share_one_datagram(self):
        def handler(req, sock, addr):
            _send_json(sock, addr,
                       [{"cmd": c["cmd"], "ack": "Ok"} for c in req])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.3)
            cmds = [{"cmd": "R0400", "param0": 0}, {"cmd": "R0300", "param0": 0}]
            results = client.send_recv_batch(cmds, timeout=0.3)
            client.close()

        assert len(srv.requests) == 1
        assert results == [{"cmd": "R0400", "ack": "Ok"},
                           {"cmd": "R0300", "ack": "Ok"}]

    def test_uncorrelatable_replies_degrade_to_single_sends(self):
        """If the device doesn't echo the address, stop batching entirely."""
        def handler(req, sock, addr):
            # No slotId/portId/recvCardId — nothing to correlate on.
            _send_json(sock, addr, [{"cmd": "R0155", "ack": "Ok"}])

        with LoopServer(handler) as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           bulk_timeout=0.2, batch_size=4)
            results = client.get_receiving_cards_batch(self.ADDRS)
            assert client._batch_unsupported is True
            # Subsequent calls skip the batch attempt altogether.
            before = len(srv.requests)
            client.get_receiving_cards_batch(self.ADDRS)
            client.close()

        assert results == [{"cmd": "R0155", "ack": "Ok"}] * 4
        assert len(srv.requests) - before == 4       # 4 single-command sends

    def test_empty_batch_returns_empty_list(self):
        client = hsj.HSeriesJSONClient("127.0.0.1", port=1, timeout=0.1)
        assert client.send_recv_batch([]) == []
        assert client.get_receiving_cards_batch([]) == []
        client.close()

    def test_no_server_returns_all_none(self):
        client = hsj.HSeriesJSONClient("127.0.0.1", port=1, bulk_timeout=0.1)
        results = client.get_receiving_cards_batch(self.ADDRS)
        client.close()
        assert results == [None] * 4


# ── Per-call-class timeouts ───────────────────────────────────────────────


class TestTimeoutClasses:
    def test_bulk_timeout_is_short_and_topology_is_long(self):
        assert hsj.BULK_TIMEOUT <= 0.5
        assert hsj.TOPOLOGY_TIMEOUT >= 2.0
        assert hsj.DEFAULT_TIMEOUT == hsj.TOPOLOGY_TIMEOUT

    def test_per_card_read_uses_bulk_timeout(self):
        """A silent device must cost bulk_timeout, not the topology timeout."""
        import time
        client = hsj.HSeriesJSONClient("127.0.0.1", port=1, timeout=3.0,
                                       bulk_timeout=0.15)
        start = time.monotonic()
        assert client.get_receiving_card(20, 0, 0) is None
        elapsed = time.monotonic() - start
        client.close()
        assert elapsed < 1.0

    def test_topology_call_uses_long_timeout(self):
        import time
        client = hsj.HSeriesJSONClient("127.0.0.1", port=1, timeout=0.4,
                                       bulk_timeout=0.05)
        start = time.monotonic()
        assert client.get_screen_output_info(0) is None
        elapsed = time.monotonic() - start
        client.close()
        assert elapsed >= 0.3


# ── Response parsing helpers ──────────────────────────────────────────────


class TestParseDeviceDetails:
    def test_minimal(self):
        r = hsj.parse_device_details({"name": "", "modelId": 29965, "slotList": []})
        assert r["model_id"] == 29965
        assert r["slot_count"] == 0
        assert r["slots"] == []

    def test_full(self):
        r0100 = {
            "name": "",
            "modelId": 29965,
            "protoVersion": "1.0.0.0",
            "memory": 9643,
            "status": 1,
            "slotList": [
                {
                    "slotId": 0,
                    "modelId": 31503,
                    "cardType": 1,
                    "sn": "003179010000003b",
                    "resolution": {"width": 3840, "height": 2160},
                    "interfaces": [
                        {"interfaceId": 0, "interfaceType": 6, "iSignal": 1, "isUsed": 1},
                        {"interfaceId": 1, "interfaceType": 6, "iSignal": 2, "isUsed": 1},
                    ],
                },
            ],
        }
        r = hsj.parse_device_details(r0100)
        assert r["model_id"] == 29965
        assert r["proto_version"] == "1.0.0.0"
        assert r["memory"] == 9643
        assert r["slot_count"] == 1
        assert len(r["slots"]) == 1
        assert r["slots"][0]["sn"] == "003179010000003b"
        assert len(r["slots"][0]["interfaces"]) == 2
        assert r["slots"][0]["interfaces"][0]["i_signal"] == 1

    def test_none_input(self):
        assert hsj.parse_device_details(None) is None

    def test_non_dict_input(self):
        assert hsj.parse_device_details([]) is None


class TestParseReceivingCard:
    """Field names come from a real capture, not from guesses."""

    CAPTURED = {
        "deviceId": 0, "slotId": 20, "portId": 0, "recvCardId": 0,
        "power0Status": 0, "power1Status": 0, "brightness": 127,
        "temp": 88, "voltage": 170, "cmd": "R0155", "ack": "Ok",
    }

    def test_captured_response(self):
        r = hsj.parse_receiving_card(self.CAPTURED)
        assert r["online"] is True
        assert r["slot"] == 20 and r["port"] == 0 and r["card_id"] == 0
        assert r["temperature_c"] == 44.0        # raw 88 / 2
        assert r["temp_c"] == 44.0
        assert r["temperature_raw"] == 88
        assert r["voltage_v"] == 5.1             # raw 170 * 0.03
        assert r["voltage_raw"] == 170
        assert r["brightness"] == 127
        assert r["primary_power_ok"] is True
        assert r["backup_power_ok"] is True

    def test_power_fault_flags(self):
        r = hsj.parse_receiving_card({**self.CAPTURED,
                                      "power0Status": 0, "power1Status": 2})
        assert r["primary_power_ok"] is True
        assert r["backup_power_ok"] is False

    def test_missing_power_fields_are_unknown(self):
        r = hsj.parse_receiving_card({"cmd": "R0155", "ack": "Ok"})
        assert r["primary_power_ok"] is None
        assert r["backup_power_ok"] is None
        assert r["temperature_c"] is None
        assert r["voltage_v"] is None

    def test_failed_ack_is_offline(self):
        r = hsj.parse_receiving_card({**self.CAPTURED, "ack": "Fail"})
        assert r["online"] is False

    def test_keeps_raw_for_debugging(self):
        r = hsj.parse_receiving_card(self.CAPTURED)
        assert r["raw"] == self.CAPTURED

    def test_none_input(self):
        assert hsj.parse_receiving_card(None) is None

    def test_non_dict_input(self):
        assert hsj.parse_receiving_card([]) is None

    def test_batch_results_feed_straight_into_the_parser(self):
        req = {"cmd": "R0155", "param0": 20, "param1": 3, "param2": 5, "param3": 0}
        parsed = hsj.parse_receiving_card(_r0155_reply(req, temp=90, voltage=171))
        assert parsed["temperature_c"] == 45.0
        assert parsed["voltage_v"] == 5.13
        assert parsed["card_id"] == 5 and parsed["port"] == 3


class TestByteDecoders:
    def test_temp_byte(self):
        # H PDF §5.4.2: temp byte / 2 = °C.
        assert hsj.decode_temp_byte(88) == 44.0
        assert hsj.decode_temp_byte(76) == 38.0
        assert hsj.decode_temp_byte(None) is None
        assert hsj.decode_temp_byte("nope") is None

    def test_voltage_byte_uses_vendor_scale(self):
        # Vendor formula raw * 0.03 (same as binary register 0x0000000A[3]).
        assert hsj.decode_voltage_byte(170) == 5.1
        assert hsj.decode_voltage_byte(165) == 4.95
        assert hsj.decode_voltage_byte(173) == 5.19
        assert hsj.decode_voltage_byte(None) is None
        assert hsj.decode_voltage_byte("nope") is None

    def test_voltage_is_not_the_masked_variant(self):
        # (raw & 0x7F) / 10 would report 4.2 V for a healthy 5 V rail.
        assert hsj.decode_voltage_byte(170) != 4.2


# ── Correlation keys ──────────────────────────────────────────────────────


class TestCorrelationKeys:
    def test_request_and_response_keys_match(self):
        req = {"cmd": "R0155", "param0": 20, "param1": 1, "param2": 0x0B,
               "param3": 0x01}
        resp = _r0155_reply(req)
        assert hsj._r0155_request_key(req) == ("R0155", 20, 1, 267)
        assert hsj._r0155_response_key(resp) == ("R0155", 20, 1, 267)

    def test_response_key_none_without_address(self):
        assert hsj._r0155_response_key({"cmd": "R0155", "ack": "Ok"}) is None
        assert hsj._r0155_response_key({"cmd": "R0100"}) is None
        assert hsj._r0155_response_key("nope") is None

    def test_request_key_none_without_card_bytes(self):
        assert hsj._r0155_request_key({"cmd": "R0155", "param0": 1}) is None

    def test_cmd_key(self):
        assert hsj._cmd_key({"cmd": "R0100"}) == "R0100"
        assert hsj._cmd_key({}) is None
        assert hsj._cmd_key(None) is None
