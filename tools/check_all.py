#!/usr/bin/env python3
"""The one command to run before committing anything.

    python3 tools/check_all.py

Regenerates every generated artifact, then checks that what is committed
matches what the generator produces, then runs the fab validator.  Exit 0
means the tree is consistent and the board is buildable; anything else
means do not commit.

Why this exists as one command: almost every way this project has been
broken is caught by it, and none of those ways is obvious from reading a
diff.

  * a generated file was hand-edited          -> drift, reported per file
  * the generator changed and artifacts were  -> drift
    not regenerated and committed
  * the board no longer verifies              -> gen_pcb_smd.py fails
  * a doc names a part that does not exist    -> validate_fab.py fails
  * an LCSC part number went end-of-life      -> validate_fab.py --online

`--online` adds the LCSC stock query (needs network).  `--fast` skips the
schematic regeneration when you have only touched the board.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Everything below these paths is written by a generator.  If git reports a
# change here after a regeneration that you did not intend, either the
# artifacts were stale or somebody edited one by hand - and the second is
# the failure this project has been bitten by most.
GENERATED = ["schematic/", "pcb/", "bom/", "sim/",
             "docs/parts.md", "docs/panel-drilling.md",
             "docs/subcircuits/images/", "docs/netlist-", "docs/crossover-ranges"]


def run(label, cmd, quiet_ok=True):
    print("== %s" % label, flush=True)
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    tail = (r.stdout + r.stderr).strip().splitlines()
    if r.returncode != 0:
        for line in tail[-25:]:
            print("   " + line)
        print("   FAILED: %s" % " ".join(cmd))
        return False
    if not quiet_ok:
        for line in tail[-6:]:
            print("   " + line)
    return True


def dirty():
    """(generated files changed, hand-written files changed)."""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                       capture_output=True, text=True)
    gen, src = [], []
    for line in r.stdout.splitlines():
        path = line[3:].strip()
        (gen if any(path.startswith(g) for g in GENERATED) else src).append(line)
    return gen, src


def main():
    online = "--online" in sys.argv
    fast = "--fast" in sys.argv

    # A generated file that is dirty BEFORE anything is regenerated, with
    # no generator or hand-written doc touched to explain it, is a hand
    # edit.  Catching it here matters more than it looks: the regeneration
    # a few lines below would quietly overwrite it, the run would pass, and
    # the author would believe a change landed that no longer exists.  That
    # is this project's most expensive recurring bug, in a new costume.
    gen_before, src_before = dirty()
    if gen_before and not src_before:
        print("== a generated file was edited by hand")
        for line in gen_before:
            print("   " + line)
        print("\n   These are written by tools/, and regenerating is about to\n"
              "   overwrite them. Change the GENERATOR instead. If you meant\n"
              "   to discard them, `git checkout --` them first.")
        return 1

    ok = True
    if not fast:
        ok &= run("regenerating schematics, netlists and BOMs",
                  [sys.executable, "tools/gen_schematic.py"])
    ok &= run("regenerating the board (routes and verifies)",
              [sys.executable, "tools/gen_pcb_smd.py"], quiet_ok=False)
    ok &= run("regenerating the SPICE deck",
              [sys.executable, "tools/gen_spice.py"])
    if not fast:
        # In the pipeline, not a one-off export: it re-derives the netlist
        # from the KiCad file it just wrote and fails if that disagrees with
        # the schematic, so the two can never drift apart unnoticed.
        ok &= run("regenerating the KiCad schematic",
                  [sys.executable, "tools/gen_kicad.py"])
        ok &= run("regenerating the KiCad routing seed",
                  [sys.executable, "tools/gen_kicad_pcb.py"])
    if not ok:
        print("\nA generator failed. Nothing else was checked.")
        return 1

    gen_after, _ = dirty()
    if gen_after:
        # Not a failure: this is what a legitimate generator change looks
        # like on the way to a commit.  It IS a failure to commit the
        # generator without them, so say what has to go in the same commit.
        print("== these artifacts changed and MUST be committed together:")
        for line in gen_after:
            print("   " + line)
    else:
        print("== no drift: every generated artifact matches what is committed")

    cmd = [sys.executable, "tools/validate_fab.py"] + (["--online"] if online else [])
    if not run("validating the artifact (fab limits, BOM, docs)", cmd,
               quiet_ok=False):
        return 1

    print("\nOK - the tree is consistent and the board verifies.")
    print("   Before committing: append what you measured to "
          "docs/decisions.md (AGENTS.md rule 5).")
    if not online:
        print("   (re-run with --online before ordering, to check LCSC stock)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
