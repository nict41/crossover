#!/usr/bin/env python3
"""
Benchmark harness for the Crossover PCB generator.

* Measures the three critical phases:
  1. Fast prefilter (PLANE_PREFILTER_ONLY)
  2. Full geometry verification (stamp_disc, path_clearance_ok)
  3. Full router pass (route_with_ripup)

* Optional flags let you toggle:
  - GEOM_NUMBA=1 : enable Numba JIT for hot geometry functions
  - CGEOM=1     : enable compiled C geometry routines
  - PARALLEL=1  : run screening in parallel (uses ProcessPoolExecutor)

The harness prints a per-phase timing breakdown and a per-board final verdict.
"""
import os
import sys
import time
import subprocess
from pathlib import Path

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
GEN = str(ROOT / "tools" / "gen_pcb_smd.py")
DEFAULT_SEEDS = "1-16"  # adjust for larger sweeps
DEFAULT_JOBS = "4"     # number of parallel workers for screening

# ----------------------------------------------------------------------
def parse_args():
    import argparse
    arg = argparse.ArgumentParser(description="End‑to‑end performance benchmark")
    arg.add_argument("--seeds", default=DEFAULT_SEEDS, help="Seed list for find_board sweep")
    arg.add_argument("--jobs", default=DEFAULT_JOBS, help="Parallelism for screening")
    arg.add_argument("--env", default="", help="Extra key=value env pairs")
    arg.add_argument("--numba", action="store_true", help="Enable Numba JIT")
    arg.add_argument("--cgeom", action="store_true", help="Enable compiled C geometry")
    arg.add_argument("--parallel", action="store_true", help="Run screening in parallel")
    arg.add_argument("--rounds", type=int, default=1, help="How many times to repeat each step")
    return arg.parse_args()

# ----------------------------------------------------------------------
def timed_run(cmd, env=None, description=""):
    """Run cmd in a subshell and return elapsed seconds and stdout."""
    start = time.time()
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=env)
    out, _ = proc.communicate()
    elapsed = time.time() - start
    print(f"{description} — {elapsed:.2f}s")
    print("---", out.strip(), "---")
    return elapsed, out

# ----------------------------------------------------------------------
def main():
    args = parse_args()

    # ------------------------------------------------------------------
    # 1️⃣ Fast prefilter (dry‑run)
    # ------------------------------------------------------------------
    print("\n=== 1️⃣ Fast prefilter dry‑run ===")
    prefilter_env = os.environ.copy()
    prefilter_env.update({
        "PLANE_PREFILTER_ONLY": "1",
        "SWEEP": "1",
        "ROUTER": "py",
    })
    if args.numba:
        prefilter_env["GEOM_NUMBA"] = "1"
    if args.cgeom:
        prefilter_env["CGEOM"] = "1"
    if args.env:
        for kv in args.env.split():
            k, v = kv.split("=", 1)
            prefilter_env[k] = v

    _, out = timed_run(
        f"{sys.executable} {GEN} {prefilter_env}",
        env=prefilter_env,
        description="Prefilter dry‑run"
    )
    # CSV‑style summary later
    summary = [("prefilter", out)]

    # ------------------------------------------------------------------
    # 2️⃣ Full screening (full verify but cheap)
    # ------------------------------------------------------------------
    print("\n=== 2️⃣ Full cheap sweep (screening) ===")
    screen_env = os.environ.copy()
    screen_env.update({
        "SWEEP": "1",
        "ROUTER": "py",
        "PLANE_PREFILTER_ONLY": "0",
    })
    screen_env.update({
        "MAX_EXPAND": "150000",
        "ORDER_LOG": "1",
    })
    if args.parallel:
        # Turn on multiprocessing via find_board.py’s --jobs flag
        seed_cmd = f"python -m find_board --seeds {args.seeds} --jobs {args.jobs}"
    else:
        seed_cmd = f"python -m find_board --seeds {args.seeds} --jobs 1"
    timed_run(seed_cmd, env=screen_env, description="Cheap sweep (screening)")

    # ------------------------------------------------------------------
    # 3️⃣ Full router run (production)
    # ------------------------------------------------------------------
    print("\n=== 3️⃣ Full router run ===")
    full_env = os.environ.copy()
    full_env.update({
        "SWEEP": "0",
        "ROUTER": "py",
    })
    full_env.update({
        "MAX_EXPAND": "400000",
        "ORDER_LOG": "1",
    })
    if args.env:
        for kv in args.env.split():
            k, v = kv.split("=", 1)
            full_env[k] = v

    # Clean start – no artefacts from previous runs
    rm_cmd = f"rd /s /q \"{ROOT}\\pcb\" \"{ROOT}\\bom\""
    subprocess.run(rm_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    timed_run(
        f"{sys.executable} {GEN}",
        env=full_env,
        description="Full router run (production)"
    )
    summary.append(("full-router", ""))  # placeholder

    # ------------------------------------------------------------------
    # CSV summary
    # ------------------------------------------------------------------
    print("\n=== Summary (csv) ===")
    print("phase,seconds")
    for phase, _ in summary:
        print(f"{phase},{phase:.2f}")

if __name__ == "__main__":
    main()