#!/usr/bin/env python3
"""Run the circuit-verification suite and print what it found.

    python3 tools/gen_spice.py && python3 tools/run_sim.py

gen_spice.py owns the deck; this only measures.  Two steps, the same way
gen_schematic and gen_pcb_smd are two steps.

What it checks, and why each is here rather than in someone's head:

  filter  the crossover points land where the silkscreen says, and the
          three bands sum flat - including the case the silkscreen does
          NOT warn about, both knobs wound together.
  dc      no DC reaches an amplifier terminal.
  mute    how deep the mute is, how long the soft start holds, and the one
          that changed a part number: whether the shunt JFET stays off on
          signal peaks.

Everything is measured from the same generated netlist the board is built
from.  Nothing here is asserted from a datasheet alone.

What this CANNOT tell you: real THD.  The op-amp is a behavioural
macromodel with no device-level nonlinearity, so the distortion floor
below is a measurement artefact, not a prediction.  What it does show is
TOPOLOGY-level distortion - a device conducting when it should not - which
is a large structural effect and is exactly what it caught.
"""

import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SIM = os.path.join(ROOT, "sim")
JOBS = 4

# The part on the board, plus the datasheet corners of the part it
# replaced, so the comparison that chose it can be re-run rather than
# taken on trust.
CORNERS = ["j111-m3", "j111-m5", "j111-m10", "j112-m1", "j112-m3", "j112-m5"]
FITTED = "j112-m5"
HEAD = ".include crossover.net\n.include models.lib\n.include jfet-%s.lib\n"


def deck(name, body):
    p = os.path.join(SIM, name + ".sp")
    open(p, "w").write(body)
    return p


def run(paths):
    procs = []
    for p in paths:
        procs.append(subprocess.Popen(
            ["ngspice", "-b", os.path.basename(p)], cwd=SIM,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if len(procs) >= JOBS:
            procs.pop(0).wait()
    for pr in procs:
        pr.wait()


def col(path, i):
    d = np.loadtxt(os.path.join(SIM, path))
    return d[:, 0], d[:, 1 + 2 * i]


def env(v):
    return (v.max() - v.min()) / 2.0


def thd(path):
    t, y = col(path, 0)
    n = 1 << 13
    tt = np.linspace(t[0], t[-1], n, endpoint=False)
    yy = np.interp(tt, t, y)
    yy -= yy.mean()
    Y = np.abs(np.fft.rfft(yy))
    k = int(np.argmax(Y[1:]) + 1)
    h = np.sqrt(sum(Y[k * m] ** 2 for m in (2, 3, 4, 5, 6, 7) if k * m < len(Y)))
    return 100.0 * h / Y[k], np.max(np.abs(yy))


def head(t):
    print("\n" + t + "\n" + "-" * len(t))


def main():
    if not os.path.exists(os.path.join(SIM, "crossover.net")):
        print("no deck - run tools/gen_spice.py first")
        return 2

    cases = [("widest", 1, 0), ("closest", 0, 1), ("middle", 0.5, 0.5)]
    run([deck("rs-ac-" + t, HEAD % FITTED + """.control
set filetype=ascii
alterparam kf1 = %g
alterparam kf2 = %g
reset
ac dec 200 5 100k
wrdata rs-ac-%s.txt v(HIGH) v(MID) v(LOW) v(INPUT)
quit
.endc
""" % (a, b, t)) for t, a, b in cases])
    head("Filter: crossover points, summed response, mid-band level")
    print("%-9s %-11s %-11s %-15s %s"
          % ("knobs", "HIGH/MID", "MID/LOW", "summed ripple", "MID peak"))
    for t, _, _ in cases:
        d = np.loadtxt(os.path.join(SIM, "rs-ac-%s.txt" % t))
        f = d[:, 0]
        H, M, L, IN = [d[:, 1 + 3 * i] + 1j * d[:, 2 + 3 * i] for i in range(4)]
        s = 20 * np.log10(np.abs(H + M + L) / np.abs(IN))
        b = (f >= 20) & (f <= 20000)

        def x(a, c):
            i = np.where(np.diff(np.sign(np.abs(a) - np.abs(c))))[0]
            return "%.0f Hz" % f[i[0]] if len(i) else "-"
        print("%-9s %-11s %-11s %-15s %+.1f dB"
              % (t, x(H, M), x(M, L), "%.2f dB" % (s[b].max() - s[b].min()),
                 (20 * np.log10(np.abs(M) / np.abs(IN))).max()))

    deck("rs-dc", HEAD % FITTED + ".control\nop\n"
         "print v(HIGH) v(MID) v(LOW) v(N2260_1550) v(MG1)\nquit\n.endc\n")
    out = subprocess.run(["ngspice", "-b", "rs-dc.sp"], cwd=SIM,
                         capture_output=True, text=True).stdout
    head("DC: what reaches an amplifier terminal")
    for line in out.splitlines():
        if line.startswith("v("):
            print("   " + line)

    lvl = (1, 2, 3, 4, 5, 6, 7)
    d = []
    for c in CORNERS:
        for a in lvl:
            d.append(deck("rs-lv-%s-%d" % (c, a), HEAD % c + """.ic V(MUTE_SS)=-14.9
.control
set filetype=ascii
alterparam tramp = 1u
alterparam kf1 = 1
alterparam vfreq = 5k
alterparam vamp = %d
reset
tran 1u 0.2040 0.2000
wrdata rs-lv-%s-%d.txt v(HIGH)
quit
.endc
""" % (a, c, a)))
        for st, mv in (("mute", 1), ("pass", 0)):
            d.append(deck("rs-%s-%s" % (st, c), HEAD % c + """.ic V(MUTE_SS)=-14.9
.control
set filetype=ascii
alterparam tramp = 1u
alterparam kf1 = 1
alterparam vfreq = 5k
alterparam mute1 = %d
alterparam vamp = 0.5
reset
tran 1u 0.2040 0.2000
wrdata rs-%s-%s.txt v(HIGH_MUTE) v(HIGH_VOL)
quit
.endc
""" % (mv, st, c)))
    run(d)
    head("Mute: shunt-JFET off-state linearity, THD at HIGH, 5 kHz")
    print("%-9s %s" % ("HIGH pk", " ".join("%-10s" % c for c in CORNERS)))
    for a in lvl:
        v, pk = [], 0
        for c in CORNERS:
            t_, p = thd("rs-lv-%s-%d.txt" % (c, a))
            v.append(t_)
            pk = max(pk, p)
        print("%-9.2f %s" % (pk, " ".join("%-10s" % ("%.3f%%" % q) for q in v)))

    head("Mute: depth and insertion loss")
    print("%-11s %-13s %s" % ("corner", "mute depth", "loss when passing"))
    for c in CORNERS:
        m = env(col("rs-mute-%s.txt" % c, 0)[1]) / env(col("rs-mute-%s.txt" % c, 1)[1])
        p = env(col("rs-pass-%s.txt" % c, 0)[1]) / env(col("rs-pass-%s.txt" % c, 1)[1])
        print("%-11s %-13s %.2f dB" % (c, "%.1f dB" % (20 * np.log10(m)),
                                       20 * np.log10(p)))

    d = []
    for c in CORNERS:
        d.append(deck("rs-up-%s" % c, HEAD % c + """.control
set filetype=ascii
alterparam kf1 = 1
alterparam vamp = 0.3
alterparam vfreq = 1k
reset
tran 2m 20 0 2m
wrdata rs-up-%s.txt v(HIGH)
quit
.endc
""" % c))
        d.append(deck("rs-dn-%s" % c, HEAD % c + """.ic V(MUTE_SS)=-14.9
.control
set filetype=ascii
alterparam tramp = 1u
alterparam tfall = 0.2
alterparam tcollapse = 50m
alterparam kf1 = 1
alterparam vfreq = 5k
alterparam vamp = 2
reset
tran 20u 0.35
wrdata rs-dn-%s.txt v(HIGH_MUTE) v(VEE)
quit
.endc
""" % c))
    run(d)
    head("Mute: soft start on power-up, and re-engagement on power-down")
    print("%-11s %-15s %s" % ("corner", "unmutes after", "power-down mute at"))
    for c in CORNERS:
        t, y = col("rs-up-%s.txt" % c, 0)
        w = max(1, len(t) // 400)
        te = np.array([t[i] for i in range(0, len(t) - w, w)])
        ev = np.array([env(y[i:i + w]) for i in range(0, len(t) - w, w)])
        up = te[np.argmax(ev > 0.5 * ev[-20:].mean())]
        dd = np.loadtxt(os.path.join(SIM, "rs-dn-%s.txt" % c))
        tt, hm, vee = dd[:, 0], dd[:, 1], dd[:, 3]
        rows = []
        for lo in np.arange(0.196, 0.256, 0.002):
            m = (tt >= lo) & (tt < lo + 0.002)
            if m.any():
                rows.append((vee[m].mean(), env(hm[m])))
        dn = next((v for v, e in rows if e < 0.1 * rows[0][1]), float("nan"))
        print("%-11s %-15s %.1f V rail (%.0f%% of it already gone)"
              % (c, "%.2f s" % up, dn, 100 * (1 - abs(dn) / 15)))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
