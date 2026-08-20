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

## Function blocks on the SMD board

The board carries a good deal more than the ESP figure does, and the
designators moved when the four duals became two quads, so this is the map:
**every part on the board, grouped by the job it does.** It is written
against the `retuned-quad-smd` variant, which is the one in `pcb/`.

Signal path, end to end:

```
J2 -> VR6 -> C0/R1/U1A -> filter 1 -+-> HIGH_PRE -> VR3 -> R31 -+-> U3A -> R25 -> C13 -> J3
   in   master   input buffer       |                          |  buffer  DC block  HIGH
        volume                      |                          Q1 (mute)
                                    |
                                    +-> LP1 -> filter 2 -+-> MID_PRE -> VR4 -> R32 -+-> U3B -> R26 -> C14 -> J4
                                                         |                          Q2 (mute)
                                                         +-> LOW_PRE -> VR5 -> R33 -+-> U3C -> R27 -> C15 -> J5
```

### Input and master volume

| Part | Value | Job |
|---|---|---|
| `J2` | 2-pin terminal block | Line input |
| `VR6` | 50 kΩ log | Master volume, ahead of everything |
| `C0` | 10 µF | DC block into the buffer |
| `R1` | 47 kΩ | Input bias return; sets input impedance |
| `U1A` | ¼ MC33079 | Unity-gain input buffer |

`VR6` sits **before** the buffer, not after it, so the master control also
sets how hard the filter is driven — turn it down and the whole chain gets
quieter and cleaner together. `R1` at 47 kΩ is high enough not to load a
typical source; it was 10 kΩ until this round.

### Crossover filter 1 — the HIGH / MID split

The whole of `U1` plus its passives. `VR1` (dual-gang 20 kΩ, panel) sets the
frequency; `R3` sets Q.

| Part | Value | Job |
|---|---|---|
| `U1B`, `R2`, `R4`, `R5`, `R6` | 5.6 kΩ each | Summing amp — its output **is** the high-pass, `HIGH_PRE` |
| `R3` | 11 kΩ | Q = (5.6k + Rq)/(3·Rq) = 0.503, near Linkwitz-Riley |
| `U1C`, `VR1A`, `R7`, `C1` | 4.7 kΩ, 33 nF | Integrator 1 — bandpass, used only as feedback |
| `U1D`, `VR1B`, `R8`, `C2` | 4.7 kΩ, 33 nF | Integrator 2 — low-pass out (`LP1`), feeds filter 2 |
| `R9` | 100 Ω | Build-out from the filter into the volume pot |
| `R10`, `R11`, `TP1` | 10 kΩ | Sums HP and LP; nulls at the crossover point |

Range 195 Hz – 1.03 kHz, printed on the silkscreen next to `VR1`.

### Crossover filter 2 — the MID / LOW split

The whole of `U2`, fed from `LP1`. Same topology, different values.

| Part | Value | Job |
|---|---|---|
| `U2A`, `R12`, `R14`, `R15`, `R16` | 5.6 kΩ each | Summing amp — high-pass out, which is `MID` |
| `R13` | 11 kΩ | Q |
| `U2B`, `VR2A`, `R17`, `C3A`+`C3B` | 13 kΩ, 2 × 33 nF | Integrator 1 — feedback only |
| `U2C`, `VR2B`, `R18`, `C4A`+`C4B` | 13 kΩ, 2 × 33 nF | Integrator 2 — low-pass out |
| `U2D`, `R20`, `R21` | 5.6 kΩ | Inverter, on the **bass** output (see below) |
| `R19`, `R22` | 100 Ω | Build-out into the MID and LOW volume pots |
| `R23`, `R24`, `TP2` | 10 kΩ | Second null test point |

Range 73 – 186 Hz. The tuning caps are two 33 nF in parallel rather than one
68 nF — see [`pcb-notes-smd.md`](pcb-notes-smd.md#why-the-tuning-caps-are-two-33nf-in-parallel).

### Level controls

Four pots on the front edge, all passive attenuators, all audio (log) taper.

| Part | Value | Job |
|---|---|---|
| `VR6` | 50 kΩ log | Master, before the input buffer |
| `VR3` | 10 kΩ log | HIGH band trim |
| `VR4` | 10 kΩ log | MID band trim |
| `VR5` | 10 kΩ log | LOW band trim |

The three band trims sit between each filter's output and its buffer, not at
the terminal block: the filter op-amps are low impedance and drive 10 kΩ
without effect, a divider only ever attenuates so there is no new clipping
risk, and putting the buffer *after* the pot means the output impedance the
amplifier sees is `R25`–`R27` and not the pot's wiper.

### Output buffers and DC blocking

| Part | Value | Job |
|---|---|---|
| `U3A`, `U3B`, `U3C` | ¼ MC33079 | Unity-gain buffer per band |
| `R25`, `R26`, `R27` | 100 Ω | Build-out — isolates the buffer from cable capacitance |
| `C13`, `C14`, `C15` | 10 µF | DC block, so an op-amp offset never reaches an amplifier |
| `R28`, `R29`, `R30` | 100 kΩ | Bleeder — holds the cap's outer plate at 0 V |
| `J3`, `J4`, `J5` | 2-pin terminal blocks | HIGH / MID / LOW out, each with its **own** ground return |
| `U3D` | ¼ MC33079 | Spare section: input grounded, output tied back |

Each output has its own ground pin rather than sharing one, because each
runs to a separate power amplifier and a shared return is a hum loop. The
bleeders cost 0.16 Hz of corner frequency against the 10 µF and stop the
board thumping when something is plugged into an output that has been
sitting idle.

`U3D` is grounded rather than left floating: an unused op-amp section with
floating inputs is a comparator with a metre of stray capacitance on it, and
it will oscillate into the three sections sharing its supply pins.

### Muting and soft start

| Part | Value | Job |
|---|---|---|
| `Q1`, `Q2`, `Q3` | MMBFJ111 JFET | Shunts one buffer input to ground = that band muted |
| `R31`, `R32`, `R33` | 10 kΩ | Series resistor the JFET shunts against |
| `R34`, `R35`, `R36` | 1 MΩ | Gate pulldown to −15 V — this is what **un**mutes |
| `D1`, `D2`, `D3` | 1N4148W | Steering, so one button pulls only its own gate |
| `R40`, `C16` | 1 MΩ, 10 µF | Soft-start RC on `MUTE_SS`, ≈ 2 s |
| `D4` | 1N4148W | Fast re-mute when the −15 V rail collapses |
| `R37`, `R38`, `R39` | 2.2 kΩ | Panel LED current, one per band |
| `SW1`, `SW2`, `SW3` | PS-22E05 | The mute buttons themselves, on the board in the panel row |
| `J6` | 4-pin header | Optional LED loom: `LD1`–`LD3` + GND |

A J111 is a **depletion-mode** JFET: it conducts at Vgs = 0. That is the
whole trick — with the rails still coming up, every gate sits near 0 V and
every band is muted, for free, with no logic and no relay. `R40`/`C16` then
walk `MUTE_SS` down to −15 V over about two seconds, the 1 MΩ pulldowns
pinch the JFETs off, and the outputs come alive after the filter has stopped
lurching. `D4` does the reverse on the way down: as −15 V collapses it drags
`MUTE_SS` up and mutes everything while the rails are still falling, which
is the messier of the two transients.

There are therefore **two independent ways** a band gets muted, and they
meet at the same gate. The soft start pulls all three gates up together
through `D1`–`D3`; a mute button pulls **one** gate up on its own, by
shorting it to ground. The steering diodes are what keep those two from
interfering: with `MUTE_SS` down at −15 V and a button holding one gate at
0 V, that band's diode is reverse-biased, so the button cannot drag
`MUTE_SS` — or the other two bands — anywhere.

#### The mute buttons

`SW1`–`SW3` are **on the board**, in the front-panel row between the volume
pots — one beside each band's volume trim:

```
 MASTER | [LOW mute]  LOW VOL | LOW/MID | [MID mute]  MID VOL | MID/HIGH |
        [HIGH mute]  HIGH VOL
```

They are **G-Switch PS-22E05** (LCSC `C2848949`, about £0.16 each): a
right-angle push-lock DPDT, so the plunger points out of the board edge in
the same direction as the pot shafts and through the same panel. Nothing
about muting leaves the board any more.

**Watch the designator order.** `SW1`/`MG1` is the **HIGH** band and
`SW3`/`MG3` is **LOW**, which is the reverse of the panel row (LOW → HIGH,
left to right). The silkscreen says `MUTE HI` / `MUTE MID` / `MUTE LO`
beside each button, so the board itself is unambiguous — but the netlist is
not, and neither is the schematic.

**Pole A does the muting.** The common (pin 4) is that band's `MG` line and
one throw is GND, so latching the button shorts the gate to ground and the
JFET shunts that band's buffer input.

**Pole B does the LED**, and it is wired to the throw at the *opposite* end
of the body from the mute ground. That is deliberate: whichever way round
the mechanism actually is, the LED is lit exactly when the band is **not**
muted. Lit = playing.

**Which button position mutes is a build option, not a fact.** G-Switch's
outline drawing shows the two poles and the non-shorting changeover but
never says which throw closes when the plunger latches, and no distributor
page does either. The generator assumes the common latches onto the throws
at the *far* end from the plunger. If the built board turns out to mute
when the buttons are **out**, regenerate with

```sh
MUTE_SW_SENSE=beta python3 tools/gen_schematic.py
python3 tools/gen_pcb_smd.py
```

which swaps GND and the LED onto the other pair of throws. Both spare
throws are drilled and plated either way (`SW`*n*`_NC1` / `_NC2`), so it is
a board respin, not a rework — check it with a meter on one switch before
ordering.

**The lug holes are on GND.** Pins 7 and 8 are the frame's mounting lugs;
they take the push force, so the board solders them rather than leaving
them dry. That assumes the metal frame is isolated from the contacts, which
is true of every switch of this construction but is *not* stated on the
drawing. **Check it with a meter before ordering** — it is the one thing in
this footprint taken on trust.

#### Is a 15 µA contact reliable?

The mute contact carries the gate pulldown current and nothing else: 15 V
across 1 MΩ, i.e. **15 µA**. That is a dry circuit, well below the level at
which a silver contact reliably breaks through its own sulphide film, and
on most circuits it would be a real reliability worry.

Here it is not, and the reason is worth stating rather than asserting. What
matters is not the contact resistance but whether the gate still gets close
enough to 0 V to keep the JFET conducting. The contact and `R34` form a
divider off −15 V:

```
Vg = -15 * Rc / (Rc + 1 MΩ)
```

A J111 conducts until Vgs reaches Vgs(off), which is −3 V for the most
easily pinched device in the spec spread. Solving gives **Rc < 250 kΩ**;
for a typical −5 V device, **Rc < 500 kΩ**. A wiping contact with a film on
it measures ohms to kilohms — three to five orders of magnitude of margin.
The dry circuit is safe *because the load is 1 MΩ*, not because the contact
is a good one.

The LED pole carries 5.9 mA, which is above the wetting threshold anyway.

#### The optional LED loom (`J6`)

`J6` is a 1×4 header carrying **DC only**, so it can be a long unshielded
loom to the panel. Leave it unpopulated and you lose the indicators and
nothing else.

| Pin | Net | Band | What it is |
|---|---|---|---|
| 1 | `LD1` | **HIGH** | LED feed, switched: live when HIGH is playing |
| 2 | `LD2` | **MID** | LED feed |
| 3 | `LD3` | **LOW** | LED feed |
| 4 | `GND` | — | Shared return for all three LEDs |

Each `LD` pin is `+15 V` through 2.2 kΩ and then through the button's
second pole, so a bare LED from `LD`*n* to the GND pin draws about
`(15 − 2) / 2200 ≈ 5.9 mA` when that band is playing and nothing when it is
muted. No resistor on the panel, one shared ground wire, four cores.

Note the same reversal as the buttons: **`LD1` is HIGH, `LD3` is LOW.**

**Do not** wire an LED to an `MG` line to save a pole — the trap is still
there even though the board no longer invites it. `LD`*n* would drive
~28 µA into the gate node, the 1 MΩ pulldown cannot sink it, and the gate
settles at roughly **+13 V**: the band stays muted with its button off and
the gate junction is forward-biased into the bargain.

#### Building without the LEDs

`MUTE_LEDS=0` on `tools/gen_schematic.py` drops `R37`-`R39` and `J6`
entirely and wires the button's **second pole in parallel with the first**
instead: same gate line, same ground, two contacts. That is worth having
on the one dry-circuit contact on the board even though (see above) it has
enormous margin already.

It was also tried as a way to buy routing room - six nets and ten pads out
of the panel row, which is the most congested strip on the board - and
**measured, it does not**: over eight seeds it produced 157 DRC problems
against 137 with the indicators in. So the LEDs are not what makes this
board hard to route, and they are on by default.

#### Building without the buttons

Leave `SW1`–`SW3` off the board and every band plays: each `MG` line then
has only its 1 MΩ pulldown and its steering diode on it, so the gates sit
at −15 V once the soft start has finished. The power-up mute and the
fast re-mute on power-down both still work — `R40`, `C16` and `D1`–`D4` are
all on the board and none of them needs the buttons.

### Power and decoupling

| Part | Value | Job |
|---|---|---|
| `J1` | 3-pin terminal block | ±15 V and ground |
| `C11`, `C12` | 10 µF | Bulk reservoir, one per rail |
| `C5`, `C7`, `C9` | 100 nF | +15 V bypass, one at each of `U1`, `U2`, `U3` |
| `C6`, `C8`, `C10` | 100 nF | −15 V bypass, likewise |

Supply pins live on the `A` section of `U1` and `U2` and on `U3D`. The
placement search is told which bypass cap belongs to which op-amp
(`BYPASS_NEAR`) and prices the pin-to-pin distance directly, because nothing
else in the cost model can see it — a cap on two board-spanning nets costs
almost no wirelength wherever it is put. They are 4.2–10.0 mm from their
pins on the committed board, against 37–63 mm before that term existed.

## Stage-by-stage

Designators here are the **original four-dual** drawing's (U1-U4), which is
what the ESP figure and the two through-hole schematic variants use. On the
quad variants - including the board - the same eight sections live in two
14-pin packages and are numbered U1A-D and U2A-D; the section above is the
map for those.

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
NE5532), or **OPA1644** / **OPA4134** / **LME49740** / **TL074**. All use the
standard 14-pin quad pinout:

| | A | B | C | D |
|---|---|---|---|---|
| output | 1 | 7 | 8 | 14 |
| inverting in | 2 | 6 | 9 | 13 |
| non-inverting in | 3 | 5 | 10 | 12 |

with V+ on pin 4 and V− on pin 11. This packaging is drawn in the
`retuned-quad` variant. Everything else in these notes uses the dual-package
designators; the sections map across like this:

| Stage | Dual variants | Quad variant |
|---|---|---|
| Input buffer | U1A | U1A |
| Filter 1 summing amp (HIGH) | U1B | U1B |
| Filter 1 integrator 1 | U2A | U1C |
| Filter 1 integrator 2 (LP1) | U2B | U1D |
| Filter 2 summing amp (MID) | U3A | U2A |
| Filter 2 integrator 1 | U3B | U2B |
| Filter 2 integrator 2 (LP2) | U4A | U2C |
| Output inverter (LOW) | U4B | U2D |

Supply pins are drawn on the A section of each package, as is conventional.

The generator checks that the quad and dual variants have identical *signal*
connectivity — comparing op-amp pins by their role rather than their number,
since the numbers necessarily differ — while verifying the supply wiring
per package, where the quad genuinely has fewer pins and fewer bypass caps.

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
