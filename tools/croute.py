#!/usr/bin/env python3
"""Loader for the compiled grid router in router.c.

Builds the shared object on first use (gcc -O2) and caches it next to the
source, rebuilding whenever router.c is newer.  If anything about that
fails - no compiler, a build error - `available()` returns False and the
caller falls back to the pure-Python A*, which is kept as the reference
implementation and the thing this is checked against.
"""

import ctypes
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "router.c")
SO = os.path.join(HERE, "_router.so")

_LIB = None
_ERR = None
# Reused across calls.  A fresh one per net is ~35 MB at this grid size, and
# a board routes ~60 nets plus retries.
_OUT = None


def _build():
    if (os.path.exists(SO) and
            os.path.getmtime(SO) >= os.path.getmtime(SRC)):
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
        lib.route.restype = ctypes.c_int
        lib.route.argtypes = [
            ctypes.c_int, ctypes.c_int,                        # NX, NY
            np.ctypeslib.ndpointer(np.int32, flags="C"),        # occ
            np.ctypeslib.ndpointer(np.uint8, flags="C"),        # contested
            ctypes.c_void_p,                                    # blocked or NULL
            ctypes.c_void_p,                                    # use or NULL
            ctypes.c_void_p,                                    # hist or NULL
            ctypes.c_int, ctypes.c_int, ctypes.c_int,           # nid, relaxed, via_r
            ctypes.c_double, ctypes.c_double,                   # pres_fac, via_cost
            np.ctypeslib.ndpointer(np.int32, flags="C"), ctypes.c_int,   # src
            np.ctypeslib.ndpointer(np.int32, flags="C"), ctypes.c_int,   # tgt
            ctypes.c_int, ctypes.c_int,                         # target x, y
            np.ctypeslib.ndpointer(np.int32, flags="C"), ctypes.c_int,   # out
            ctypes.c_long,                                      # max_expand
        ]
        _LIB = lib
    except Exception as e:                     # noqa: BLE001 - any failure falls back
        _ERR = e
        print("croute: falling back to the Python router (%s)" % e,
              file=sys.stderr)


def available():
    _load()
    return _LIB is not None


MAX_EXPAND = int(os.environ.get("MAX_EXPAND", 0))     # 0 = uncapped


def route(occ, contested, blocked, use, hist, nid, relaxed, via_r,
          pres_fac, via_cost, sources, targets, tgt_xy, ny, nx):
    """One net.  Arrays are (2, NY, NX); sources/targets are (L, x, y).

    Returns the path as a list of (L, x, y), or None.  Semantics match
    astar() in gen_pcb_smd.py exactly - this exists to make that search
    fast, not to make it behave differently."""
    _load()
    # sources/targets arrive as sets of (L, x, y) from the caller's
    # connectivity bookkeeping; sort them so a run is reproducible.
    src = np.array(sorted(sources), dtype=np.int32).reshape(-1)
    tgt = np.array(sorted(targets), dtype=np.int32).reshape(-1)
    global _OUT
    need = 3 * 2 * ny * nx
    if _OUT is None or _OUT.size < need:
        _OUT = np.empty(need, dtype=np.int32)
    out = _OUT

    def ptr(a):
        return a.ctypes.data_as(ctypes.c_void_p) if a is not None else None

    n = _LIB.route(nx, ny,
                   np.ascontiguousarray(occ, dtype=np.int32),
                   np.ascontiguousarray(contested, dtype=np.uint8),
                   ptr(blocked), ptr(use), ptr(hist),
                   nid, 1 if relaxed else 0, via_r, pres_fac, via_cost,
                   src, len(src) // 3, tgt, len(tgt) // 3,
                   int(tgt_xy[0]), int(tgt_xy[1]),
                   out, 2 * ny * nx, MAX_EXPAND)
    if n < 0:
        return None
    return [tuple(int(v) for v in out[3 * i:3 * i + 3]) for i in range(n)]
