#!/usr/bin/env python3
"""Loader for the compiled exact-geometry checks in geom.c.

Same bargain as croute.py: build on first use, cache the .so next to the
source, rebuild when the source is newer, and if anything about that fails
return unavailable so the caller falls back to the Python.  The Python in
gen_pcb_smd.py stays the reference implementation - this exists to make it
fast, not to make it behave differently, and `GEOM_CHECK=1` runs both and
asserts they agree.
"""

import ctypes
import os
import subprocess

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "geom.c")
SO = os.path.join(HERE, "_geom.so")

_LIB = None
_ERR = None

_DP = np.ctypeslib.ndpointer(np.float64, flags="C")
_IP = np.ctypeslib.ndpointer(np.int32, flags="C")
_LP = np.ctypeslib.ndpointer(np.int64, flags="C")


def _build():
    if (os.path.exists(SO)
            and os.path.getmtime(SO) >= os.path.getmtime(SRC)):
        return
    subprocess.run(["gcc", "-O3", "-shared", "-fPIC", "-o", SO, SRC],
                   check=True, capture_output=True)


def _load():
    global _LIB, _ERR
    if _LIB is not None or _ERR is not None:
        return
    try:
        _build()
        lib = ctypes.CDLL(SO)
        lib.pair_scan.restype = ctypes.c_int
        lib.pair_scan.argtypes = [
            ctypes.c_int, _IP, _DP, _DP, _LP, _LP,
            ctypes.c_double, ctypes.c_double, ctypes.c_int,
            _IP, ctypes.c_int,
        ]
        lib.cross_first.restype = None
        lib.cross_first.argtypes = [
            ctypes.c_int, _IP, _DP, _DP, _LP,
            ctypes.c_int, _IP, _DP, _DP, _LP,
            ctypes.c_double, ctypes.c_double, _IP,
        ]
        lib.cross_any.restype = ctypes.c_int
        lib.cross_any.argtypes = [
            ctypes.c_int, _IP, _DP, _DP, _LP,
            ctypes.c_int, _IP, _DP, _DP, _LP,
            ctypes.c_double, ctypes.c_double,
        ]
        _LIB = lib
    except Exception as exc:                      # no compiler, build error
        _ERR = exc


def available():
    _load()
    return _LIB is not None


def pack(feats):
    """Flatten a feature list into the arrays geom.c expects.

    `net` is interned to small ints exactly the way _feature_arrays() does
    it in gen_pcb_smd.py, INCLUDING the None that a pad with no net carries.
    That matters: Python gives every netless feature the same id, so a pair
    of them counts as same-net and is skipped by the clearance check.  An
    earlier version here handed each one a unique id, which would have
    turned those pairs into brand-new clearance complaints that the
    reference implementation never reported.
    """
    n = len(feats)
    kind = np.empty(n, dtype=np.int32)
    g = np.zeros((n, 4), dtype=np.float64)
    hw = np.empty(n, dtype=np.float64)
    lay = np.empty(n, dtype=np.int64)
    net = np.empty(n, dtype=np.int64)
    seen = {}
    for i, f in enumerate(feats):
        k = f["k"]
        if k == "rect":
            kind[i] = 0
            g[i] = f["g"]
        elif k == "seg":
            kind[i] = 1
            (ax, ay), (bx, by) = f["g"]
            g[i] = (ax, ay, bx, by)
        else:
            kind[i] = 2
            g[i, 0], g[i, 1] = f["g"][0], f["g"][1]
        hw[i] = f["hw"]
        lay[i] = sum(1 << L for L in f["L"])
        net[i] = seen.setdefault(f.get("net"), len(seen))
    return kind, np.ascontiguousarray(g).reshape(-1), hw, lay, net


def pair_scan(feats, limit, eps, same_net):
    """Pairs (i, j), i < j, sharing a layer and breaking the threshold.

    same_net False -> different nets, gap < limit - eps  (clearance)
    same_net True  -> same net,       gap <= eps         (connectivity)
    """
    _load()
    kind, g, hw, lay, net = pack(feats)
    cap = 1 << 14
    while True:
        out = np.empty(2 * cap, dtype=np.int32)
        n = _LIB.pair_scan(len(feats), kind, g, hw, lay, net,
                           float(limit), float(eps), 1 if same_net else 0,
                           out, cap)
        if n >= 0:
            return out[:2 * n].reshape(-1, 2)
        cap *= 4                                  # overflowed; try again


def cross_first(a, b, limit, eps=0.0):
    """For each feature in `a`, the index of the FIRST feature in `b` it
    violates, or -1.

    First, not any: the silkscreen check reports one problem per label and
    the Python picks the earliest offender in scan order, so returning an
    arbitrary hit would reorder verify()'s output.
    """
    _load()
    out = np.full(len(a), -1, dtype=np.int32)
    if not a or not b:
        return out
    ka, ga, hwa, la, _ = pack(a)
    kb, gb, hwb, lb, _ = pack(b)
    _LIB.cross_first(len(a), ka, ga, hwa, la,
                     len(b), kb, gb, hwb, lb,
                     float(limit), float(eps), out)
    return out


def cross_any(cand, others, limit, eps=1e-9):
    """True if any candidate feature violates `limit` against any other."""
    _load()
    if not cand or not others:
        return False
    ka, ga, hwa, la, _ = pack(cand)
    kb, gb, hwb, lb, _ = pack(others)
    return bool(_LIB.cross_any(len(cand), ka, ga, hwa, la,
                               len(others), kb, gb, hwb, lb,
                               float(limit), float(eps)))
