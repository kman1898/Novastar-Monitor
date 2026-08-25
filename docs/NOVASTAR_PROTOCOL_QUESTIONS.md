**Subject:** H-series protocol questions — SNMP per-card OIDs, R0155 schema change, and W0120 behavior

Hi,

I'm building an internal read-only monitoring dashboard for our H-series wall — temperature, voltage and fault status per receiving card, displayed during shows so we can catch a failing panel before it becomes visible. Everything below comes from testing against an H15 running firmware V2.0.0.6, plus Wireshark captures of NovaLCT and the device's own web UI.

A few things don't match the documentation, and one of them may explain a real problem we had during a show. I've put that one first. Where I'm confident I've reproduced something I've said so; where I'm inferring, I've flagged it.

One thing to explain up front so the numbers below don't look inconsistent: the same H15 runs several different show configurations, so the panel counts differ from item to item — 1548 installed / 1374 enumerated on one configuration, 286 on another, 943 on a third. Where the configuration matters to a question I've named it.

---

**1. Does W0120 take over control from another client? It isn't documented either way**

This is the one I'd most like an answer on.

Our monitoring tool sent the W0120 heartbeat every 3 seconds, because that's what a Bitfocus Companion module for H-series splicers does. While our tool was running, we **lost control of the device from Companion**. Killing our tool restored control immediately. We've since removed W0120 entirely and haven't seen it since.

So it looks like W0120 might register the sender as the active controller, with a second sender displacing the first. That would be a reasonable design, and it isn't in any documentation I have.

In fairness I should say that's one uncontrolled observation and I changed two things at once, so I can't call it proven. Item 8 below describes the controller going silent for roughly 45 seconds under our polling load, and that on its own would look like a loss of control from another client. So the W0120 questions are really what I'm after, whether or not it caused that particular incident:

- What does W0120 actually do? Does it claim exclusive control?
- Is there a limit on concurrent controllers?
- **Is there a supported way for a read-only client to identify itself as an observer**, so it can never displace a control client? That's really what I need — a monitoring tool should be incapable of interfering with show control.

---

**2. ~~The per-receiving-card SNMP OIDs don't work~~ — WITHDRAWN, my error**

This item was wrong and is withdrawn.

The selector key is **`netportId`**. I had transcribed it as `ropportId` from
the section 5 tables, which are images rather than selectable text in the PDF,
and the variations I then tried included `netPortId` with a capital P — which,
JSON keys being case-sensitive, fails identically. The correct call is:

```
{"outputSlotId": 4, "netportId": 0, "recvCardId": 0}
```

NovaStar's spreadsheet (*H Series Video Wall Splicer SNMP Instructions.xlsx*)
is authoritative and unambiguous on this. Retest pending — the wall is off the
network.

If it works, `.30.7.1` through `.30.7.8` give working status, temperature
status, temperature, power status, voltage, FPGA/MCU version and max
temperature per receiving card, and `.30.6` gives a JSON summary. That is
everything this project needs, over a read-only-shaped interface, and it would
displace the binary per-card path entirely.

One design note worth raising even if it all works: selecting the card is a
SET. A monitoring client therefore cannot be structurally read-only — it has
to write a selector before every read, and two monitoring clients would race
on that shared selector. Is there a read-only addressing form (the card
identity as OID suffixes rather than as a SET selector)?

---

**3. The R0155 response format changed between firmware versions, with no way to tell which you're talking to**

We have captures of two different response shapes from H-series devices.

Older firmware returns:
```
{"power0Status":0,"power1Status":0,"brightness":127,"temp":88,"voltage":170}
```
where temp ÷ 2 gives °C (88 → 44 °C) and voltage × 0.03 gives volts (170 → 5.1 V).

V2.0.0.6 returns:
```
{"workStatus":0,"tempStatus":0,"temp":3700,"tempMax":70,"voltStatus":0,
 "volt":440,"power0Status":0,"power1Status":0,"brightness":25,
 "mcuVersion":"V4.5.1.81","fpgaVersion":"V4.5.1.81"}
```
where temp ÷ 100 gives °C (3700 → 37.00 °C) and volt ÷ 100 gives volts (440 → 4.40 V).

Between versions the field was renamed from `voltage` to `volt`, the temperature scale changed by a factor of 50, and the voltage scale changed as well. Applying the old formula to the new firmware gives **1850 °C**, which is exactly what our dashboard displayed until we caught it.

Is there a documented way to detect which schema a device speaks, other than checking which keys came back? Which firmware version introduced the change, and are the V2.0.0.6 scalings (÷100 for both) correct and stable going forward?

---

**4. R0155 returns zeros for cards that aren't there**

When `workStatus` is non-zero, the response still includes `temp: 0`, `volt: 0` and `brightness: 0` rather than omitting them.

Anything reading those as measurements will report "0.0 V — dead power supply" for every address that has no card. On a wall our size that's hundreds of false critical alerts at once. We hit this too.

Could the documentation state plainly that readings are only valid when `workStatus == 0`?

On the enum: the SNMP spreadsheet documents the equivalent per-card fields as `0: Normal / 1: Alarm / 2: Abnormal` for `.30.7.2` and `.30.7.4`, and `0: Normal / 1: Abnormal` for `.30.7.1`. Does `R0155` use the same encoding for `tempStatus`, `voltStatus` and `workStatus`? We've only ever observed 0 and 1 for `workStatus`, and 0 and 2 for `tempStatus` and `voltStatus` — is 1 (Alarm) reachable on the status fields, and what distinguishes it from 2?

---

**5. R0155 seems to skip low card IDs on most chains**

Enumerating a three-sender-card wall, 24 of 27 chains returned nothing for `recvCardId` 0 through 4, then were perfectly contiguous from 5 upward. Only port 0 on each sender card started at 0. Enumeration found 1374 cards where 1548 are installed.

I'll flag my own uncertainty here: since writing that up we've found the rate limit described in item 8, and that sweep was an unpaced burst. Some or all of those gaps may have been the controller declining to answer rather than a numbering quirk. I'd still like to know:

Is `recvCardId` the physical position in the chain, or a different index? Are low IDs ever reserved on non-first ports? And what *is* the correct way to enumerate every receiving card on a port — is there a supported call that returns the count directly?

---

**6. What we would need from SNMP for it to be the monitoring interface**

Grouping this separately because it is the constructive version of items 2 and
the list below: if SNMP is the interface you would like third parties to use
for monitoring, here is the gap between what it reports and what monitoring a
wall actually requires. Everything here is measured on an H15 at V2.0.0.6.

SNMP does a lot right. A walk of the `.1` subtree — about fifteen GETNEXT
round trips — returns model, firmware, serial, MAC, device time, CPU status,
temperature status, ten fan statuses, four PSU statuses, and the screen's
name, resolution and brightness. That's the whole chassis in under a second,
read-only, with no risk to a control client. That is exactly the shape we
want.

The gap is that it reports **flags where monitoring needs numbers**, and stops
at the chassis:

| What we need | What SNMP gives | Consequence |
|---|---|---|
| Per-card temperature / voltage | pending — see item 2 | if the corrected `netportId` selector works this row goes away entirely, and SNMP becomes the answer rather than the gap |
| Fan speed | an undocumented `speed` key, 0 on all 10 while running | not in your OID table at all — implemented, or should we ignore it? |
| PSU voltage | an undocumented `voltage` key, 0 on all 4 | same |
| Chassis temperature in °C | the `temperature` field in `.1.0` reads 0 on both our devices; only the `.1.8` status flag is populated | no trend, no early warning — only "it is now too hot" |
| Which output card `.30` describes | serial, firmware, port count all empty | the data cannot be attributed to a slot |
| Bit-error counters | nothing | the best data-break signal we have is binary-only |
| Fibre vs copper per output | nothing | `R0100`'s `lightstatus`/`linkstatus` have no SNMP equivalent |
| Slot / topology map | nothing | `R0405`'s slots and per-output geometry have no SNMP equivalent |

Concretely, the three that would matter most, in order:

1. **Confirm the per-card OIDs work** (item 2 — my transcription error, retest
   pending). Per-panel temperature and voltage over a read-only interface is
   the whole thing. Everything else here is secondary, and if that one works I
   may not need much of the rest.
2. **Populate the numeric fields the device already returns** — fan `speed`,
   PSU `voltage`, and the `temperature` in `.1.0`. They are all present in the
   payloads and all read 0; a monitoring tool cannot alarm on a field that
   reads 0 on healthy hardware.
3. **Identify the output card** in the `.30` subtree, so its port and link
   data can be attributed to a slot.

If per-card data over SNMP is not planned, we would find that useful to know
plainly — we would stop trying to make it work and treat the binary protocol
as the supported path for per-panel monitoring, which is really item 7.

---

**6b. Smaller SNMP discrepancies**

- **`.1.17` power status: we think the documented polarity is wrong.** The
  table gives `0: Not connected / 1: Connected`. Every H-series device we have
  reports `0` for every supply while running:

  | Device | Firmware | `.1.10` | `.1.17` |
  |---|---|---|---|
  | H15 | V2.0.0.6 | 4 | four entries, all `status: 0`, `iSignal: 1` |
  | H2 | V2.2.0.0 | 1 | one entry, `status: 0`, `iSignal: 1` |

  Each unit returned an entry count (four on the H15, one on the H2), and both
  devices were powered and answering SNMP over the network at the time — so
  "not connected" cannot be literally true of a supply that is running the
  device.

  Read the other way, `status: 0` = normal (matching `.1.16` fans and every
  other health field), both devices are simply healthy, and everything is
  self-consistent. (There's also an undocumented `iSignal` field in each entry,
  which reads 1 on both — not in the `.1.17` example in your spreadsheet, so
  I don't know what it means.) That is our reading. Can you confirm, and
  correct the document if so? Right now we show the raw value and
  refuse to alarm either way, because guessing wrong in one direction hides a
  dead supply and in the other direction cries wolf on a healthy one.

- **Trap switch OID — confirmed on two models.** Section 5.5 documents
  `.10.200.2` as the trap reporting switch. On BOTH our H15 (V2.0.0.6) and our
  H2 (V2.2.0.0), `.200.2` does not exist while `.200.3` does and returns 0. Is
  `.200.3` the switch, and does it use the documented 1 = enable / 2 = disable
  encoding? (`.200.1`, the trap target, reads `{}` on both — presumably just
  unset.)

- **Output card summary is empty on every device we have.** `.30.3` returns
  `{"SN":"","netPortCount":0,"status":1,"version":"0"}` — byte-identical on
  the H15 (V2.0.0.6) and the H2 (V2.2.0.0). No serial, no version, zero ports,
  while `status` says Normal. Since `netPortCount` is how we would know how
  many Ethernet ports a card has, and `SN` is the only way to tell which
  physical card the `.30` subtree is describing, this being blank is what
  makes the whole output-card subtree unusable for us. Is it populated on any
  firmware?

  Related, and this is just a numbering question: is the `.30.0` output-card
  slot selector 0-based or 1-based? Section 5 says N ranges "from 1 to the
  maximum", but on our H15 slot 0 is accepted and slot 4 returns "ERROR: the
  slot is empty".

- **`.30.5.x` — does it need the `.30.4` selector set first?** We read
  `.30.5.1`, `.30.5.3` and `.30.5.4` without setting `.30.4` and got 0, 0, 0.
  Read against the table that's "primary link down, backup inactive, backup not
  linked", and the last two are exactly what a healthy wall with an idle backup
  should say — so I may simply be reading an unselected port. Should `.30.4` be
  SET first, the way `.30.6` and `.30.7.x` require their selectors? And
  separately: is there anywhere an array of link states for all of a card's
  Ethernet ports at once, rather than one selected port at a time? Per-port
  link state across a whole card is something we'd use.

- **Fan speed and PSU voltage.** `.1.16` returns 10 fans and `.1.17` returns 4 power supplies. Every fan reports `speed: 0` and every PSU reports `voltage: 0`, while all report `status: 0` on a device that's running normally. Are those fields implemented in V2.0.0.6? A monitoring tool can't tell "fan stopped" from "speed not reported", so for now we're showing them as raw values and refusing to alarm on them.

- **Undocumented leaves under `.20.2`.** The table documents `.20.2.1` through `.20.2.4`, but we see leaves at `.20.2.5`, `.20.2.6` and `.20.2.7` that aren't in it. Could you publish the OID-to-field mapping for those three? We're leaving them unnamed rather than guessing, since a mislabeled status field is worse than a missing one.

---

**7. Binary protocol (TCP 5201–5203) — is any of this documented?**

I couldn't find a public document covering per-receiving-card addressing over the binary protocol, so we worked it out from captures. What we have:

In a 20-byte read frame, byte 5 is the sender card index, byte 6 is 0x01 marking it as a per-card read, byte 7 is the chain index (0–15) and byte 8 is the card position within that chain. The checksum is the sum of the bytes between the header and the checksum field, plus 0x5555, little-endian.

That reproduces a known 245-panel count exactly, so I think it's right — but I'd much rather follow a spec than our own reverse engineering. Is there an official document?

**How we derived this, so you can check it or correct it precisely**

It all comes from Wireshark captures of NovaLCT talking to our own H-series
hardware, plus reads we issued ourselves against the same devices. The captures
this rests on:

- `H series Monitioring Monitor Refresh.pcapng` — a full binary monitoring
  sweep on TCP 5203, ~57,000 frames, one device, per-card reads across card
  positions 0–91. This is where the per-card register set came from.
- `H series Bit errors detection.pcapng` and `H series More.pcapng` — a
  single-sender-card H2 rig. The addressing layout above and the bit-error
  register come from these.
- `Bit error 4x clear erros.pcapng` — an operator clicking NovaLCT's "clear
  bit errors" button. That is the only write we make (item 10e).
- `Monitoring.pcapng` — a NovaLCT monitoring pass over the 286-card
  configuration, used for the temperature check below.

Frames are cut out of the **reassembled TCP stream**, not out of packets — the
controller coalesces replies into one segment under load, and it also splits
them. Each frame is cut using its own length field at `bytes[16:18]`, read with
the split encoding: a non-zero low byte means low × 256 bytes, a zero low byte
means high-byte-many bytes, so `0x5200` is 82 and `0x0010` is 4096. Every frame
has to check out before we read anything positional out of it: exactly one
frame's worth of bytes, and a checksum matching `sum(bytes between the header
and the checksum) + 0x5555`, little-endian on the wire, header excluded. We
verify that formula against the worked example in Sending Card Central Control
Protocol V1.3. Replies are paired to requests
by the sequence number at `bytes[2:4]` and the register at `bytes[12:16]`,
never by arrival order.

Each part of the decode was then checked against something known independently,
rather than assumed:

- **Addressing.** Counting distinct (`byte[7]`, `byte[8]`) pairs across the 16
  chains in the bit-error capture gives **245** — the panel count the operator
  already knew for that rig. NovaLCT probes one address past the end of each
  chain, and probes empty chains once; those are boundary detection, not
  panels, and the total only lands on 245 once they're excluded.
- **Temperature.** In the 286-card capture NovaLCT polls `0x0000000A` once per
  card, and `byte[1] / 2` reproduces the 36–43 °C spread NovaLCT itself
  displays — for all 286. A wrong scaling would not land on the right range for
  every card.
- **Presence.** Checked against a chain we know holds exactly 22 panels: cards
  0–21 answer `0x80`, and every address past the end has the `0x40` bit set
  while repeating the previous card's readings (values in the bullet below).
- **The one write.** Our clear-bit-errors builder reproduces all five captured
  clear frames byte-for-byte, sequence numbers included. Those bytes are pinned
  as fixtures in our test suite so the frame cannot drift.

Where this is weak, plainly: it is inference from observed traffic on two
devices — an H15 on V2.0.0.6 and an H2 on V2.2.0.0 — across a handful of wall
configurations. **A value we have never seen on the wire carries no meaning we
can confirm**; `byte[12]` = `0x0B` in item 10c is exactly that. And we have had
to correct ourselves several times where the fault was our own bug rather than
device behaviour: requesting `0x0000000A` with the wrong length field (above),
mapping every unrecognised `byte[12]` value to "disconnected" and so labelling
124 working panels as down, and — the one that should have been avoidable —
replacing your documented voltage formula with a guess. We had
`(raw & 0x7F) * 0.1`, which is what §4.3.4 of the control protocol specifies,
decided it was wrong because it put every card under our own 4.7 V alarm, and
changed it to `raw * 0.03`. The alarm threshold was what was wrong. Your
worked example (172 → 4.4 V) settled it, and it also resolved a ~0.9 V
disagreement between our binary and R0155 readings that we had been treating
as a separate mystery. Each of those looked exactly like
device behaviour until it didn't. That is why these are questions rather than
statements.

I can send you the captures. We also have a small decoder that rebuilds these
frames and reproduces every result above, if that is easier to check than raw
pcaps — happy to send it instead, or as well.

Two things about it cost us a lot of time, and I mention them mostly in case they're worth a note in whatever documentation exists:

- **What is the `rqProMI` discovery reply's port list telling us?** Broadcasting `"rqProMI:"` on UDP 3800 returns `rpProMI:App,0161 H_SUB_CARD@^^@5201 H_SUB_CARD@^^@5202 H_SUB_CARD@^^@5203`. We modelled that as one service per sender card — "sender card N is on port 5200+N" — and opened a connection per card, which cost us a lot of time before we worked out that the sender card is actually selected by byte 5 of the frame over a single connection. Our NovaLCT captures agree: a full per-card monitoring sweep runs over one port and addresses every card by byte 5. So: is one connection plus byte 5 the intended model, and what should we take the advertised port list to mean? Related, what determines which of those ports a given chassis actually serves — our captures are all on 5203, but our own tool connects on 5201.

- **Register `0x0000000A` — please confirm our decode.** I previously said
  this register "is not per-card on H-series and returns a free-running
  counter". That was wrong and was my own bug: I was requesting it with a
  length field of `0x0010`, which decodes to 4096 bytes, so every reply was
  mis-framed and I was reading the wrong offsets. Requested correctly (length
  `0x5200`, 82 bytes) it is per-card and it is the single most useful register
  we have found. Our decode:

  | Offset | Read as |
  |---|---|
  | `byte[0]` | presence — `0x80` present, bit `0x40` set = no card at this address |
  | `byte[1] / 2` | temperature °C |
  | `byte[3] × 0.03` | voltage |
  | `byte[12]` | link status |

  Two independent checks: a NovaLCT capture of a full monitoring pass polls
  this register once per card for all 286 cards on our wall, and `byte[1]/2`
  reproduces the 36–43 °C range we see in NovaLCT itself; and on a chain we
  know holds exactly 22 panels, cards 0–21 answer `0x80` while 22–25 answer
  `0xC0`/`0xE0`/`0xE2`/`0xE4`/`0xE6`. Is that layout right, and are those
  scalings (÷2 and ×0.03) correct and stable?

  One thing worth documenting whatever the answer: **an address past the end
  of a chain still returns a well-formed 82-byte reply carrying the PREVIOUS
  card's temperature and voltage**, behind the absent flag in byte[0]. So
  "the device answered" is not a presence test, and anything that skips the
  byte[0] check invents a plausible panel for every empty address on the wall.
  That is what my original mistake looked like from the outside.

- **Nothing distinguishes a primary sender card from a backup.** Our H15 has four sender cards — two primary, two backup — and `R0405` correctly lists all four slots: 20, 22, 28 and 30. But only the two primaries answer anything. `R0155` responds for 20 and 22 and is silent for 28 and 30, and a per-card binary read against the backups reports every address as absent. That's presumably correct behaviour for an idle backup, but it means we can't tell "this is a backup, standing by" from "this sender card has failed" — which for a monitoring tool is the whole question. Is there a field anywhere (R0405, SNMP, the web API) that reports a sender card's primary/backup role, and which primary a given backup is covering? And is there a way to read a backup's own health while it is idle?

---

**8. The controller stops answering after roughly 150–200 reads, and silence is indistinguishable from "no card here"**

*Update since first writing this:* we tested the same thing on an H2 with no
receiving cards attached, and **220 consecutive per-card reads at empty
addresses all answered — no silence at all.** That suggests the limit is not
simply a request counter but is tied to the controller actually going out to a
receiving card over the chain, with empty addresses answering immediately and
apparently from cache. It's a weak control though: the H2 is a different model
on different firmware (V2.2.0.0 against the H15's V2.0.0.6), so we can't
separate "empty addresses don't consume the budget" from "the H2 doesn't have
this limit at all". If the first reading is the right one, a rate limit
expressed as "N cards read per interval" rather than "N requests" would let us
pace correctly, and would explain why our empty chains never triggered it.

This is the one that has cost us the most, and I think it affects anyone building monitoring against either protocol.

Enumerating a wall means walking each chain until the controller says there's no card at the next position. Two separate things make that unreliable:

**An empty address answers much more slowly than an occupied one.** At a 0.5 s socket timeout, *every* chain on our wall ended on a timeout rather than on the controller's answer. At 1.5 s they end on a real answer. That's fine once you know — but a timeout and "no card here" produce the same conclusion in naive code, so the wall just comes out short with no error anywhere.

**The controller stops answering after roughly 150–200 reads and does not recover quickly.** Measured on our H15 with a 0.15 s gap between reads: the first 150–200 per-card reads are answered correctly, then it goes quiet, and it stays quiet for the rest of the sweep. Ten consecutive chains reported 0 cards. After 40–50 s of no traffic it answers normally again.

Concretely, one chain that has exactly 22 receiving cards:

| How it was read | Result |
|---|---|
| rested, that chain alone | 22 (correct, boundary answered) |
| during a full sweep, 0.5 s timeout, no pacing | 7, then 9 on a retry |
| during a full sweep, paced, 1.5 s timeout | 20 |
| re-walked alone after a 50 s pause | 22, boundary answered |

The batched JSON `R0155` path is worse rather than better: a burst of 95 addresses (8 commands per datagram) got 5 answers and then silence, and afterwards `R0155` returned nothing at all for about 45 s — including for addresses it had answered a moment earlier.

We've worked around it by pacing reads, resting 45 s before we think the budget runs out, and re-walking any chain that ended on silence one at a time from a rested controller. That works, but it turns a wall map into a several-minute operation and it's all guesswork about limits we can't see.

So:

- Is there a documented request-rate limit for the binary per-card reads and for JSON `R0155`? A number we could design to would replace all of the above.
- Is the recovery period configurable, or is there a way to ask the controller whether it's currently accepting reads?
- Is there a *bulk* read — one request that returns the card count for a chain, or the whole chain's status — rather than one request per card position? That's really what we want. Walking 250 addresses to learn a wall's shape is what puts us near the limit in the first place.
- Does NovaLCT hit this limit too, and if not, what is it doing differently? Its own captures show it polling several registers per card per cycle, which is far more traffic than we send.

The reason this matters beyond enumeration: it means an under-reported wall looks exactly like a correct one. A chain that reads 20 instead of 22 doesn't raise an error — it just quietly stops monitoring two panels.

Related: NovaLCT polls several registers per card every cycle that I can't identify. We've decoded `0x4A010002` as a bit-error counter (3-byte response, uint16 little-endian, 0xFFFF meaning saturated) — could you confirm that? The others are `0x0500001B` (1 byte, polled most frequently of all), `0x80070003` (68 bytes), `0x20040014` (8 bytes), `0x04000008` and `0x04000009` (4 bytes each, always polled as a pair), and the per-card data channels `0x00400003` through `0x004E0003`. Of those, `0x00400003` returns a constant 0x5D on every card on the wall regardless of state, yet is still polled every cycle — is it reserved?

---

**9. Is the web UI's HTTP API supported for third-party use?**

The device's own web interface on port 80 uses endpoints including `/api/screen/readAllList`, `/api/device/readDetail`, `/api/device/readSlot` and `/api/input/readAllList`. These return richer topology than SNMP exposes — per-output pixel geometry and `isCardOnline` in particular, which would be genuinely useful to us.

Is that API documented and supported for third parties, or should we treat it as internal and liable to change?

---

**10. Receiving cards — the questions that matter most to us**

Everything in this project comes down to reading receiving cards, so these are
the ones I would trade all the others for.

**a. Is there a bulk read?** Reading one register per card is 286 requests for
one pass on this configuration, and item 8's limit is 150–200 reads. So **a
single pass of one wall cannot complete inside one budget** — it always has to
stop partway and wait out a recovery period, and a bigger configuration is
proportionally worse. A call that returns a whole chain's temperatures, or
even just a chain's card count, would remove the problem rather than us pacing
around it.

**b. What do the per-card power flags mean?** `R0155` returns `power0Status`
and `power1Status`. On our 286-panel configuration, fifteen cards report
`power0Status: 1` AND `power1Status: 1` while simultaneously reporting 39–42 °C
and 4.0–4.1 V. On the larger 1374-card configuration, 287 cards are in that
same both-flags-set state, so this isn't a handful of odd cards. A
card cannot measure and transmit its own temperature through a failed primary
supply, so "1 = failed" cannot be right for a card that is reporting. We had
been treating non-zero as a fault and it raised a warning every polling cycle
on a lit, healthy wall. What are the actual values, and does 1 perhaps mean
"not fitted" or "not monitored" on panels with a single supply?

**c. What does `byte[12]` = `0x0B` mean?** In the live-monitoring register, the
only encoding I have for this field gives `0x01` = primary and `0x02` =
backup. On our H15 a healthy wall reports `0x01` on 162 cards and **`0x0B` (decimal 11) on the other
124**, and whole chains that are working normally report `0x0B`. We had been
mapping "anything else" to disconnected, which labelled 124 healthy panels as
disconnected.

**d. Please confirm the bit-error counter.** We read `0x4A010002` as a 3-byte
reply: `byte[0]` presence (`0x05`), `bytes[1:3]` a uint16 little-endian count,
`0xFFFF` meaning saturated. Is the count cumulative since power-on? What
exactly increments it? It is the only continuous data-integrity signal we have
found and it has no JSON or SNMP equivalent, so we lean on it heavily.

**e. Please confirm the clear command.** From a capture of NovaLCT's "clear"
button we have: write one byte `0x05` to register `0x76000001`, broadcast
(`byte[5] = 0xFF`, target bytes `FF FF FF`). Is that right, and is there a
per-card or per-chain form? Clearing everything discards evidence of an
intermittent link for whoever looks next, so we would rather clear one chain.

**f. Can we tell which sender card is currently driving a card?** This is the
one I most want. We pulled a cable at panel 12 of a 22-panel chain and the
chain split cleanly: the primary sender card served panels 1–11 and the backup
served 12–22, with the backup's panels carrying a non-zero bit-error count and
the primary's clean. Every panel stayed lit and every panel still answered, so
nothing in a naive "is it online" check showed a fault at all. We can detect it
by probing both sender cards and comparing, but that doubles the traffic on a
protocol that is already rate-limited. Is there a field that reports, per
receiving card, which sender card is currently feeding it — or per chain,
whether it has failed over?

**g. Is the sender card slot mapping fixed?** We derive the operator-facing
card number from the chassis slot as `card = (slot − 20) / 2 + 1`, so slots
20, 22, 28 and 30 come out as cards 1, 2, 5 and 6 — which is what I call them
on the floor. Is that guaranteed across H-series models, or should we be
reading it from somewhere? There's a second numbering in play as well: our own
data carries `user_slot = slot + 1`, giving 21, 23, 29 and 31 for the same four
slots. Which of those is the canonical way to refer to a slot in your
documentation and UI?

**h. What is `tempMax` in `R0155`?** We see `tempMax: 70` on every card. Is
that the card's configured alarm threshold, or the highest temperature it has
recorded? We currently show it as a threshold and do not alarm on it.

---

**What I'm ultimately after**

A dashboard that reports panel health during a live show and is structurally incapable of interfering with control. Given all of the above, what would NovaStar recommend as the supported path?

Concretely: should SNMP be the primary monitoring transport — and if so, can the per-card OIDs in item 2 be made to work? Is there a documented polling rate limit for the JSON UDP protocol? And is there a supported way to read per-panel temperature and voltage without the risk described in item 1?

Happy to share packet captures for any of this if it helps.

Thanks,
Matt Knotts
