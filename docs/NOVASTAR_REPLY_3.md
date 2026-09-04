Hi Lovis,

Thank you — and please thank R&D. That's the three things I was stuck on, all
in one message.

The R0155 document is the one I most needed. The reply my H15 sends is exactly
what's in it, so I had the right hardware and the wrong paperwork. I'd worked
the scaling out by comparing my readings to what NovaLCT shows for the same
cards, but I was guessing. Your worked examples — 4200 for 42 degrees, 480 for
4.8 V — are in my tests now, so that code can't drift back. It showed
1850 degrees once, using the old document's scaling on this reply.

Two questions on it.

**The power flags.** I had them backwards, assuming 0 meant OK because that's
what most of my cards report. Fixed. But 21 of the 36 cards I have readings for
report 0 on **both** power fields while lit and answering normally — read
literally, both supplies failed on a card that's clearly working. I think these
panels have one supply fitted and an unfitted one reports 0. Is that right?
Until I know I show the value and won't alarm either way, because guessing
wrong either hides a dead supply or puts a red fault on most of a healthy wall.

**The older reply.** I also have captures of an R0155 reply using `voltage`
instead of `volt`, with temperature in half degrees. My code supports both and
picks by field name. Is that older reply still sent by any firmware in the
field, or can I drop it?

R0102 is exactly what I was after — one read per sender card, which costs me
almost nothing next to reading every panel. Three questions before I put a
warning on screen:

- Is `linkstatus` one value per sender card, or one per connector? At the
  moment I only ask for connector 0.
- Does 3, "Redundancy enabled", mean redundancy is **configured** on that card,
  or that the card is **currently carrying** the backup path? Those are very
  different answers to "am I on backup right now".
- Does it report on my current firmware, V2.0.0.6, or is it waiting on
  V2.3.0.0? That decides whether I can test it next time the wall is up.

The port 7000 firmware sounds like the right fix. One pass of my wall is 286
reads against a limit of 150 to 200, so it can never finish in one go however
I pace it.

- **Does that firmware only read over port 7000, or does everything I use today
  keep working?** This is the one I need answered before installing anything.
  My dashboard reads the binary protocol on 5201 and the JSON commands on 6000,
  and our show control runs through Bitfocus Companion on the same control
  protocol. If 7000 replaces any of that rather than being added alongside it,
  upgrading breaks a working show system to fix a monitoring problem.
- How do I get it, and would you put it on a production H15? Mine runs live
  shows.
- Will the frame format be documented? I can work from captures, but I've had
  to correct myself several times doing that.
- When V2.3.0.0 ships, is the batch read standard, or still a separate build?

Three older items are still open, and none of them look like firmware problems.

**Byte 12 of the live monitoring reply.** The only encoding I have says 1 is
primary and 2 is backup. On my healthy wall 162 cards report 1 and 124 report
11 — and the 11s are whole chains working perfectly. I'd been treating anything
unrecognised as disconnected, which showed 124 lit panels as down. Now only 0
counts as disconnected and the rest is unknown. Safe, but I'm ignoring a field
that's clearly telling me something.

**The per-card SNMP OIDs — I typed it wrong, twice.** I reported `.30.6` and
`.30.7.x` returning BizIdError. The key is `netportId` and I'd written
`ropportId`; separately I was passing `outputSlotId` 0 to 4 when my output
cards are at slots 20, 22, 28 and 30. Please ignore that item — I'll retest
when the wall is back on the network. If it works I can retire most of my
binary code, so I'd like to know whether it's expected to work on V2.0.0.6.

**W0120.** The one I've been asking longest about. While my tool sent W0120
every 3 seconds we lost control of the wall from Companion, and killing my tool
got it straight back. I changed two things at once, and the read limit you've
confirmed would look identical from the control side, so I can't say W0120
caused it. I removed it either way. Still:

- What does W0120 do, and does it claim exclusive control?
- Is there a limit on how many clients can be connected at once?
- **Is there a supported way for a read-only client to identify itself as an
  observer, so it can never take control from something else?**

That last one is what I really need. Everything I'm building is read only, for
one purpose: knowing during a show which panels are talking, which chains are
on backup, and where a cable has broken. But a monitoring tool that can take
the desk from the operator mid-show is worse than none, and right now that's
only safe because I removed things and hoped — not because the protocol
guarantees it.

Thanks again,
Matt
