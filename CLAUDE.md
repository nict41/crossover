# Project context for agents

ESP Project 148 3-way state-variable crossover, redrawn as EasyEDA-importable
schematics plus a fully routed SMD PCB. Everything in `schematic/`, `pcb/`,
`bom/` and most of `docs/` is **generated** — never hand-edit those; edit the
generator in `tools/` and re-run it.

## Regeneration

```sh
python3 tools/gen_schematic.py    # schematics, previews, netlists, BOMs
python3 tools/gen_pcb_smd.py      # SMD board, routed + verified (needs numpy)
python3 tools/gen_range_diagram.py
python3 tools/validate_fab.py --online   # EasyEDA import + JLCPCB limits
python3 tools/find_board.py --seeds 1-24 --route-seeds 0-7   # search for a clean board
```

**Order matters**: `gen_pcb_smd.py` reads the netlist JSON that `gen_schematic.py`
writes, so the board cannot drift from the schematic. Run the schematic first.

`gen_pcb_smd.py` knobs (all optional): `MOVES` / `RESTARTS` / `SEED` tune the
placement search, `PLACE_LOG=n` prints its cost breakdown every n moves,
`GRID` sets the routing grid, `SWEEP=1` skips writing artifacts, `PANEL_PITCH`
sets the front-panel control spacing, `PLANE_LOG=1` traces the ground-plane
stubs, `RIPUP_LOG=1` traces the rip-up rounds, `PLANE_GAP` sets the
plane-escape distance (`0` disables that placement term),
`PLANE_PREFILTER=0/1` overrides the pre-routing pour check, and
`PLANE_PREFILTER_ONLY=1` stops after it (that is the search's cheap screen).
There is deliberately no `BOARD_W` or `BOARD_H` — see below.

`find_board.py` screens every seed with the pre-filter, then **ranks
cheaply and confirms expensively**: one capped route with a single rip-up
round per seed (~90 s), then the best `--confirm` of them re-run at
production settings. It streams each verdict as it lands and stops at the
first clean board (`--all` sweeps the whole field, `--no-screen` skips the
screen, `--no-cheap-rank` routes everything at full settings).

**A search must never discard what it has already learned.** A 36-seed
sweep once hit its outer timeout and printed nothing at all - ninety
minutes, thirty-six boards genuinely routed, not one verdict recoverable,
because results were collected and printed at the end. Being slow is a
cost; being slow *and* losing the answers is a bug.

**Budget the search before launching it.** That sweep ran the router
uncapped with full rip-up, which costs **707 s a trial** on this board.
36 trials over 4 jobs is 106 minutes against a 90-minute timeout: it could
not have finished, and one line of arithmetic beforehand would have said
so. Per-trial cost, measured:

| config | per trial |
|---|---|
| **ranking config (cap 400k, rip-up 1)** | **~34 s** |
| capped 400k, rip-up 0 | 28 s |
| capped 150k, rip-up 1 (rejected - see below) | 21 s |
| capped 400k, rip-up 6 | 201 s |
| **uncapped, rip-up 6** | **707 s** |

The 400k/rip-up-1 row was 71 s when it was measured and is 34 s now; the
`path_clearance_ok` and disc-cache fixes below took 1.68x off every row
without changing a single verdict, so any older timing in this file is
high by roughly that factor.

`seeds x route-orders x per-trial / jobs` is the number to check against the
time available. If it does not fit, cut the field or cheapen the config -
do not start it and hope.

**Rank with one rip-up round, not zero.** Measured against six seeds whose
expensive ordering was known (rip-up 6: `1->2 4->3 2->11 3->13 5->15
6->16`):

| config | ordering it produces | cost |
|---|---|---|
| rip-up 0 | `1->13 4->15 2->22 3->20 5->14 6->20` | 35-63 s |
| rip-up 1 | `1->2 4->5 2->15 3->20 5->14 6->17` | 73-104 s |
| rip-up 6 | the ground truth | ~201 s |

Rip-up 0 is not a blurred version of the answer, it is a *different* one -
it ranks seed 2, genuinely third best, dead last. Rip-up 1 reproduces the
top two exactly. The reason says where rip-up's value lives: a good
placement needs barely any (seed 1 reaches its final 2 problems after one
round), while a marginal one is where the loop grinds through six rounds
trading one failure for another. Round one buys the ranking signal;
rounds two to six buy polish on candidates a search is about to throw away.

**A clean verdict from the cheap pass needs no confirming.** Capping
expansions and limiting rip-up only make the ROUTER give up sooner;
neither touches `verify()`, which measures finished copper. The cheap pass
can miss a good board but cannot invent one.

`gen_schematic.py` builds all four variants and asserts they share identical
signal connectivity, so a retune that accidentally changed a connection fails
the build instead of shipping quietly. If you add a variant-specific feature
(as the volume pots are), teach `signal_map()` to normalise it rather than
disabling the check.

## The verification philosophy — read this before "fixing" a DRC failure

`gen_pcb_smd.py` routes the board and then checks it with **exact geometry that
does not reuse the router's own bookkeeping** (`verify()`): pairwise clearance,
union-find connectivity, mounting-hole keepout, board-edge clearance,
schematic-pin agreement, silk-over-copper, footprint-courtyard overlap,
ground-pour reachability, and a largest-empty-rectangle wasted-area
measure.

This checker is the source of truth and has caught many real defects that were
invisible in the preview image. When it complains, the default assumption is
that it is right and the layout is wrong. Do not weaken a check to make a board
pass.

## Layout first, then board size — the pipeline's central idea

**The board size is an output, not an input.** `tools/place.py` arranges the
parts on an unbounded canvas and `gen_pcb_smd.py` draws the outline around the
result. There is no `BOARD_W`/`BOARD_H` to set, and re-adding one would undo
the whole point: for most of this project's life the flow was "pick a size,
fit the layout into it, grow it when a net won't route", and that procedure
*cannot fix a layout problem — it can only find a size big enough to hide
one*. Every time it was reached for here, the real cause turned out to be
structural (the four listed below).

The placement search is a simulated anneal over **position and rotation**,
costed on:

| term | what it prices |
|---|---|
| overlap | courtyards may not intersect (ramped hard; asserted afterwards) |
| **escape** | each part side needs clear depth ∝ the pads breaking out through it |
| plane escape | a side carrying a pad on a POURED net needs room for a via |
| congestion | RUDY wire-demand vs tracks that physically fit per cell |
| wirelength | half-perimeter per net |
| size | height, plus width past the panel row's floor |

**The escape term is what makes rotation meaningful.** Turning a SOIC barely
changes its area or wirelength; it changes which of its sides has seven pins
queuing to get out. Starved escape corridors — not board area — are what left
nets unroutable every single time it happened here, so that is the term to
suspect if a placement routes badly.

Placement is *seconds*; routing is *minutes*. That asymmetry is why the search
does multiple restarts and keeps the best: it is far cheaper to search
placement properly and route once than to route a mediocre placement and then
go hunting for a board size that rescues it.

## Four layers, and what that changed in the code

The board is **4-layer** as of the on-board-mute-button work:

```
L1  TopLayer     signal, every SMD pad, GND pour
L2  Inner1 (21)  SOLID GND PLANE - not routable, no signal ever
L3  Inner2 (22)  signal
L4  BottomLayer  signal, GND pour
```

`LAYERS=2` still builds the two-layer board, which is what every
measurement in this file dated before the stackup change was taken on.

**Why.** Two layers ran out. With the three panel mute buttons in, ~115
placement seeds could not produce a clean board - eight nets in pieces,
both supply rails among them - on a board only 54% utilised. Out of
CHANNELS, not out of area, and the lever for that is layers. Finer design
rules were measured first and were *worse*: `CLEAR=0.6` scored 106 against
54, because `CLEAR` feeds the placer's own model as well as the routing
rule, so turning it down told the placer escapes were cheaper than they
are and it packed tighter.

**The first measurement, same seed, same router config: 10 DRC problems
became 4.**

Three things in the code carry the stackup, and all three used to be
written as the literal `2`:

* **`GRID_LAYER` / `LAYER_GRID`.** Grid index to EasyEDA layer id and back.
  The old code converted with `index + 1` and `layer - 1`, which is right
  for exactly two layers and silently wrong for any other number. It is a
  table now, and `grid_layers(layer)` expands `MULTI` to every layer.
* **`PLANE_L`.** The ground plane is a real copper layer that the router
  may not put a track on. It is *excluded* rather than filled with an
  occupying net id, because a through-hole via has to pass through it -
  its antipad is cut by the pour, not by the router - and `via_ok()` would
  otherwise refuse every via on the board. `router.c` takes it as a
  parameter and `passable()` returns false there.
* **Vias are through-hole, so every other layer is ONE via away.** The
  neighbour step `nl = 1 - L` became `(L + 1 + (d - 4)) % NL` in both the
  C router and `flood()`, and `via_ok()` now checks every routable layer.
  At `NL = 2` that is exactly the old behaviour.

**What the plane buys beyond capacity.** GND stops depending on a pour
threading between pads on a SIGNAL layer. Every ground pad now reaches the
plane straight down through a via, which is what used to produce "the
ground pour does not reach N pad(s)" - the single most common reason a
board in this project failed to verify.

**`SUPPLY` scales with the stackup.** The placer's congestion gauge counts
tracks per cell per ROUTING layer, and its calibrated 3.0 was measured on
two layers, so `gen_pcb_smd` now sets the default from the routable layer
count. It is `setdefault`, so an explicit `SUPPLY=` still wins, and
`_place_key()` hashes it - changing the stackup invalidates the placement
cache by itself.

**Search costs more per trial now.** The grid is twice the cells, so a
FAILING A* drains twice as many nodes. `find_board.py --trial-timeout`
defaults to 420 s, which was tuned on two layers and abandons roughly a
third of 4-layer trials mid-route; pass 1500 or more.

## Ground is a plane, and that is a load-bearing decision

`GND` is **not routed**. It is a solid plane on Inner1, poured on the
three signal layers, and connected by a
handful of short stubs where the pour physically cannot squeeze in. For
most of this project's life it was poured *and* routed, as insurance
against an importer forgetting to rebuild copper areas, and that insurance
turned out to cost more than anything else on the board: `GND` is the
largest net, `route_order()` sends supply rails first, so ground took the
best channels near every IC before a single signal net got a turn.

That single fact is what had blocked two correct circuit improvements
for months — 54 footprints routed clean **0 times in 320** attempts, and
shrinking the added parts to a third of their area changed nothing. With
ground off the routing queue, the same 54 footprints route clean. **If a
correct change is repeatedly impossible to fit, suspect what the board is
already spending its routing resources on, not the size of the thing you
are adding.**

Dropping the routed copy is only safe because the pour is now *checked*.
`pour_connectivity()` models the pour from the **real copper** — pad
rectangles, committed traces at their own width, via pads, mounting holes
and pot bosses — each grown by `POUR_CLEAR`, the clearance the emitted
`COPPERAREA` actually declares. It erodes by `POUR_MIN_W`/2 so a hairline
neck cannot count, stamps committed GND traces back in **after** the
erosion at full trace width, ties the layers at every plated GND hole and
via, and floods (`flood()` in `router.c`) from one ground pad. Any ground
pad the flood misses is a DRC problem, exactly like an unrouted net.

**Do not model the pour from `occ`.** It was, and it was wrong by a
factor of two: `occ` reserves `CLEAR + MAX_W/2` around foreign copper
because a *trace* routed there needs room for its own half-width, and a
pour needs clearance and nothing else — 1.6 units reserved against a true
1.0, so every gap on the board read 1.2 units narrower than it is. That
was mostly what produced "the ground pour does not reach N pads". The
copper-based model is *accuracy*, not optimism, and it is still bounded
on the safe side: `dilate()` grows by a box, a superset of the disc.

It also gets silkscreen right for free. A label keeps a *trace* out, so
that "no silk over a trace" holds by construction; it has no business
keeping the pour out, because silkscreen over a ground plane is what every
board does. The `occ` version had to be taught that as a special case, and
until it was, `U1C.10` and `U2B.5` were sealed into pockets whose only
exit ran under a designator.

Four bugs in this model each looked like a routing failure and were not:

* **Eroding the router's own stubs away.** A stub threading a 0.5 mm
  channel between two SOIC pads is precisely the geometry the min-width
  erosion exists to delete. `R1.2` was routed to the plane on all six
  passes and reported unreached on all six.
* **Rasterising a stub as a centreline.** The flood is 4-connected and a
  chamfered 45° segment steps diagonally, so the flood could not walk
  along copper that was really there. Stamp at trace width.
* **Aiming a stub at the *nearest* plane cell.** Nearest is not cheapest
  to reach. Offer A* every reached plane cell nearby **on either layer**,
  plus every ground pad the plane already reaches, and let it choose — the
  top pour around a SOIC pin is chopped up by its neighbours while the
  bottom layer under it is nearly solid ground, so the move a person makes
  without thinking is a via straight down.
* **Modelling the pour with the router's keepout instead of its own
  clearance** — the factor-of-two above.

`plane_stubs()` runs **twice**: once before any signal net is routed and
again afterwards. Which pads sit in a structural pocket is a fact about
the footprints, knowable with only pads on the board, and those stubs are
worth cutting while the channels are still free. Running it only at the
end swapped GND-takes-the-best-channels for GND-gets-whatever-is-left.
`_foreign_static()` caches the half of the mask that cannot move once the
parts are placed, because the whole thing runs about a dozen times a
board.

The placement search is told as well (`plane_nets`): a plane net has no
wirelength and makes no wire demand, so pricing GND's half-perimeter and
RUDY congestion swamped every real signal net. Its pads still count
towards **escape**, because a ground pin does still have to reach the
plane — just not across the board.

Consequence for the user: **rebuild copper areas on import.** That is now
load-bearing, and `validate_fab.py` checks the pour is present on both
layers.

## Six ways a fix can look applied and not be

* **A hardcoded `2` that the compiler cannot see.** Going to four layers,
  every `[0, 1]` and `for L in (0, 1)` in the Python had to become a range
  over `NLAY` - and the one that was missed, `blocked_extra`, is handed
  straight to `router.c`, which indexes it as `(NLAY, NY, NX)`. A
  two-element list on a four-layer board is an out-of-bounds READ, and it
  did not fail cleanly: three of four seeds segfaulted `_router.so` and
  one completed normally, because an overrun of that size usually lands in
  mapped memory and returns garbage instead of faulting. The verdict of
  the run that "worked" was not trustworthy either. **`dmesg` is where a
  ctypes crash says what happened** - the Python side just exits 0 with no
  output, which reads exactly like a timeout. Two more of the same class
  were found by grepping for `(0, 1)` afterwards, one of them silently
  narrowing the plane stubs' target scan to the wrong layers.

Every one of these shipped a commit message that was wrong, and every one
was caught by measuring the ARTIFACT rather than reading the change.

* **The patch never wrote.** A scripted edit hit an assertion partway
  through, wrote nothing, and printed the traceback into a background log
  nobody read. The follow-up test produced plausible numbers, so the
  commit went in describing a `via_ok` memo fix that was not in the file.
  *After a scripted edit, grep the file for the change.*
* **The fix landed in dead code.** A drill-spacing guard went into
  `via_ok()`, which the production router never calls - `ROUTER="c"` is
  the default and `router.c` picks its own layer changes. `validate_fab`
  reported the identical -0.305 mm before and after.
* **A cache served the old answer.** `_place_key()` hashed the netlist,
  the geometry and the weights but not the cost model, so the run after a
  `_rudy` fix printed "reusing the cached layout" and re-routed a pose
  computed under the bug. The key now hashes `place.py`, `anneal.c` and
  `canneal.py`.
* **The checker measured something else.** The rip-up loop scored
  attempts with the ROUTER's bookkeeping, reported "0 unrouted", and
  `verify()` then found `TP1` in two pieces - the router thinks in
  0.25-unit cells. It now scores with `split_nets()`, the same exact
  geometry `verify()` uses.
* **A validation harness that did not contain the case that matters.** Six
  seeds with a known expensive ordering were the harness that (correctly)
  rejected rip-up 0, so it was reused to test a tighter router cap. The
  cap scored `rho` 0.94 against 0.71 for the incumbent - faster *and*
  apparently a better ranker - and was committed. Run over the full
  40-seed field it puts `SEED=16`, the best board this project has, at
  14th of 40, where `--confirm 3` never sees it. The six seeds contained
  nothing like 16. *A harness that has passed before is not thereby the
  right harness for the next question; check that it contains the case the
  change could break.* Cost: one search round, and the revert is
  documented in find_board.py so the next person does not re-derive the
  0.94 and believe it.
* **The problem had already been fixed by something else.** The plane-escape
  placer term was built against "every failing board has a ground pad sealed
  in a pocket before any signal net is routed", which this file asserted and
  which was true when it was written. Screening 40 seeds says otherwise now:
  40 of 40 pour clean pre-routing, with the term AND with `PLANE_GAP=0`.
  `plane_stubs()` running before the signal nets had already closed those
  pockets, and the note describing them had gone stale without anyone
  noticing. *Measure the metric a change targets, in both arms, before
  believing the change did anything.* Cost: one full search round. The term
  was kept - an A/B on routed boards showed 60 problems against 75 over six
  seeds - but for a different reason than the one it was built for.

## Hard-won learnings (each of these cost real debugging time)

**On-board mute buttons made the board 28% wider, and every weight that
was tuned against the narrow board was re-checked rather than assumed.**
Three right-angle push-lock DPDTs (G-Switch PS-22E05) went into the panel
row between the volume pots, taking it from six controls to nine and the
board from 114.3 mm wide to ~146 mm. Three knobs' worth of extra edge is
unavoidable - a control costs panel width whatever the router does - but
the row now has TWO pitches, `PANEL_PITCH` between knobs and
`SWITCH_PITCH` between a knob and a button, because a 2.8 mm plunger does
not need 20.3 mm of edge and pricing it as if it did cost 30 mm of board.

The re-checks, all measured on the same four seeds under the ranking
config, total DRC problems (lower is better):

| change | problems | verdict |
|---|---|---|
| baseline (`W_H=400`, `MUTE_Q_NEAR=40`) | 72 | - |
| `W_H=200` (taller boards) | 108 | **rejected** |
| `MUTE_Q_NEAR=75` (looser JFET anchor) | 84 | **rejected** |

And the one that looked most promising and was not, over eight seeds:
`MUTE_LEDS=0` takes `R37`-`R39` and `J6` off the board and parallels the
button's second pole onto the first, removing six nets and ten pads from
the panel row - the most congested strip on the board. **157 problems
against 137 with the LEDs in.** The indicators are not what is costing the
routing, so they stay; the option stays too, because paralleling the
contacts is a real improvement if you do not want indicators.

Two more, each measured on the four best seeds against a baseline of 54:

| change | problems | verdict |
|---|---|---|
| `CLEAR=0.6` (6 mil, JLCPCB standard) | 106 | **rejected** |
| `W_H=60` (the pre-2026-08 height weight) | 114 | **rejected** |

`CLEAR=0.6` is the one worth understanding, because "give the router more
room between things" sounds unarguable and it made the board twice as bad.
`CLEAR` is not only a routing rule: it sets the courtyard margin the
footprint probe adds, `track_pitch` in the placer, and the escape term's
idea of how many tracks fit down a gap. Turning it down tells the PLACER
that escapes are cheaper than they are, so it packs tighter, and the
router gets a denser board with proportionally the same corridors. **A
constant that feeds both the model and the thing being modelled cannot be
tuned as if it only fed one of them.**

`W_H=400` was tuned when the panel row was 400 units wide and it still
wins at 496, which was not obvious: the whole argument for 400 was "the
panel fixes the width, so height is the only thing left to trade", and a
wider panel makes that argument *more* true, not less. Worth knowing that
the knob did not need re-tuning; worth more that it was checked.

**The failures are in the mute chain, not in the long haul to the panel.**
The obvious theory - buttons on the panel turn every mute net into a
board-spanning two-pad net, the `*_PRE` failure mode times nine - was
wrong, and the diagnostic said so plainly: on the best seed the unrouted
nets were `MID_MUTE`, `LOW_MUTE`, and two filter nets, and the pour missed
`Q1.2`/`Q2.2`/`Q3.2`. `MG1`-`MG3` route fine. The congestion is around the
JFETs on the buffer inputs, which is where it was before the buttons
existed; the buttons just took away the slack. *Read the named nets before
theorising about which nets are hard.*

The re-anchoring that theory produced was kept anyway, on its own merits:
`R37`-`R39` are anchored to their own SWITCH rather than to `J6`, and `J6`
to `SW2`. That is right for the same reason a bypass cap is anchored to its
own op-amp - the resistor sets the current for one button's LED - and it
removes six board-spanning nets by construction. Measured over four seeds
it is 69 against 72, which is noise; it is in because the reasoning stands,
not because the number moved.

**Every ground symbol was missing from all eight subcircuit diagrams.**
`_shape_bbox()` had no case for the `F~` net-flag shape, so the crop
predicate dropped every one of them, and the diagrams looked finished. This
is the same failure the subcircuit code's own comment warns about, one
layer down: a diagram drawn by the same code as the sheet cannot drift in
STYLE, but it can still drift in CONTENT if the filter silently discards a
shape kind. If a generated picture is missing something, check what the
filter can and cannot see before checking the drawing code.


**Board-size sweeps cannot fix layout problems.** Kept here because the
reasoning still applies to anything that looks like "just give it more room".
Causes found, in order of discovery:

1. **Pot spacing, not board area.** Wedging a third pot into the gap between
   two existing ones left ~1.4 units of clearance and starved neighbouring
   pins' escape routes. Fixed with a uniform `PANEL_PITCH`.
2. **Route order.** `route_order()` sorts smallest-net-first, which sent `GND`
   (a dozen-plus pads) to the back of the queue; by its turn, every cell near
   the IC ground pins was taken. Supply rails now go first.
3. **Long-haul 2-pad nets.** `*_PRE` nets (filter output → volume pot) are the
   *smallest* nets but cross nearly the whole board. Member count is not a
   proxy for distance. `route_order()` now promotes the four widest-reaching
   nets, **measured from the actual placement** (`LONG_HAUL`) — it used to
   name `LP1` explicitly, which became meaningless once the layout stopped
   being hand-tuned.
4. **A silkscreen label parked in a pin-escape corridor.** `U1D.13`/`U2D.13`/
   `U2D.14` were unroutable at *every* board size tried. The cause was the
   U1/U2 designator sitting directly above the top-left pins, whose silk
   keepout blocked the only channel those pins have at `IC_ROT=90`. Moving the
   label beside the package fixed it. **Silk keepout is routing-relevant** —
   labels are not cosmetic.

**Absolute coordinates rot.** Anchors tuned for an older, narrower board
(SOICs, terminal blocks, C0) stayed left-anchored while the pot row grew the
board, leaving the right third empty. This is now structurally impossible —
nothing has a hardcoded position any more — but the lesson generalises to any
constant tuned against a board that has since changed shape.

**Footprints are drawn once, in their own frame.** A transform stack
(`xf_push`/`xf_pop`) rotates them. Before that, rotation was open-coded per
footprint, which meant only chips and SOICs could rotate at all, and
`fp_soic14` at `rot=90` laid its pads out from `x` rightward so the package
centre silently landed at `x+15` — every "centred" SOIC was 15 units off. Both
bug classes are gone; don't reintroduce a per-footprint rotation branch.

**Text anchors rotate; glyphs don't.** Silkscreen is kept upright for
legibility, so rotating a part moves its label's anchor while the text still
runs left-to-right — which can fold the label back across the part it names.
That is not cosmetic: silk reserves copper, so a label lying over a pin is a
pin that cannot escape. `silk_ref_beside()` picks the side in the *local*
frame and lays the text out in *board* space. `verify()` caught this as "silk
U2 sits over pad U2A.1".

**The placement model is probed, never re-derived.** `probe()` draws each
footprint into a scratch buffer at each allowed angle and reads its geometry
back off the shapes it actually emitted. A second, hand-maintained copy of
those dimensions is exactly how the dual-gang pot came to be drawn with its
body hanging over the board edge while every check thought it fit.

**Verify footprints against real manufacturer data, not other footprints.**
The single-gang pot footprint was first copied from the dual-gang one — 5.08 mm
pitch where the real RK097 is 2.5 mm, ~2x too wide. LCSC/EasyEDA expose real
footprints; fetch and *look at the rendered PNG*, not just the raw SVG numbers:

```sh
curl -sS "https://easyeda.com/api/products/<LCSC-CODE>/svgs"   # has a `png` url
```

Also: don't assume same-family parts share a body size. The dual-gang pot is
physically larger than the single-gang one because its pads sit on a wider
pitch.

## Router behaviour and performance

The router is a 4-directional grid A* with a weighted (1.02x) heuristic, a
relaxed second-pass retry for nets the strict pass misses, and 45° chamfering
applied to finished polylines. Relaxed retries are **safety-gated**: every
candidate gets an exact-geometry clearance check before being committed, after
a bare relaxed retry once produced a real −5 mil via overlap. `via_ok()` stays
strict even in relaxed mode.

**Production runs: 5.95 s -> 5.11 s (14%), and how to measure that
honestly.** Profiling a WARM production run (cached placement and learned
route order, the thing you actually run to regenerate the board) put the
Python cost here:

| | tottime | calls |
|---|---|---|
| `croute.route` (real C time) | 1.70 s | 146 |
| **`_block` + `cells_in_rect`** in the pour | ~1.2 s | **~280k** |
| **`sorted`** in `croute` | 0.72 s | 608 |
| `stamp_disc` | 0.62 s | 37k |

Both were fixed, both bit-identical:

* **The pour's segment stamping is compiled** (`geom.c: block_segments`).
  `pour_connectivity()` walks every non-GND trace sampling it every
  `GRID/2` and stamping a square - 280000 interpreter round trips. The C is
  a transcription, same sample count and cell arithmetic, so the mask is
  bit-identical; `CGEOM_CHECK=1` asserts it. Rasterising the capsule
  analytically would be faster and NOT identical, which is not a trade
  worth making in the checker that decides whether the board can be built.
* **`croute` sorts its cell sets with `np.lexsort`, not `sorted()`.**
  `plane_stubs()` offers A* every reached plane cell as a target: **1.04
  million target triples** over a run, one call alone sorting 132492.
  `np.lexsort` with the keys reversed gives exactly the tuple ordering, so
  a run stays reproducible; checked against `sorted()` over 200 random
  sets.

**Two measurement lessons, both of which cost a wrong conclusion first:**

* **cProfile overstates call-heavy Python by roughly its own per-call
  overhead.** The pour loop profiled at ~1.2 s and was really ~0.34 s -
  280k calls x ~2 us of profiler is most of the difference. Use the profile
  to RANK candidates, never to predict the size of a win.
* **Three runs cannot see a 10% effect here.** Run-to-run variance on this
  box is +-11%, so the first A/B of the sort change read as "no change".
  Interleave the two arms and compare MEDIANS over 6+ runs: base 5.75 /
  5.95 / 6.00 (min/median/mean) against 4.70 / 5.11 / 5.12.

What is left is mostly the compiled router (~1.7 s of ~5.1 s) and
`stamp_disc`; the pure-Python A* (`astar_py`, `passable`) is **not** on the
production path at all - see the note below about counting calls first.

**Runtime is no longer dominated by the router — profile before optimising
it.** That sentence used to read "Runtime is dominated by A* in pure
Python", and it stayed in this file long after `router.c` made it false.
The first profile ever taken of a ranking trial said:

| | cumulative | calls |
|---|---|---|
| `croute.route` (compiled) | 18.6 s | 375 |
| **`path_clearance_ok`** | **32.4 s** | **38** |
| `plane_stubs` | 33.9 s | 5 (nearly all the above) |
| `gap` / `d_seg_seg` / `d_pt_seg` | 41.5 s | 3.4M / 4.0M / 16.4M |

The compiled router was **under a quarter** of the run and the exact
geometry was most of it. Two exact fixes took a trial from 57.3 s to
34.1 s (1.68x) with **byte-identical verdicts on all three test seeds**:

* **`path_clearance_ok` never got the bounding-box pre-filter.** It compares
  every candidate segment against every piece of foreign copper - a few
  hundred thousand pairs - where `verify()` has skipped the far ones since
  it was the slow one. Box-to-box distance is a lower bound on the real
  gap, so this is exact. It matters more here than in `verify()`:
  `plane_stubs()` calls it once per ground stub and the relaxed retry once
  per candidate path.
* **`stamp_disc` rebuilt the same disc ~94000 times.** `commit_path()`
  stamps one per *cell* of every finished trace, and each call allocated
  two `np.mgrid` coordinate grids to compare against a radius. Every centre
  is grid-aligned and there are two radii on the board.

Two things measured and NOT kept, both worth not re-trying:

* **Hoisting the closure out of `d_seg_seg` and unrolling `d_seg_rect`.**
  Sound in principle - 950k closure creations a run - and worth **zero**:
  27.5/43.6/32.1 s against 27.5/43.4/31.5. After the pre-filter those
  functions' own tottime is a couple of seconds profiled, which is a
  fraction of a second real. Reverted rather than keep unreadable code for
  an unmeasurable gain.
* **"The ctypes marshalling is copying the whole grid per net."** It is
  not. `np.ascontiguousarray` on an already-contiguous matching-dtype array
  returns the *same object*, and `OCC`/`CONTESTED` are already one
  contiguous `(2, NY, NX)` block passed straight through. What made it look
  like 16 s of Python was cProfile: a ctypes foreign function gets no frame
  of its own, so its real C time is billed to the Python frame that called
  it. **Read a profile of ctypes code with that in mind.**

What helped historically, and what didn't:

* **Works: parallelism.** 4 cores; sweeps run 4 sizes at once
  (`scratchpad/psweep.sh` pattern). Straight ~4x.
* **Works: `SWEEP=1`.** Skips all artifact writes. Also important for
  correctness — without it, sweep runs *overwrite the committed board's*
  PNG/SVG/JSON/BOM with a candidate that may not verify.
* **Works: `via_ok` memo.** Hottest call in the router (once per expanded node,
  six numpy ops on an 11×11×2 window, same cell re-checked from up to four
  neighbours). Memoised per A* call — occ/contested can't change mid-search, so
  it's exactly equivalent, not an approximation.
* **Reverted: capping A* expansions.** Looked like a big win because failing
  searches give up early — but it silently broke *routable* nets (one config
  went from 1 failing net to 6 at 300k; 1.2M still cost routes). There is no
  cap that speeds up the hopeless case without also breaking the merely
  difficult one. `MAX_EXPAND` is left effectively disabled.
* **`GRID=0.5` was tried as the ranking config and REJECTED.** The old note
  here said it was "useful for ranking two placements against each other".
  Measured against the six seeds with a known rip-up-6 ordering, it is not:
  it puts seed 2, genuinely third best, **dead last** — the same failure
  that disqualified rip-up 0 — for a Spearman rho of 0.37 against 0.94 for
  the committed config. It is also only ~1.4x, not the ~3x folklore said,
  because `verify()` and the exact-geometry checks do not scale with the
  routing grid at all; only the router does, and the router is now under
  half of a trial. Production stays at 0.25, and so does search.

Annealer bugs worth not rediscovering, all of which looked like "the search
just isn't very good":

* **A cost cached across a changing weight.** The overlap weight is ramped
  every step, so a total computed under earlier weights is stale immediately
  and the accept/reject test silently stops meaning anything — it froze the
  search for thousands of moves at a stretch. Score both sides with the
  *current* weights.
* **Move amplitude tied to temperature.** The temperature has to start high
  enough to accept a costly move; if the jump distance scales with it, the
  first few thousand moves fling parts hundreds of units apart and the rest of
  the budget is spent walking them back. It reached 1710×2443 units that way.
  Amplitude gets its own schedule.
* **"Be the outermost part" is the wrong edge constraint.** Parts facing the
  same edge with different depths (the dual-gang pots reach 15 units further
  than the single-gang ones) can never all be flush, so the penalty never
  reaches zero and the anneal keeps stretching the board trying to pay it off.
  What matters is that nothing is *between* the part and its edge, within its
  own column.

Expect ~2–6 minutes per full run at `GRID=0.25`. Run long jobs in the
background with a timeout; don't poll them in a tight loop.

## Design decisions worth not relitigating

* **Trace widths (12 mil signal / 16 mil power)** are chosen for *pad
  proportion and robustness*, not current capacity. IPC-2221 gives 13–21x
  margin at every width considered; 8 mil would carry the current fine but
  reads as an error next to 40 mil pads.
* **Volume pots are passive attenuators** between each filter output and its
  terminal block. All 8 op-amp sections are committed to the filters; the
  outputs are low-impedance so they drive a 10 kΩ pot with no effect on
  response, and a divider only attenuates, so there's no new clipping risk.
* **Audio (log) taper** for volume, linear for the frequency pots.
* **Panel order is LOW → HIGH, left to right** (user preference), with each
  volume trim beside the frequency pot of the band it belongs to. The row is a
  rigid group in the placement search — it only moves as a unit.
* **`PANEL_PITCH` (80 units ≈ 20.3 mm) is a human-factors number, not a
  routing one.** A knob for a 6 mm shaft is typically 15–20 mm across, so
  much under 20 mm pitch has adjacent knobs touching. The pot *courtyards*
  would allow ~58 units, which is why this can't be left to the optimiser —
  it has no model of the thing that actually sets the limit, which is
  fingers.
* **Per-output ground returns** — each output terminal block carries its own
  ground, because each runs to a separate amplifier; sharing one invites hum
  loops.
* **Two 33nF in parallel** for the tuning caps: 68nF isn't stocked in a
  suitable dielectric at 50 V, and this keeps every tuning cap the same part
  from the same reel (matching matters more than absolute value here).

## Current state / open threads

### The board does NOT currently verify clean. Read this first.

The committed board is `SEED=7 ROUTE_SEED=0`, **146.3 x 74.7 mm**, 86
footprints, 242 pads, 240 tracks, 155 vias. It has **10 DRC problems**:
eight nets left in pieces - including **both supply rails** - one
ground-pour line (`Q3.2`, `U2B.5`, `U3D.12`), and the wasted-area note,
which is not a defect. It is committed anyway, because the alternative was
leaving a board in the repo that no longer matches its own schematic - but
**it is not orderable as it stands** and nothing here should be read as
saying otherwise.

Worth noting for whoever picks this up: the capped RANKING run of the same
seed and route order also scores 10, but fails on *different* nets
(`MID_MUTE`, `LOW_MUTE` and two filter nets, with the pour missing all
three JFET sources). Same count, different composition - so the count is a
fair ranking signal and a poor description of what is wrong. Read the
list, not the number.

What broke it: the three panel mute buttons (see below). The board before
them was 114.3 x 77.5 mm with 83 footprints and verified clean.

**What was tried, all measured, none of it enough.** Roughly 115 seeds
were routed, plus route-order sweeps on the best of them, and the floor
never came below 10 problems:

| lever | result |
|---|---|
| ~115 seeds, ROUTE_SEED=0 | best 10 (SEED=7), second 11 (SEED=99) |
| ROUTE_SEED 0-3 on the best 6 seeds | best unchanged; SEED=61 16 -> 13 |
| re-anchoring the LED resistors to their own switch | 69 vs 72 over 4 seeds - noise |
| `MUTE_LEDS=0` (six nets and ten pads out of the panel row) | 157 vs 137 over 8 seeds - WORSE |
| `MUTE_Q_NEAR=75` (room around the JFETs) | 84 vs 72 - worse |
| `W_H=200` / `W_H=60` (taller boards) | 108 / 114 vs 72 and 54 - worse |
| `CLEAR=0.6` (6 mil, JLCPCB standard) | 106 vs 54 - much worse |

**The failures are at the output-buffer end, not at the buttons.** On the
best seed the unrouted nets are `MID_MUTE`, `LOW_MUTE` and two filter
nets, and the pour misses `Q1.2`/`Q2.2`/`Q3.2` - the JFET sources. The
buttons did not congest the panel row so much as take away the slack that
the mute cluster around `U3` was living on. `MUTE_LEDS=0` proves the
point: removing six nets and ten pads from the panel row made the board
worse, not better.

**The levers NOT yet tried, in the order worth trying them:**

1. **Four layers.** This is the honest answer to a two-layer board that is
   35% utilised and still out of channels. It is also the one CLAUDE.md
   has been pointing at since the fill-term round: "if the board genuinely
   needs to be smaller, the lever is more layers or finer design rules".
   Finer rules have now been measured and rejected (`CLEAR=0.6`, above),
   which leaves layers.
2. **A thinner signal trace** (8 mil rather than 12) WITHOUT touching
   `CLEAR`. The two were never separated - `CLEAR=0.6` moved the placement
   model as well as the routing rule, and that is what made it worse. Trace
   width feeds `track_pitch` too, so the same care applies, but `SIG_W` and
   `CLEAR` are not the same experiment and only one of them has been run.
3. **Negotiated congestion routing**, which was tried once, would not
   converge, and is documented above with two specific things to fix if it
   is retried.
4. **Giving the buttons their own row on a daughterboard** - i.e. undoing
   this change. Kept last deliberately: it is a real option and the user
   chose the on-board version knowingly.

Beyond the filter the board carries: a master volume, per-band volume, a
47 kΩ line input, buffered outputs with build-out resistors, DC blocking
and bleeders, bulk and per-IC supply decoupling, and **per-output muting
with power-up soft start** - a JFET shunting each buffer input, steering
diodes so one button mutes one band, and an RC that holds everything muted
until the rails settle then re-mutes fast when they collapse. The three
mute buttons are now **on the board**, in the panel row; `J6` carries only
the optional LEDs. No audio leaves the board.

**Width is set by the panel, not by routing.** Nine controls - six knobs
at `PANEL_PITCH` and three buttons at `SWITCH_PITCH` - come to 146.3 mm
and every seed lands there. A control costs panel edge whatever the router
does.

**The empty space on the board is ROUTING space, and squeezing it out
costs nets.** This is the answer to "there is a 24 x 29 mm hole, use it",
and it took building the term to find out.

A column-load ("fill") term was added to the placer: divide the width into
strips, give each part's area to the strips it covers, and price the
imbalance as a sum of squares. That is a proper surrogate for height -
board height is really its fullest strip - and unlike `h` it gives EVERY
part a gradient towards an empty column, not just the topmost and
bottommost. Mirrored in `anneal.c`, struct layout checked field by field
(`sizeof` and every offset identical).

Two bugs in it worth not repeating, both found by measuring:
* **Bucketing on the current extent makes the extent a free variable.**
  Widening the board widens every strip and lowers the imbalance for free,
  so at a high weight the anneal pushed the board out to 120.7 mm and paid
  the `w` penalty to do it. Bucket on `wfloor`, which is fixed.
* **The outer strips have to be unbounded.** Otherwise a part shoved past
  the last strip falls outside every strip and contributes no load at all,
  so the cheapest way to even out the columns is to push parts off the
  edge.

Then the term itself, measured, and **it does not work**:

| W_FILL | mean height, 4 seeds | mean utilisation |
|---|---|---|
| 0 | 80.8 mm | 34.3% |
| 1000 | 79.1 mm | 34.5% |
| 4000 | 83.9 mm | 33% |
| 15000 | 86.4 mm | 32% |

Utilisation is **flat at ~34% whatever the weight** - the term
redistributes area, it does not densify. And routing it is worse: over 21
seeds at `W_FILL=1000`, **0 verified clean**, where the same field at 0
produced `SEED=4`. Spearman rho between board height and DRC problem count
across that field is **-0.41**: the shorter the board, the more problems.
The four shortest averaged 24 problems, the four tallest 12.5. `SEED=4`
itself goes from **1 problem at 77.5 mm to 39 problems at 70.6 mm**.

The mechanism is visible in the cost breakdown, so this is not just
correlation: compressing the layout took `esc` from 8865 to 23703, and
escape starvation is exactly the failure that leaves nets unroutable here
(see the pin-escape note above). The parts are not lazily spread out; the
gaps between them are the corridors the nets use.

So the term is REVERTED. What survives is `WIDTH_LOG=1`, which now reports
utilisation and the largest empty rectangle from the placement alone, and
this entry. **Before trying to fill the empty space again, note that this
is the fourth time optimising the placement surrogate harder produced
boards the router rejected** - MOVES=90000, RESTARTS=4, and now W_FILL.
If the board genuinely needs to be smaller, the lever is more layers or
finer design rules, not tighter placement.

**And because the width is fixed, the HEIGHT is the whole objective - so
price it accordingly.** `w` only charges for width past the panel floor,
which makes the width up to that floor free; at `W_H=60` the anneal did not
spend it. The right-hand fifth of the board carried 10 of 222 pads while
the middle ran at 1.7x the average density, and the board was 102.9 mm
tall. That is structural, not a bad seed: height is a BOUNDING BOX, so only
the topmost and bottommost parts get any credit for moving and everything
in between sees pure wirelength cost for spreading sideways.

`W_H=400` makes that trade worth taking. Four seeds routed at each of
60/150/300/400 - **not monotonic**, 150 is worse than either neighbour on
both height and DRC count - but 400 produced **114.3 x 77.5 mm, verified
clean**, against 102.9 mm for the board it replaced. A quarter less board.

Watch the right metric. `WIDTH_LOG=1`'s coefficient of variation got
*worse* across that change (0.464 to 0.606) because it is normalised on the
occupied extent: the mass moved right (the two right-hand columns went from
26 pads to 43) and the left thinned out. Shorter and less even. Height and
utilisation are the objective; the histogram says where the parts went.

**Route order does real work at this size.** A blind 24-seed sweep found
nothing; sweeping ROUTE_SEED 1-3 over the five best of those seeds found
two clean boards in 15 trials. A seed stuck at the same problem count
across every route order has a structural pocket rerouting cannot open; a
seed that moves has one the pour just needed the traces to fall differently
around. Sweep route orders on near-misses before searching more seeds.

**The router is no longer single-pass.** `route_with_ripup()` throws the
whole route away and runs it again with whatever failed promoted to the
front, keeping the best attempt and hill-climbing from the best order
rather than the last. That is what made 63 footprints reachable at all:
the same part count was **0 clean in 96** before it, and the first search
after it found a clean board in 24 seeds. The learned order is cached
beside the placement, so regenerating the committed board routes clean on
attempt 0.

**The placement anneal is compiled too** (`anneal.c`, loaded by
`canneal.py`; `PLACER=py` forces the Python one). After the router moved to
C, placement was 85% of a cold run - 26.6 s of 31.2 s - and a board search
is hundreds of placements, so it dominated the whole workflow. It is now
**1.1 s**. `place.py` remains the readable reference: the cost model is
written and explained there, and the C version applies the same terms with
the same weights. The RNGs differ, so a given `SEED` does *not* give the
same layout in both - measured head to head on three seeds, the C one gave
5/14/13 DRC problems where Python gave 19/25/15, so it is not a quality
regression.

**The search cap cannot deliver a VERDICT, only a ranking.**
`MAX_EXPAND=400000` was a fair filter at 54 footprints. At 63 it is not:
it reported **0 clean out of 96** where the best candidate in that same
set verifies with a single problem when re-run uncapped. More nets means
more *failing* A* calls per board, and the cap turns "hard" into
"impossible" for all of them at once.

The advice here used to be "pass `--env MAX_EXPAND=0` and skip the
filter". That is no longer right, and the reason is worth keeping: the cap
is bad at saying whether a board is clean and *good* at saying which board
is cleanest, and those are different jobs. `find_board.py` now does both -
rank every seed capped, then re-run the best few uncapped - so the cap
never produces a verdict anybody acts on.

**Search uses a capped router** (`MAX_EXPAND`, set by `find_board.py`, never
in production). A *failing* A* is far more expensive than a passing one -
it drains the queue over the whole reachable grid - and a search spends
most of its time on boards that fail.

The cap is **400000**. A tighter 150k was tried and reverted, and *how it
fooled the validation harness* is the part worth keeping.

Against the six seeds whose rip-up-6 ordering is known, 150k looked
strictly better - faster AND a closer ranking (Spearman rho against that
ground truth):

| config | ordering | rho | per trial |
|---|---|---|---|
| rip-up 1, cap 150k | `4 1 2 3 5 6` | 0.94 | ~21 s |
| rip-up 1, cap 400k | `1 4 5 2 6 3` | 0.71 | ~34 s |
| rip-up 0, cap 400k | `1 5 4 3 6 2` | 0.43 | ~35 s |
| GRID=0.5, cap 400k | `4 1 6 5 3 2` | 0.37 | ~25 s |

Then it was run over the whole 40-seed field and checked against what a
search actually has to get right - keeping the seeds that verify well
UNCAPPED inside the handful that get confirmed:

| seed | uncapped | cap 400k | cap 150k |
|---|---|---|---|
| **16** | **1 defect** | **3** | **16** (14th of 40) |
| 37 | 3 | 3 | 12 |
| 28 | 3 | 3 | 15 |
| 34 | 5 | 5 | 9 |

`SEED=16` is the best board found since the plane-escape term went in. At
400k it ties for top of the field and gets confirmed; at 150k it lands
14th and `--confirm 3` never looks at it. **The six-seed rho harness
contained no seed like 16, so it blessed a config that loses the winner** -
the same failure that disqualified rip-up 0 and `GRID=0.5`, but hiding
behind a *better* correlation instead of an obviously worse one.

So: rank correlation over a handful of seeds is not sufficient evidence to
move this knob. The test that decides it is whether the seeds with known
good uncapped verdicts stay in the confirm set.

It is still a pessimistic filter, so confirm any winner with an uncapped
run. Capping in *production* was tried twice and reverted twice: it
silently breaks routable nets.

**Run times, after the optimisation work** — a full cold run is ~33 s
(placement ~25 s, routing ~5 s, verify ~3 s); a re-run reusing the cached
placement is **~4.5 s**. Before: ~25 minutes. (Those are *production*
numbers on the committed board with a learned route order; a SEARCH trial
on a cold seed is the ~21 s in the table above, because it routes with
rip-up and has no learned order to start from.) What did it:
* `router.c` — the grid A* compiled and called via ctypes. Routing was
  ~20 min of every run.
* `anneal.c` — the placement anneal, once routing stopped being the
  bottleneck and placement became 85% of the run.
* **Placement cache** (`.place-cache/`) — placement is deterministic in its
  inputs, and route-order search re-runs the same placement many times.
  Keyed on the probed footprint geometry and every placement weight, so
  editing a footprint invalidates it.
* **Bounding-box pre-filter in `verify()`** — the clearance and
  connectivity checks consider ~2.9M pairs but only a handful are near each
  other. Expanded-box distance is a lower bound on the real gap, so
  skipping the rest is exact, not approximate.
* **Separable `dilate()`** — a box dilation is the Minkowski sum with a
  box, and a box is a horizontal segment summed with a vertical one, so
  dilating along x then along y is *identical* in `2(2r+1)` passes rather
  than `(2r+1)^2`. At `r=4` that is 18 passes against 81; measured 5x on a
  1600x1400 grid, and checked bit-for-bit against the square version over
  random grids and every single-cell border position. It is on the hot path
  of `pour_connectivity()`, which runs about a dozen times per board.
* **The plane pre-filter** (`PLANE_PREFILTER`) — not a speedup of a run,
  but of a SEARCH: it settles in ~1.5 s whether a placement's ground plane
  is reachable at all, so `find_board.py` never routes one that cannot work.
* **`geom.c`** — the exact copper-to-copper pair scans, compiled. Three
  of them: the clearance scan and the connectivity scan in `verify()` /
  `split_nets()`, and the silkscreen-over-copper scan. `cgeom.py` loads it
  the way `croute.py` loads the router, the Python stays the reference
  implementation, and `CGEOM_CHECK=1` runs both and asserts they return
  the same pairs. **Port all the loops, not the ones the profile names.**
  Doing only the first two bought 7%, because `d_seg_rect` was still being
  called 175000 times a run from the silk check - which had no bounding-box
  pre-filter at all and was invisible under `verify()`'s own line in the
  profile.
* **The bounding-box pre-filter in `path_clearance_ok`** and **the disc
  cache in `stamp_disc`** — 1.68x on a trial, verdicts unchanged. See
  "Runtime is no longer dominated by the router" above; these were found by
  profiling rather than by reasoning about which part *ought* to be slow,
  and the part that ought to have been slow was not.

Measured and NOT worth doing: caching the anneal's cost across moves
(changed the search trajectory and cost a verified board), and replacing
numpy with plain Python in the placer (numpy is still ~3x faster at n=49).
* **Seed choice still matters, but far less than it did.** With rip-up,
  1 seed in 24 verified clean at 63 footprints; without it the same part
  count was 0 in 96. `tools/find_board.py` routes candidates in parallel
  and reports which verified - re-run it after any change that moves the
  layout, and never trust a placement that has not been routed. It ranks
  capped and confirms uncapped by itself now, so the old advice to pass
  `--env MAX_EXPAND=0` is obsolete: the cap is unfair as a VERDICT above
  ~54 footprints (0/96 clean where the best verified with one problem
  uncapped) but is the best RANKER measured.
* **Do not turn up `MOVES` or `RESTARTS`.** Both are measured, and both get
  *worse* above their committed values (see docs/pcb-notes-smd.md). The
  anneal optimises a surrogate; fitting it harder fits the router less.
* **The bypass caps were 37-63 mm from their op-amp supply pins; they are
  now 4.2-10.0 mm** (`BYPASS_NEAR`, on). It was switched off for one round
  because with it on nothing routed across 208 seed/route-order
  combinations - a budget problem disguised as an engineering one, since
  each attempt then cost ~30 s. Compiling the anneal took a trial to
  ~2.5 s and the next search found a clean board in 80 trials. See
  docs/design-review.md §1.
* **Negotiated congestion routing was attempted and reverted.** Every net
  routed (0 unreached, which the single-pass router never manages) but it
  would not converge: ~500-1000 cells stayed contested at any pressure, on
  a placement the single-pass router routes with zero violations. Two
  things learned, both worth keeping if it is retried: the conflict test
  is a net's CENTRELINE inside another net's KEEPOUT (intersecting two
  keepouts double-counts the gap and plateaus at ~11000 contested cells),
  and present-cost must not be ramped so hard that history never gets a
  say. It is a coordination deadlock the per-net rip-up granularity does
  not escape; rip up *regions*, not single nets.
* **The circuit fixes that were blocked are now ON and shipped.** Output
  DC blocking (`OUTPUT_CAPS`, `C11`-`C13`) and bulk supply decoupling
  (`BULK_CAPS`, `C9`/`C10`) default to on in gen_schematic.py; set either
  to `0` to build without. History worth keeping: with GND still routed as
  a net, 49 footprints routed clean ~1 try in 80, 52 was 0 in 320, 54 was
  0 in 320, and shrinking the added parts to a third of their area changed
  nothing (0 in 160). None of that was about area, and none of it was
  fixed in the router - see "Ground is a plane" above.
* **Determinism is not free and is worth checking.** A `set` of designators
  was iterated to assign rotations; Python randomises set-of-string
  iteration per process, so part indices moved, so the annealer's move
  sequence moved, and the same SEED gave a different board about one run in
  three - which silently invalidated a whole round of searches. If a search
  result will not reproduce, suspect ordering before anything else.
* **The placer now prices what actually rejected boards: a plane-net pad
  needs room for a VIA.** Every board this project failed to verify failed
  the same way - `the ground pour does not reach N pad(s)`, always at an
  op-amp ground pin (`U1C.10`, `U2C.10`, `U1D.12`, `U2B.5`) or a bypass
  cap's, always a sealed pocket formed *before any signal net is routed*.
  The escape term could not express it: it is capped on pad count, so a
  SOIC face asks for four track pitches and scores the same whether the gap
  in front of it is three units or nine - and three fits no via. `pesc`
  (`place.PLANE_GAP`, 4.4 units = `VIA_PAD + 2*CLEAR`, weight 60) prices
  the shortfall per plane pad per side, sharing `escape_of()`'s neighbour
  scan so it costs four multiplications rather than a second pass. Mirrored
  in `anneal.c`; `canneal.Cfg` must stay field-for-field identical.
  **Measured, weakly positive.** It does NOT change the pre-routing pour
  verdict (see the pre-filter below), so what it buys is room for the pour
  to survive signal routing. Six seeds, same capped router, problems with
  against without: 2/11/13/3/15/16 = 60 against 6/18/12/6/9/24 = 75. Four
  seeds better, two worse, 20% fewer problems overall - a better average
  placement, not a guarantee for any one seed. It also moves every
  placement, so `SEED=38`, which routed clean, does not any more.
* **The structural pre-filter.** `PLANE_PREFILTER` pours the board with
  only pads on it and asks whether every ground pad can reach the plane. A
  pad unreached there can never be reached later - signal traces are
  foreign copper, they only take room away - so the verdict is final and
  costs one stub pass instead of seven full routes. `find_board.py` screens
  every seed with it first (`PLANE_PREFILTER_ONLY=1`, ~1.5 s each against
  several minutes) and routes only the survivors. Default: off in
  production, on under `SWEEP=1`.
  **It currently rejects nothing** - 40 seeds out of 40 pour clean before
  routing, with `pesc` on *and* with `PLANE_GAP=0` - which dates the
  "sealed pocket before any signal net" note above: those were closed by
  running `plane_stubs()` before the signal nets, not by the placer. The
  pour failures that remain are made by the signal traces. Keep the screen
  anyway: exact, one second, and it catches the thing that used to cost a
  search round.
* **The remaining lever is the router.** Route *order* still matters, which
  is why `route_order()` and rip-up exist. Negotiated congestion routing
  (route everything, price up contested cells, reroute) deletes that whole
  category; it was tried once and would not converge (see below), and
  `router.c` already carries the `use`/`hist` cost terms unused.
* Not built or measured — verified against its own netlist and geometry only.
* Hand-drawn footprints still to confirm against datasheets before ordering:
  SOIC-14, the 10 µF electrolytic, the dual-gang pot body. The pot
  locating-boss holes are **gone**, not fixed: they were a guess, LCSC's
  footprint API now answers 403, and a hole in the wrong place is a
  re-order where a missing one is a hand drill.

## Recent agent edits

2026-08-20: Small performance-focused refactor applied to the validation
step. `tools/validate_fab.py` now tokenises the emitted shape strings once
via a `parse_shape_tokens()` helper and re-uses those token lists in the
various import/fab checks instead of repeatedly calling `split("~")` in
hot loops. This change is internal-only (behaviour preserved) and aimed at
reducing CPU/time spent validating the generated PCB artifact so agents
and CI can iterate faster.

2026-08-20: Follow-up low-risk optimisations added by the agent:
- `tools/gen_pcb_smd.py`: cached the expanded bounding-box arrays used by
  `path_clearance_ok()` to avoid rebuilding them on every candidate check;
  the cache is cleared by `commit_path()` when occupancy changes.
- `tools/gen_pcb_smd.py`: `_expanded_boxes()` now returns contiguous
  `numpy` arrays to improve vectorised throughput and safer interop with
  compiled helpers.
- `tools/croute.py`: added a pure-Python `flood()` fallback so the toolset
  can run (slowly) when compiled libraries are not present.
 - `tools/gen_pcb_smd.py`: added a spatial-hash (uniform grid) index into
   the `path_clearance_ok()` cache to avoid scanning all existing copper
   features for each candidate; this significantly reduces Python-level
   pair-filter overhead on dense boards.

Note: edits were applied in-workspace; please review and commit/push as you
prefer so other collaborators and CI see the change.

## Agent decisions & notes (2026-08-20)

- A tracked performance plan was created to reduce placement and routing
  time while preserving quality (profiling, Numba/C fallbacks, adaptive
  routing caps, parallel confirmation, and CI benchmarks).
- Recommendation: add a `if __name__ == '__main__':` entry to `tools/gen_pcb_smd.py`
  so the generator can be invoked programmatically for reliable dry-runs,
  unit tests, and targeted profiling.
- Quick dry-run: an attempt to run `gen_pcb_smd.py` in `PLANE_PREFILTER_ONLY`
  mode with the Python router in the sandbox produced no visible output;
  the run could not be confirmed here.
- User instruction: no implementation work performed beyond documentation —
  the user asked to pause further changes; this note records the
  recommendation and the current state.

Next step (awaiting approval): add the lightweight programmatic entrypoint
to `tools/gen_pcb_smd.py` and re-run the fast prefilter locally or in CI to
collect timings.  Do not proceed until you grant permission.

## Agent edits (2026-08-20 — post-approval)

Applied three pure-optimisation edits to `tools/gen_pcb_smd.py` that
preserve every routing decision and verification verdict:

1. **`via_ok` memo no longer cleared per-A* call.** The key already
   carries `OCC_VERSION`; only `commit_path()` changes occupancy.  The
   per-call `VIA_MEMO.clear()` in `astar_py()` threw away answers that
   were still exactly correct — the memo is the router's single hottest
   win (once per expanded node, 11×11×2 numpy window).  Removed that
   clear; memo now lives across searches until `OCC_VERSION` increments.

2. **`_GAP_CACHE` cleared only in `build_features()`, not on every
   `commit_path()`.** `gap()` keys on feature-object identity (`id()`);
   those objects are only replaced when `build_features()` rebuilds the
   `FEATURES` list (once per rip-up round).  Clearing it per-commit
   threw away answers the relaxed retry had already paid for and were
   still exact — they get re-used for every subsequent candidate against
   the same live objects.

3. **`_PATH_CLEAR_CACHE` invalidated by simple `= None` instead of
   per-commit `np.concatenate`.** The old incremental branch copied the
   entire growing arrays on every trace commit — O(C×N) total, where C
   is ~400 commits and N ~ 700 features.  Now the cache is just
   invalidated; the next `path_clearance_ok` rebuilds from scratch in
   O(N) (~1 ms).  Strictly fewer allocations, identical verdicts.

4. **`_pad_span` cached per placement.** `route_order()` key called it
   once per net per pass; it rescanned every pad of that net each time
   — O(pads²) in the worst case.  Now cached in `_SPAN_CACHE` on first
   use; zero cost thereafter.

5. **`passable()` bounds re-check removed.** The neighbour loop only ever
   steps ±1 from cells already known in-range, so the `1 <= x < NX-1`
   test was pure overhead in the hottest inner loop.

All changes are internal; no verdicts change.  Run `python
tools/gen_pcb_smd.py` (or the fast prefilter `PLANE_PREFILTER_ONLY=1
SWEEP=1 ROUTER=py python tools/gen_pcb_smd.py`) to verify behaviour.

**Measured afterwards, and the honest result is ~0%.** A search trial,
alternating the two versions on the same seed: 25.2/25.6 s before against
24.9/25.6 s after, verdicts identical. The reason is worth keeping, because
it is a trap anyone optimising this file can fall into:

    CALLS in a production run: passable 0, astar_py 0, via_ok 11

**`passable()` and `astar_py()` do not execute at all.** `astar()` hands
every net to `croute.route` unless `ROUTER=py`, so items 1 and 5 above
tuned the pure-Python fallback that `router.c` replaced. `via_ok` survives
only because the stitching-via loop calls it directly, 11 times a board.
Before optimising anything here, **count the calls in a real run** - the
Python router is a reference implementation, not the thing that runs.

Three of the five edits also did not survive review:

* the `commit_path()` change left a `try:` with no `except`, so the module
  did not parse and the generator could not run at all
* `build_features()` kept a `_GAP_CACHE.clear()` against a name that no
  longer exists - a NameError on the first rip-up round
* `passable()`'s bounds test was removed as "pure overhead" on the
  reasoning that the neighbour loop cannot leave 1..NX-2.  It can: the loop
  pushes x+-1 from every expanded cell, and that check is precisely what
  stops the frontier ENTERING the border ring.  Without it `occ[L][y, -1]`
  silently reads the far edge of the board - numpy wraps negative indices -
  so a trace could cross from one side to the other, while `x == NX` raises
  IndexError instead.  Restored.

Kept: the `VIA_MEMO` lifetime change (`reset_routing()` clears it, so
rip-up is covered) and the `_pad_span` cache (pad positions are fixed once
the placement is committed).
