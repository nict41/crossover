#!/usr/bin/env python3
"""Search for a board that verifies clean, by routing candidates.

The placement search scores layouts with a surrogate - escape depth,
congestion, wirelength, size - because for most of this project's life
routing a candidate cost minutes and could not be used as feedback.  That
surrogate is a decent guide and an unreliable judge: raising RESTARTS from
2 to 4 (which keeps a placement the surrogate scores BETTER) produced a
board with seven DRC problems where best-of-2 had none.

Now that router.c has full runs down to under a minute, the honest way to
choose is to route the candidates and look.  This runs gen_pcb_smd.py at
several seeds in parallel with SWEEP=1 (so none of them touch the
committed artifacts), and reports which verified clean, smallest first.

    python3 tools/find_board.py --seeds 1-32 --jobs 4

Then regenerate the winner for real, without SWEEP, to write the artifacts:

    SEED=<winner> python3 tools/gen_pcb_smd.py
"""

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "gen_pcb_smd.py")
BOARD = re.compile(r"^board ([\d.]+) x ([\d.]+) mm", re.M)
PROBLEMS = re.compile(r"^DRC PROBLEMS \((\d+)\)", re.M)


def problem_count(out):
    """How many DRC problems a run reported, or None if that cannot be
    determined from its output.

    None is deliberately NOT zero.  An earlier version treated a missing
    count as a clean board, and since sweep mode did not print one, it
    declared 24 boards out of 24 clean when none of them were.  A result
    that cannot be read is a failed trial, never a passing one."""
    m = PROBLEMS.search(out)
    if m:
        return int(m.group(1))
    if BOARD.search(out) is None:
        return None
    # Older/sweep output: count the reported lines directly.
    return (len(re.findall(r"^  - ", out, re.M))
            + len(re.findall(r"^  UNROUTED", out, re.M)))


def trial(job, env_extra):
    seed, rseed = job
    env = dict(os.environ, SWEEP="1", SEED=str(seed),
               ROUTE_SEED=str(rseed), **env_extra)
    try:
        out = subprocess.run([sys.executable, GEN], env=env, timeout=1800,
                             capture_output=True, text=True).stdout
    except subprocess.TimeoutExpired:
        return dict(seed=seed, rseed=rseed, ok=False, note="timeout")
    m = BOARD.search(out)
    if not m:
        return dict(seed=seed, rseed=rseed, ok=False,
                    note="crashed or produced no board")
    w, h = float(m.group(1)), float(m.group(2))
    n = problem_count(out)
    if n is None:
        return dict(seed=seed, rseed=rseed, ok=False,
                    note="could not read a DRC verdict")
    return dict(seed=seed, rseed=rseed, ok=(n == 0), problems=n, w=w, h=h,
                area=w * h, note="")


def parse_seeds(spec):
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="1-16")
    ap.add_argument("--route-seeds", default="0",
                    help="net-order permutations to try per placement")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--env", action="append", default=[],
                    help="extra VAR=VALUE passed to every trial")
    a = ap.parse_args()
    extra = dict(kv.split("=", 1) for kv in a.env)
    jobs = [(s, r) for s in parse_seeds(a.seeds)
            for r in parse_seeds(a.route_seeds)]

    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        results = list(pool.map(lambda j: trial(j, extra), jobs))

    clean = sorted((r for r in results if r["ok"]), key=lambda r: r["area"])
    dirty = sorted((r for r in results if not r["ok"] and not r["note"]),
                   key=lambda r: (r["problems"], r["area"]))
    broke = [r for r in results if r["note"]]

    for r in clean:
        print("CLEAN  %6.0f mm2  %.1f x %.1f  SEED=%d ROUTE_SEED=%d"
              % (r["area"], r["w"], r["h"], r["seed"], r["rseed"]))
    for r in dirty:
        print("  %2d    %6.0f mm2  %.1f x %.1f  SEED=%d ROUTE_SEED=%d"
              % (r["problems"], r["area"], r["w"], r["h"], r["seed"], r["rseed"]))
    for r in broke:
        print("  --                            SEED=%d ROUTE_SEED=%d  (%s)"
              % (r["seed"], r["rseed"], r["note"]))
    print("\n%d/%d verified clean" % (len(clean), len(results)))
    if clean:
        print("smallest clean: SEED=%d ROUTE_SEED=%d at %.1f x %.1f mm"
              % (clean[0]["seed"], clean[0]["rseed"], clean[0]["w"], clean[0]["h"]))
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
