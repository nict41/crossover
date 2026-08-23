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

### 2026-08-23 — a two-pin part has two ways round (POLISH / flip_pass)
**Question:** spotted by eye on the render — `C14`'s trace to `R26`, the
part immediately to its right, leaves from its LEFT pad, so both its nets
cross and wrap around the body. Is the placer accounting for this at all?
**Method:** audited every two-pad part on the committed board, comparing
each one's nets' half-perimeter against the same part turned end for end.
Then a paired 3-arm A/B over 12 seeds at the ranking config, and a fourth
arm for the generalisation.
**Result:** no, it is not. `ROTS` gave chip passives and electrolytics
`(0, 90)`, on the reasoning that 180 is the same SHAPE — true, and beside
the point, because the two pads carry different NETS. **9 of 61 two-pad
parts sat the wrong way round, together 30.5 mm of half-perimeter**
(`C14` and `C15` worst at 6.1 mm each).

Two obstacles. `fp_chip` anchored its designator in the local frame, so
every chip on the board put its label on its own pad 1 at 180/270 and
failed the footprint probe — third instance of that bug class, now routed
through `silk_ref_beside()`. And the angles must NOT go to the anneal:

| arm | mean DRC | total |
|---|---|---|
| baseline `(0, 90)` | 8.42 | 101 |
| flip in a polish pass | **7.58** | **91** |
| flip in the anneal's move set | 9.67 | 116 |
| `POLISH=all` (every angle, every part) | 9.00 | 108 |

**Decision:** kept as `POLISH=flip` (default), with the angles in
`Part.flip_rots` so the anneal's trajectory is bit-for-bit unchanged.
`POLISH=all` measured and rejected — it accepts 21 moves against 6 on
seed 1 and routes worse than doing nothing, because a 180 on a two-pad
part changes only which pad carries which net while a 90 on a SOIC
changes which face has seven pins queuing to get out. `off` and `all`
kept so the measurement can be repeated.

A deterministic pass is the right shape regardless: `hpwl` is weighted
0.15 and contributes ~1533 of a ~490000 total, so the whole prize is ~18
cost units — 0.004%. The anneal is structurally unable to chase it.

Caveat, not glossed: seed 10 went **9 -> 16**. A flip moves the
designator, so it is not perfectly geometry-neutral.
**Touched the placement?** Yes, twice over — the silk fix moves every
chip's courtyard by half a unit, so the committed `SEED=41` board no
longer reproduces (157.2 x 74.2 mm instead of 147.3 x 68.3). Board search
pending; artifacts are stale until it lands.

### 2026-08-23 — flip_pass profiled: 2.53 s -> 0.42 s, output unchanged
**Question:** is the polish pass implemented efficiently, and should any
of it be in C?
**Method:** cProfile around `flip_pass` on a cold placement; then two
exact changes, each verified by diffing the run output before and after.
**Result:** 2.53 s, 5.6% of a search trial. 55% of it was `escape_of`,
called 21228 times — 244 `full_cost` evaluations x 86 parts. Two fixes:
`full_cost` was being called twice per candidate (once to score, once
after reverting, purely to restore derived state — now a copy), and
`escape_of` was 86 numpy calls on 86-element arrays where one (n, n)
masked reduction does the same arithmetic (`escape_all()`, checked
against the per-part version over 300 random layouts, worst difference
1.4e-14).

**2.53 s -> 0.42 s, 6.0x.** Output byte-identical on seeds 1-5 under both
`POLISH=flip` and `POLISH=all`: same parts turned, same half-perimeter,
same board.
**Decision:** kept. **C rejected**: `anneal.c` has the same cost model but
exports only `anneal`, and its `rot_base` excludes the flip angles, so
reuse needs a new export plus a `Cfg` change — structural risk in the one
struct that must stay field-for-field identical, for a term now at 1% of
a trial. Incremental scoring also rejected: it is not exact, so it would
change which flips are accepted and invalidate the A/B.
**Touched the placement?** No — that is the point, and it was checked
rather than assumed.

### 2026-08-23 — new committed board: SEED=17 ROUTE_SEED=0
**Question:** the silkscreen fix and the flip pass move every placement,
so the old `SEED=41` board no longer reproduces. What replaces it?
**Method:** ranked 24 seeds at the committed config (capped router, one
rip-up round, `ROUTE_SEED=0`), then confirmed the top four uncapped.
Chunked deliberately — this container is reprovisioned roughly hourly and
took two long searches with it, so verdicts are flushed per trial.
**Result:** ranking put `SEED=17` first at 2 problems (next best 3), and
uncapped it verifies **clean, 0 DRC problems**.

| | old board | new board |
|---|---|---|
| seed | `41` / `RS=5` | **`17` / `RS=0`** |
| size | 147.3 x 68.3 mm | **149.6 x 63.5 mm** |
| area | 10061 mm2 | **9500 mm2** (-5.6%) |
| tracks / vias | 238 / 179 | **227 / 159** |
| utilisation | 57% | 58% |

Wider by 2.3 mm, shallower by 4.8 mm, and 20 fewer vias. Every ranked
candidate was tagged "unrouted", which is the failure the cap manufactures
rather than a property of the placements — the usual reminder that the cap
ranks well and judges badly.
**Decision:** committed. `SEED`/`ROUTE_SEED` defaults pinned to 17/0.
**Touched the placement?** It IS the placement. `check_all.py` passes: no
drift, board verifies, fab limits and BOM clean.

### 2026-08-23 — why the flip pass stops: the designator is courtyard
**Question:** the polish ran and the board is clean, so is `C14` — the
part that started this — now the right way round? No. Why not?
**Method:** audited every two-pad part on the committed `SEED=17` board,
then scored each rejected flip term by term.
**Result:** **9 of 48 two-pad parts are still reversed, worth 23.5 mm**,
and `ov` blocks every one of them:

```
ref      d(total) d(hpwl mm)   terms that moved
C14        1518.3      -3.05   ov +1741, esc -221, hpwl -2
D1         9166.4      -3.40   ov +5508, near +3660, hpwl -2
R32        5864.6      -4.06   ov +5867, hpwl -2
R8         1956.0      -4.06   ov +1958, hpwl -2
```

`probe()` reads the courtyard off the emitted shapes and `shape_box()`
counts a silkscreen TEXT as body, so a footprint's courtyard is
asymmetric by about a label's height. Flipping moves that box to the
other side and into a neighbour: the wire gets shorter and the placement
becomes illegal.
**Decision:** not fixed here, written up instead. The constraint is
probably wrong — a label may not lie over foreign COPPER, which
`verify()` already checks exactly, but a label over a neighbour's silk is
what every board does. The fix is to split the term (courtyard overlap on
pads and body; a separate cheaper label-over-foreign-PAD term), not to
drop it. That moves every placement, so it needs its own search round and
its own A/B. On the task board as the top layout item.
**Touched the placement?** No.

### 2026-08-23 — POLISH=nudge: move as well as turn. Measured, rejected.
**Question:** the flip pass is blocked by courtyard overlap on 9 parts
(previous entry). If a part cannot turn where it stands, can it turn a
track pitch to the left instead? That needs no cost-model change, no
`anneal.c` change and no check weakened.
**Method:** `POLISH=nudge` tries the 180 at the current position and at
each of the eight neighbours one track pitch away. 13 seeds, ranking
config, against the `flip` baseline.
**Result:** it does step aside — 25 to 98 moves accepted a board where
`flip` accepts 5 to 9 — and it routes worse.

| POLISH | total DRC over seeds 1-12 | |
|---|---|---|
| `flip` | **91** | |
| `nudge` | 96 | better 3, worse 6, tied 3 |

`SEED=17`, the committed board, goes from 2 problems to 5.
**Decision:** rejected; default stays `flip`. The move count is the tell:
this is a second greedy search over POSITION, and position is what the
anneal already spends 60000 moves on. Turning a part in place is nearly
free geometrically and can be taken on the surrogate; moving it is not.
Sixth time this trade has been lost here (MOVES, RESTARTS, W_FILL twice,
POLISH=all, and now this). Mode kept so the measurement can be repeated.
The finding stands: fix the courtyard, not the pass that trips over it.
**Touched the placement?** No — default unchanged, so `SEED=17` is
untouched.
