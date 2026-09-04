Hi Lovis,

Thank you — and please thank R&D for me. That's the three things I was stuck
on, all in one message.

The R0155 document is the one I most needed. The reply my H15 sends is exactly
what's in it, so I had the right hardware and the wrong paperwork the whole
time. I'd worked the scaling out by comparing my readings against what NovaLCT
shows for the same cards and got 0.01 degrees and 0.01 volts, but I was
guessing. Your worked examples — 4200 for 42 degrees, 480 for 4.8 V — are now
written into my tests, so if that code ever changes the tests fail rather than
the dashboard quietly showing nonsense. It showed 1850 degrees once, when I
applied the old document's scaling to this reply.

Two things I'd still like to ask about it.

First, the power flags. I had them backwards — I'd assumed 0 meant OK, because
that's what most of my cards report, and your document says plainly that 0 is
Fault. I've fixed that. But it leaves me with something I can't explain: on my
286 panel wall, 21 of the 36 cards I have readings for report 0 on **both**
power fields while the panels are lit and answering me normally. Read literally
that's both supplies failed on a card that's clearly working. My guess is that
these panels only have one supply fitted and an unfitted or unmonitored supply
reports 0 — but that's a guess, and I don't want to guess about a fault
indicator. Is that what 0 means on a panel with a single supply? Until I know,
I show the value and refuse to raise an alarm either way, because getting it
wrong in one direction hides a dead supply and in the other direction puts a
red fault on most of a healthy wall.

Second, a smaller one. I also have captures of an older R0155 reply that uses
`voltage` instead of `volt`, with temperature as half degrees and voltage as
the lower 7 bits — the encoding in the older document. My code supports both
and picks between them by which field name came back. Is that older reply still
sent by any firmware in the field, or can I eventually drop it?

On R0102, that's exactly what I was after. I've built it in already — one read
per sender card, which costs me almost nothing compared to reading every panel.
Two questions before I trust it enough to put a warning on screen:

Is `linkstatus` one value for the whole sender card, or one per connector? My
code handles either, but at the moment I only ask for connector 0, and if it's
per connector I need to read all of them.

And does 3, "Redundancy enabled", mean redundancy is **configured** on that
card, or that the card is **currently carrying** the backup path? Those are
very different answers to the question I actually need answered, which is
"am I running on backup right now". I'll test it by reading a healthy wall,
then pulling a cable and reading again to see whether the value moves — but
I'd rather know the intended meaning than infer it.

And one practical thing: does R0102 report this on my current firmware,
V2.0.0.6, or is it also waiting on V2.3.0.0? It changes whether I can test it
next time the wall is up or have to wait.

The port 7000 firmware sounds like the right fix. Reading one register per
panel is 286 requests for a single pass of my wall, against a limit of about
150 to 200, so it can never finish in one go no matter how I pace it — that's
been the hardest constraint in this whole project. A few practical questions:

- **Does the customized firmware only read over port 7000, or does everything
  I use today keep working?** This is the one I need answered before I'd put it
  on anything. My dashboard reads the binary protocol on 5201 and the JSON
  commands on 6000, and our show control runs through Bitfocus Companion on the
  same control protocol. If port 7000 replaces any of that rather than being
  added alongside it, upgrading breaks a working show system to fix a
  monitoring problem, and I'd want to plan for that rather than find out.
- How do I get it, and is it something you'd put on a production H15? Mine runs
  live shows, so I'd want to know what I'm taking on.
- Will the port 7000 frame format be documented? I can work from captures, but
  I've had to correct myself several times doing that and I'd rather follow a
  specification.
- When V2.3.0.0 ships, does the batch read come with it as standard, or does it
  stay a separate customized build?

I'm not in a rush on that one. Knowing it's coming means I can stop building
workarounds for the rate limit, which is worth a lot on its own.

There are three older items still open from my earlier list. None of them are
firmware problems as far as I can tell — I think they're all gaps in what's
written down.

**What is byte 12 of the live monitoring reply?** The only encoding I have for
it says 1 is primary and 2 is backup. On my healthy wall 162 cards report 1 and
the other 124 report 11, and the ones reporting 11 are whole chains that are
working perfectly. I had been treating anything I didn't recognise as
disconnected, which showed 124 lit panels as down. Now I only treat 0 as
disconnected and leave the rest as unknown, which is safe but means I'm
ignoring a field that's clearly telling me something.

**The per-receiving-card SNMP OIDs — I typed it wrong, twice.** I reported that
`.30.6` and `.30.7.x` always returned BizIdError. The selector key is
`netportId` and I had written `ropportId`, and separately I was passing
`outputSlotId` 0 to 4 when the output cards on my chassis are at slots 20, 22,
28 and 30. Please ignore that item. My wall is off the network at the moment,
so I'll retest with a real output slot and let you know. If it does work, that
subtree gives me per-panel temperature and voltage over a read-only interface
and I can retire most of the binary code — so it matters a lot to me whether
it's expected to work on V2.0.0.6 or whether I need newer firmware for it too.

**W0120, and whether a monitor can declare itself read only.** This is the one
I've been asking longest and it's still the most important to me. While my tool
was sending W0120 every 3 seconds we lost control of the wall from Companion,
and killing my tool got it straight back. I'll be honest that I changed two
things at once and the read limit you've now confirmed would look identical
from the control side, so I can't say W0120 caused it. I've removed it either
way. The questions stand:

- What does W0120 actually do, and does it claim exclusive control?
- Is there a limit on how many clients can be connected at once?
- **Is there a supported way for a read-only client to identify itself as an
  observer, so it can never take control from something else?**

That last one is what I really need. Everything I'm building is read only, and
it's for one purpose: knowing during a show which panels are talking, which
chains are running on their backup, and where a cable has broken, so I can send
someone to the right panel instead of walking the wall. But a monitoring tool
that can take the desk away from the operator mid-show is worse than no
monitoring tool, and at the moment I've only made that safe by removing things
and hoping — not by anything the protocol guarantees.

Thanks again,
Matt
