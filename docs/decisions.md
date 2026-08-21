# Decision log

Append-only, newest at the bottom. One entry per measurement, whether or
not it resulted in a change.

**Why this exists:** the expensive mistakes in this project have not been
bad code, they have been *re-doing work whose answer was already known* —
re-trying a knob that had been measured and rejected, or believing a change
had landed because a commit message said so. An entry here costs two
minutes and saves the next agent an afternoon.

**A rejected experiment is worth as much as an accepted one.** Write it
down even when — especially when — the answer was "no difference".

## Template

Copy this block, fill it in, append it at the end. Keep it short; the
numbers matter more than the prose.

```
### YYYY-MM-DD — one-line summary
**Question:** what you were trying to find out.
**Method:** the exact command(s), so it can be re-run.
**Result:** the numbers. Both arms if it was a comparison.
**Decision:** kept / reverted / needs more work — and why.
**Touched the placement?** yes (board re-searched, new SEED) / no.
```

---

### 2026-08-21 — the mute JFET was the wrong member of its family
**Question:** does the shunt JFET stay off on signal peaks?
**Method:** `python3 tools/gen_spice.py && python3 tools/run_sim.py`, which
sweeps level against both parts' datasheet corners.
**Result:** the gate rests at −15 V while the drain carries signal, so
Vgs(eff) = −15 − Vpeak. A worst-case J111 (Vgs(off) = −10 V) reaches
**2.28 % THD at 4.6 V pk and 11.24 % at 6.4 V**, below the op-amp clipping
ceiling. A J112 (−1.0 to −5.0 V) stays at 0.004–0.021 % everywhere. Cost:
mute depth −45.7 dB against −49.9, because J112 allows 50 Ω of Rds(on)
against 30.
**Decision:** kept. `MMBFJ112` (C258195) replaces `MMBFJ111`. Same die
family, same SOT-23, same pinout.
**Touched the placement?** No — identical board, same track and via count.

### 2026-08-21 — supply rails are not worth widening
**Question:** are the 16 mil power traces thick enough?
**Method:** measured routed length per net out of the committed board JSON,
then IPC-2221 for capacity and ρL/A for drop.
**Result:** +15 V is 216 mm of 0.406 mm trace = 261 mΩ, carrying 48 mA →
**12 mV** drop. IPC-2221 rates that trace at **1244 mA**, a 26× margin.
**Decision:** no change. The widths are set by pad proportion, which
remains the only reason to revisit them.
**Touched the placement?** No.

### 2026-08-21 — the input network needed a constraint, not a checker
**Question:** the render showed `R1` in the opposite corner from the op-amp
it biases. How bad is it?
**Method:** pad bounding box per net, out of the committed board.
**Result:** `N160_280` (input pin, 47 kΩ to ground, ahead of all gain)
spanned **49 mm**; `INPUT` (master volume wiper) spanned **73 mm**. Adding
them to `near` as `INPUT_NEAR` brought them to **9.2 mm and 18.4 mm**.
**Decision:** kept. Note that no geometric check had anything to say about
either — the requirement is local and physical, so it belongs in the cost
model, not in a checker.
**Touched the placement?** Yes. Re-searched; `SEED=1 ROUTE_SEED=0`,
145.8 × 66.5 mm, clean.

### 2026-08-21 — two hand-drawn footprints were wrong, from supplied datasheets
**Question:** do the hand-drawn footprints match the real parts? Five
datasheets were supplied: Alps RK097, RV09, ST MC33079, KNSCHA RVT 10 µF
(C2858858), CIXI MAIXU MX350-3.5 terminal block.
**Method:** read each mechanical drawing and compared with the emitted
geometry in `tools/gen_pcb_smd.py`.
**Result:**
* `fp_pot` (dual-gang, `VR1`/`VR2`) — drew 3 pins at **5.08 mm** with the
  gangs **5.08 mm** apart. Real: **six ø1 mm holes on a 2.5 mm grid**,
  three per gang at 2.5 mm, gangs 2.5 mm apart. Both pitches double. The
  body was 52 units wide on the same wrong reasoning; real is the same
  9.5 mm as the single-gang, 2.5 mm deeper.
* `fp_elec` — drew 2.03 × 2.54 mm pads on **4.32 mm** centres. Real
  (KNSCHA "Recommended Land Size", size 5): **1.6 × 3.0 mm on 3.0 mm
  centres**, gap 1.4 mm. Pads 1.3 mm too far apart.
* `fp_soic14` — **correct.** 1.27 mm pitch; pads inside IPC tolerance for
  E = 6.0 mm nominal.
* `fp_term` — **correct enough.** 3.50 mm real against 3.556 drawn, ø1.00
  real against 1.09 drawn; both inside the slack a ø0.8 mm pin leaves.
**Decision:** both fixed. Note for anyone tempted to trust the checkers
here: a footprint is self-consistent whatever its dimensions, so every
geometric check passes on a part that cannot be soldered. This class of
defect can only be found outside the repository.
**Touched the placement?** Yes — both footprints shrank. Re-searched;
`SEED=20 ROUTE_SEED=2`, 149.6 × 64.0 mm, clean at production settings.

### 2026-08-21 — the busiest parts had the least room (ESC_CAP 4 -> 7)
**Question:** are high-connection parts getting the space their routing
needs?
**Method:** measured pad-box gap to nearest neighbour per part on the
committed board, grouped by pin count; then A/B'd `ESC_CAP` over 16 seeds
at the ranking config.
**Result:** before, parts with >=6 pins sat a mean **2.32 mm** from their
nearest neighbour and parts with <=2 pins **3.32 mm** - backwards. `U3`
(14 pins) had 1.02 mm. `ESC_CAP` caps a side's escape demand at that many
track pitches, and it was 4, so a 7-pin SOIC face asked for no more than a
4-pin one.

| ESC_CAP | mean DRC | clean of 16 | mean height |
|---|---|---|---|
| 4 | 7.9 | 0 | 73.0 mm |
| 7 | 5.2 | 2 | 73.0 mm |
| 10 | 5.1 | 1 | 73.2 mm |

On the committed board the gap is now **3.14 mm** high-pin against
**3.34 mm** low-pin - the imbalance is essentially gone.
**Decision:** kept, `ESC_CAP=7`. Distinct from W_FILL (rejected twice):
that priced column AREA, this prices escape depth weighted by pin count.
**Touched the placement?** Yes. Re-searched; `SEED=41 ROUTE_SEED=5`,
147.3 x 68.3 mm, clean with no quality flags. Costs 5% area against the
board it replaces (2.3 mm narrower, 4.3 mm deeper). `SEED=39 ROUTE_SEED=6`
is the strictly-no-growth alternative: 145.3 x 65.8 mm, defect-free, high
-pin gap 2.88 mm, but it trips the wasted-rectangle quality note.
