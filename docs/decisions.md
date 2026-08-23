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

### 2026-08-23 — the courtyard fix: correct, measured, reverted
**Question:** the overlap term treats a designator as solid, which blocked
9 of 48 two-pad parts from turning the right way round. Fix it so a label
blocks copper rather than another label.
**Method:** priced overlap as part i's FULL extent against part j's HARD
box (pads and body, no silk text), both ways round — which needs no new
term and forbids exactly the right things. Implemented in `probe()`,
`Placer`, `anneal.c` (`Cfg` verified by compiling it and comparing
`sizeof` and every offset against `ctypes`: 344 bytes both sides) and
`verify()`. 12 seeds at the ranking config in two size-weight settings,
then uncapped confirms on the smallest candidates.
**Result:** it works as intended — `silk ... sits over` never fired,
`flip_pass` went from 5-9 moves to up to 24 — and it loses.

| arm | total DRC | mean area |
|---|---|---|
| baseline (full-against-full, `W_H=400`) | **91** | 10465 mm2 |
| courtyard fix, `W_H=400` | 115 | 10212 mm2 |
| courtyard fix, `W_H=150` | 84 | 11079 mm2 |

At `W_H=400` the freed space goes straight into a smaller board —
`SEED=12` at **8384 mm2**, 12% under the shipped board — and uncapped
none of the small ones route (`+15V`, `N1400_900`, `HIGH_VOL`/`MG2` each
left in two pieces). Its only clean board is 10706 mm2, 13% bigger than
the incumbent. At `W_H=150` it wins on count and pays 6% more area, best
candidate 10461 mm2.
**Decision:** reverted. A board search ships the smallest CLEAN board, not
a mean, and on that metric the correct model loses in every configuration
while the incorrect one holds 9500 mm2 verified clean. Being right about
the geometry does not entitle a change to ship. Done as a clean revert of
one self-contained commit (131a7ea) so retrying it is a `git revert` away.
If retried: re-tune `W_H` (it is coupled to the courtyard and was
calibrated against the old one) and use a full-width seed field — 12
seeds against the incumbent's 24 is not a fair budget.
**Touched the placement?** Net zero. `check_all.py` passes with a cold
cache: `SEED=17`, 149.6 x 63.5 mm, verifies clean.

### 2026-08-23 — a stale placement cache reported a board that did not exist
**Question:** a parallel route-order batch reported `SEED=15` as
**194.6 x 66.5 mm with 5 DRC problems** on two of four trials, and
145.3 x 62.2 mm on the other two. Same seed. Same code. Which is real?
**Method:** re-ran the two odd trials alone with `.place-cache` cleared,
and ran six cold-cache placement-only trials of the same seed back to
back.
**Result:** the six isolated runs are byte-identical (145.3 x 62.2 every
time), so the placer is deterministic — this is NOT the set-iteration
non-determinism this file documents. Re-run with the cache cleared,
`RS21` gives **145.3 x 62.2 and 2 problems** instead of 194.6 x 66.5 and
5. The cache had served a pose computed by code that no longer existed,
and the run printed "reusing the cached layout" and routed it.

The new trigger is worth knowing: **the container image ships a
`.place-cache` directory** (entries dated days before this session), and
the repo is re-cloned at a pinned revision on every reprovision, so a
fresh session can start with a cache full of poses from code that is no
longer in the tree.
**Decision:** `_place_key()` now hashes `gen_pcb_smd.py` as well as
`place.py`, `anneal.c` and `canneal.py`. `GEOM` covered footprint
geometry, but everything else that file decides before the anneal runs —
pinned rows, edge margins, mounting-hole keepouts, which parts are
grouped — was outside the key. Costs a re-placement whenever the file is
edited, which is a few seconds; the alternative is routing a layout from
a different codebase and believing the number. Third time a cache has
served a stale answer here.
**Touched the placement?** No. `check_all.py` passes with the cache
cleared: `SEED=17`, 149.6 x 63.5 mm, verifies clean, no drift.

### 2026-08-23 — correction: the cached-pose diagnosis was not proven
**Question:** the previous entry blamed a stale `.place-cache` for
`SEED=15` reporting a 194.6 x 66.5 mm board. Is that actually the
mechanism?
**Method:** tried to reproduce it. Also tested a second hypothesis — that
four concurrent trials race on building `tools/_anneal.so`, and a process
loading a half-written library falls back to the PYTHON annealer, which
`CLAUDE.md` says gives a different board for the same seed.
**Result:** the build race is **disproved**. Four concurrent cold builds
of the `.so`, same seed, all produced 145.3 x 62.2 and none printed the
fallback warning. And the key-collision story does not hold up either:
`_place_key()` hashes the netlist, and the stale entries were written
against a netlist with 83 parts against today's 86, so their keys cannot
match.

What survives is one causal observation and no mechanism: clearing
`.place-cache` changed the SAME trial from 194.6 x 66.5 / 5 problems to
145.3 x 62.2 / 2, and six isolated cold-cache runs of that seed are
byte-identical. The offending entry was deleted before it could be
examined.
**Decision:** stop guessing at the mechanism and make the failure
impossible to ship instead. `_cached_pose()` now rejects any cached pose
whose part set is not exactly this board's, naming what is missing or
unknown, and recomputes. Tested by planting a doctored entry both ways:
each is rejected and the correct board is produced. Keeping the
`gen_pcb_smd.py` addition to the key from the previous entry — it is
sound hardening — but it should NOT be credited with fixing the observed
anomaly, because nothing here has established what did.
**Touched the placement?** No. `check_all.py` passes with the cache
cleared.

### 2026-08-23 — SEED=15 diagnosed and dropped: congestion, not a pocket
**Question:** `SEED=15` (9038 mm2, 5% smaller than the shipped board) came
within one net of clean on two independent route orders, both failing on
`N570_920` with `R13.1` stranded. Is that a structural pocket worth
opening, or is the placement simply full?
**Method:** rebuilt `SEED=15 ROUTE_SEED=12` in an isolated copy of the
tree (so the committed artifacts could not be touched), reproduced the
failure exactly, and mapped the copper around `R13.1` layer by layer.
Then compared which nets fail across all 18 route orders.
**Result:** it is congestion, and the evidence is two-sided.

Locally, `R13.1` is a top-layer SMD pad with **no via within 6.6 mm** in
any direction. Inside that window the top layer already carries 12 foreign
track segments and 8 foreign pads, the bottom 6 segments, Inner2 8. Nearest
foreign copper edge: 1.02 mm right, 2.59 mm down. The router never found
room to drop a via, and the top-layer channels were spoken for. Its two
partners are 10.8 mm right (`U2A.3`) and 19.8 mm left (`R14.1`).

Globally — and this is what settles it — **the failing net is different
almost every time.** Across 18 uncapped route orders the casualties
include `N570_920`, `N1280_900`, `N2150_1550`, `N530_100`, `N530_700`,
`N1580_640`, `MID_MUTE`, `LOW_MUTE`, `HIGH_MUTE`, `MUTE_SS`, `MG2` and
`+15V`. A structural pocket strands the same pad whatever the order; this
placement is about one net over capacity and route order only chooses
which net loses.

For scale, it carries slightly MORE copper in less area than the board it
would replace: 233 tracks / 163 vias against 228 / 159.
**Decision:** dropped. Making it routable means giving it room, at which
point it stops being smaller. `SEED=17` at 9500 mm2 verified clean stands
as the shipped board. The size search is closed at 60 seeds ranked and 18
uncapped route orders on the best sub-9500 candidate.
**Touched the placement?** No — the whole diagnosis ran in a scratch copy.

### 2026-08-23 — every terminal prints its designator over its own pin name
**Question:** found by reading the render, not by any check: the rear row
shows "J5LOW", "J4MID", "J2IN", "J1+15". Are those labels really
colliding, and can it be fixed?
**Method:** measured every silk-label pair on the board; then tried the
two ways the designator can clear the name row, pricing each with a
re-search rather than at one seed (a footprint silk change moves every
placement).
**Result:** five real collisions, all the same defect — each screw
terminal's designator overlaps its own first pin name by 0.58-0.98 mm
horizontally and 1.08 mm vertically. `verify()` cannot see it: it checks
silk against COPPER, and two labels on top of each other is legal
copper-wise and useless to a human.

Both fixes cost real board:

| fix | result |
|---|---|
| designator above the name row | `SEED=17` 149.6 x 63.5 -> **168.7 x 79.0 mm** |
| designator beside the block | 24 seeds re-ranked; best routable **11858 mm2 @ 1 DRC**, next 12008 @ 3. Small ones (9513, 9542) route at 10-12. |

Against the incumbent's 9500 mm2 verified clean, that is **+25% board** to
un-overlap a designator. The pin names — the labels that actually tell you
which terminal drives which amplifier — remain readable either way, and
the designators are on the assembly drawing and in the CPL.

The geometry says why there is no free fix: the block is 14 units (3.556
mm) per pin and `LABEL_SIZE` text is ~3.1 units per character, so "GND"
alone eats 9.3 of a 14-unit pitch. There is no room on that line for a
designator, and the only clear space is outward, which is what costs.
**Decision:** refused, and made VISIBLE instead. `validate_fab.py` gains
`check_silk_overlaps()`, which reports every overlapping label pair as a
note with its dimensions. The defect is now measured on every run instead
of waiting for someone to read a render.
**Touched the placement?** No — the footprint change was reverted.
`check_all.py` passes with a cold cache: `SEED=17`, 149.6 x 63.5 mm, clean.
