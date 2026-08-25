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


# ── Read-only guarantee ───────────────────────────────────────────────────


class TestReadOnly:
    """The client must never send a write-class command.

    It used to send `W0120` every 3s as a keepalive, copied from the vendor's
    Companion *control* module. While this monitor ran, the operator lost
    control of the wall from Companion; killing the app restored it. The
    heartbeat is gone and must stay gone — these tests are the tripwire.
    """

    def test_no_heartbeat_method_exists(self):
        client = hsj.HSeriesJSONClient("192.0.2.1")
        assert not hasattr(client, "heartbeat")
        # …and no second socket for it to have run on, either.
        assert not hasattr(client, "_hb_sock")

    def test_source_sends_no_w_class_commands(self):
        source = open(hsj.__file__, encoding="utf-8").read()
        for forbidden in ('"cmd": "W', "'cmd': 'W", '"cmd":"W'):
            assert forbidden not in source

    def test_every_public_command_is_a_read(self):
        """Every command method builds an R.... query, nothing else."""
        client = hsj.HSeriesJSONClient("192.0.2.1")
        sent = []
        client.send_recv = lambda obj, timeout=None: sent.append(obj)
        client.get_device_details()
        client.get_slot_info(1)
        client.get_connector_info(1, 0)
        client.get_receiving_card(20, 0, 0)
        client.get_device_init_status()
        client.get_screen_list()
        client.get_screen_details(1)
        client.get_screen_output_info(1)
        client.get_output_list()
        client.get_output_details(1)
        client.get_input_list_simplify()
        assert sent, "expected commands to have been built"
        assert all(obj["cmd"].startswith("R") for obj in sent)


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

    def test_only_one_socket_is_ever_opened(self):
        """There is a single command socket — the heartbeat's is gone."""
        with self._echo_server() as srv:
            client = hsj.HSeriesJSONClient("127.0.0.1", port=srv.port,
                                           timeout=0.5)
            client.get_device_details()
            client.get_screen_list()
            client.get_receiving_card(20, 0, 0)
            assert len({addr[1] for addr in srv.sources}) == 1
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
    """Byte schema: field names come from a real capture, not from guesses."""

    CAPTURED = {
        "deviceId": 0, "slotId": 20, "portId": 0, "recvCardId": 0,
        "power0Status": 0, "power1Status": 0, "brightness": 127,
        "temp": 88, "voltage": 170, "cmd": "R0155", "ack": "Ok",
    }

    def test_still_decodes_as_the_byte_schema(self):
        """The old capture is real hardware too — it must keep working."""
        r = hsj.parse_receiving_card(self.CAPTURED)
        assert r["schema"] == hsj.SCHEMA_BYTE
        assert r["temperature_c"] == 44.0     # 88 / 2, NOT 88 / 100
        # 170 & 0x7F = 42, units of 0.1 V → 4.2 V. NOT 170 / 100.
        assert r["voltage_v"] == 4.2
        # It has no work-status field, so an answering card is reporting.
        assert r["reporting"] is True
        assert r["work_status"] is None
        # And none of the centi-only extras.
        assert r["temp_limit_c"] is None
        assert r["temp_status_ok"] is None
        assert r["mcu_version"] is None

    def test_captured_response(self):
        r = hsj.parse_receiving_card(self.CAPTURED)
        assert r["online"] is True
        assert r["slot"] == 20 and r["port"] == 0 and r["card_id"] == 0
        assert r["temperature_c"] == 44.0        # raw 88 / 2
        assert r["temp_c"] == 44.0
        assert r["temperature_raw"] == 88
        assert r["voltage_v"] == 4.2             # raw 170 & 0x7F = 42 → 4.2 V
        assert r["voltage_raw"] == 170
        assert r["brightness"] == 127
        assert r["primary_power_ok"] is True
        assert r["backup_power_ok"] is True

    def test_power_flags_are_true_or_unknown_never_false(self):
        """`0 = healthy` held up; `non-zero = failed` did not, so non-zero is
        reported as unknown rather than as a fault. See _power_status."""
        r = hsj.parse_receiving_card({**self.CAPTURED,
                                      "power0Status": 0, "power1Status": 2})
        assert r["primary_power_ok"] is True
        assert r["backup_power_ok"] is None

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
        assert parsed["voltage_v"] == 4.3        # 171 & 0x7F = 43 → 4.3 V
        assert parsed["card_id"] == 5 and parsed["port"] == 3


class TestParseReceivingCardCentiSchema:
    """The centi schema, captured live off the operator's H-series.

    CAPTURED is the exact response object taken off 192.168.0.10 — copied
    verbatim, not reconstructed. ABSENT is the shape every unreachable card on
    that wall returned: it ANSWERS, with zeros standing in for readings.
    """

    CAPTURED = {
        "deviceId": 0, "slotId": 20, "portId": 0, "recvCardId": 10,
        "mcuVersion": "V4.5.1.81", "fpgaVersion": "V4.5.1.81",
        "workStatus": 0, "tempStatus": 0, "temp": 3700, "tempMax": 70,
        "voltStatus": 0, "volt": 440, "power0Status": 0, "power1Status": 0,
        "brightness": 25, "cmd": "R0155", "ack": "Ok",
    }

    ABSENT = {
        "deviceId": 0, "slotId": 20, "portId": 0, "recvCardId": 11,
        "mcuVersion": "V4.5.1.81", "fpgaVersion": "V4.5.1.81",
        "workStatus": 1, "tempStatus": 2, "temp": 0, "tempMax": 70,
        "voltStatus": 2, "volt": 0, "power0Status": 0, "power1Status": 0,
        "brightness": 0, "cmd": "R0155", "ack": "Ok",
    }

    def test_reporting_card_decodes_at_the_centi_scale(self):
        r = hsj.parse_receiving_card(self.CAPTURED)
        assert r["schema"] == hsj.SCHEMA_CENTI
        assert r["online"] is True
        assert r["reporting"] is True
        assert r["work_status"] == 0
        assert r["slot"] == 20 and r["port"] == 0 and r["card_id"] == 10
        assert r["temperature_c"] == 37.0     # 3700 / 100
        assert r["temp_c"] == 37.0
        assert r["temperature_raw"] == 3700
        assert r["voltage_v"] == 4.4          # 440 / 100
        assert r["voltage_raw"] == 440
        assert r["brightness"] == 25
        assert r["primary_power_ok"] is True
        assert r["backup_power_ok"] is True

    def test_temperature_is_not_the_byte_scaling(self):
        """The bug: 3700 / 2 = 1850 °C, which is what the operator saw."""
        r = hsj.parse_receiving_card(self.CAPTURED)
        assert r["temperature_c"] != 1850.0
        # And the byte-schema voltage decoder would make 440 into
        # (440 & 0x7F) * 0.1 = 5.6 V, which is not a voltage this card has.
        assert r["voltage_v"] != 5.6

    def test_surfaces_device_reported_extras(self):
        r = hsj.parse_receiving_card(self.CAPTURED)
        assert r["temp_limit_c"] == 70        # controller's own limit, whole °C
        assert r["temp_status"] == 0 and r["temp_status_ok"] is True
        assert r["volt_status"] == 0 and r["volt_status_ok"] is True
        assert r["mcu_version"] == "V4.5.1.81"
        assert r["fpga_version"] == "V4.5.1.81"

    def test_non_reporting_card_is_offline(self):
        r = hsj.parse_receiving_card(self.ABSENT)
        assert r["online"] is False
        assert r["reporting"] is False
        assert r["work_status"] == 1
        # It still answered, so the address and the device's verdict survive.
        assert r["card_id"] == 11
        assert r["volt_status"] == 2 and r["volt_status_ok"] is False
        assert r["temp_status_ok"] is False

    def test_non_reporting_card_has_no_readings_at_all(self):
        """Zeros are placeholders — absent, never 0.0."""
        r = hsj.parse_receiving_card(self.ABSENT)
        for key in ("temp_c", "temperature_c", "temperature_raw",
                    "voltage_v", "voltage_raw", "brightness"):
            assert r[key] is None, f"{key} must be absent, not a placeholder"

    def test_non_reporting_card_claims_nothing_about_its_supplies(self):
        """power0/1Status 0 on an absent card is not 'both supplies healthy'."""
        r = hsj.parse_receiving_card(self.ABSENT)
        assert r["primary_power_ok"] is None
        assert r["backup_power_ok"] is None

    def test_any_nonzero_work_status_is_not_reporting(self):
        """Only 0 and 1 were observed; nothing else may count as reporting."""
        for status in (1, 2, 3, 255):
            r = hsj.parse_receiving_card({**self.ABSENT, "workStatus": status})
            assert r["online"] is False
            assert r["voltage_v"] is None

    def test_reporting_card_with_both_power_flags_set(self):
        """Fifteen plainly working cards reported power0/1Status 1,1 while
        also reporting 41-42 C and 4.0-4.1 V. A card cannot measure and send
        its own temperature through a failed primary supply, so "both supplies
        failed" is not a tenable reading — it raised a warning every polling
        cycle on a lit, healthy wall. Unknown, not failed."""
        r = hsj.parse_receiving_card({**self.CAPTURED,
                                      "power0Status": 1, "power1Status": 1})
        assert r["online"] is True
        assert r["primary_power_ok"] is None
        assert r["backup_power_ok"] is None
        # Its readings are still real — only the flags are in question.
        assert r["temperature_c"] == 37.0
        # The raw values are kept so the meaning can be settled later.
        assert r["power0_status_raw"] == 1
        assert r["power1_status_raw"] == 1

    def test_failed_ack_yields_no_readings(self):
        r = hsj.parse_receiving_card({**self.CAPTURED, "ack": "Fail"})
        assert r["online"] is False
        assert r["temperature_c"] is None
        assert r["voltage_v"] is None
        assert r["primary_power_ok"] is None

    def test_keeps_raw_for_debugging(self):
        r = hsj.parse_receiving_card(self.ABSENT)
        assert r["raw"] == self.ABSENT


class TestStatusOkCoercion:
    """`tempStatus` / `voltStatus` arriving as strings must not read as faults.

    The captured firmware sends these as JSON numbers, but this device family
    has been seen quoting numbers, and `"0" == 0` is False in Python. A card
    whose only peculiarity is a quoted status would have been reported as a
    temperature or voltage FAULT — on a healthy panel, every polling cycle,
    with the only available fix being somebody walking to the wall during a
    show. `snmp_client._status_ok` already coerced; this one did not.
    """

    def test_quoted_zero_is_ok_not_a_fault(self):
        assert hsj._status_ok("0") is True

    def test_quoted_nonzero_is_still_a_fault(self):
        """Coercing must not swallow the fault it is there to report."""
        assert hsj._status_ok("2") is False

    def test_numeric_values_are_unchanged(self):
        assert hsj._status_ok(0) is True
        assert hsj._status_ok(2) is False

    def test_missing_is_unknown(self):
        assert hsj._status_ok(None) is None

    def test_a_value_that_is_not_a_number_is_unknown_not_a_fault(self):
        """An unparsable status says nothing about the card. Reporting it as
        a fault would be inventing one out of a field we cannot read."""
        for value in ("", "OK", "n/a", [], {}):
            assert hsj._status_ok(value) is None, repr(value)

    def test_it_agrees_with_its_twin_in_snmp_client(self):
        """The two live in different modules and are meant to be the same
        function; drift between them is how one of them ended up uncoerced."""
        import snmp_client as sc
        for value in (0, 2, "0", "2", None, "OK", ""):
            assert hsj._status_ok(value) == sc._status_ok(value), repr(value)

    def test_a_quoted_status_on_a_whole_card_reads_as_healthy(self):
        """End to end: the card is otherwise the live-wall capture, so nothing
        but the quoting differs, and nothing but the quoting may change."""
        card = hsj.parse_receiving_card(
            {**TestParseReceivingCardCentiSchema.CAPTURED,
             "tempStatus": "0", "voltStatus": "0"})
        assert card["temp_status_ok"] is True
        assert card["volt_status_ok"] is True
        assert card["temperature_c"] == 37.0


class TestSchemaDetection:
    """Detection is by key presence — never by value range."""

    def test_volt_key_selects_the_centi_schema(self):
        assert hsj.detect_receiving_card_schema(
            {"cmd": "R0155", "temp": 3700, "volt": 440}) == hsj.SCHEMA_CENTI

    def test_voltage_key_selects_the_byte_schema(self):
        assert hsj.detect_receiving_card_schema(
            {"cmd": "R0155", "temp": 88, "voltage": 170}) == hsj.SCHEMA_BYTE

    def test_status_block_is_a_backstop_without_a_voltage_key(self):
        assert hsj.detect_receiving_card_schema(
            {"cmd": "R0155", "temp": 3700, "workStatus": 0,
             "tempMax": 70}) == hsj.SCHEMA_CENTI

    def test_neither_key_falls_back_to_the_byte_schema(self):
        assert hsj.detect_receiving_card_schema(
            {"cmd": "R0155", "ack": "Ok"}) == hsj.SCHEMA_BYTE
        assert hsj.detect_receiving_card_schema(None) == hsj.SCHEMA_BYTE

    def test_detection_ignores_the_magnitude_of_the_values(self):
        """A byte-schema reply is decoded as bytes however small volt-like
        numbers look, and vice versa — the key name decides, nothing else."""
        low = hsj.parse_receiving_card(
            {"cmd": "R0155", "ack": "Ok", "temp": 88, "voltage": 170})
        assert low["temperature_c"] == 44.0
        # Same numbers under the centi key name decode the other way.
        high = hsj.parse_receiving_card(
            {"cmd": "R0155", "ack": "Ok", "temp": 88, "volt": 170})
        assert high["temperature_c"] == 0.88
        assert high["voltage_v"] == 1.7


class TestCentiDecoders:
    def test_temp_centi(self):
        # Live wall: temp 3600-3800 on reporting cards → 36.0-38.0 °C.
        assert hsj.decode_temp_centi(3700) == 37.0
        assert hsj.decode_temp_centi(3600) == 36.0
        assert hsj.decode_temp_centi(3800) == 38.0
        assert hsj.decode_temp_centi(None) is None
        assert hsj.decode_temp_centi("nope") is None

    def test_volt_centi(self):
        # Live wall: volt 410-440 on reporting cards → 4.10-4.40 V.
        assert hsj.decode_volt_centi(440) == 4.4
        assert hsj.decode_volt_centi(410) == 4.1
        assert hsj.decode_volt_centi(430) == 4.3
        assert hsj.decode_volt_centi(None) is None
        assert hsj.decode_volt_centi("nope") is None


class TestByteDecoders:
    def test_temp_byte(self):
        # H PDF §5.4.2: temp byte / 2 = °C.
        assert hsj.decode_temp_byte(88) == 44.0
        assert hsj.decode_temp_byte(76) == 38.0
        assert hsj.decode_temp_byte(None) is None
        assert hsj.decode_temp_byte("nope") is None

    def test_voltage_byte_uses_vendor_scale(self):
        """(raw & 0x7F) * 0.1, straight out of the vendor document.

        NovaStar's H Series Video Wall Splicers Control Protocol, §4.3.4 and
        §5.4.2 (identical in V1.0.18 and V1.0.20): "The lower 7 bits represent
        the voltage value, in units of 0.1V. For instance, a value of 172
        indicates a voltage of 4.4V."

        These assertions used to read 5.1 / 4.95 / 5.19, i.e. `raw * 0.03`.
        That formula was this project's own invention. It was adopted because
        the documented one put every card under the app's 4.7 V low-voltage
        alarm, and the conclusion drawn — "the formula must be wrong" — was
        backwards. The 4.7 V threshold was wrong; receiving cards on this
        hardware sit around 4.2 V and the alarm floor is now 3.8 V. The
        documented form is also the one that agrees with the centi-schema
        firmware, which reports 4.30 V for cards on the same chain that
        byte-schema-decode to 4.2 V masked and 5.10 V unmasked.
        """
        assert hsj.decode_voltage_byte(170) == 4.2   # 170 & 0x7F = 42
        assert hsj.decode_voltage_byte(165) == 3.7   # 165 & 0x7F = 37
        assert hsj.decode_voltage_byte(173) == 4.5   # 173 & 0x7F = 45
        assert hsj.decode_voltage_byte(None) is None
        assert hsj.decode_voltage_byte("nope") is None

    def test_voltage_uses_the_masked_variant(self):
        """Bit 7 is not part of the number, so it must be masked off.

        The vendor says "the lower 7 bits", and nothing in the document
        assigns a meaning to bit 7 of this byte. Unmasked, every raw value
        above 127 reads 12.8 V too high — which is exactly how a wall of
        4.2 V cards came to look like a 5 V rail.
        """
        assert hsj.decode_voltage_byte(170) != 5.1   # the old unmasked answer
        assert hsj.decode_voltage_byte(170) == hsj.decode_voltage_byte(42)

    def test_vendor_worked_example(self):
        """"a value of 172 indicates a voltage of 4.4V" — §4.3.4 / §5.4.2.

        Pinned on its own so the one number NovaStar published cannot drift
        without someone deleting an explicit citation. 172 & 0x7F = 44, and 44
        unmasked is the same 4.4 V, which is the mask test in miniature.
        """
        assert hsj.decode_voltage_byte(172) == 4.4
        assert hsj.decode_voltage_byte(44) == 4.4

    def test_temperature_vendor_worked_example(self):
        """"a value of 104 represents a temperature of 52°C" — §4.3.4.

        The temperature scaling was never in doubt, but it comes from the same
        two sections as the voltage one and is cheap to pin alongside it.
        """
        assert hsj.decode_temp_byte(104) == 52.0


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


class TestOutputLinkMedium:
    """R0100 says whether a sender card is running fibre or copper.

    All fixtures below are the real shapes off the operator's H15, where the
    wiring was known independently: card 1 (slot 20) and its backup (28) on
    OPT, card 2 (slot 22) and its backup (30) straight out of the Ethernet
    ports. Both structures agreed with the wiring on all four cards.
    """

    OPT_CARD = {
        'lightstatus': {'link0': 2, 'link1': 2},
        'linkstatus': {f'link{i}': 0 for i in range(16)},
    }
    ETH_CARD = {
        'lightstatus': {'link0': 0, 'link1': 0},
        'linkstatus': dict({f'link{i}': 0 for i in range(16)},
                           link0=1, link1=1),
    }

    def test_a_fibre_card_reads_as_opt(self):
        links = hsj.parse_output_links(self.OPT_CARD)
        assert links['medium'] == 'opt'
        assert links['opt_up'] == [0, 1]
        assert links['ethernet_up'] == []

    def test_a_copper_card_reads_as_ethernet(self):
        links = hsj.parse_output_links(self.ETH_CARD)
        assert links['medium'] == 'ethernet'
        assert links['ethernet_up'] == [0, 1]
        assert links['opt_up'] == []

    def test_an_idle_card_claims_neither(self):
        links = hsj.parse_output_links({
            'lightstatus': {'link0': 0, 'link1': 0},
            'linkstatus': {f'link{i}': 0 for i in range(16)},
        })
        assert links['medium'] is None

    def test_both_media_in_use_is_reported_as_mixed(self):
        """Legal hardware, so it gets its own answer rather than being forced
        into one bucket."""
        links = hsj.parse_output_links({
            'lightstatus': {'link0': 2, 'link1': 0},
            'linkstatus': dict({f'link{i}': 0 for i in range(16)}, link3=1),
        })
        assert links['medium'] == 'mixed'
        assert links['opt_up'] == [0] and links['ethernet_up'] == [3]

    def test_opt_has_two_ports_and_ethernet_sixteen(self):
        links = hsj.parse_output_links(self.ETH_CARD)
        assert len(links['opt']) == 2
        assert len(links['ethernet']) == 16

    def test_sender_interface_status_is_the_ethernet_fallback(self):
        """Firmware that omits `linkstatus` still reports the same 16 ports in
        list form."""
        links = hsj.parse_output_links({
            'lightstatus': {'link0': 0, 'link1': 0},
            'senderInterfaceStatus': [
                {'id': i, 'type': 0, 'status': 1 if i < 2 else 0}
                for i in range(16)],
        })
        assert links['medium'] == 'ethernet'
        assert links['ethernet_up'] == [0, 1]

    def test_a_missing_block_is_not_an_error(self):
        links = hsj.parse_output_links({})
        assert links['medium'] is None
        assert links['opt'] == [] and links['ethernet'] == []

    def test_state_is_passed_through_not_graded(self):
        """1 and 2 were both observed and nothing distinguishes them
        reliably, so the raw value is reported and only up/down is claimed."""
        links = hsj.parse_output_links({
            'lightstatus': {'link0': 2, 'link1': 0},
            'linkstatus': {f'link{i}': 0 for i in range(16)},
        })
        assert links['opt'][0]['state'] == 2
        assert links['opt'][0]['up'] is True
        assert links['opt'][1]['up'] is False

    def test_device_details_carries_the_links_per_slot(self):
        r0100 = {'slotList': [
            dict(self.OPT_CARD, slotId=20, cardType=2),
            dict(self.ETH_CARD, slotId=22, cardType=2),
        ]}
        parsed = hsj.parse_device_details(r0100)
        by_slot = {s['slot_id']: s for s in parsed['slots']}
        assert by_slot[20]['output_links']['medium'] == 'opt'
        assert by_slot[22]['output_links']['medium'] == 'ethernet'
