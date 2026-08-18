# Project context for agents

ESP Project 148 3-way state-variable crossover, redrawn as EasyEDA-importable
schematics plus a fully routed SMD PCB. Everything in `schematic/`, `pcb/`,
`bom/` and most of `docs/` is **generated** — never hand-edit those; edit the
generator in `tools/` and re-run it.

## Regeneration

```sh
python3 tools/gen_schematic.py    # schematics, previews, netlists, BOMs
python3 tools/gen_pcb.py          # through-hole board (placement only)
python3 tools/gen_pcb_smd.py      # SMD board, routed + verified (needs numpy)
python3 tools/gen_range_diagram.py
python3 tools/validate_fab.py --online   # EasyEDA import + JLCPCB limits
python3 tools/find_board.py --seeds 1-24 --route-seeds 0-7   # search for a clean board
```

**Order matters**: `gen_pcb*.py` read the netlist JSON that `gen_schematic.py`
writes, so the board cannot drift from the schematic. Run the schematic first.

`gen_pcb_smd.py` knobs (all optional): `MOVES` / `RESTARTS` / `SEED` tune the
placement search, `PLACE_LOG=n` prints its cost breakdown every n moves,
`GRID` sets the routing grid, `SWEEP=1` skips writing artifacts, `PANEL_PITCH`
sets the front-panel control spacing. There is deliberately no `BOARD_W` or
`BOARD_H` — see below.

`gen_schematic.py` builds all four variants and asserts they share identical
signal connectivity, so a retune that accidentally changed a connection fails
the build instead of shipping quietly. If you add a variant-specific feature
(as the volume pots are), teach `signal_map()` to normalise it rather than
disabling the check.

## The verification philosophy — read this before "fixing" a DRC failure

`gen_pcb_smd.py` routes the board and then checks it with **exact geometry that
does not reuse the router's own bookkeeping** (`verify()`): pairwise clearance,
union-find connectivity, mounting-hole keepout, board-edge clearance,
schematic-pin agreement, silk-over-copper, footprint-courtyard overlap, and a
largest-empty-rectangle wasted-area measure.

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

## Hard-won learnings (each of these cost real debugging time)

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

Runtime is dominated by A* in pure Python. What actually helped, and what
didn't:

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
* **`GRID=0.5` is a comparative filter only.** ~3x faster and systematically
  more pessimistic (it reports failures 0.25 routes fine). Useful for ranking
  two placements against each other, useless for judging one in absolute
  terms. Production stays at 0.25.

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

The committed board is **94.2 × 66.3 mm**, generated by the current
pipeline. It verifies clean AND passes `validate_fab.py` (EasyEDA import
structure, JLCPCB fab limits, BOM/CPL, LCSC stock). It is ~5% larger in
area than the hand-placed board it replaced, mostly because silkscreen had
to thicken to be printable; the gain is in method, not size.

**Run times, after the optimisation work** — a full cold run is ~33 s
(placement ~25 s, routing ~5 s, verify ~3 s); a re-run reusing the cached
placement is **~4.5 s**. Before: ~25 minutes. What did it:
* `router.c` — the grid A* compiled and called via ctypes. Routing was
  ~20 min of every run.
* **Placement cache** (`.place-cache/`) — placement is deterministic in its
  inputs, and route-order search re-runs the same placement many times.
  Keyed on the probed footprint geometry and every placement weight, so
  editing a footprint invalidates it.
* **Bounding-box pre-filter in `verify()`** — the clearance and
  connectivity checks consider ~2.9M pairs but only a handful are near each
  other. Expanded-box distance is a lower bound on the real gap, so
  skipping the rest is exact, not approximate.

Measured and NOT worth doing: caching the anneal's cost across moves
(changed the search trajectory and cost a verified board), and replacing
numpy with plain Python in the placer (numpy is still ~3x faster at n=49).
* **Only 1 seed in 28 verifies clean** at the committed settings. Seed
  choice is doing far more work than the search is. `tools/find_board.py`
  routes candidates in parallel and reports which verified — re-run it
  after any change that moves the layout, and never trust a placement that
  has not been routed.
* **Do not turn up `MOVES` or `RESTARTS`.** Both are measured, and both get
  *worse* above their committed values (see docs/pcb-notes-smd.md). The
  anneal optimises a surrogate; fitting it harder fits the router less.
* **A known, shipped defect: the bypass caps are 37-63 mm from their
  op-amp supply pins.** The fix is written and switched off (`BYPASS_NEAR=1`)
  because with it on NOTHING routes - 0 clean boards out of 208
  seed/route-order combinations, and 0 of 40 more with the size pressure
  slackened to give the router room (bigger board, longer nets, worse).
  See docs/design-review.md. Do not delete the constraint; it is correct
  and the router is what is behind it.
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
* **The next real lever is the router, not the placer.** It is still
  single-pass with no rip-up, which is why route *order* matters and why
  `route_order()` exists at all. Negotiated congestion routing (route
  everything, price up contested cells, reroute) deletes that entire
  category — and now that a full route costs ~20s instead of ~20min, it is
  affordable to develop. `router.c` already carries the `use`/`hist` cost
  terms it needs, unused.
* Not built or measured — verified against its own netlist and geometry only.
* Hand-drawn footprints still to confirm against datasheets before ordering:
  SOIC-14, the 10 µF electrolytic, the dual-gang pot body/boss positions.
