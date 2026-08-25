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

Still waiting on:

1. The R0155 reply this firmware sends (`workStatus`, `temp: 3700`,
   `volt: 440`, `mcuVersion`...) is in NEITHER V1.0.18 nor V1.0.20 of the
   control protocol. Which firmware introduced it, and is it documented?
2. Any way to tell which sender card is currently feeding a receiving card, or
   whether a chain has failed over. **This is the one that matters most** — a
   break with a working backup leaves every panel lit and answering.
3. A documented rate limit, or a bulk read that returns a whole chain.
4. What `status` means on `.1.17`, given `iSignal` is the power field.
5. What `byte[12]` = `0x0B` means in the live-monitoring reply (0x01 on 162
   cards, 0x0B on 124, all healthy).

Draft reply to their last message: `docs/NOVASTAR_REPLY_2.md`.
Full question list: `docs/NOVASTAR_PROTOCOL_QUESTIONS.md`.

## Things NOT to repeat

- Do not send `W0120`. It is the heartbeat implicated in the Companion lockout
  and has been removed entirely.
- Do not add a SET path to `src/snmp_client.py`. Test tooling that needs one
  lives in `tools/`.
- Per-card reads are on demand only. Nothing may put them back on a poll cycle.
