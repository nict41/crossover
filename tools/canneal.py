#!/usr/bin/env python3
"""Loader and marshalling for the compiled annealer in anneal.c.

place.py stays the readable reference implementation; this hands the same
model to C because placement became 85% of a cold run once the router was
compiled, and a board search is hundreds of placements.

The two are NOT expected to produce identical layouts - the RNGs differ -
only comparable ones.  `PLACER=py` forces the Python version.
"""

import ctypes
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "anneal.c")
SO = os.path.join(HERE, "_anneal.so")

_LIB = None
_ERR = None

_ip = ctypes.POINTER(ctypes.c_int)
_dp = ctypes.POINTER(ctypes.c_double)


class Cfg(ctypes.Structure):
    """Must match the Cfg struct in anneal.c field for field."""
    _fields_ = [
        ("n", ctypes.c_int), ("nr", ctypes.c_int), ("nnets", ctypes.c_int),
        ("nfixed", ctypes.c_int), ("nnear", ctypes.c_int),
        ("ngroups", ctypes.c_int),
        ("rot_base", _ip), ("box", _dp), ("hbox", _dp),
        ("need", _dp), ("pneed", _dp),
        ("pad_base", _ip), ("pad_dx", _dp), ("pad_dy", _dp),
        ("group", _ip), ("outward", _ip),
        ("net_base", _ip), ("net_part", _ip), ("net_pk", _ip),
        ("pnet_base", _ip), ("pnet", _ip),
        ("fixed", _dp),
        ("near_a", _ip), ("near_b", _ip), ("near_pka", _ip), ("near_pkb", _ip),
        ("near_tgt", _dp),
        ("w_ov", ctypes.c_double), ("w_esc", ctypes.c_double),
        ("w_pesc", ctypes.c_double),
        ("w_cong", ctypes.c_double), ("w_hpwl", ctypes.c_double),
        ("w_h", ctypes.c_double), ("w_w", ctypes.c_double),
        ("w_wfloor", ctypes.c_double), ("w_edge", ctypes.c_double),
        ("w_near", ctypes.c_double), ("plane_gap", ctypes.c_double),
        ("t0", ctypes.c_double), ("t1", ctypes.c_double),
        ("amp0", ctypes.c_double), ("amp1", ctypes.c_double),
        ("ov_hi_mul", ctypes.c_double),
        ("cell", ctypes.c_double), ("supply", ctypes.c_double),
        ("gN", ctypes.c_int),
    ]


def _load():
    global _LIB, _ERR
    if _LIB is not None or _ERR is not None:
        return
    try:
        if not (os.path.exists(SO) and
                os.path.getmtime(SO) >= os.path.getmtime(SRC)):
            subprocess.run(["gcc", "-O3", "-shared", "-fPIC", "-o", SO, SRC],
                           check=True, capture_output=True)
        lib = ctypes.CDLL(SO)
        lib.anneal.restype = ctypes.c_int
        lib.anneal.argtypes = [ctypes.POINTER(Cfg), ctypes.c_int,
                               ctypes.c_ulonglong, _dp, _dp, _ip]
        _LIB = lib
    except Exception as e:                     # noqa: BLE001
        _ERR = e
        print("canneal: falling back to the Python annealer (%s)" % e,
              file=sys.stderr)


def available():
    _load()
    return _LIB is not None and not os.environ.get("PLACER") == "py"


_SIDE = {(-1, 0): 0, (1, 0): 1, (0, -1): 2, (0, 1): 3}
_KEEP = []          # anchors the arrays for as long as the Cfg lives


def _arr(seq, dtype):
    a = np.ascontiguousarray(seq, dtype=dtype)
    if a.size == 0:                     # ctypes cannot point into an empty array
        a = np.zeros(1, dtype=dtype)
    _KEEP.append(a)
    return a


def build(p, w):
    """Flatten a Placer into the layout anneal.c expects."""
    from place import _rotate
    _KEEP.clear()
    rot_base, box, need, pad_base, pdx, pdy, outward = [0], [], [], [0], [], [], []
    pneed, hbox = [], []
    for i, part in enumerate(p.parts):
        for r in part.rots:
            box.extend(p.boxoff[i][r])
            hbox.extend(p.hardoff[i][r])
            need.extend(p.need[i][r])
            pneed.extend(p.pneed[i][r])
            pads = p.padoff[i][r]
            pdx.extend(q[1] for q in pads)
            pdy.extend(q[2] for q in pads)
            pad_base.append(pad_base[-1] + len(pads))
            if part.outward is None:
                outward.append(-1)
            else:
                outward.append(_SIDE[tuple(_rotate(part.outward, r // 90))])
        rot_base.append(rot_base[-1] + len(part.rots))

    gid, seen = [], {}
    for part in p.parts:
        gid.append(seen.setdefault(part.group, len(seen)) if part.group else -1)

    net_base, net_part, net_pk = [0], [], []
    for mem in p.nets:
        for i, k in mem:
            net_part.append(i)
            net_pk.append(k)
        net_base.append(len(net_part))

    pnet_base, pnet = [0], []
    for i in range(len(p.parts)):
        pnet.extend(p.of_part[i])
        pnet_base.append(len(pnet))

    na, nb, npka, npkb, ntg = [], [], [], [], []
    for i, j, net, tgt in p.near:
        def pk(idx):
            pads = p.padoff[idx][p.parts[idx].rots[0]]
            for k, q in enumerate(pads):
                if q[0] == net:
                    return k
            return -1
        na.append(i); nb.append(j)
        npka.append(pk(i)); npkb.append(pk(j)); ntg.append(tgt)

    fixed = [v for b in p.fixed_boxes for v in b]

    A = _arr
    c = Cfg()
    c.n = len(p.parts); c.nr = len(rot_base) - 1 and rot_base[-1]
    c.nnets = len(p.nets); c.nfixed = len(p.fixed_boxes); c.nnear = len(p.near)
    c.ngroups = len(seen)
    c.rot_base = A(rot_base, np.int32).ctypes.data_as(_ip)
    c.box = A(box, np.float64).ctypes.data_as(_dp)
    c.hbox = A(hbox, np.float64).ctypes.data_as(_dp)
    c.need = A(need, np.float64).ctypes.data_as(_dp)
    c.pneed = A(pneed, np.float64).ctypes.data_as(_dp)
    c.pad_base = A(pad_base, np.int32).ctypes.data_as(_ip)
    c.pad_dx = A(pdx, np.float64).ctypes.data_as(_dp)
    c.pad_dy = A(pdy, np.float64).ctypes.data_as(_dp)
    c.group = A(gid, np.int32).ctypes.data_as(_ip)
    c.outward = A(outward, np.int32).ctypes.data_as(_ip)
    c.net_base = A(net_base, np.int32).ctypes.data_as(_ip)
    c.net_part = A(net_part, np.int32).ctypes.data_as(_ip)
    c.net_pk = A(net_pk, np.int32).ctypes.data_as(_ip)
    c.pnet_base = A(pnet_base, np.int32).ctypes.data_as(_ip)
    c.pnet = A(pnet, np.int32).ctypes.data_as(_ip)
    c.fixed = A(fixed, np.float64).ctypes.data_as(_dp)
    c.near_a = A(na, np.int32).ctypes.data_as(_ip)
    c.near_b = A(nb, np.int32).ctypes.data_as(_ip)
    c.near_pka = A(npka, np.int32).ctypes.data_as(_ip)
    c.near_pkb = A(npkb, np.int32).ctypes.data_as(_ip)
    c.near_tgt = A(ntg, np.float64).ctypes.data_as(_dp)
    c.w_ov = w["ov"]; c.w_esc = w["esc"]; c.w_pesc = w["pesc"]
    c.w_cong = w["cong"]
    c.w_hpwl = w["hpwl"]; c.w_h = w["h"]; c.w_w = w["w"]
    c.w_wfloor = w["wfloor"]; c.w_edge = w["edge"]; c.w_near = w.get("near", 0.0)
    from place import PLANE_GAP
    c.plane_gap = PLANE_GAP
    c.t0 = w.get("t0", 120.0); c.t1 = w.get("t1", 0.5)
    c.amp0 = w.get("amp0", 30.0); c.amp1 = w.get("amp1", 1.0)
    c.ov_hi_mul = 400.0
    c.cell = p.cell; c.supply = p.supply; c.gN = p.gN
    return c


def anneal(p, moves, w, seed):
    """Run the compiled anneal over Placer `p`, in place."""
    _load()
    cfg = build(p, w)
    rot_index = [p.parts[i].rots.index(p.R[i]) for i in range(p.n)]
    X = np.ascontiguousarray(p.X, dtype=np.float64)
    Y = np.ascontiguousarray(p.Y, dtype=np.float64)
    R = np.ascontiguousarray(rot_index, dtype=np.int32)
    ok = _LIB.anneal(ctypes.byref(cfg), int(moves), ctypes.c_ulonglong(seed),
                     X.ctypes.data_as(_dp), Y.ctypes.data_as(_dp),
                     R.ctypes.data_as(_ip))
    p.X[:] = X
    p.Y[:] = Y
    p.R = [p.parts[i].rots[int(R[i])] for i in range(p.n)]
    for i in range(p.n):
        p._sync_box(i)
    return bool(ok)
