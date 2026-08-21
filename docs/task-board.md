# Task board

Open work, sized for one agent per item. **Read `AGENTS.md` first.**

Every task is marked **[serial]** or **[parallel]**:

* **[serial]** moves the parts. It invalidates the placement, forces a
  board re-search of tens of minutes, and produces a *different board*.
  Only one of these may be in flight at a time — if two agents do it, the
  second one's search is thrown away. Claim it before starting.
* **[parallel]** cannot move the parts. Any number of these can run at
  once.

Finish every task by running `python3 tools/check_all.py` and appending to
`docs/decisions.md`.

---

## Blocked on physical parts or datasheets

These cannot be closed by an agent. They need the parts in hand or a
mechanical drawing. See the top of this file's companion discussion in
`README.md` ("Of the routed board").

* **Confirm the dual-gang pot footprint.** `fp_pot` in `tools/gen_pcb_smd.py`
  draws a 5.08 mm pin pitch; real 9 mm dual-gang parts are commonly
  2.50 mm. No verified mechanical drawing has ever been found for it, and
  LCSC's footprint API answers 403. **Needs: the exact part number and its
  drawing.** Highest-value open item — if this is wrong, the board is
  scrap.
* **Confirm the SOIC-14 land pattern** (`fp_soic14`): 1.27 mm pitch,
  1.52 × 0.64 mm pads, rows 5.33 mm apart. Check against onsemi's
  recommended pattern for the MC33079.
* **Confirm the electrolytic land pattern** (`fp_elec`): pads 2.03 × 2.54 mm
  on 4.32 mm centres, for a D5 × L5.4 mm can (LCSC C2858858).
* **Confirm the screw terminals** (`fp_term`): 3.556 mm pitch, 2.03 mm pads,
  1.09 mm holes. Depends which part is actually bought.
* **Measure the shaft and plunger heights above the board** and add them to
  `docs/panel-drilling.md`. That file deliberately omits them today,
  because inventing the one dimension nobody can check is worse than
  leaving a gap.

---

## Ready to work on

### [parallel] Verification and analysis

* **Add a THD-capable op-amp model.** `sim/models.lib` is a behavioural
  macromodel with no device-level nonlinearity, so `run_sim.py` cannot
  predict distortion — only topology. A real MC33079 (or a generic
  transistor-level) model would let the suite report a distortion figure.
  Say clearly in the docs what the new model can and cannot claim.
* **Simulate crosstalk between bands.** Three outputs, one ground plane,
  per-output ground returns. Nothing has measured it.
* **Simulate PSRR** — inject ripple on the rails and measure what reaches
  an output. The bulk and per-IC decoupling were added on reasoning, never
  measured.
* **Extend `run_sim.py` to sweep the volume pots**, not just the frequency
  pots. The band trims are attenuators feeding a buffer; confirm nothing
  interesting happens at the extremes.
* **Check the mute chain's dry-circuit margin by simulation** rather than by
  the hand calculation now in `docs/circuit-notes.md`.

### [parallel] Checks worth adding to `validate_fab.py`

* **Silkscreen legibility against the courtyard**, not just against pads —
  a designator that lands on a neighbouring part's body is still bad.
* **Acid-trap / acute-angle detection** on the routed copper.
* **Assert the CPL rotations** against each footprint's pin-1 direction, so
  a footprint edit that rotates a part is caught.

### [parallel] Documentation

* **A build guide.** The docs explain the design thoroughly and the
  assembly barely: what to solder first, how to set the two frequency
  knobs, what the test points are for, how to check it works before
  connecting amplifiers.
* **Trim `CLAUDE.md`.** It is over 1300 lines and mixes durable principles
  with narrative history. Split the principles from the war stories
  without losing any measurement.

### [serial] Layout — one at a time, claim first

* **Move `J6` to a board edge.** The LED loom connector currently sits
  mid-board, so the loom crosses the layout to reach the panel. It is
  anchored to `SW2` by `MUTE_NEAR`. Cost: a re-search.
* **Try to beat 145.8 × 66.5 mm.** The seed field has produced smaller
  near-misses (`SEED=39` at 148.3 × 60.7 with 2 unrouted). Sweep route
  orders on the small ones. Cheap to try, no code change, but it changes
  the committed board so it serialises.
* **Reduce the largest empty rectangle** (30 × 27 mm at present, 61 %
  utilisation). Note that `W_FILL` has been measured **twice** and rejected
  both times — read the entry in `CLAUDE.md` before attempting anything
  that prices area.

---

## Do not do these

Measured, rejected, and documented in `CLAUDE.md`. Reopening one needs new
evidence, not a new idea.

* Adding a `BOARD_W`/`BOARD_H` knob.
* Raising `MOVES` or `RESTARTS`.
* Capping `MAX_EXPAND` in production routing.
* A column-load / area-filling placement term (`W_FILL`).
* `GRID=0.5` for search ranking.
* Octilinear routing, cheap vias, or layer-grain routing.
* Negotiated-congestion routing at per-net granularity.
