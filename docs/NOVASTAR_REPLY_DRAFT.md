Hi,

Thanks for sending the control protocol, and for putting the rest to R&D.

I should say I already use that document — it's what our Bitfocus Companion
setup drives the H series with, so I've had V1.0.20 for a while. Reading it
against my monitoring code was still worth doing, because it caught two real
mistakes on my side.

Voltage first. Section 4.3.4 says the lower 7 bits of "voltage" are the value in
units of 0.1V, and 172 means 4.4V. I had been using the whole byte times 0.03,
so I was reading about 5.1V where your formula gives 4.2V. That also cleared up
something that had been bothering me for a while: my binary reads and my R0155
reads disagreed by roughly 0.9V on the same card, and with your formula they
agree. Fixed, and I've pinned your worked example in my tests so it can't drift
back.

Second, the power flags. You document power0Status and power1Status as
0: Fault, 1: Normal, and I had them backwards. But reading them your way gives
me something I don't believe: of the 36 cards I have readings for, 21 report 0
on both while the wall is lit and running normally. You describe both fields as
"backup power supply status", and I don't think our panels have a second supply
fitted at all — so is 0 simply what a panel with no redundant supply reports?
If so it isn't a fault and I shouldn't show it as one. I've stopped alarming on
it in either direction until I know.

And the one the document doesn't cover, which is the one I'd most like
clarified. My H15 on V2.0.0.6 doesn't return the R0155 reply you document. I
get workStatus, tempStatus, temp as 3700, tempMax, voltStatus, volt as 440,
mcuVersion and fpgaVersion — temperature in hundredths of a degree rather than
half degrees, voltage in hundredths of a volt rather than the lower 7 bits.
That reply isn't in V1.0.18 or V1.0.20. Which firmware introduced it, and is
there a document for it? Right now I work out which reply I'm looking at from
which keys came back, which works but is a guess, and getting it wrong once had
my dashboard showing 1850°C.

On the SNMP side — you're right, I typed it wrong. It's netportId and I had
ropportId. I also tried netPortId with a capital P, which failed the same way.
So please ignore my item 2 about the per-receiving-card OIDs returning
BizIdError. I'll retest with {"outputSlotId": 4, "netportId": 0,
"recvCardId": 0} once my wall is back on the network and let you know either
way.

Two smaller SNMP corrections while I'm at it. I complained that fan speed and
PSU voltage always read 0 — your doc doesn't define speed or voltage fields at
all, so the device is sending those as extras. Are they meant to work, or
should I ignore them? And I'd assumed 0 meant OK on every status field, when
your table shows it varies by OID. My mistake, fixed.

One SNMP thing I'd still like confirmed: .1.17 is documented as 0: Not
connected / 1: Connected. My H15 reports four entries all status 0 while
driving a 286 panel wall, and my H2 on V2.2.0.0 reports one entry, status 0.
Both were powered and answering me over the network at the time, so "not
connected" can't be true of a supply that's running the device. I think 0 means
normal there, same as the fan OID. There's also an iSignal field reading 1 that
I can't find in your example.

And two I checked on both units, in case they're useful: .200.2, the documented
trap switch, doesn't exist on either, while .200.3 does and returns 0. And
.30.3 comes back as {"SN":"","netPortCount":0,"status":1,"version":"0"} on both
— blank serial, zero ports. SN is the only way I can tell which physical card
the .30 subtree is describing.

Let me also be clearer about what I'm building, because I don't think my first
message said it well. It isn't really a temperature dashboard. What I need
during a show is live connection status for every panel: which ones are talking
to me right now, which chains have failed over to the backup sender card, and
where a cable has broken so I can send someone to the right panel instead of
walking the wall. Temperature and voltage matter, but they're the easy part —
the connectivity is what actually saves a show, and it's the part I'm having to
work hardest for.

The rest of this is what I've learned since my first message. Everything below
about the binary protocol came from watching NovaLCT talk to my own hardware in
Wireshark — I captured its monitoring passes, worked out the frames it sends,
and reproduced them. Happy to send those captures.


THE CONTROLLER STOPS ANSWERING AFTER ROUGHLY 150 TO 200 PER-CARD READS

Here's exactly what I send, so you can tell me if I'm going about it wrong. One
TCP connection to port 5201, one card per frame. Card 0 on chain 3 of sender
card 0:

    55 aa 00 01 fe 00 01 03 00 00 00 00 00 00 00 0a 52 00 b4 56

55 AA header, sequence, FE, then byte 5 = sender card, byte 6 = 01 for a
per-card read, byte 7 = chain, byte 8 = card position. Register 0x0000000A,
length 0x5200 (82 bytes), checksum. Card 21 is the same frame with byte 8 = 15
hex. I leave 150 ms between reads and wait up to 1.5 s for a reply. In the 82
byte reply, byte 0 is presence — 0x80 means a card is there, past the end of a
chain the 0x40 bit is set — so I walk a chain by counting byte 8 up until the
controller says there's nothing at the next position.

I assumed the limit was my own bug for a long time. A chain I know has 22
panels came back as 7, then 9, then 20, then 22 across runs of the same code
against the same wall. The only difference was how much reading happened before
it. After checking my framing and timing, I tried just waiting: leave the
controller alone about 45 seconds, read that one chain by itself, and I get 22
every time, with position 22 answering a proper "no card here". Read it at the
end of a full sweep and I get a short count.

The hard part is that it doesn't return an error or a busy response, it just
stops replying. A card that's there but unanswered looks exactly like a card
that isn't there, so a short chain reads as a real short chain. I now re-read
anything that ended in silence, one chain at a time from a rested controller,
which recovers the missing panels but turns reading one wall into a several
minute job.

For what it's worth, 220 of the same reads against the H2 with no panels
connected all answered — so it may be tied to actually reaching a receiving
card rather than to request count. The H2 is a different model on different
firmware though, so I can't rule out that it just doesn't have the limit.

Is there a documented rate limit, or better, a bulk read that returns a whole
chain at once? One pass of my 286 panel wall is 286 reads against a limit of
150 to 200, so it can't finish inside one budget no matter how I pace it.


BYTE 12 OF THE LIVE MONITORING REPLY

Your document confirmed byte 1 and byte 3 of that reply for me — raw / 2 for
temperature and the lower 7 bits times 0.1 for voltage, same as R0155. Byte 12
is the one I still can't place. It's 0x01 on 162 of my cards and 0x0B on the
other 124. The only encoding I have lists 1 for primary and 2 for backup, so
I'd been treating anything else as disconnected — which had my dashboard
calling 124 lit, healthy panels disconnected. They're whole chains, not
scattered cards. What is 0x0B?


A BREAK THAT LOOKS LIKE NOTHING IS WRONG — THE ONE I MOST WANT SOLVED

I unplugged the cable at panel 12 of a 22 panel chain deliberately, to see what
a break looks like on the wire. Read through the primary sender card, panels 1
to 11 answered and 12 to 22 didn't. Read through the backup sender card, the
exact opposite. The chain had split at the break, each sender card feeding one
half. Every panel stayed lit and still answered something, so nothing that asks
"is this card online" showed a fault at all.

The only trace was a bit error count on the half the backup was carrying, zero
on the primary's. That comes from register 0x4A010002, same frame shape, three
byte reply:

    55 aa 00 03 fe 00 01 03 00 00 00 00 4a 01 00 02 03 00 aa 56

Byte 0 is presence again (05 means a card answered), bytes 1 and 2 a 16 bit
count, low byte first. I'd like that register confirmed — it's the only
continuous signal I've found that shows a link degrading before anything goes
dark.

Is there a field that tells me which sender card is currently feeding a given
receiving card, or whether a chain has failed over? Right now I can only tell
by probing both sender cards and comparing, which doubles my traffic on a
protocol that's already rate limited.


WHAT I'D ACTUALLY PREFER

Everything above about the binary protocol exists only because I couldn't get
per-panel connection state any other way. I'd much rather do all of it over
SNMP.

SNMP is read only, with no session and no keepalive, so there's nothing left
behind on the device and nothing that can claim the controller. Your agent
answers several managers at once, so my dashboard sits alongside Companion and
NovaLCT without arguing with them. That's what a monitoring tool should be —
something that can't take the desk away from the operator mid show.

If netportId fixes item 2, .30.6 and .30.7.x get me the per-card readings.
Three things would let me drop the binary path completely, and the first two
matter more to me than the readings do:

Which sender card is currently feeding each receiving card, or at least whether
a chain has failed over. Without this I can't tell a healthy wall from one
that's already lost its primary path and is running with no redundancy left.

Something that shows a link degrading before it drops — the bit error counter
or an equivalent. I can't find an SNMP version of it.

Per-card data at a rate that lets me poll the whole wall. A SET to select a
card then a read back is a round trip per card, which on 286 panels is no
better than what I do now. Anything returning a whole chain, or all of one
card's fields at once, solves it.

If those are reachable over SNMP I'll drop the binary code gladly. If they
aren't going to be, I'd rather know plainly so I can stop trying and build
carefully around the binary protocol instead.


Everything else in my original list still stands. The one I'd most like an
answer on is still W0120. I originally put it down to losing control from
Companion mid show while my tool was sending that heartbeat, but I should be
straight with you: I changed two things at once, and the silent period above
would look the same from the control side, so I can't say W0120 caused it. The
questions stand either way. What does W0120 do, does it claim exclusive
control, and is there a supported way for a read only client to identify itself
as an observer so it can never take control from something else? That last one
is what I really need.

Happy to send captures for any of it.

Thanks,
Matt
