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
            ctypes.c_int, ctypes.c_int,                        # NL, plane_l
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
        lib.flood.restype = ctypes.c_int
        lib.flood.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,          # NX, NY, NL
            np.ctypeslib.ndpointer(np.uint8, flags="C"),
            np.ctypeslib.ndpointer(np.uint8, flags="C"),
            np.ctypeslib.ndpointer(np.int32, flags="C"), ctypes.c_int,
            np.ctypeslib.ndpointer(np.uint8, flags="C"),
        ]
        _LIB = lib
    except Exception as e:                     # noqa: BLE001 - any failure falls back
        _ERR = e
        print("croute: falling back to the Python router (%s)" % e,
              file=sys.stderr)


def flood(mask, joint, seeds, ny, nx):
    """Connected cells of a copper pour, across every copper layer.

    mask/joint are (NL, NY, NX) uint8; seeds are (L, x, y).  Returns the
    reached mask, so the caller can check every ground pad landed in the
    same piece of copper.  The layer count comes from the mask - a plated
    hole ties ALL layers, so on a 4-layer board a joint cell reaches three
    other layers rather than one."""
    _load()
    nl = mask.shape[0]
    seen = np.zeros((nl, ny, nx), dtype=np.uint8)
    sd = _sorted_cells(seeds)
    if sd.size == 0:
        return seen
    if _LIB is None:
        # Pure-Python fallback flood fill when the compiled router is
        # unavailable.  This mirrors the C behaviour: 4-connected flood on
        # each layer, and layer transitions where `joint` ties the layers.
        from collections import deque
        q = deque()
        for i in range(0, sd.size, 3):
            L = int(sd[i])
            x = int(sd[i + 1])
            y = int(sd[i + 2])
            if 0 <= x < nx and 0 <= y < ny and mask[L, y, x]:
                seen[L, y, x] = 1
                q.append((L, x, y))
        while q:
            L, x, y = q.popleft()
            # 4-neighbour moves
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxp = x + dx
                nyp = y + dy
                if 0 <= nxp < nx and 0 <= nyp < ny and not seen[L, nyp, nxp] and mask[L, nyp, nxp]:
                    seen[L, nyp, nxp] = 1
                    q.append((L, nxp, nyp))
            # layer transition if a plated hole ties the layers here -
            # and it ties all of them, not just the opposite one
            if joint[L, y, x]:
                for other in range(nl):
                    if other == L:
                        continue
                    if not seen[other, y, x] and mask[other, y, x]:
                        seen[other, y, x] = 1
                        q.append((other, x, y))
        return seen
    _LIB.flood(nx, ny, nl,
               np.ascontiguousarray(mask, dtype=np.uint8),
               np.ascontiguousarray(joint, dtype=np.uint8),
               sd, len(sd) // 3, seen)
    return seen


def available():
    _load()
    return _LIB is not None


MAX_EXPAND = int(os.environ.get("MAX_EXPAND", 0))     # 0 = uncapped


def _sorted_cells(cells):
    """(L, x, y) triples as a flat int32 array, in tuple-sorted order.

    The sort is for reproducibility - these arrive as sets - but Python's
    `sorted()` compares three-tuples, and plane_stubs() offers A* every
    reached plane cell as a target: 1.04 MILLION target triples over a
    production run, one call alone sorting 132492.  That made `sorted` the
    largest single block of Python left outside the router itself.

    np.lexsort with the keys reversed gives exactly the tuple ordering -
    primary L, then x, then y - and is 2.4x faster on the big case.
    Checked against `sorted()` over random sets; the orderings are
    identical, so a run stays reproducible.
    """
    n = len(cells)
    if n == 0:
        return np.empty(0, dtype=np.int32)
    a = np.fromiter((v for c in cells for v in c), dtype=np.int32,
                    count=3 * n).reshape(n, 3)
    idx = np.lexsort((a[:, 2], a[:, 1], a[:, 0]))
    return a[idx].reshape(-1)


def route(occ, contested, blocked, use, hist, nid, relaxed, via_r,
          pres_fac, via_cost, sources, targets, tgt_xy, ny, nx, plane_l=-1):
    """One net.  Arrays are (NL, NY, NX); sources/targets are (L, x, y).

    `plane_l` names a copper layer that is present but not routable - the
    solid GND plane on the 4-layer stackup.  Vias still pass through it;
    the router simply never puts a track there.

    Returns the path as a list of (L, x, y), or None.  Semantics match
    astar() in gen_pcb_smd.py exactly - this exists to make that search
    fast, not to make it behave differently."""
    _load()
    # sources/targets arrive as sets of (L, x, y) from the caller's
    # connectivity bookkeeping; sort them so a run is reproducible.
    src = _sorted_cells(sources)
    tgt = _sorted_cells(targets)
    nl = occ.shape[0]
    global _OUT
    need = 3 * nl * ny * nx
    if _OUT is None or _OUT.size < need:
        _OUT = np.empty(need, dtype=np.int32)
    out = _OUT

    def ptr(a):
        return a.ctypes.data_as(ctypes.c_void_p) if a is not None else None

    n = _LIB.route(nx, ny, nl, plane_l,
                   np.ascontiguousarray(occ, dtype=np.int32),
                   np.ascontiguousarray(contested, dtype=np.uint8),
                   ptr(blocked), ptr(use), ptr(hist),
                   nid, 1 if relaxed else 0, via_r, pres_fac, via_cost,
                   src, len(src) // 3, tgt, len(tgt) // 3,
                   int(tgt_xy[0]), int(tgt_xy[1]),
                   out, nl * ny * nx, MAX_EXPAND)
    if n < 0:
        return None
    return [tuple(int(v) for v in out[3 * i:3 * i + 3]) for i in range(n)]
