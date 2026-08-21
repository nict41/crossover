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
