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

* ~~Confirm the dual-gang pot footprint.~~ **DONE** — and it was wrong by
  2× in both axes. Fixed from the Alps RK097 catalogue drawing. See
  `docs/decisions.md`.
* ~~Confirm the SOIC-14 land pattern.~~ **DONE** — correct.
* ~~Confirm the electrolytic land pattern.~~ **DONE** — and it was wrong;
  pads were 1.3 mm too far apart. Fixed from the KNSCHA datasheet.
* ~~Confirm the screw terminals.~~ **DONE** — 3.50 mm real against 3.556
  drawn, inside the slack a ø0.8 mm pin leaves in a ø1.09 mm hole.
* **Confirm the pot's locating bosses.** The RK097 drawing shows the PCB
  hole pattern but this board drills no boss holes. If the parts bought
  have bosses, they need holes — check before soldering.
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
* **Retry the courtyard split, with the two things the first attempt
  could not afford.** BUILT AND REVERTED once already — see
  `docs/decisions.md` and `CLAUDE.md`. `git revert 131a7ea` brings the
  whole implementation back (probe, Placer, `anneal.c` `hbox`, `verify()`,
  cache key), so do NOT re-derive it. It is correct and it lost: the freed
  space goes into a smaller board that will not route, and its only clean
  board was 13% bigger than the incumbent. What was NOT tried, and must be
  before it is judged again: a `W_H` re-tune (400 was calibrated against
  the old courtyard, and the two are coupled) and a full-width seed field
  (12 seeds against the incumbent's 24 is not a fair budget). Background
  on why it matters: `probe()` reads the courtyard off the
  emitted shapes and `shape_box()` counts a silkscreen TEXT as body, so a
  part's courtyard is asymmetric by about a label's height. Turning a
  two-pin part end for end therefore pushes that box into a neighbour, and
  the overlap term rejects the move. On the committed board **9 of 48
  two-pad parts are still the wrong way round, worth 23.5 mm of
  half-perimeter, and every single one is blocked by `ov`** — `C14` by
  +1741, `R32` by +5867. The constraint is wrong: a label may not lie over
  foreign COPPER (a pin that cannot escape — `verify()` already checks
  that exactly), but a label over a neighbour's silk outline is what every
  board does. Do NOT just drop the check: split it into courtyard overlap
  on pads and body, plus a cheaper term for label-over-foreign-PAD. Moves
  every placement, so it needs its own search round and its own A/B.
  Evidence and the per-part cost breakdown are in `CLAUDE.md`.
* **Try to beat 149.6 × 63.5 mm (9500 mm²).** 36 seeds have been ranked at
  the committed config; `SEED=17` won at 2 problems and confirms clean.
  Smaller placements exist but route worse (`SEED=15`, 9038 mm², 9
  problems). Sweep route orders on the small ones before adding seeds.
* **Reduce the largest empty rectangle** (29 × 28 mm at present, 58 %
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
