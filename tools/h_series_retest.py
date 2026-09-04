#!/usr/bin/env python3
"""Retest the open NovaStar questions against a live H-series controller.

    python3 tools/h_series_retest.py 192.168.0.10

Run this with the wall CONNECTED and panels powered. Every test prints what it
found and what that means, so the output can be pasted straight into a reply to
NovaStar.

WHY THIS FILE EXISTS
--------------------
These checks were originally written in a scratch directory and lost when it
was cleaned up. They are in the repo now because the wall is only occasionally
available and re-deriving them costs more than keeping them.

SAFETY
------
Everything here is a read EXCEPT the SNMP SetRequests in tests 1, 2 and 5.
Those are unavoidable: NovaStar addresses per-card SNMP data by SETting a
selector first (.30.4), and the trap switch is a read/write OID. That SET code
lives HERE and deliberately NOT in src/snmp_client.py, which has no write path
at all and has tests asserting so against its own source. SNMP SETs were
verified earlier to coexist with Companion — different protocol, different
subsystem — but this is still the one script in the project that writes.

Test 5 changes the trap switch and puts it back.
"""
import json
import socket
import struct
import sys
import time
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import snmp_client as sc                       # noqa: E402
import enumerate_wall as ew                    # noqa: E402

OID_BASE = "1.3.6.1.4.1.319.10"

# Chassis slot numbers of the OUTPUT cards, from R0100's slotList (cardType 2).
#
# NovaStar R&D, by email: slot IDs are PHYSICAL chassis positions, input slots
# numbered first and output slots behind them. Their H5 example has its first
# output slot at 10, then 11/12/13. So `.30.1` reporting 8 does NOT mean the
# selector takes 0..7 — and our earlier attempts with 0-3 and with the doc's
# example value of 4 were addressing input-side or non-existent slots, which is
# very likely why `.30.6` answered ERROR: BizIdError every time.
#
# On the operator's H15, R0100 puts output cards at 20, 22, 24, 26, 28, 30, 32,
# 34, with cards actually fitted in 20, 22, 28 and 30.
H15_OUTPUT_SLOTS = (20, 22, 28, 30)
LEGACY_GUESSES = (0, 1, 4)          # what we tried before, kept to show contrast


# ── minimal SNMP SET (test tooling only — never in src/) ───────────────────

def _tlv(tag, payload):
    if len(payload) < 128:
        return bytes([tag, len(payload)]) + payload
    enc = len(payload).to_bytes((len(payload).bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(enc)]) + enc + payload


def _oid_bytes(oid):
    parts = [int(x) for x in oid.split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 128:
            out.append(p)
            continue
        chunk = []
        while p:
            chunk.insert(0, (p & 0x7F) | 0x80)
            p >>= 7
        chunk[-1] &= 0x7F
        out.extend(chunk)
    return bytes(out)


def snmp_set(host, oid, value, community="public", timeout=3.0):
    """SetRequest with an OctetString payload. True if the agent replied."""
    varbind = _tlv(0x30, _tlv(0x06, _oid_bytes(oid)) + _tlv(0x04, value.encode()))
    pdu = _tlv(0xA3,
               _tlv(0x02, b"\x00\x00\x00\x2a")
               + _tlv(0x02, b"\x00") + _tlv(0x02, b"\x00")
               + _tlv(0x30, varbind))
    msg = _tlv(0x30, _tlv(0x02, b"\x00") + _tlv(0x04, community.encode()) + pdu)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg, (host, 161))
        s.recvfrom(4096)
        return True
    except socket.timeout:
        return False
    finally:
        s.close()


def main(ip):
    client = sc.SNMPClient(ip, timeout=2.0)

    def g(oid):
        try:
            return client.get(f"{OID_BASE}.{oid}")
        except Exception as exc:
            return f"<error {exc}>"

    def show(label, value):
        print(f"  {label:<44} {str(value)[:86]}")

    def select(slot, port, card=None):
        sel = {"outputSlotId": slot, "netportId": port}
        if card is not None:
            sel["recvCardId"] = card
        ok = snmp_set(ip, f"{OID_BASE}.10.30.4", json.dumps(sel))
        time.sleep(0.3)
        return ok

    print(f"\n=== H-series retest against {ip} ===")

    # ── 1. THE BIG ONE ────────────────────────────────────────────────────
    print("\n--- 1. Per-card SNMP: correct key AND correct slot numbers ---")
    print("    Two things were wrong before: the key was ropportId instead of")
    print("    netportId, and the slot was 0-3/4 instead of a real chassis")
    print("    output slot. Anything other than BizIdError here is the answer.")
    FIELDS = {1: "work status", 2: "temp status", 3: "temperature",
              4: "power status", 5: "voltage", 6: "FPGA", 7: "MCU",
              8: "max temp"}
    won = False
    for slot in list(H15_OUTPUT_SLOTS) + list(LEGACY_GUESSES):
        for port in (0, 1):
            if not select(slot, port, 0):
                show(f"slot {slot} port {port}", "SET got no reply")
                continue
            summary = g("10.30.6")
            if isinstance(summary, str) and summary.startswith("ERROR:"):
                show(f"slot {slot} port {port} .30.6", f"{summary}  <- refused")
                continue
            show(f"slot {slot} port {port} .30.6", f"{summary}  <-- ANSWERED")
            for n, name in FIELDS.items():
                show(f"    .30.7.{n} {name}", g(f"10.30.7.{n}"))
            won = True
            break
        if won:
            break
    if not won:
        print("    -> Still refused on every slot. The key was not the only")
        print("       problem; tell NovaStar which slots you tried.")

    # ── 2. Slot selector ──────────────────────────────────────────────────
    print("\n--- 2. .30.0 slot selector with real chassis slot numbers ---")
    print("    An empty summary here now means 'no card in that slot', which")
    print("    R&D says is normal — not a broken subtree.")
    show(".30.1 number of output card slots", g("10.30.1"))
    for slot in H15_OUTPUT_SLOTS:
        snmp_set(ip, f"{OID_BASE}.10.30.0", str(slot))
        time.sleep(0.25)
        show(f"slot {slot} .30.3 summary", g("10.30.3"))
        show(f"slot {slot} .30.2.3 serial", g("10.30.2.3"))

    # ── 3. iSignal ────────────────────────────────────────────────────────
    print("\n--- 3. .1.17 power: iSignal is the field (R&D) ---")
    show(".1.10 power supply count", g("10.1.10"))
    show(".1.17 power status", g("10.1.17"))
    print("    Expect iSignal 1 on every fitted supply. status is undocumented.")

    # ── 4. .30.5.x with a selector set ────────────────────────────────────
    print("\n--- 4. .30.5.x is a FIELD table for the selected port ---")
    for slot, port in ((H15_OUTPUT_SLOTS[0], 0), (H15_OUTPUT_SLOTS[0], 3)):
        if not select(slot, port):
            continue
        show(f"slot {slot} port {port} .5.1 link", g("10.30.5.1"))
        show(f"slot {slot} port {port} .5.2 (undocumented)", g("10.30.5.2"))
        show(f"slot {slot} port {port} .5.3 backup working", g("10.30.5.3"))
        show(f"slot {slot} port {port} .5.4 backup link", g("10.30.5.4"))

    # ── 5. Trap switch ────────────────────────────────────────────────────
    print("\n--- 5. Trap switch is .200.3 (R&D confirmed .200.2 is a doc bug) ---")
    before = g("200.3")
    show("before", before)
    snmp_set(ip, f"{OID_BASE}.200.3", "2")
    time.sleep(0.3)
    show("after writing 2 (disable)", g("200.3"))
    snmp_set(ip, f"{OID_BASE}.200.3", "1")
    time.sleep(0.3)
    show("after writing 1 (enable)", g("200.3"))
    print("    If it tracks the writes, .200.3 is confirmed as the switch.")

    client.close()

    # ── 7. R0102 linkstatus ───────────────────────────────────────────────
    print("\n--- 7. R0102 linkstatus (NovaStar's failover answer) ---")
    print("    0 cable not connected / 1 connected / 2 redundancy not set /")
    print("    3 redundancy enabled. NO R0102 reply has ever been captured, so")
    print("    print the WHOLE object — the container shape matters as much as")
    print("    the value. Run this once on a healthy wall, then pull a cable at")
    print("    a known panel and run it again. If the value moves, 3 is live")
    print("    failover state; if it doesn't, 3 is only configuration.")
    try:
        import h_series_json as hsj              # noqa: E402
        jc = hsj.HSeriesJSONClient(ip)
        for slot in H15_OUTPUT_SLOTS:
            for connector in (0, 1, 2, 3):
                reply = jc.get_slot_info(slot, connector_id=connector)
                if reply is None:
                    show(f"slot {slot} connector {connector}", "no answer")
                    continue
                show(f"slot {slot} connector {connector} raw",
                     json.dumps(reply))
                parsed = hsj.parse_slot_info(reply)
                if parsed:
                    show("    decoded",
                         f"links={parsed['links']} "
                         f"cable={parsed['cable_connected']} "
                         f"redundancy={parsed['redundancy_enabled']}")
        jc.close()
    except Exception as exc:
        print(f"  skipped: {exc}")

    # ── 6. Rate limit: requests, or cards actually reached? ───────────────
    print("\n--- 6. Does the rate limit count REQUESTS or CARDS REACHED? ---")
    print("    The H2 with no panels answered 220 reads with no silence. If a")
    print("    populated chain goes quiet at the same count, it is requests;")
    print("    if it survives, it is cards actually reached.")
    try:
        probe = ew.BinarySenderCardProbe(ip, 5201, timeout=1.5,
                                         connect_timeout=3.0, register="live",
                                         sender_card=0, pace=0.12, budget=None)
        first_silence = None
        answered = 0
        with probe:
            for i in range(240):
                r = probe.probe(0, i % 36)
                if r.answered:
                    answered += 1
                elif first_silence is None:
                    first_silence = i
        print(f"  240 reads at POPULATED addresses: {answered} answered")
        print(f"  first silence at read: {first_silence}")
    except Exception as exc:
        print(f"  skipped: {exc}")

    print("\nDone. Paste the interesting parts into the reply.\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1])
