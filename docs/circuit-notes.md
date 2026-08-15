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

| Filter | C | R range | Frequency range |
|---|---|---|---|
| Filter 1 (High/Mid) | C1, C2 = 10 nF | 3.3 k … 23.3 k | 683 Hz … 4.82 kHz |
| Filter 2 (Mid/Low) | C3, C4 = 100 nF | 3.3 k … 23.3 k | 68.3 Hz … 482 Hz |

Both gangs of a pot must track; both capacitors in a filter must be the same
value, as must both series resistors. To move a range, change the capacitors
(smaller C = higher frequency). Reducing R7/R8/R17/R18 to 2.2 k widens the
range; the article warns against going much lower. 4.7 nF in filter 1 gives
roughly 1.45 kHz … 10 kHz.

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
