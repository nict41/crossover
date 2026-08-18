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

**Severity: high, for this specific application. Status: inherent, not fixed.**

Every output is DC-coupled — op-amp, 100 Ω series resistor, volume pot,
terminal block — and there is no muting, no output relay and no power-up
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

**Mitigations, cheapest first.** Power the crossover up *before* the
amplifiers and down *after* them — an outlet strip with a switched
sequence, or just discipline. Better: a muting relay shorting each output
to ground, released a couple of seconds after the rails are up. ESP
publishes designs for exactly this.

---

## 3. A single op-amp failure puts DC into your power amplifiers

**Severity: high impact, low probability. Status: inherent, not fixed.**

There is no DC blocking on the outputs. If an op-amp section fails with
its output stuck at a rail, up to about 14 V DC reaches the volume pot,
and the wiper delivers a fraction of that to the amplifier input. A
DC-coupled power amplifier will faithfully amplify it, and the speaker
will absorb the result until something gives.

Many amplifiers are AC-coupled at the input or have DC protection, in
which case this is a non-event. **Check yours before trusting it.** If
they are DC-coupled and unprotected, add output coupling capacitors
(around 4.7 µF film into a 10 kΩ-or-higher amplifier input keeps the
low-frequency corner well below the LOW band) or rely on the amplifiers'
own speaker protection.

---

## 4. Using this as a preamp means three volume knobs and no master

**Severity: high usability. Status: architectural, worth deciding before
you build.**

The three volume pots are per-band level controls. There is no master
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

Note the taper interacts with this: the fitted pots are **audio (log)**
taper, which is right for a control you sweep to silence and wrong for a
trim you nudge around 0 to −12 dB, where linear gives you far more useful
rotation. If you adopt a master volume, the band pots would be better as
linear.

**Watch the loading if you do add a master volume:** the board's input
impedance is 10 kΩ (`R1`), so a 10 kΩ master pot feeding it would be
loaded hard enough to distort its taper badly. Use 10 kΩ into a buffer, or
raise `R1`.

---

## 5. No bulk supply decoupling on the board

**Severity: moderate. Status: not fixed.**

The board's entire supply decoupling is 4 × 100 nF. The ±15 V arrives
over an umbilical from an off-board supply, and wiring runs about 1 µH per
metre. That inductance and the on-board ceramics form a high-Q resonance
in the low hundreds of kHz, which the op-amps' supply rejection is poor at
by that frequency.

A 47–100 µF electrolytic per rail next to `J1` damps it. This is standard
practice and the board does not do it.

---

## 6. Reverse-polarity or mis-wired supply destroys both op-amps

**Severity: moderate. Status: not fixed.**

`J1` is a 3-way screw terminal, `+15 / GND / −15`. Swap the outer two and
both MC33079s are reverse-biased. There is no series protection diode, no
fuse, and no keying beyond the silkscreen.

`GND` in the middle is a good choice — the most likely single-wire error
is harmless. Two-wire transposition is not. A pair of series diodes costs
about 0.7 V of rail; a polarised connector costs nothing electrically and
is the better answer if you are making the loom yourself.

---

## 7. Output impedance is set by the volume pot, not the op-amp

**Severity: low. Status: by design, worth knowing.**

The op-amp drives through a 100 Ω build-out resistor into the top of a
10 kΩ pot, and the **wiper** feeds the terminal block. Source impedance
therefore peaks around 2.5 kΩ at mid-rotation rather than the few ohms an
op-amp output would give.

Consequences, none fatal: cable capacitance rolls off the top end (about
200 kHz with 3 m of typical cable — fine; keep runs short and it stays
fine); a low-impedance amplifier input loads the pot and shifts its taper
and maximum level (10 kΩ input costs about 1.9 dB); and 2.5 kΩ is high
enough to pick up hum over a long unshielded run. **Use screened cable to
the amplifiers**, and keep it short.

---

## 8. Assembly and mechanical

* **`U1`/`U2` pin-1 orientation is unverified against JLCPCB's convention.**
  The SOIC-14 is a hand-drawn footprint. Check pin 1 in the assembly
  preview before paying — this is the classic way an assembled board comes
  back useless.
* **The pot locating-boss holes are a guess.** No verified dual-gang
  mechanical drawing was found. Check them against your actual pots before
  ordering, or the pots will not sit flat.
* **The pots are board-mount with shafts perpendicular to the PCB**, so
  the board mounts *parallel to and behind* the front panel, with shafts
  through it. If you were planning to mount the board horizontally on the
  chassis floor, you need right-angle pots instead and the footprint
  changes.
* **`C569866` (33 nF) had 2001 in stock** and the board uses 6. That is
  about 330 boards' worth — fine for you, but check before a batch.
* **`C46550416` (10 µF) did not come back from an LCSC search** even
  though its part page resolves. Confirm it is still orderable.

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
