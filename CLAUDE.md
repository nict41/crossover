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
```

**Order matters**: `gen_pcb*.py` read the netlist JSON that `gen_schematic.py`
writes, so the board cannot drift from the schematic. Run the schematic first.

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

## Hard-won learnings (each of these cost real debugging time)

**Board-size sweeps cannot fix layout problems.** Repeatedly, nets failed to
route and the reflex was to grow the board; the actual causes were always
structural. If a net won't route, find out *why* before touching `BOARD_W/H`.
Causes found so far, in order of discovery:

1. **Pot spacing, not board area.** Wedging a third pot into the gap between
   two existing ones left ~1.4 units of clearance and starved neighbouring
   pins' escape routes. Fixed with a uniform `PANEL_PITCH`.
2. **Route order.** `route_order()` sorts smallest-net-first, which sent `GND`
   (a dozen-plus pads) to the back of the queue; by its turn, every cell near
   the IC ground pins was taken. Supply rails now go first.
3. **Long-haul 2-pad nets.** `*_PRE` nets (filter output → volume pot) are the
   *smallest* nets but cross nearly the whole board. Member count is not a
   proxy for distance. Now any net whose pads span >50% of board width gets an
   early turn — generalised after hardcoding `LP1` and others one at a time
   proved to be chasing the pattern instead of fixing it.
4. **A silkscreen label parked in a pin-escape corridor.** `U1D.13`/`U2D.13`/
   `U2D.14` were unroutable at *every* board size tried. The cause was the
   U1/U2 designator sitting directly above the top-left pins, whose silk
   keepout blocked the only channel those pins have at `IC_ROT=90`. Moving the
   label beside the package fixed it. **Silk keepout is routing-relevant** —
   labels are not cosmetic.

**Absolute coordinates rot.** Anchors tuned for an older, narrower board
(SOICs, terminal blocks, C0) stayed left-anchored while the pot row grew the
board, leaving the right third empty. Position things relative to `BW`/`BH` or
to the part they belong to, not at fixed x/y.

**`fp_soic14` at `rot=90` centres the package at `x+15`, not `x`.** Passing a
centre coordinate straight through shifts it. Bitten once.

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
* **Not usable as a filter: `GRID=0.5`.** ~3x faster (1m47 vs 5m35) but far
  more pessimistic than expected — 11–17 failing nets where 0.25 gives 4. Too
  coarse to rank candidate sizes meaningfully. Production stays at 0.25.

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
  volume trim beside the frequency pot of the band it belongs to.
* **Per-output ground returns** — each output terminal block carries its own
  ground, because each runs to a separate amplifier; sharing one invites hum
  loops.
* **Two 33nF in parallel** for the tuning caps: 68nF isn't stocked in a
  suitable dielectric at 50 V, and this keeps every tuning cap the same part
  from the same reel (matching matters more than absolute value here).

## Current state / open threads

* Board is **106.7 × 55.9 mm** as last committed and verifies clean at that
  size. A layout rearrangement (centred SOICs, terminal blocks spread across
  the real width, `C0` and pot-body/boss fixes) is **in the working tree but
  not yet settled on a final size** — with it, 400×185 units reached 47.0 mm
  height with 4 failing nets. The remaining failures are all IC-pin escapes
  around U2's D section.
* Board size is *searched, not proven minimal*. Treat the committed size as
  "smallest found", not a floor.
* Not built or measured — verified against its own netlist and geometry only.
* Hand-drawn footprints still to confirm against datasheets before ordering:
  SOIC-14, the 10 µF electrolytic, the dual-gang pot body/boss positions.
