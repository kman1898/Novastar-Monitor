# Next time the wall is available

Everything below needs the H15 on the network with panels powered. Written
2026-08-24; the questions come from an exchange with NovaStar support that is
mid-flight.

## Run this first

```
python3 tools/h_series_retest.py 192.168.0.10
```

It answers, in order of importance, the things NovaStar and we are both waiting
on. Output is written to be pasted into a reply to them.

## What each result means

**1. Per-card SNMP.** We had TWO things wrong: the selector key was
`ropportId` (correct: `netportId`) and the slot was 0-3 or 4 (correct: a real
chassis output slot, which on this H15 means 20 / 22 / 28 / 30). If `.30.6`
answers instead of returning `ERROR: BizIdError`, per-card temperature,
voltage and status are available over SNMP and most of the binary per-card
code can be retired. This is the single most valuable result.

If it still refuses on every slot, say so — it means the key was not the only
problem and NovaStar needs to know which slots were tried.

**2. Slot selector.** An empty `{"SN":"","netPortCount":0,...}` summary means
"no card in that slot", which R&D says is normal. It is NOT evidence of a
broken subtree — we told them it was, and that was wrong.

**3. `.1.17` power.** R&D: use `iSignal`, not `status` (0 = not connected to
power, 1 = connected). Expect 1 on every fitted supply. Confirms the code
change already made.

**4. `.30.5.x`.** A FIELD table describing ONE selected (slot, port) — link
status, backup working, backup link — not a map of ports. Reading it without
setting `.30.4` first is what produced the bogus "3 of 16 ports are down".

**5. Trap switch.** R&D confirmed `.200.2` is a documentation error and
`.10.200.3` is the switch. The script writes 2 then 1 and puts it back.

**6. Rate limit.** An H2 with no panels answered 240 reads with no silence. If
a populated chain goes quiet at roughly the same count, the limit counts
requests; if it survives, it counts cards actually reached. That decides
whether our pacing can be smarter.

**7. R0102 `linkstatus` — the new one, and the most important read here.**
NovaStar's answer of 2026-09-04 to "how do we detect primary/backup switching":
R0102 carries the sender card's `linkstatus`, 0 = cable not connected,
1 = connected, 2 = redundancy not set, 3 = redundancy enabled. It is wired up
(`h_series_json.parse_slot_info`, stored per slot as `slot_link_status`) but
**no R0102 reply has ever been captured**, so three things need confirming:

- **The reply shape.** Is `linkstatus` a single value per card, or a
  `{link0..linkN}` block per connector? The parser handles both, but the
  device_manager only asks for connector 0. If it turns out to be
  per-connector, `_refresh_slot_link_status` needs a 0..3 sweep.
- **Does 3 mean redundancy is CONFIGURED or currently CARRYING the load?**
  Those are different answers to "are we on backup right now" and the vendor's
  wording does not decide it. Read it on a healthy wall, then pull a cable and
  read it again — if the value moves, it is live state; if it doesn't, it is
  configuration and the failover answer is still the bit-error signature.
- **Whether it is the same field as R0100's `linkstatus`.** If it is, then
  `parse_output_links`'s `up = bool(state)` is wrong: it would be reading
  "redundancy not set" (2) as "port up". R0100's blocks correlated exactly with
  the physical wiring on all four cards, which argues they are different
  fields, but that was never checked against this encoding.

## Also worth doing while connected

- **Re-run the wall enumeration.** `python3 src/enumerate_wall.py 192.168.0.10
  --yes-contact-hardware -v`. The stored snapshot is from 2026-08-09 and the
  wall may have been reconfigured. Takes ~20 minutes with the verification
  pass; halt app polling first via `POST /api/halt {"halted":true}` and resume
  after.
- **Read bit errors** from the Wall View and confirm the counters still read 0
  after the NovaLCT clear.
- **Pull a cable again** if there is time, and confirm the break banner names
  the right panel. Last time: primary fed panels 1-11, backup fed 12-22, every
  panel stayed lit.

## Open questions with NovaStar

**Answered 2026-09-04** — the three that had been at the top of this list. See
`docs/H_SERIES_FINDINGS.md` §6.7 for the detail:

1. ~~Which firmware introduced the R0155 reply we actually get?~~ The published
   R0155 page had unit errors. The corrected one documents exactly the reply
   this hardware sends — temp in 0.01 °C, volt in 0.01 V. Ships with H V2.3.0.0.
2. ~~How do we tell whether a chain has failed over?~~ R0102 `linkstatus`,
   0/1/2/3. Implemented, unverified — see item 7 above.
3. ~~A rate limit, or a bulk read?~~ Customized firmware reading over **TCP port
   7000**, with batch processing and large data packets, folding into V2.3.0.0.

Still waiting on:

1. What `status` means on `.1.17`, given `iSignal` is the power field.
2. What `byte[12]` = `0x0B` means in the live-monitoring reply (0x01 on 162
   cards, 0x0B on 124, all healthy).
3. **Why 21 of 36 lit, answering cards report `power0Status: 0` AND
   `power1Status: 0`.** The polarity is settled — 0 is Fault — so read
   literally that is a double supply failure on hardware that is working.
   Almost certainly "not fitted / not monitored" on a single-supply panel, but
   until it is confirmed nothing can alert on a supply flag at all.
4. How to obtain the customized V2.3.0.0 firmware, and whether the port 7000
   batch format will be documented. Nothing is implemented for it — port 7000
   has never been seen on the wire here.

Draft reply to their last message: `docs/NOVASTAR_REPLY_3.md`.
Full question list: `docs/NOVASTAR_PROTOCOL_QUESTIONS.md`.

## Things NOT to repeat

- Do not send `W0120`. It is the heartbeat implicated in the Companion lockout
  and has been removed entirely.
- Do not add a SET path to `src/snmp_client.py`. Test tooling that needs one
  lives in `tools/`.
- Per-card reads are on demand only. Nothing may put them back on a poll cycle.
