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
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "gen_pcb_smd.py")
BOARD = re.compile(r"^board ([\d.]+) x ([\d.]+) mm", re.M)
PROBLEMS = re.compile(r"^DRC PROBLEMS \((\d+)\)", re.M)
# What KIND of problem a board has decides what to do about it, and the
# counts alone cannot say.  A board failing only on "the ground pour does
# not reach N pad(s)" has a structural pocket the placer made, and more
# route orders will not open it; a board with unrouted nets or clearance
# violations may well come good on a different order.  Sweeping without
# this distinction is how several rounds got spent re-rolling route seeds
# against a placement problem.
POUR = re.compile(r"^  - the ground pour does not reach", re.M)
UNROUTED = re.compile(r"^  UNROUTED", re.M)
# The wasted-area complaint is the one entry in verify()'s output that is
# not a defect.  Every other line means the board would come back from the
# fab broken or would not work; this one means the layout leaves a hole and
# could probably be smaller, which is a judgement about quality, not about
# whether it can be built.
#
# It is reported separately because it changes what to DO.  It is set by
# the placement, not the routing - seed 1 gives the identical "13% at
# (0,5)" under three different route orders - so a seed carrying it will
# carry it however the nets fall, and sweeping route orders against it is
# wasted time.  Do NOT drop the check to make a board pass: the right
# response is a different seed, and a board that is defect-free but wastes
# space is still a real, orderable board if you want it.
WASTED = re.compile(r"^  - a single empty rectangle is", re.M)


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


# A failing board is far more expensive to route than a passing one - a
# failed A* drains the queue across the whole reachable grid - and a search
# spends most of its time on boards that fail.  Capping expansions makes
# the router pessimistic, so this is a FILTER: whatever comes back clean
# still has to be confirmed by an uncapped production run.  Same bargain as
# GRID=0.5, and the same rule - never in production, where capping was
# tried twice and silently broke routable nets.
SEARCH_MAX_EXPAND = "400000"

# Rip-up rounds for the RANKING pass.  ONE, and the difference between one
# and none is the whole reason ranking cheaply works at all.
#
# Measured against six seeds whose expensive ordering was already known
# (rip-up 6, same cap: seed 1->2, 4->3, 2->11, 3->13, 5->15, 6->16):
#
#   rip-up 0   1->13  4->15  2->22  3->20  5->14  6->20    ~35-63 s
#   rip-up 1   1->2   4->5   2->15  3->20  5->14  6->17    ~73-104 s
#   rip-up 6   the ground truth above                      ~201 s
#
# For scale, the config a search once ran by default - uncapped AND rip-up
# 6 - costs 707 s a trial, so 36 seeds over 4 jobs is 106 minutes.  It was
# given 90 and died with nothing to show.  Multiply before launching.
#
# Rip-up 0 is not a blurred version of the answer, it is a different one:
# it ranks seed 2 - genuinely the third best - DEAD LAST, and promotes
# seed 5 into second.  Rip-up 1 reproduces the top two exactly and gets
# close to their real problem counts.
#
# The reason is worth keeping, because it says where rip-up's value
# actually lives: a good placement needs barely any rip-up (seed 1 is at
# its final 2 problems after one round), while a marginal one is where the
# loop grinds through six rounds trading one failure for another.  So the
# first round buys nearly all of the ranking signal and rounds two to six
# buy polish on candidates a search is about to discard anyway.
RANK_RIPUP = "1"


PREFILTER_OK = re.compile(r"^PREFILTER OK", re.M)


def screen(seed, env_extra):
    """Cheap structural verdict for one placement: place it, pour it, and
    see whether every ground pad can reach the plane before a single signal
    net exists.

    A placement that fails here can never be rescued by routing - signal
    traces only take room away from the pour - so the minutes a full route
    would spend on it are known waste before they are spent.  A seed costs
    a second or two here against several minutes there.

    Measured at the current part count it rejects NOTHING: 40 seeds of 40
    pour clean before routing, because plane_stubs() runs before the signal
    nets and vias its way out of the structural pockets.  Kept regardless -
    it is exact, it costs a second, and it is the check that tells a
    placement problem apart from a route-order one."""
    env = dict(os.environ, SWEEP="1", SEED=str(seed), ROUTE_SEED="0",
               PLANE_PREFILTER_ONLY="1", **env_extra)
    env.setdefault("MAX_EXPAND", SEARCH_MAX_EXPAND)
    try:
        out = subprocess.run([sys.executable, GEN], env=env, timeout=600,
                             capture_output=True, text=True).stdout
    except subprocess.TimeoutExpired:
        return seed, False, "timeout"
    if PREFILTER_OK.search(out):
        return seed, True, ""
    m = POUR.search(out)
    return seed, False, ("pour-blocked" if m else "no verdict")


def trial(job, env_extra, timeout=1800):
    seed, rseed = job
    env = dict(os.environ, SWEEP="1", SEED=str(seed),
               ROUTE_SEED=str(rseed), **env_extra)
    env.setdefault("MAX_EXPAND", SEARCH_MAX_EXPAND)
    try:
        out = subprocess.run([sys.executable, GEN], env=env, timeout=timeout,
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
    pour = len(POUR.findall(out))
    waste = len(WASTED.findall(out))
    return dict(seed=seed, rseed=rseed, ok=(n == 0), problems=n, w=w, h=h,
                area=w * h, note="", pour=pour, waste=waste,
                defects=n - waste,
                unrouted=len(UNROUTED.findall(out)))


def describe(r):
    """One line for one finished trial, printed the moment it lands."""
    if r["note"]:
        return "  --     %-28s SEED=%d ROUTE_SEED=%d  (%s)" % (
            "", r["seed"], r["rseed"], r["note"])
    why = []
    if r["pour"]:
        why.append("%d pour" % r["pour"])
    if r["unrouted"]:
        why.append("%d unrouted" % r["unrouted"])
    if r["waste"]:
        why.append("wastes space")
    tag = ("CLEAN " if r["ok"] else "SOUND " if r["defects"] == 0
           else "  %2d  " % r["defects"])
    return "%s %6.0f mm2  %.1f x %.1f  SEED=%d ROUTE_SEED=%d  %s" % (
        tag, r["area"], r["w"], r["h"], r["seed"], r["rseed"],
        ", ".join(why))


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
    ap.add_argument("--all", action="store_true",
                    help="run every trial even after a clean board is found "
                         "(for comparing the whole field, not for finding one)")
    ap.add_argument("--trial-timeout", type=int, default=420,
                    help="seconds one trial may take before it is abandoned "
                         "(default 420; the tail is what eats a search)")
    ap.add_argument("--confirm", type=int, default=3,
                    help="how many of the best ranked seeds to re-run at "
                         "production settings (0 to skip)")
    ap.add_argument("--cheap-rank", action="store_true", default=True,
                    help="rank seeds cheaply, then confirm only the best "
                         "(the default)")
    ap.add_argument("--no-cheap-rank", dest="cheap_rank",
                    action="store_false",
                    help="route every trial at production settings")
    ap.add_argument("--no-screen", action="store_true",
                    help="skip the cheap ground-plane pre-screen and route "
                         "every seed (for measuring the screen itself)")
    ap.add_argument("--env", action="append", default=[],
                    help="extra VAR=VALUE passed to every trial")
    a = ap.parse_args()
    extra = dict(kv.split("=", 1) for kv in a.env)
    seeds = parse_seeds(a.seeds)

    # Stage one: throw out the placements that cannot work, before paying to
    # route any of them.
    if not a.no_screen:
        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            screened = list(pool.map(lambda s: screen(s, extra), seeds))
        keep = [s for s, ok, _ in screened if ok]
        for s, ok, why in screened:
            if not ok:
                print("  screened out SEED=%d (%s)" % (s, why))
        print("%d of %d placements have a reachable ground plane; routing "
              "those" % (len(keep), len(seeds)), flush=True)
        if not keep:
            print("nothing to route - every placement seals a ground pad. "
                  "That is place.PLANE_GAP's job, not the router's.")
            return 1
        seeds = keep

    jobs = [(s, r) for s in seeds for r in parse_seeds(a.route_seeds)]

    # Stop as soon as a clean board turns up.  A search exists to find ONE
    # verifying layout, and running the remaining trials after that is pure
    # waste - on the run that found SEED=38 the answer arrived a third of
    # the way in and the other two thirds of the compute told us nothing we
    # acted on.  Trials already in flight are allowed to finish (they are
    # subprocesses, and killing them mid-write is how a half-written
    # artifact would happen); only unstarted ones are cancelled.
    # Rank cheaply, confirm expensively.
    #
    # A search spends nearly all of its time on boards it is going to
    # reject, and it does not need a production-quality route to know that.
    # Two settings dominate the cost and neither changes the RANKING much:
    # the expansion cap (a failing A* drains the queue over the whole grid,
    # and capping it is ~2x) and rip-up, which re-routes the entire board up
    # to seven times to polish a candidate that is about to be thrown away.
    #
    # Measured on this board: one capped route with a single rip-up round
    # costs ~90 s against ~200 s for the full treatment and far more
    # uncapped, and reproduces the expensive ordering at the top - see
    # RANK_RIPUP for the numbers, including why zero rounds does NOT.  So
    # stage two answers "which seeds are promising" cheaply and stage three
    # pays the real price on the handful that survive.  Both filters are
    # pessimistic in the same direction, which is what makes them safe to
    # rank with and unsafe to judge with - the same bargain GRID=0.5 offers.
    rank_env = dict(extra)
    if a.cheap_rank:
        rank_env.setdefault("MAX_EXPAND", SEARCH_MAX_EXPAND)
        rank_env.setdefault("RIPUP_ROUNDS", RANK_RIPUP)

    def sweep(jobs, env, timeout, label):
        """Run these trials, printing each one THE MOMENT IT LANDS.

        Streaming is not cosmetic.  The previous version collected every
        result and printed at the end, so when a 36-seed search hit its
        outer timeout it was killed with nothing on stdout - ninety minutes
        of routing discarded, and not one seed's verdict recoverable.  A
        long search must be interruptible without losing what it has
        already learned."""
        out = []
        print("%s: %d trial(s), %d at a time" % (label, len(jobs), a.jobs),
              flush=True)
        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            futures = {pool.submit(trial, j, env, timeout): j for j in jobs}
            try:
                for fut in as_completed(futures):
                    r = fut.result()
                    out.append(r)
                    print(describe(r), flush=True)
                    if r["ok"] and not a.all:
                        print("clean board found - cancelling the rest "
                              "(pass --all to sweep every trial)", flush=True)
                        for f in futures:
                            f.cancel()
                        break
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                raise
        return out

    results = sweep(jobs, rank_env, a.trial_timeout,
                    "ranking" if a.cheap_rank else "routing")

    # Stage three: the cheap pass is pessimistic, so anything it liked has
    # to be re-run for real before it can be believed.
    #
    # Note the asymmetry: a CLEAN verdict from the cheap pass needs no
    # confirming.  Capping expansions and switching rip-up off can only
    # make the ROUTER give up sooner; neither touches verify(), which
    # measures the finished copper.  So the cheap pass can miss a good
    # board but cannot invent one, and a clean result short-circuits the
    # whole thing.
    if a.cheap_rank and a.confirm and not any(r["ok"] for r in results):
        scored = sorted((r for r in results if not r["note"]),
                        key=lambda r: (r["defects"], r["area"]))[:a.confirm]
        if scored:
            print("\nconfirming the best %d at production settings "
                  "(uncapped, rip-up on)" % len(scored), flush=True)
            confirm_env = dict(extra)
            confirm_env["MAX_EXPAND"] = extra.get("MAX_EXPAND", "0")
            confirmed = sweep([(r["seed"], r["rseed"]) for r in scored],
                              confirm_env, a.trial_timeout * 3, "confirming")
            # The confirmed verdicts REPLACE the ranked ones rather than
            # joining them: a capped, rip-up-free route is pessimistic by
            # construction, so reporting its problem counts next to real
            # ones would invite comparing two different measurements.
            print("(ranked %d seed(s) cheaply; the verdicts below are the "
                  "%d confirmed at production settings)"
                  % (len(results), len(confirmed)), flush=True)
            results = confirmed

    clean = sorted((r for r in results if r["ok"]), key=lambda r: r["area"])
    sound = sorted((r for r in results
                    if not r["ok"] and not r["note"] and r["defects"] == 0),
                   key=lambda r: r["area"])
    dirty = sorted((r for r in results
                    if not r["ok"] and not r["note"] and r["defects"] > 0),
                   key=lambda r: (r["defects"], r["area"]))
    broke = [r for r in results if r["note"]]

    # Every trial already printed itself as it landed; this is the same
    # field sorted, so a reader who watched it go by gets the ranking and a
    # reader who was killed part way through has lost nothing.
    print("\n--- ranked ---")
    for r in clean + sound + dirty + broke:
        print(describe(r))
    # Report the cap the TRIALS actually ran with, which is the one in
    # `extra` if --env set it, not whatever this parent process happens to
    # have.  Reading the parent's environment printed "MAX_EXPAND=400000"
    # under `--env MAX_EXPAND=0`, i.e. the exact opposite of the truth,
    # on the run that found the first clean 63-footprint board.
    cap = extra.get("MAX_EXPAND", os.environ.get("MAX_EXPAND",
                                                 SEARCH_MAX_EXPAND))
    note = ("uncapped" if str(cap) == "0"
            else "MAX_EXPAND=%s - confirm the winner with an uncapped run"
                 % cap)
    print("\n%d/%d passed the filter (%s)" % (len(clean), len(results), note))
    if sound:
        print("%d board(s) have no DEFECTS and fail only the wasted-area "
              "check - orderable, just not tight" % len(sound))
    if dirty:
        pour_only = sum(1 for r in dirty if r["pour"] and r["pour"] == r["defects"])
        print("%d of %d failures were ground-pour reachability ONLY - that is a "
              "PLACEMENT problem (see place.PLANE_GAP), not a route-order one"
              % (pour_only, len(dirty)))
    if clean:
        print("smallest clean: SEED=%d ROUTE_SEED=%d at %.1f x %.1f mm"
              % (clean[0]["seed"], clean[0]["rseed"], clean[0]["w"], clean[0]["h"]))
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
