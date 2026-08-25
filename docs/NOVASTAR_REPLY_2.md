Hi Steven,

That's very helpful, thank you — and thanks for getting R&D to look at it.

iSignal was the missing piece. I'd been reading "status" and never thought to
look at iSignal because it isn't in the .1.17 example in the document. Both my
units report iSignal 1 on every supply, so they were healthy the whole time and
I was reading the wrong field. I've switched to it. While you're editing that
section, adding iSignal to the example would save the next person the same
trip. Is there any meaning to "status" on that OID, or should I just ignore it?

Good to have .10.200.3 confirmed, and thanks for logging the .200.2 error.

The slot ID answer explains a lot. I'd been feeding the selector 0 to 3 and
getting the empty summary back, and reading that as the subtree not being
populated — it was just me querying slots with no card in them. On my H15,
R0100 lists output cards at chassis slots 20, 22, 24, 26, 28, 30, 32 and 34, so
I'll try those with the selector rather than 0 to 3. That probably also
explains my per-card OID problem: I was sending outputSlotId 4, and 4 isn't an
output slot on this chassis. I'll retest with 20 and 22 and let you know.

Understood on fan speed and PSU voltage not being implemented. I'll ignore
those fields rather than showing them.

That clears most of my SNMP list. What's still open for me, in order of how
much it matters:

The R0155 reply my H15 actually sends isn't the one in the control protocol
document. On V2.0.0.6 I get workStatus, tempStatus, temp as 3700, tempMax,
voltStatus, volt as 440, mcuVersion and fpgaVersion — temperature in hundredths
of a degree instead of half degrees, voltage in hundredths of a volt instead of
the lower 7 bits. That reply isn't in V1.0.18 or V1.0.20. Which firmware
introduced it, and is there a document for it? I currently work out which reply
I'm looking at from which keys came back, and getting that wrong once had my
dashboard showing 1850 degrees.

Whether there's any way to tell which sender card is currently feeding a given
receiving card, or whether a chain has failed over to its backup. This is the
one I care most about. When I pulled a cable at panel 12 of a 22 panel chain,
the chain split — the primary fed panels 1 to 11 and the backup fed 12 to 22 —
and every panel stayed lit and kept answering, so nothing that just asks "is
this card online" showed a fault at all. Right now the only way I can detect it
is to read the chain through both sender cards and compare.

Whether there's a documented request rate limit on the per-card binary reads,
or better, a bulk read that returns a whole chain in one request. One pass of my
286 panel wall is 286 reads and the controller stops answering after roughly
150 to 200, so a full pass can't complete without stopping partway and waiting.

Those three would let me build what I'm actually after, which is live
connection status for every panel during a show — what's talking, what's on
backup, and where a cable has broken so I can send someone to the right panel
instead of walking the wall.

Thanks again for chasing these down.

Matt
