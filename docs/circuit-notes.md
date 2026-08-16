# Circuit notes — ESP Project 148, 3-way state variable crossover

Source: Rod Elliott, Elliott Sound Products, **Project 148 — State Variable
Electronic Crossover**, <https://sound-au.com/project148.htm> (Figure 2, the
3-way version). The article text and Figure 2 are © Rod Elliott; this repository
contains only a re-drawn schematic and the notes needed to build it.

## What the circuit is

Two cascaded 2nd-order **state variable** filters give a 3-way, 12 dB/octave
crossover whose crossover frequencies are continuously variable by turning a
pot. Q is set by one resistor per filter, so the alignment can be moved between
Linkwitz-Riley (Q = 0.5, as drawn) and Butterworth (Q = 0.707).

```
                +---- HIGH  (HP of filter 1)
                |
INPUT -> U1A -> filter 1 (VR1, 680 Hz - 4.8 kHz)
 buffer          |
                 +-- LP1 -> filter 2 (VR2, 68 Hz - 480 Hz) -+-- HP2 -> MID
                                                            |
                                                            +-- LP2 -> U4B -> LOW
                                                                  (inverter)
```

## Stage-by-stage

| Stage | Devices | Function |
|---|---|---|
| Input buffer | C0, R1, U1A | AC couples the input and presents a low source impedance to the filter. Figure 1 of the article assumes a low-impedance source; Figure 2 adds this buffer. |
| Filter 1 summing amp | U1B, R2, R3, R4, R5, R6 | Produces the **high-pass** output (HIGH). |
| Filter 1 integrator 1 | VR1A, R7, C1, U2A | Bandpass output — used only as feedback, not brought out. |
| Filter 1 integrator 2 | VR1B, R8, C2, U2B | **Low-pass** output (LP1), which feeds filter 2. |
| Filter 2 summing amp | U3A, R12, R13, R14, R15, R16 | Produces the **mid** output (MID). |
| Filter 2 integrator 1 | VR2A, R17, C3, U3B | Bandpass — feedback only. |
| Filter 2 integrator 2 | VR2B, R18, C4, U4A | Low-pass output (LP2). |
| Output inverter | R20, R21, U4B | Inverts LP2 to give **LOW** in phase with MID. |
| Test points | R10/R11 → TP1, R23/R24 → TP2 | Sum HP and LP of each filter; the sum nulls at the crossover frequency. Optional. |

### Why the inverter is on the bass output

Any 12 dB/octave crossover has one output 180° out of phase with the other, so
one has to be inverted. You would expect the inverter on the midrange, but the
input amplifier of the second filter (U3A) is itself inverting, which already
corrects the mid. The remaining inversion is therefore needed on the low output,
hence U4B. This matches the article.

### The three feedback paths into each summing amp

Per the article, the summing amp has three feedback paths — one local, one from
the first integrator, one from the final integrator:

| Path | Filter 1 | Filter 2 | Goes to |
|---|---|---|---|
| Input | R2 | R15 | inverting input |
| Local negative feedback | R5 | R16 | inverting input |
| From final integrator (LP) | R6 | R12 | inverting input |
| From first integrator (BP) | R4 | R14 | non-inverting input |
| Q setting, to ground | R3 | R13 | non-inverting input |

With the non-inverting input fed from the bandpass output through Rq and
loaded to ground by Rg, and the three equal resistors R on the inverting node:

```
Q = (Rq + Rg) / (3 * Rg)
```

* Rg = 11k2 → Q = 0.500 (exact Linkwitz-Riley)
* Rg = 12k  → Q = 0.489 (as drawn; passband ripple < 0.2 dB)
* Rg = 5k04 → Q = 0.704 (Butterworth; 3 dB peak in the summed response)

## Frequency setting

Crossover frequency is set by the series combination of the pot gang and its
3.3 k series resistor, against the integrator capacitor:

```
f = 1 / (2 * pi * R * C)
```

### Stock ESP values

| Filter | C | R range | Frequency range |
|---|---|---|---|
| Filter 1 (High/Mid) | C1, C2 = 10 nF | 3.3 k … 23.3 k | 683 Hz … 4.82 kHz |
| Filter 2 (Mid/Low) | C3, C4 = 100 nF | 3.3 k … 23.3 k | 68.3 Hz … 482 Hz |

Both gangs of a pot must track; both capacitors in a filter must be the same
value, as must both series resistors.

### Retuning to a different range

Two things are being chosen at once, and it's worth separating them:

* The **series resistor** sets the top of the range (pot at minimum), *and*
  fixes the max/min **ratio** at `(Rs + Rpot) / Rs`.
* The **capacitor** then slides the whole range up or down without changing
  that ratio.

So pick Rs from the ratio you want, then C from the frequency you want:

```
Rs   = Rpot / (ratio - 1)
C    = 1 / (2 * pi * Rs * f_max)
```

With the stock 3.3 k and a 20 k pot the ratio is 7.06 — a very wide sweep. The
`retuned` variant in this repository narrows both filters:

| | Rs (R7/R8, R17/R18) | C | Range | Ratio |
|---|---|---|---|---|
| Filter 1, stock | 3.3 k | 10 nF | 683 Hz … 4.82 kHz | 7.06 |
| Filter 1, retuned | 4.7 k | 33 nF | 195 Hz … 1.03 kHz | 5.26 |
| Filter 2, stock | 3.3 k | 100 nF | 68.3 Hz … 482 Hz | 7.06 |
| Filter 2, retuned | 13 k | 68 nF | 70.9 Hz … 180 Hz | 2.54 |

Eight components change in total (R7, R8, C1, C2, R17, R18, C3, C4) and nothing
else — Q, topology, op-amps and pots are untouched, because the Q network and
the frequency network are independent.

Raising the series resistors also *reduces* loading on the preceding stage, so
this direction is always safe. The article's warning is about the opposite:
don't go much below 2.2 k.

The retuned ranges don't overlap: filter 2 stops at 180 Hz, filter 1 starts at
195 Hz. Note that this does **not** stop you setting the two crossover points
too close together — see the next section, which is the thing that actually
matters.

Other ranges from the article: 4.7 nF in filter 1 with the stock 3.3 k gives
roughly 1.45 kHz … 10 kHz; dropping the series resistors to 2.2 k widens the
sweep.

## Setting the two points too close together

![Crossover tuning ranges](crossover-ranges.png)

Because the two filters are cascaded and tuned independently, nothing stops you
setting the Mid/Low point *above* the High/Mid point. It's worth being precise
about what happens, because the intuitive answer — "the bands swap over" — is
wrong.

The outputs never swap. Filter 1 has no knowledge of filter 2, so **HIGH is
unaffected** whatever VR2 does. What suffers is the midrange:

> MID is the high-pass output of filter 2, taken from a signal that filter 1 has
> already low-passed. Its passband is the gap between the two corner
> frequencies. Close that gap and the two 12 dB/octave roll-offs start to
> overlap, so the mid band gets thinner and quieter; invert the order and there
> is no passband left at all — only the region where both slopes are falling.

Because MID is the term that reconstructs the middle of the audio band, the
summed response (HIGH + MID + LOW) develops a broad suck-out at the same time.

| Separation (High/Mid : Mid/Low) | Worst error in the summed response | MID band peaks at |
|---|---|---|
| 16 : 1 | −0.06 dB | −1.1 dB |
| 8 : 1 | −0.20 dB | −2.0 dB |
| 4 : 1 (2 octaves) | −0.62 dB | −3.9 dB |
| 3 : 1 (1.5 octaves) | −0.96 dB | −5.0 dB |
| 2 : 1 (1 octave) | −1.69 dB | −7.0 dB |
| 1 : 1 (coincident) | −3.90 dB | −12.0 dB |
| 1 : 1.3 (inverted, worst of the retuned ranges) | −5.18 dB | −14.6 dB |

Three things to take from this:

* **The degradation is gradual and starts well before the points cross.** At a
  1.6 : 1 separation — still correctly ordered — the sum is already 2.3 dB down
  and the mid band is 8 dB below the others. There is no cliff edge at 1 : 1;
  crossing over is just further along the same curve.
* **Making the ranges non-overlapping does not fix this**, which is worth being
  clear about because it is the obvious thing to try. Two ranges that merely
  abut still meet at the boundary, so the pots can still be set ~1 : 1 apart:

  | Range layout | Closest possible setting | Sum error there |
  |---|---|---|
  | Stock ESP P148 (68–482 / 683–4820, no overlap) | 1.42 : 1 | −2.62 dB |
  | Overlapping (61–257 / 195–1026) | 0.76 : 1 | −5.17 dB |
  | Retuned, non-overlapping (71–180 / 195–1026) | 1.08 : 1 | −3.57 dB |

  Removing the overlap buys about 1.6 dB in the worst case. It does not make
  a bad setting unreachable — and note that **Rod Elliott's own values have the
  same property**, at 1.42 : 1. Nothing in this topology enforces separation;
  the two filters are tuned independently and neither knows about the other.
* **Nothing is damaged or unstable.** No oscillation, no clipping, no latch-up.
  It is simply a filter setting that produces a poor response, and it undoes
  itself the moment you turn VR2 back down.

Practical rule: **keep the two crossover points at least 3 : 1 apart** (about
1.5 octaves) to stay inside 1 dB, and prefer 4 : 1 or more. This is a setting
discipline, not something the hardware can guarantee. If you did want the
hardware to enforce it, the only lever is to give up range — with mid/low
topping out at 180 Hz, high/mid would have to start at 540 Hz for 3 : 1, or
360 Hz for 2 : 1. That is a real cost, and for a test instrument whose whole
point is a free sweep it isn't obviously worth paying. Use TP1 and TP2 to set
the two frequencies accurately, and keep them apart.

## Where accuracy actually pays

The circuit is already lean — 40 years of iteration by its author — but the
build effort is easy to spend in the wrong places. What matters, in order:

**Matching beats absolute accuracy.** Within one filter, the two integrator
capacitors (C1/C2, or C3/C4) and the two series resistors (R7/R8, R17/R18) need
to match *each other*. Their absolute value only sets where the range sits; a
mismatch between them behaves exactly like pot gang mistracking — it moves both
f0 and Q by the square root of the mismatch:

| Mismatch between the two halves | Resulting Q (from 0.5) | Summed response |
|---|---|---|
| 5 % | 0.488 | −0.21 dB |
| 10 % | 0.477 | −0.41 dB |
| 20 % | 0.456 | −0.79 dB |

So: 1 % resistors, and either 2.5 % capacitors or 5 % ones sorted into matched
pairs with a cheap meter. Everything else in the circuit can be 5 % without
anyone noticing.

**Pot tracking matters more than the op-amp.** The same table applies to the
two gangs of VR1 and VR2. A good dual-gang pot buys more measurable performance
than an exotic op-amp does. For the same reason, **don't** substitute log-taper
pots to get a more even frequency scale across the rotation — tempting on a
test instrument, but their gang tracking is far worse than a linear pot's, and
it lands straight on Q.

**The Q resistor is free accuracy.** R3/R13 set Q against the 5.6 k bandpass
feedback resistor:

| R3 / R13 | Q | Worst summed response |
|---|---|---|
| 12k (ESP drawing) | 0.489 | −0.20 dB |
| **11k (E12)** | **0.503** | **+0.05 dB** |
| 11k2 (11k + 200R) | 0.500 | 0.00 dB |
| 11.3k (E96, 1 %) | 0.4985 | −0.03 dB |

11k is a stock value and four times closer than 12k, so the `retuned` variant
uses it. The article makes the same point. (The `stock` variant keeps 12k, to
stay faithful to the published drawing.)

**Diminishing returns below 5.6 k.** The article dropped the feedback network
from 10 k to 5.6 k for thermal noise. Going lower gains little here, because
the largest resistance in the signal path is the 20 k pot plus its series
resistor, not the 5.6 k network.

## Two quad op-amps instead of four duals

The circuit partitions perfectly into two groups of four sections:

| Package | Sections | Filter |
|---|---|---|
| 1 | input buffer, summing amp, integrator 1, integrator 2 | Filter 1 |
| 2 | summing amp, integrator 1, integrator 2, output inverter | Filter 2 |

So two quad op-amps replace four duals: half the ICs, half the bypass caps
(4 instead of 8), and a smaller board — with no change to the circuit itself.
Because the split falls on the filter boundary, the sections that share a
package are the ones already tightly coupled by design, so the extra
inter-section crosstalk of a quad lands where it does no harm.

Suitable parts: **MC33079** (bipolar, low noise, closest in character to the
NE5532), or **OPA1644** / **OPA4134** / **LME49740**. Note the pinout differs —
14-pin, V+ on pin 4 and V− on pin 11 — so the schematic's power pins need
remapping before layout.

## Things not to "simplify"

* **Don't delete the input buffer U1A.** It looks redundant, but it is what
  keeps the source impedance out of the filter: any impedance in series with
  the input resistor changes both the gain and the Q.
* **Don't break the loop for DC.** The integrators have no DC feedback resistor
  of their own — an integrator alone would drift to a rail. What holds them is
  the overall state-variable loop, through the low-pass feedback resistor
  (R6 / R12). It must stay intact.
* **The output inverter U4B can go** if you invert the woofer's polarity at the
  amplifier instead, saving U4B, R20 and R21. It doesn't save a package (seven
  sections still need two quads), and it leaves an unused section that must be
  wired as a grounded unity-gain follower rather than left floating. Rarely
  worth it.
* **The test point networks (R10/R11, R23/R24) can be omitted** — the ESP PCB
  omits them, and they permanently load the outputs. But given how much the
  separation between the two crossover points matters, being able to measure
  each one exactly is worth four resistors.

## Test points

TP1 and TP2 each sum that filter's high-pass and low-pass outputs through two
10 k resistors. At the tuned frequency the two are equal and opposite, so the
signal at the test point nulls. Sweep an oscillator for minimum at TP1 or TP2
and read off the exact crossover frequency. Omit R10/R11 and R23/R24 if you
don't want them — the ESP PCB doesn't have them.

## Power

Not shown on the original drawings. ±15 V rails, brought in on J1. Every dual
op-amp needs a 100 nF ceramic from pin 8 to ground and from pin 4 to ground,
physically close to the IC — C5/C6 (U1), C7/C8 (U2), C9/C10 (U3), C11/C12 (U4).

## Designator mapping to the original figure

The designators follow Figure 2 of the article. Parts that Figure 2 shows
without a designator have been given one here:

| This schematic | In the original Figure 2 |
|---|---|
| R10, R11 | the two unlabelled 10 k resistors feeding TP1 |
| R23, R24 | the two unlabelled 10 k resistors feeding TP2 |
| C5 – C12 | supply bypass caps (text only: "100nF ceramic … at pins 4 and 8") |
| J1 | power input (not shown in the original) |

Values are written in decimal form (5.6k, 3.3k, 11k2 → 11.2k) rather than the
article's 5k6 / 3k3 notation, because that is what EasyEDA and LCSC expect.

## Performance, per the article

* Rolloff 12 dB/octave, summed response flat within 0.5 dB DC–100 kHz.
* Distortion well below 0.01 % at sensible levels; above ~3.5 V RMS the op-amps
  start to clip.
* Overall phase goes from about +180° at 20 Hz to −180° at 20 kHz, which is
  normal for any 3-way 12 dB/octave Linkwitz-Riley filter.
* The circuit is mono. Build two for stereo; the frequency pots of the two
  channels have to be set independently unless you can find 4-gang pots.
