# Design review — what could go wrong, in the fab and in use

Written by working through the board and the circuit looking for failure
modes, not by running the checkers. Everything the checkers cover is
already covered; this is the set of problems that pass every automated
test and still bite you.

Ordered by **what it costs you if it happens**, not by how likely it is.

---

## 1. Bypass capacitors are 37–63 mm from the pins they decouple

**Severity: high. Status: FIXED.** Now 4.2–10.0 mm, against the
hand-placed through-hole board's 11 mm. Kept here because the *reason* it
happened is the durable lesson, and because it took a 24x speedup to make
the fix affordable.

`C5`–`C8` are the 100 nF supply bypasses, one per rail per op-amp. They
are 37, 38, 40 and 63 mm from the supply pins they belong to. A bypass
cap's entire job is to supply transient current through as little loop
inductance as possible; a centimetre of trace is already about 10 nH, so
at 60 mm the capacitor is decoupling nothing that matters. The op-amps are
effectively running with no local supply bypassing at all.

**Why it happened, and why no check caught it.** The placement search
optimises courtyard overlap, pin escape, congestion, wirelength and board
size. A bypass cap is invisible to every one of those: it is two pads on
`+15V`/`-15V` and `GND`, nets which already span the whole board, so
moving it barely changes any half-perimeter, and it is far too small for
area or congestion to notice. Nothing was broken — the optimiser did
exactly what it was told, and nobody had told it about decoupling. The
*hand-placed* through-hole board gets this right at 11 mm, by eye.

**How it was fixed, and why that took two attempts.** The placement search
gains a pin-to-pin proximity term (`BYPASS_NEAR`). On the first attempt it
produced a correct placement that **would not route** — 0 clean boards out
of 208 seed and route-order combinations, and 0 of 40 more after
slackening size pressure to give the router room, which made things
*worse* because a bigger board means longer nets.

That was a budget problem disguised as an engineering one. Each attempt
cost ~30 s, almost all of it in the Python placement anneal, so 208
attempts was as far as the budget stretched. Compiling the anneal took a
trial to ~2.5 s; the next search found a clean board in 80 trials. The
constraint had been right all along and the search had simply been too
expensive to run far enough.

---

## 2. Turn-on and turn-off thumps go straight to your tweeters

**Severity: high, for this specific application. Status: FIXED on the
board.** Every output now has a JFET mute (`Q1`-`Q3`) with a power-up soft
start (`R40`/`C16`, ~2 s) and a fast re-mute as the rails collapse (`D4`),
plus 10 µF DC blocking (`C13`-`C15`) and bleeders (`R28`-`R30`). See
[`circuit-notes.md`](circuit-notes.md#muting-and-soft-start). The text
below is what the board looked like before, and why it mattered.

Every output was DC-coupled — op-amp, 100 Ω series resistor, volume pot,
terminal block — with no muting, no output relay and no power-up
delay. When the ±15 V supply comes up, the filter sections settle through
whatever transient their RC networks dictate, and that transient appears
at all three outputs at once.

An active crossover is worse than a normal preamp here, because **the HIGH
output feeds the amplifier driving your tweeters**. A tweeter has no
mechanical or thermal headroom for a large step, and it is the driver
least able to survive one.

It is also asymmetric in a way that catches people out: supply *collapse*
at switch-off is usually messier than switch-on, because the two rails
rarely decay together.

**What a blocking capacitor would and would not do.** The output DC
blocking caps under §3 are now fitted, and they do not stop a thump — the
transient edge passes straight through them — they only stop the
*sustained* offset that follows one. Muting is the fix for thumps; a
capacitor is the fix for faults. Do not read §3 being fixed as this one
being fixed.

**What it deliberately does not do.** A muting relay wants a relay, a
timing circuit, a flyback diode and a transistor per channel, and it needs
to hold the mute until the rails are *stable*, which means sensing them.
That is a power-supply-side function, not a filter-side one, and putting it
here would roughly double the part count of a board already at the limit of
what its router can place. It belongs on the PSU board or as a dedicated
module — ESP's P33 is exactly this, and any "speaker protection / soft
start" module does the job.

**If you build nothing extra:** power the crossover up *before* the
amplifiers and down *after* them. A switched outlet strip enforces it; so
does habit, until the one time it doesn't.

---

## 3. A single op-amp failure puts DC into your power amplifiers

**Severity: high impact, low probability. Status: FIXED and shipped.**

There was no DC blocking on the outputs. If an op-amp section failed with
its output stuck at a rail, up to about 14 V DC reached the volume pot and
the wiper delivered a fraction of that to the amplifier input — which a
DC-coupled power amplifier would faithfully amplify until the speaker gave
up.

`C11`–`C13` are 10 µF in series with each output, on the wiper side of the
attenuator. Into a 10 kΩ amplifier input that puts the corner near 1.3 Hz,
far below anything the LOW band carries. They are on by default; set
`OUTPUT_CAPS=0` to build without them.

**Why this was blocked for so long, and what actually unblocked it.** For
several rounds these caps were written and switched off, because the board
would not route with them. Measured, with the placement search made
deterministic first:

| footprints | what | clean boards |
|---|---|---|
| 49 | as shipped then | 1 in 80 |
| 52 | + output blocking caps | **0 in 320** |
| 54 | + bulk decoupling as well | **0 in 320** |
| 52 | output caps shrunk to 1210 ceramic (⅓ the area) | **0 in 160** |

The last row is the one that mattered: making the parts three times
smaller changed nothing, so it was never the area they occupied. Two other
things were tried and are worth not repeating — putting the caps *between*
the filter and the pot (electrically better, since it also keeps DC off
the pot track) splits the `*_PRE` nets, which already cross most of the
board, and made it strictly worse; and giving the board more room made it
worse again, for the reason this repo keeps rediscovering.

The fix was not in the router at all. **Ground stopped being routed.** The
board has always carried a GND pour on every signal layer, but nothing
verified it, so ground was *also* drawn as an ordinary net — the largest net on the
board, and `route_order()` sends supply rails first, so it claimed the
best channels before a single signal net got a turn. The board was paying
for ground twice and spending its best routing resource on the payment.

`pour_connectivity()` now proves the plane is one piece of copper that
reaches every ground pad, modelled conservatively from the router's own
occupancy and eroded to the pour's minimum width so a hairline neck cannot
count as a connection. That check is what makes leaning on the plane safe.
GND is then routed only where the pour genuinely cannot squeeze in — a
handful of short stubs into the plane, instead of a tree spanning the
board. With the channels that freed up, 54 footprints route clean, and
both this fix and §5 are in the shipped board.

The general lesson is worth more than the fix: **when adding a correct
circuit change is repeatedly impossible, suspect what the board is already
spending its resources on, not the size of the thing you are adding.**

---

## 4. Using this as a preamp means three volume knobs and no master

**Severity: high usability. Status: FIXED.** `VR6` is a 50 kΩ audio-taper
master, sitting ahead of the input buffer so it sets listening level
without touching the crossover balance; the three band pots became trims.
The reasoning below is why that was the right place to put it.

The three volume pots are per-band level controls. There was no master
volume. To change listening level you have to move three knobs together
and match them, which is not practical to do accurately by ear or by eye —
and any mismatch is a change in crossover balance, not just level.

In stereo it is worse: two boards, **six** single-gang pots, with no
mechanical tracking between left and right. Pot tolerance alone
(typically ±20 %) means the two channels will not match at the same knob
angle.

**The conventional architecture** is a single dual-gang master volume
*ahead* of the crossover input, with the per-band pots set once as trims
and then left alone. That is one knob for listening level, correct stereo
tracking, and the band trims doing the job they are actually good at.

**This cannot be fixed on this board, and that is not a cop-out.** The
board is one channel. A master volume for a stereo pair has to be one
dual-gang pot feeding *both* boards, so it is inherently off-board — put it
between your source and the two `J2` inputs. Anything fitted per-board
would be one gang per channel and would not track, which is the problem
you started with.

Note the taper interacts with this: the fitted pots are **audio (log)**
taper, which is right for a control you sweep to silence and wrong for a
trim you nudge around 0 to −12 dB, where linear gives you far more useful
rotation. If you adopt a master volume, the band pots would be better as
linear.

**The loading objection is gone.** This used to end with a warning: the
board's input impedance was 10 kΩ (`R1`), so a 10 kΩ master pot feeding it
would be loaded hard enough to distort its taper badly, and you had to put
a buffer in between or raise `R1`. `R1` is now **47 kΩ**, the ordinary
line-level figure, so a 10 kΩ dual-gang master pot straight into both
channels' `J2` is fine — the worst-case wiper impedance of 2.5 kΩ into
47 kΩ is about 0.45 dB of error at one point on the travel.

10 kΩ was never a line input; it was a power-amp input, and it also loaded
a passive source or a valve output stage hard enough to lose level and
bass. Raising it costs bias-current offset — 300 nA of MC33079 input
current through 47 kΩ is 14 mV at the buffer output — and that is only
acceptable because §3's blocking capacitors now stop it reaching an
amplifier. At DC the high-pass outputs have no gain, so it only actually
appears at `LOW`. A FET-input op-amp (`OPA1644`, `OPA4134`, `TL074`)
removes it entirely.

---

## 5. No bulk supply decoupling on the board

**Severity: moderate. Status: FIXED and shipped.**

The board's entire supply decoupling was 4 × 100 nF. The ±15 V arrives
over an umbilical from an off-board supply, and wiring runs about 1 µH per
metre; that inductance and the ceramics alone form a high-Q resonance in
the low hundreds of kHz, exactly where an op-amp's supply rejection has
fallen away.

`C9`/`C10` add 10 µF per rail at the connector. That is on the small
side — 47–100 µF is the textbook figure — and the value was chosen to
reuse the 10 µF part already on the board rather than add a BOM line and a
second electrolytic footprint. It moves the resonance down by a factor of
ten and damps it with the electrolytic's own ESR, which is most of the
benefit. On by default; `BULK_CAPS=0` builds without them.

Same history as §3: correct from the day it was written, unfittable until
ground stopped being routed as a net. See §3 for why.

**Put the larger bulk capacitance at the supply end**, where it belongs
and where there is room for it. Decoupling an umbilical wants capacitance
at *both* ends; this board now holds up its end.

---

## 6. Reverse-polarity or mis-wired supply destroys both op-amps

**Severity: moderate. Status: not fixed, deliberately.**

`J1` is a 3-way screw terminal, `+15 / GND / −15`. Swap the outer two and
both MC33079s are reverse-biased. There is no series protection diode, no
fuse, and no keying beyond the silkscreen.

`GND` in the middle is a good choice — the most likely single-wire error
is harmless. Two-wire transposition is not.

Series protection diodes were considered and left out. They would need a
new footprint class and two more parts on a board whose router is already
the binding constraint, they cost about 0.5 V of rail each (Schottky), and
they protect against a mistake that a **polarised connector on the loom
prevents outright, for nothing**. If you are making the umbilical yourself,
key it. If you want the belt as well as the braces, `SS14` in SMA is the
part (LCSC `C55127344`), one in each rail, cathode toward the board on
+15 V and anode toward the board on −15 V.

---

## 7. Output impedance is set by the volume pot, not the op-amp

**Severity: low. Status: FIXED.**

The op-amp used to drive through a 100 Ω build-out resistor into the top of
a 10 kΩ pot, with the **wiper** feeding the terminal block, so the source
impedance peaked around 2.5 kΩ at mid-rotation rather than the few
milliohms an op-amp output gives.

Two things followed from that, and the second is the one that actually
mattered. Cable capacitance rolled off the top end — about 200 kHz with 3 m
of typical cable, which is fine. But a low-impedance amplifier input sat
across the lower half of the pot track, so it both lost level (about 1.9 dB
into 10 kΩ) and **changed the taper by an amount that depended on which
amplifier was plugged in**. Tolerable for a crossover fed from a preamp;
wrong for something used *as* one.

`U3` is a third quad op-amp: one unity-gain follower per band, between the
wiper and the terminal block. The output impedance is now the op-amp's, the
pot is unloaded so its taper is the taper it was bought with, and 2.5 kΩ of
source impedance is no longer sitting on a cable run picking up hum.
`R25`–`R27` are the buffers' build-out resistors; `R9`/`R19`/`R22` stay
where they were, doing the same job for the filter op-amp that drives the
pot. `U3D` is the spare section, input grounded and output tied back —
an unused op-amp section left floating oscillates and couples that into
the three sharing its supply pins.

Order along the chain is deliberate: pot, buffer, build-out, **blocking
capacitor**, terminal. The capacitor stays last so it still blocks a
failure of the last active device in the path. Ahead of the buffer it
would keep DC off the pot track — worth something, since DC through a
wiper is what makes a volume control crackle as it wears — at the cost of
leaving a failed `U3` section wired straight to a power amplifier, which is
worth much more.

`R28`–`R30` (100 kΩ, output to ground, after the capacitor) are the other
half of this. The coupling capacitor's outer plate otherwise has a DC path
only through whatever is plugged into the terminal block, so a board left
with nothing connected drifts on leakage and thumps into the amplifier the
moment one is connected.

**Use screened cable to the amplifiers** anyway. It is no longer needed to
keep hum out of a 2.5 kΩ source, but it is still an unbalanced line-level
interconnect.

---

## 8. Assembly and mechanical

* **`U1`/`U2`/`U3` pin-1 orientation is unverified against JLCPCB's
  convention.** The SOIC-14 is a hand-drawn footprint. Check pin 1 in the
  assembly preview before paying — this is the classic way an assembled
  board comes back useless. The same goes for the polarity of `D1`–`D4`
  and the seven electrolytics.
* **The pots have no locating-boss holes.** There is no verified dual-gang
  mechanical drawing to place them from, and shipping a hole in the wrong
  place is a re-order where a missing one is a hand drill. Check your parts:
  if yours have bosses, drill for them before soldering, or the pots will
  not sit flat.
* **The controls are right-angle parts on the front edge**, so the board
  lies **flat in the enclosure, perpendicular to the panel**, with shafts
  and plungers coming out of the board's edge and through the panel. (This
  entry used to say the opposite — that the shafts stand perpendicular to
  the PCB and the board mounts behind the panel — which stopped being true
  when the controls became edge-mount parts and nothing caught it. The
  hole positions are in
  [`panel-drilling.md`](panel-drilling.md), generated from the placement.)
* **The four mounting holes are no longer at fixed corners.** They start
  3.56 mm in and move as far as they must to clear the pinned edge rows,
  so the set is not symmetric. Drill from `panel-drilling.md`, not from
  the corners. **Status: FIXED** — they used to sit 0.69 mm from the board
  edge, which passes every fab limit and cracks when the screw is
  tightened.
* **`C569866` (33 nF) has 2001 in stock** and the board uses 6. That is
  about 330 boards' worth — fine for one build, worth checking before a
  batch. It is the thinnest line in the BOM.
* **A dead part number is now a build failure, not a note.** `C46550416`
  (10 µF) covered seven designators and returned *zero* LCSC search
  results — an end-of-lifed number, not a temporary stock-out — while
  every geometric check passed, because nothing about the copper was
  wrong. Replaced with `C2858858` (KNSCHA 10 µF 50 V, same D5×L5.4 mm
  package, 50 000 in stock), and `validate_fab.py --online` now *fails* on
  a part it cannot find rather than printing a note under thirty lines of
  healthy stock figures.

---

## What is *not* a problem

Worth saying explicitly, because these look alarming and are not:

* **Trace widths.** 12 mil signal and 16 mil power carry 13–21× the
  worst-case current by IPC-2221. They were chosen for pad proportion, not
  capacity.
* **Two 33 nF in parallel** for each tuning capacitor. Deliberate: it
  keeps every tuning cap the same part from the same reel, and matching
  matters far more here than absolute value.
* **Per-output grounds.** Each output terminal carries its own ground
  return precisely so three amplifiers do not share one and form a hum
  loop.
* **Same-net vias 0.43 mm apart** — fixed, and it was a drilling limit
  rather than an electrical one.
