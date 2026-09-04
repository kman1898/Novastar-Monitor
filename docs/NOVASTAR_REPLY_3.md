Hi Lovis,

Thank you — and please thank R&D for me. That's the three things I was stuck
on, all in one message.

The R0155 document is the one I most needed. The reply my H15 sends is exactly
what's in it, so I had the right hardware and the wrong paperwork the whole
time. I'd worked the scaling out by comparing readings against what NovaLCT
displays for the same cards, and I got 0.01 degrees and 0.01 volts, but I was
guessing and I knew it. Your worked examples — 4200 for 42 degrees, 480 for
4.8 V — are now written into my tests, so if anyone ever changes that code the
tests fail rather than the dashboard quietly showing nonsense. It showed
1850 degrees once, when I applied the old document's scaling to this reply.

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

The port 7000 firmware sounds like the right fix. Reading one register per
panel is 286 requests for a single pass of my wall, against a limit of about
150 to 200, so it can never finish in one go no matter how I pace it — that's
been the hardest constraint in this whole project. A few practical questions:

- How do I get the customized firmware, and is it something I can run on a
  production H15?
- Will the port 7000 frame format be documented? I can work from captures, but
  I've already had to correct myself several times doing that and I'd rather
  follow a specification.
- When V2.3.0.0 ships, does the batch read come with it as standard, or does it
  stay a separate customized build?

I'm not in a rush on that one. Knowing it's coming means I can stop building
workarounds for the rate limit, which is worth a lot on its own.

Everything I'm building here is read only, and it's for one purpose: knowing
during a show which panels are talking, which chains are running on their
backup, and where a cable has broken, so I can send someone to the right panel
instead of walking the wall. R0102 is the first thing anyone has pointed me at
that answers the middle one directly.

Thanks again,
Matt
