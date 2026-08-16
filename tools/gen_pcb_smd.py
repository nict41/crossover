#!/usr/bin/env python3
"""
Routed SMD board for the `retuned-quad-smd` variant - JLCPCB fab + assembly.

Reads docs/netlist-esp-p148-3way-crossover-retuned-quad-smd.json, places SMD
footprints matching real LCSC parts, routes every net (including ground) with a
two-layer grid maze router, then verifies the result two ways that do not trust
the router:

  connectivity  rasterise each net's copper across both layers, vias linking
                them, flood-fill from one pad, and require every other pad of
                that net to be reached.
  clearance     rasterise all copper by net, dilate by the clearance rule, and
                require that no two different nets ever overlap.

Also writes JLCPCB-format BOM and CPL files.

Units: 1 unit = 10 mil = 0.254 mm.  Everything is placed on a 0.5-unit grid so
that pad centres land exactly on the routing grid.
"""

import json
import math
import os
from heapq import heappush, heappop

import numpy as np

MM = 1.0 / 0.254
TOP, BOT, TOPSILK, OUTLINE, MULTI = 1, 2, 3, 10, 11

TRACK_W, CLEAR = 0.8, 0.8            # 8 mil track, 8 mil clearance
VIA_PAD, VIA_DRILL = 2.4, 1.2
GRID = 0.25                           # routing grid, units

_next = [100]


def gid():
    _next[0] += 1
    return "gge%d" % _next[0]


shapes, pads, placed = [], [], []
PARTS = {}

LCSC = {                              # verified live against the JLCPCB API
    "MC33079": ("C9376", "SOIC-14", "extended", 25307),
    "100R":    ("C17408", "0805", "basic", 7430702),
    "4.7k":    ("C17673", "0805", "basic", 6989256),
    "5.6k":    ("C4382", "0805", "basic", 787448),
    "10k":     ("C17414", "0805", "basic", 34645450),
    "11k":     ("C17429", "0805", "extended", 78383),
    "13k":     ("C2933304", "0805", "extended", 213066),
    "33nF":    ("C569866", "1210", "extended", 2001),
    "100nF":   ("C49678", "0805", "basic", 14609213),
    "10uF":    ("C46550416", "CASE-D5xL5.4", "extended", 288863),
}


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
def pad_rect(ref, num, x, y, net, w, h, layer=TOP):
    pts = "%g %g %g %g %g %g %g %g" % (x - w / 2, y - h / 2, x + w / 2, y - h / 2,
                                       x + w / 2, y + h / 2, x - w / 2, y + h / 2)
    shapes.append("PAD~RECT~%g~%g~%g~%g~%d~%s~%s~0~%s~0~%s~~~Y"
                  % (x, y, w, h, layer, net, num, pts, gid()))
    pads.append(dict(ref=ref, num=str(num), x=x, y=y, w=w, h=h, layer=layer, net=net))


def pad_tht(ref, num, x, y, net, dia=7.0, hole=2.0):
    shapes.append("PAD~ELLIPSE~%g~%g~%g~%g~%d~%s~%s~%g~~0~%s~~~Y"
                  % (x, y, dia, dia, MULTI, net, num, hole / 2, gid()))
    pads.append(dict(ref=ref, num=str(num), x=x, y=y, w=dia, h=dia,
                     layer=MULTI, net=net))


def track(points, layer, width, net=""):
    shapes.append("TRACK~%g~%d~%s~%s~%s"
                  % (width, layer, net, " ".join("%g %g" % p for p in points), gid()))


def silk_rect(x0, y0, x1, y1, w=0.5):
    track([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], TOPSILK, w)


def silk(x, y, s, size=2.4):
    shapes.append("TEXT~L~%g~%g~0.5~0~0~%d~~%g~%s~~~%s"
                  % (x, y, TOPSILK, size, s, gid()))


def silk_ref(x, y, s, size=2.4):
    """Designator text - TEXT~P so EasyEDA treats it as the component name."""
    shapes.append("TEXT~P~%g~%g~0.5~0~0~%d~~%g~%s~~~%s"
                  % (x, y, TOPSILK, size, s, gid()))


FP_SPANS = []          # (ref, first shape index, last+1, x, y)


def fp_begin():
    return len(shapes)


def fp_end(ref, mark, x, y):
    FP_SPANS.append((ref, mark, len(shapes), x, y))


# --------------------------------------------------------------------------
# footprints - all dimensions chosen so pad centres sit on the 0.5 grid
# --------------------------------------------------------------------------
def fp_chip(ref, x, y, net_of, value, kind="0805", rot=0):
    _m = fp_begin()
    w, h, off = dict(**{"0805": (4.0, 5.6, 4.0), "1210": (5.0, 10.0, 6.0)})[kind]
    d = [(-off, 0), (off, 0)] if rot == 0 else [(0, -off), (0, off)]
    pw, ph = (w, h) if rot == 0 else (h, w)
    for i, (dx, dy) in enumerate(d):
        pad_rect(ref, i + 1, x + dx, y + dy, net_of(ref, i + 1), pw, ph)
    ex = off + pw / 2
    ey = ph / 2
    silk_ref(x - 4, y - ey - 1.5, ref, 2.2)
    fp_end(ref, _m, x, y)
    PARTS[ref] = dict(value=value, x=x, y=y, rot=rot * 90, assembled=True,
                      pkg=kind)
    return (x - ex - CLEAR, y - ey - CLEAR, x + ex + CLEAR, y + ey + CLEAR)


def fp_soic14(pkg_ref, sections, x, y, net_of):
    _m = fp_begin()
    pitch, row, pw, ph = 5.0, 10.5, 6.0, 2.5
    for i in range(7):
        pad_rect(sections[i + 1], i + 1, x - row, y + i * pitch,
                 net_of(sections[i + 1], i + 1), pw, ph)
    for i in range(7):
        n = 8 + i
        pad_rect(sections[n], n, x + row, y + (6 - i) * pitch,
                 net_of(sections[n], n), pw, ph)
    silk_rect(x - 7.5, y - 4, x + 7.5, y + 34)
    track([(x - 7.5, y - 4), (x - 4, y - 4)], TOPSILK, 0.5)
    silk_ref(x - 7.5, y - 6.5, pkg_ref.split()[0], 2.4)
    fp_end(pkg_ref.split()[0], _m, x, y + 15)
    PARTS[pkg_ref] = dict(value="MC33079", x=x, y=y + 15, rot=0,
                          assembled=True, pkg="SOIC-14")
    return (x - row - pw / 2 - CLEAR, y - ph / 2 - CLEAR,
            x + row + pw / 2 + CLEAR, y + 30 + ph / 2 + CLEAR)


def fp_elec(ref, x, y, net_of, value):
    _m = fp_begin()
    for i, dx in enumerate((-8.5, 8.5)):
        pad_rect(ref, i + 1, x + dx, y, net_of(ref, i + 1), 8.0, 10.0)
    n = 20
    track([(x + 10 * math.cos(2 * math.pi * i / n),
            y + 10 * math.sin(2 * math.pi * i / n)) for i in range(n + 1)],
          TOPSILK, 0.5)
    silk_ref(x - 10, y - 12, ref, 2.2)
    silk(x - 15.5, y + 1, "+", 2.6)
    fp_end(ref, _m, x, y)
    PARTS[ref] = dict(value=value, x=x, y=y, rot=0, assembled=True,
                      pkg="CASE-D5xL5.4")
    return (x - 13 - CLEAR, y - 6 - CLEAR, x + 13 + CLEAR, y + 6 + CLEAR)


def fp_pads(ref, x, y, net_of, n=3, label="", horiz=False):
    _m = fp_begin()
    for i in range(n):
        px, py = (x + i * 10.0, y) if horiz else (x, y + i * 10.0)
        pad_tht(ref, i + 1, px, py, net_of(ref, i + 1))
    if horiz:
        silk_rect(x - 5, y - 5, x + (n - 1) * 10 + 5, y + 5)
        silk_ref(x - 5, y - 7.5, ref, 2.2)
        e = (x - 5, y - 5, x + (n - 1) * 10 + 5, y + 5)
    else:
        silk_rect(x - 5, y - 5, x + 5, y + (n - 1) * 10 + 5)
        silk_ref(x + 6.5, y - 2, ref, 2.2)
        e = (x - 5, y - 5, x + 5, y + (n - 1) * 10 + 5)
    fp_end(ref, _m, x, y)
    PARTS[ref] = dict(value=label or ref, x=x, y=y, rot=0, assembled=False,
                      pkg="THT-PAD")
    return (e[0] - CLEAR, e[1] - CLEAR, e[2] + CLEAR, e[3] + CLEAR)


def fp_pot(pkg_ref, gang_a, gang_b, x, y, net_of):
    """Board-mount 9 mm dual-gang pot.

    Six terminals in two rows of three on 0.2 in (5.08 mm) centres - the
    near-universal 9 mm dual pattern (Alps RK09K12, RV09 dual and the many
    equivalents).  Real parts are on 5.00 mm; the 0.08 mm/pitch difference is
    absorbed by the 1.2 mm holes.  VERIFY against your pot's datasheet.
    Two non-plated holes take the locating bosses.
    """
    _m = fp_begin()
    for i, dx in enumerate((-20.0, 0.0, 20.0)):
        pad_tht(gang_a, i + 1, x + dx, y, net_of(gang_a, i + 1), dia=7.0, hole=4.7)
        pad_tht(gang_b, i + 1, x + dx, y + 20.0, net_of(gang_b, i + 1),
                dia=7.0, hole=4.7)
    silk_rect(x - 24, y - 30, x + 24, y + 28)
    n = 20
    track([(x + 17 * math.cos(2 * math.pi * i / n),
            y - 4 + 17 * math.sin(2 * math.pi * i / n)) for i in range(n + 1)],
          TOPSILK, 0.5)
    silk_ref(x - 24, y - 32.5, pkg_ref, 2.6)
    fp_end(pkg_ref, _m, x, y + 10)
    PARTS[pkg_ref] = dict(value="20k dual", x=x, y=y + 10, rot=0,
                          assembled=False, pkg="POT-9MM-DUAL")
    return (x - 26 - CLEAR, y - 32 - CLEAR, x + 26 + CLEAR, y + 30 + CLEAR)


POT_BOSSES = []          # non-plated locating holes, filled in at placement


# ==========================================================================
#  netlist and placement
# ==========================================================================
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SLUG = "esp-p148-3way-crossover-retuned-quad-smd"
netdoc = json.load(open(os.path.join(ROOT, "docs", "netlist-%s.json" % SLUG)))
PIN_NET = {}
for name, members in netdoc["nets"].items():
    for m in members:
        r, _, n = m.rpartition(".")
        PIN_NET[(r, n)] = name


def N(ref, num):
    return PIN_NET.get((ref, str(num)), "")


VALUE = {r: netdoc["parts"][r]["value"] for r in netdoc["parts"]}
# Board size and slot pitch are overridable, so the smallest size that still
# routes and verifies can be searched for rather than guessed.  The defaults
# are the smallest found by that search: sweeping down from 340x280, sizes at
# and below 310x250 leave nets unroutable.  Component area is only ~13% of the
# board - the rest is routing headroom, and on two layers with this router it
# is what sets the floor, not the parts.
BW = float(os.environ.get("BOARD_W", 320.0))
BH = float(os.environ.get("BOARD_H", 260.0))
SLOT_PITCH = int(os.environ.get("SLOT_PITCH", 18))
MOUNT_HOLES = [(9, 9), (BW - 9, 9), (9, BH - 9), (BW - 9, BH - 9)]
MOUNT_R = 12.6 / 2
BOSS_R = 4.5

U1S = {1: "U1A", 2: "U1A", 3: "U1A", 4: "U1A", 5: "U1B", 6: "U1B", 7: "U1B",
       8: "U1C", 9: "U1C", 10: "U1C", 11: "U1A", 12: "U1D", 13: "U1D", 14: "U1D"}
U2S = {1: "U2A", 2: "U2A", 3: "U2A", 4: "U2A", 5: "U2B", 6: "U2B", 7: "U2B",
       8: "U2C", 9: "U2C", 10: "U2C", 11: "U2A", 12: "U2D", 13: "U2D", 14: "U2D"}

FIXED = [
    ("J2", dict(fn=fp_pads, n=2, label="IN", horiz=True), 26, 14),
    ("J1", dict(fn=fp_pads, n=3, label="PWR", horiz=True), 66, 14),
    ("J3", dict(fn=fp_pads, n=4, label="OUT", horiz=True), 126, 14),
    ("C0", dict(fn=fp_elec), 30, 42),
    ("TP1", dict(fn=fp_pads, n=1, label="TP1"), BW - 100, 14),
    ("TP2", dict(fn=fp_pads, n=1, label="TP2"), BW - 70, 14),
]
SOICS = [("U1", U1S, 110, 62), ("U2", U2S, 110, 150)]
POTS = [("VR1", "VR1A", "VR1B", BW / 2 - 65, BH - 40),
        ("VR2", "VR2A", "VR2B", BW / 2 + 45, BH - 40)]

# candidate slots for the movable chip parts, 18 x 18 grid over the interior
SLOT_XS = list(range(34, int(BW) - 10, SLOT_PITCH))
SLOT_YS = list(range(36, int(BH) - 75, SLOT_PITCH))


def place_fixed():
    for ref, spec, x, y in FIXED:
        fn = spec.pop("fn")
        if fn is fp_elec:
            placed.append(fn(ref, x, y, N, VALUE[ref]))
        else:
            placed.append(fn(ref, x, y, N, **spec))
        spec["fn"] = fn
    for pkg, sec, x, y in SOICS:
        placed.append(fp_soic14(pkg, sec, x, y, N))
    for pkg, ga, gb, x, y in POTS:
        placed.append(fp_pot(pkg, ga, gb, x, y, N))
        POT_BOSSES.extend([(x - 14, y - 22), (x + 14, y - 22)])


def boxes_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


CHIPS = {r: ("1210" if VALUE[r] == "33nF" else "0805")
         for r in VALUE if r.startswith("R") or
         (r.startswith("C") and r not in ("C0",))}


def slot_box(x, y, kind):
    w, h, off = {"0805": (4.0, 5.6, 4.0), "1210": (5.0, 10.0, 6.0)}[kind]
    ex, ey = off + w / 2, h / 2
    return (x - ex - CLEAR, y - ey - CLEAR, x + ex + CLEAR, y + ey + CLEAR)


def auto_place():
    """Anchor each part at the centroid of the fixed pads it touches, then
    take the nearest free slot; afterwards improve by pairwise swaps."""
    place_fixed()
    fixed_boxes = list(placed)
    # SOIC pads can only break out sideways, so reserve elbow room beside each
    for _pkg, _sec, _sx, _sy in SOICS:
        fixed_boxes.append((_sx - 26, _sy - 8, _sx + 26, _sy + 38))
    anchors = {}
    padpos = {(p["ref"], p["num"]): (p["x"], p["y"]) for p in pads}
    for ref in CHIPS:
        pts = []
        for name, members in netdoc["nets"].items():
            mine = [m for m in members if m.rpartition(".")[0] == ref]
            if not mine:
                continue
            for m in members:
                k = tuple(m.rpartition(".")[::2])
                if k in padpos:
                    pts.append(padpos[k])
        anchors[ref] = (sum(p[0] for p in pts) / len(pts),
                        sum(p[1] for p in pts) / len(pts)) if pts else (BW / 2, BH / 2)

    slots = [(x, y) for y in SLOT_YS for x in SLOT_XS]
    free = [s for s in slots
            if not any(boxes_overlap(slot_box(s[0], s[1], "1210"), b)
                       for b in fixed_boxes)
            and 20 < s[0] < BW - 14 and 20 < s[1] < BH - 20]

    assign, taken = {}, set()
    for ref in sorted(CHIPS, key=lambda r: anchors[r][0]):
        best = min((s for s in free if s not in taken),
                   key=lambda s: (s[0] - anchors[ref][0]) ** 2 +
                                 (s[1] - anchors[ref][1]) ** 2)
        assign[ref] = best
        taken.add(best)
    return assign, free


def rats(assign):
    pos = {(p["ref"], p["num"]): (p["x"], p["y"]) for p in pads}
    for ref, (x, y) in assign.items():
        off = 6.0 if CHIPS[ref] == "1210" else 4.0
        pos[(ref, "1")] = (x - off, y)
        pos[(ref, "2")] = (x + off, y)
    total = 0.0
    for name, members in netdoc["nets"].items():
        pts = [pos[tuple(m.rpartition(".")[::2])] for m in members
               if tuple(m.rpartition(".")[::2]) in pos]
        if len(pts) < 2:
            continue
        inside, rest = [pts[0]], pts[1:]
        while rest:
            d, j = min((math.dist(a, b), k) for k, b in enumerate(rest) for a in inside)
            total += d
            inside.append(rest.pop(j))
    return total


ASSIGN, FREE_SLOTS = auto_place()
BEFORE = rats(ASSIGN)
refs = list(ASSIGN)
improved = True
while improved:
    improved = False
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            a, b = refs[i], refs[j]
            cur = rats(ASSIGN)
            ASSIGN[a], ASSIGN[b] = ASSIGN[b], ASSIGN[a]
            if rats(ASSIGN) < cur - 1e-9:
                improved = True
            else:
                ASSIGN[a], ASSIGN[b] = ASSIGN[b], ASSIGN[a]
AFTER = rats(ASSIGN)

for ref, (x, y) in sorted(ASSIGN.items()):
    placed.append(fp_chip(ref, x, y, N, VALUE[ref], CHIPS[ref]))


# ==========================================================================
#  routing
# ==========================================================================
NX, NY = int(BW / GRID), int(BH / GRID)
occ = [np.zeros((NY, NX), dtype=np.int32), np.zeros((NY, NX), dtype=np.int32)]
# A cell can fall inside the keep-out of more than one net.  Recording only the
# first claimant would let the second net route there, so contested cells are
# flagged separately and are passable to nobody.
contested = [np.zeros((NY, NX), dtype=bool), np.zeros((NY, NX), dtype=bool)]
NETID = {n: i + 1 for i, n in enumerate(sorted(netdoc["nets"]))}
ROUTED = []          # (layer, [(x, y), ...], net) track polylines
VIAS = []            # (x, y, net)

# Dilation radii: a cell left unstamped guarantees that a track centreline
# placed there clears foreign copper.  GRID is added so that quantising a
# centreline onto the grid cannot eat into the clearance.
# No grid margin here: pad centres and the routing grid are both multiples of
# 0.5, so a centreline between two SOIC pads lands exactly on the midpoint.
# Keeping the margin would close every channel between adjacent SOIC pins.
PAD_DIL = CLEAR + TRACK_W / 2
TRK_DIL = TRACK_W / 2 + CLEAR + TRACK_W / 2 + GRID
VIA_DIL = VIA_PAD / 2 + CLEAR + TRACK_W / 2 + GRID
# a via is fatter than a track, so a cell that is safe for a track is not
# necessarily safe for a via - this is the extra margin one needs
VIA_EXTRA = VIA_PAD / 2 - TRACK_W / 2 + GRID


def cells_in_rect(x0, y0, x1, y1):
    return (max(0, int(math.floor(x0 / GRID))), max(0, int(math.floor(y0 / GRID))),
            min(NX - 1, int(math.ceil(x1 / GRID))), min(NY - 1, int(math.ceil(y1 / GRID))))


def stamp_rect(layer, x0, y0, x1, y1, netid, dil):
    a, b, c, d = cells_in_rect(x0 - dil, y0 - dil, x1 + dil, y1 + dil)
    for L in ([0, 1] if layer == MULTI else [layer - 1]):
        sub = occ[L][b:d + 1, a:c + 1]
        contested[L][b:d + 1, a:c + 1] |= (sub != 0) & (sub != netid)
        sub[sub == 0] = netid


def stamp_disc(layer, x, y, r, netid):
    a, b, c, d = cells_in_rect(x - r, y - r, x + r, y + r)
    ys, xs = np.mgrid[b:d + 1, a:c + 1]
    m = ((xs * GRID - x) ** 2 + (ys * GRID - y) ** 2) <= r * r
    for L in ([0, 1] if layer == MULTI else [layer - 1]):
        sub = occ[L][b:d + 1, a:c + 1]
        csub = contested[L][b:d + 1, a:c + 1]
        csub |= m & (sub != 0) & (sub != netid)
        sub[m & (sub == 0)] = netid


HOLE_NET = 10 ** 6           # matches no net, so nothing can route through
for _hx, _hy in MOUNT_HOLES:
    stamp_disc(MULTI, _hx, _hy, MOUNT_R + CLEAR + TRACK_W / 2 + GRID, HOLE_NET)
for _hx, _hy in POT_BOSSES:
    stamp_disc(MULTI, _hx, _hy, BOSS_R + CLEAR + TRACK_W / 2 + GRID, HOLE_NET)

for p in pads:
    if p["net"]:
        stamp_rect(p["layer"], p["x"] - p["w"] / 2, p["y"] - p["h"] / 2,
                   p["x"] + p["w"] / 2, p["y"] + p["h"] / 2,
                   NETID[p["net"]], PAD_DIL)


def core_cells(p):
    a, b, c, d = cells_in_rect(p["x"] - p["w"] / 2 + 0.4, p["y"] - p["h"] / 2 + 0.4,
                               p["x"] + p["w"] / 2 - 0.4, p["y"] + p["h"] / 2 - 0.4)
    layers = [0, 1] if p["layer"] == MULTI else [p["layer"] - 1]
    return {(L, xx, yy) for L in layers for yy in range(b, d + 1)
            for xx in range(a, c + 1)}


def via_ok(x, y, nid):
    """A via needs more room than the track that leads to it."""
    r = int(math.ceil(VIA_EXTRA / GRID))
    for L in (0, 1):
        a, b = max(0, x - r), max(0, y - r)
        c, d = min(NX - 1, x + r), min(NY - 1, y + r)
        sub = occ[L][b:d + 1, a:c + 1]
        if np.any(((sub != 0) & (sub != nid)) |
                  contested[L][b:d + 1, a:c + 1]):
            return False
    return True


def passable(L, x, y, nid):
    if not (1 <= x < NX - 1 and 1 <= y < NY - 1):
        return False
    if contested[L][y, x]:
        return False
    v = occ[L][y, x]
    return v == 0 or v == nid


def astar(sources, targets, nid, tgt_xy):
    seen, prev = {}, {}
    h = []
    for s in sources:
        if passable(s[0], s[1], s[2], nid):
            key = s
            seen[key] = 0
            heappush(h, (0, key))
    tset = set(targets)
    while h:
        f, cur = heappop(h)
        if cur in tset:
            path = [cur]
            while cur in prev:
                cur = prev[cur]
                path.append(cur)
            return path[::-1]
        L, x, y = cur
        g = seen[cur]
        nbrs = [(L, x + 1, y, 1), (L, x - 1, y, 1), (L, x, y + 1, 1),
                (L, x, y - 1, 1), (1 - L, x, y, 24)]
        for nl, nx_, ny_, c in nbrs:
            if not passable(nl, nx_, ny_, nid):
                continue
            if nl != L and not via_ok(x, y, nid):
                continue
            ng = g + c
            k = (nl, nx_, ny_)
            if k in seen and seen[k] <= ng:
                continue
            seen[k] = ng
            prev[k] = cur
            heappush(h, (ng + abs(nx_ - tgt_xy[0]) + abs(ny_ - tgt_xy[1]), k))
    return None


def emit_path(path, net, nid):
    """Split a cell path into per-layer polylines, stamping as we go."""
    runs, cur = [], [path[0]]
    for a, b in zip(path, path[1:]):
        if a[0] != b[0]:
            runs.append(cur)
            VIAS.append((a[1] * GRID, a[2] * GRID, net))
            stamp_disc(MULTI, a[1] * GRID, a[2] * GRID, VIA_DIL, nid)
            cur = [b]
        else:
            cur.append(b)
    runs.append(cur)
    for run in runs:
        for c in run:
            stamp_disc(c[0] + 1, c[1] * GRID, c[2] * GRID, TRK_DIL, nid)
        if len(run) < 2:
            continue
        pts = [(run[0][1] * GRID, run[0][2] * GRID)]
        for i in range(1, len(run) - 1):
            dx1, dy1 = run[i][1] - run[i - 1][1], run[i][2] - run[i - 1][2]
            dx2, dy2 = run[i + 1][1] - run[i][1], run[i + 1][2] - run[i][2]
            if (dx1, dy1) != (dx2, dy2):
                pts.append((run[i][1] * GRID, run[i][2] * GRID))
        pts.append((run[-1][1] * GRID, run[-1][2] * GRID))
        ROUTED.append((run[0][0] + 1, pts, net))


def route_order(n):
    """IC pins first.  An SOIC pad can only break out sideways, so if the
    general nets take those channels first the op-amp pins are trapped."""
    members = netdoc["nets"][n]
    return (0 if any(m.startswith("U") for m in members) else 1, len(members))


FAILED = []
for name in sorted(netdoc["nets"], key=route_order):
    nid = NETID[name]
    mine = [p for p in pads if p["net"] == name]
    if len(mine) < 2:
        continue
    connected = core_cells(mine[0])
    done = [mine[0]]
    todo = mine[1:]
    while todo:
        tgt = min(todo, key=lambda p: min(math.dist((p["x"], p["y"]),
                                                    (q["x"], q["y"])) for q in done))
        tc = core_cells(tgt)
        tx = int(round(tgt["x"] / GRID))
        ty = int(round(tgt["y"] / GRID))
        path = astar(connected, tc, nid, (tx, ty))
        if path is None:
            FAILED.append("%s: %s.%s unreachable" % (name, tgt["ref"], tgt["num"]))
            todo.remove(tgt)
            done.append(tgt)
            continue
        emit_path(path, name, nid)
        connected |= set(path) | tc
        todo.remove(tgt)
        done.append(tgt)


# ==========================================================================
#  independent verification - exact geometry, not the router's own bookkeeping
# ==========================================================================
def d_pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def d_seg_seg(a, b, c, d):
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2 = cross(c, d, a), cross(c, d, b)
    d3, d4 = cross(a, b, c), cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(d_pt_seg(a[0], a[1], c[0], c[1], d[0], d[1]),
               d_pt_seg(b[0], b[1], c[0], c[1], d[0], d[1]),
               d_pt_seg(c[0], c[1], a[0], a[1], b[0], b[1]),
               d_pt_seg(d[0], d[1], a[0], a[1], b[0], b[1]))


def d_pt_rect(px, py, r):
    return math.hypot(max(r[0] - px, 0, px - r[2]), max(r[1] - py, 0, py - r[3]))


def d_seg_rect(a, b, r):
    if d_pt_rect(a[0], a[1], r) == 0 or d_pt_rect(b[0], b[1], r) == 0:
        return 0.0
    corners = [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]
    return min(d_seg_seg(a, b, corners[i], corners[(i + 1) % 4]) for i in range(4))


def d_rect_rect(p, q):
    return math.hypot(max(p[0] - q[2], q[0] - p[2], 0),
                      max(p[1] - q[3], q[1] - p[3], 0))


def gap(f, g):
    """Copper-to-copper gap between two features, in units."""
    if f["k"] == "rect" and g["k"] == "rect":
        d = d_rect_rect(f["g"], g["g"])
    elif f["k"] == "rect" and g["k"] == "seg":
        d = d_seg_rect(g["g"][0], g["g"][1], f["g"])
    elif f["k"] == "seg" and g["k"] == "rect":
        d = d_seg_rect(f["g"][0], f["g"][1], g["g"])
    elif f["k"] == "rect" and g["k"] == "pt":
        d = d_pt_rect(g["g"][0], g["g"][1], f["g"])
    elif f["k"] == "pt" and g["k"] == "rect":
        d = d_pt_rect(f["g"][0], f["g"][1], g["g"])
    elif f["k"] == "seg" and g["k"] == "seg":
        d = d_seg_seg(f["g"][0], f["g"][1], g["g"][0], g["g"][1])
    elif f["k"] == "seg" and g["k"] == "pt":
        d = d_pt_seg(g["g"][0], g["g"][1], f["g"][0][0], f["g"][0][1],
                     f["g"][1][0], f["g"][1][1])
    elif f["k"] == "pt" and g["k"] == "seg":
        d = d_pt_seg(f["g"][0], f["g"][1], g["g"][0][0], g["g"][0][1],
                     g["g"][1][0], g["g"][1][1])
    else:
        d = math.dist(f["g"], g["g"])
    return d - f["hw"] - g["hw"]


FEATURES = []
for p in pads:
    FEATURES.append(dict(net=p["net"], k="rect", hw=0.0,
                         L={1, 2} if p["layer"] == MULTI else {p["layer"]},
                         g=(p["x"] - p["w"] / 2, p["y"] - p["h"] / 2,
                            p["x"] + p["w"] / 2, p["y"] + p["h"] / 2),
                         tag="%s.%s" % (p["ref"], p["num"])))
PAD_FEATURES = list(FEATURES)
for layer, pts, name in ROUTED:
    for a, b in zip(pts, pts[1:]):
        FEATURES.append(dict(net=name, k="seg", hw=TRACK_W / 2, L={layer},
                             g=(a, b), tag="track"))
for x, y, name in VIAS:
    FEATURES.append(dict(net=name, k="pt", hw=VIA_PAD / 2, L={1, 2},
                         g=(x, y), tag="via"))


def verify():
    problems = []
    # -- clearance: every pair of features on a shared layer, different nets
    for i, f in enumerate(FEATURES):
        for g in FEATURES[i + 1:]:
            if f["net"] == g["net"] or not (f["L"] & g["L"]):
                continue
            if gap(f, g) < CLEAR - 1e-9:
                problems.append("clearance %.2f mil between %s (%s) and %s (%s)"
                                % (gap(f, g) * 10, f["tag"], f["net"],
                                   g["tag"], g["net"]))
    # -- connectivity: union-find over touching same-net features
    parent = list(range(len(FEATURES)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, f in enumerate(FEATURES):
        for j in range(i + 1, len(FEATURES)):
            g = FEATURES[j]
            if f["net"] != g["net"] or not (f["L"] & g["L"]):
                continue
            if gap(f, g) <= 1e-9:
                parent[find(i)] = find(j)
    bypad = {}
    for i, f in enumerate(FEATURES):
        if f in PAD_FEATURES and f["net"]:
            bypad.setdefault(f["net"], []).append((i, f["tag"]))
    for name, items in bypad.items():
        roots = {find(i) for i, _ in items}
        if len(roots) > 1:
            groups = {}
            for i, tag in items:
                groups.setdefault(find(i), []).append(tag)
            problems.append("net %s is in %d pieces: %s"
                            % (name, len(roots), list(groups.values())))
    # -- nothing may sit under a mounting hole
    for hx, hy in MOUNT_HOLES + [(a, b) for a, b in POT_BOSSES]:
        for f in FEATURES:
            if f["k"] == "rect":
                d = d_pt_rect(hx, hy, f["g"])
            elif f["k"] == "seg":
                d = d_pt_seg(hx, hy, f["g"][0][0], f["g"][0][1],
                             f["g"][1][0], f["g"][1][1])
            else:
                d = math.dist((hx, hy), f["g"])
            r = MOUNT_R if (hx, hy) in MOUNT_HOLES else BOSS_R
            if d - f["hw"] < r + CLEAR:
                problems.append("%s (%s) is %.1f mil from the hole at "
                                "%g,%g" % (f["tag"], f["net"], (d - f["hw"]) * 10,
                                           hx, hy))
    # -- everything inside the board
    for p in pads:
        if not (2 < p["x"] < BW - 2 and 2 < p["y"] < BH - 2):
            problems.append("pad %s.%s outside board" % (p["ref"], p["num"]))
    # -- schematic agreement
    sch = {tuple(m.rpartition(".")[::2]) for mem in netdoc["nets"].values() for m in mem}
    have = {(p["ref"], p["num"]) for p in pads}
    if sch - have:
        problems.append("schematic pins with no pad: %s" % sorted(sch - have))
    if have - sch:
        problems.append("pads with no schematic pin: %s" % sorted(have - sch))
    return problems


ISSUES = verify()


# ==========================================================================
#  emit copper, outline, pour; wrap footprints as EasyEDA components
# ==========================================================================
for layer, pts, name in ROUTED:
    track(pts, layer, TRACK_W, name)
for x, y, name in VIAS:
    shapes.append("VIA~%g~%g~%g~%s~%g~%s" % (x, y, VIA_PAD, name, VIA_DRILL / 2, gid()))

for hx, hy in MOUNT_HOLES:
    shapes.append("HOLE~%g~%g~12.6~%s" % (hx, hy, gid()))
for hx, hy in POT_BOSSES:
    shapes.append("HOLE~%g~%g~9.0~%s" % (hx, hy, gid()))
track([(0, 0), (BW, 0), (BW, BH), (0, BH), (0, 0)], OUTLINE, 0.6)
silk(196, 96, "ESP P148 3-WAY VARIABLE CROSSOVER", 3.4)
silk(196, 104, "RETUNED QUAD SMD", 2.6)
silk(196, 112, "195Hz-1.03kHz / 73-186Hz", 2.6)
silk(196, 120, "ONE CHANNEL - BUILD TWO FOR STEREO", 2.6)

for L in (TOP, BOT):
    shapes.append("COPPERAREA~%g~%d~GND~%s~1~solid~%s~spoke~none~[]~0~2~1~none"
                  % (TRACK_W, L,
                     " ".join("%g %g" % p for p in
                              [(2, 2), (BW - 2, 2), (BW - 2, BH - 2), (2, BH - 2)]),
                     gid()))


def wrap_footprints(shape_list):
    out, idx = [], 0
    for ref, a, b, x, y in sorted(FP_SPANS, key=lambda t: t[1]):
        out.extend(shape_list[idx:a])
        info = PARTS.get(ref) or PARTS.get(ref + "  MC33079") or {}
        val = info.get("value", "")
        lcsc, pkg, _lib, _stk = LCSC.get(val, ("", info.get("pkg", ""), "", 0))
        attrs = ("package`%s`Contributor`gen_pcb_smd`Value`%s`"
                 % (pkg or info.get("pkg", ""), val))
        if lcsc:
            attrs += "Supplier`LCSC`Supplier Part`%s`" % lcsc
        out.append("LIB~%g~%g~%s~0~~%s~0#@$%s"
                   % (x, y, attrs, gid(), "#@$".join(shape_list[a:b])))
        idx = b
    out.extend(shape_list[idx:])
    return out


FINAL = wrap_footprints(shapes)

doc = {
    "head": {"docType": "3", "editorVersion": "6.5.46", "newgId": True,
             "c_para": {"Prefix Start": "1"}, "hasIdFlag": True,
             "importFlag": 0, "transformList": ""},
    "canvas": "CA~%g~%g~#000000~yes~#FFFFFF~10~%g~%g~line~0.5~mil~0.5~45~visible~0.5~0~0"
              % (BW * 2, BH * 2, BW * 2, BH * 2),
    "shape": FINAL,
    "layers": ["1~TopLayer~#FF0000~true~true~true",
               "2~BottomLayer~#0000FF~true~false~true",
               "3~TopSilkLayer~#FFFF00~true~false~true",
               "4~BottomSilkLayer~#808000~true~false~true",
               "5~TopPasterLayer~#808080~false~false~false",
               "6~BottomPasterLayer~#800000~false~false~false",
               "7~TopSolderLayer~#800080~false~false~false",
               "8~BottomSolderLayer~#AA00FF~false~false~false",
               "9~Ratlines~#6464FF~true~false~true",
               "10~BoardOutline~#FF00FF~true~false~true",
               "11~Multi-Layer~#C0C0C0~true~false~true",
               "12~Document~#FFFFFF~true~false~true"],
    "objects": ["Component", "Prefix", "Name", "BoardOutLine", "Pad", "Via",
                "Track", "Hole", "Copper", "Text", "Dimension", "Solid"],
    "BBox": {"x": 0, "y": 0, "width": BW, "height": BH},
    "preference": {"hideFootprints": "", "hideNets": ""},
    "DRCRULE": {"trackWidth": TRACK_W, "track2Track": CLEAR, "pad2Pad": CLEAR,
                "track2Pad": CLEAR, "hole2Hole": 1.2, "holeSize": VIA_DRILL,
                "Default": {"trackWidth": TRACK_W, "clearance": CLEAR,
                            "viaHoleDiameter": VIA_DRILL, "viaDiameter": VIA_PAD}},
    "netColors": {},
}
pcbdir = os.path.join(ROOT, "pcb")
os.makedirs(pcbdir, exist_ok=True)
with open(os.path.join(pcbdir, SLUG + "-pcb.json"), "w") as f:
    json.dump(doc, f, indent=1)

# ---- JLCPCB BOM (origin independent) and CPL (reference only) ------------
bom = {}
for ref, info in PARTS.items():
    if not info["assembled"]:
        continue
    r = ref.split()[0]
    val = info["value"]
    lcsc, pkg, lib, stk = LCSC.get(val, ("", info["pkg"], "?", 0))
    bom.setdefault((val, pkg, lcsc, lib), []).append(r)


def sortkey(r):
    import re as _re
    m = _re.match(r"([A-Za-z]+)(\d+)([A-Za-z]*)", r)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (r, 0, "")


lines = ["Comment,Designator,Footprint,LCSC Part #,JLC Library"]
for (val, pkg, lcsc, lib), refs in sorted(bom.items(), key=lambda kv: kv[0][1]):
    refs.sort(key=sortkey)
    lines.append('%s,"%s",%s,%s,%s' % (val, ",".join(refs), pkg, lcsc, lib))
bomdir = os.path.join(ROOT, "bom")
os.makedirs(bomdir, exist_ok=True)
with open(os.path.join(bomdir, "jlcpcb-bom-retuned-quad-smd.csv"), "w") as f:
    f.write("\n".join(lines) + "\n")

cpl = ["Designator,Mid X,Mid Y,Layer,Rotation"]
for ref, info in sorted(PARTS.items()):
    if not info["assembled"]:
        continue
    cpl.append("%s,%.4fmm,%.4fmm,top,%d"
               % (ref.split()[0], info["x"] * 0.254, (BH - info["y"]) * 0.254,
                  info["rot"]))
with open(os.path.join(bomdir, "jlcpcb-cpl-retuned-quad-smd.csv"), "w") as f:
    f.write("\n".join(cpl) + "\n")


# ---- preview -------------------------------------------------------------
def preview():
    sc = 4
    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%g" height="%g" '
         'viewBox="-4 -4 %g %g">' % (BW * sc, BH * sc, BW + 8, BH + 8),
         '<rect x="-4" y="-4" width="%g" height="%g" fill="#0b3d2e"/>' % (BW + 8, BH + 8)]
    for layer, pts, name in ROUTED:
        o.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="%g" '
                 'stroke-opacity="0.95" stroke-linecap="round" stroke-linejoin="round"/>'
                 % (" ".join("%g,%g" % p for p in pts),
                    "#d94b3a" if layer == TOP else "#3a6fd9", TRACK_W))
    for p in pads:
        o.append('<rect x="%g" y="%g" width="%g" height="%g" fill="#e8c069" rx="0.3"/>'
                 % (p["x"] - p["w"] / 2, p["y"] - p["h"] / 2, p["w"], p["h"]))
        if p["layer"] == MULTI:
            o.append('<circle cx="%g" cy="%g" r="1" fill="#0b3d2e"/>' % (p["x"], p["y"]))
    for x, y, name in VIAS:
        o.append('<circle cx="%g" cy="%g" r="%g" fill="#c9c9c9"/>' % (x, y, VIA_PAD / 2))
        o.append('<circle cx="%g" cy="%g" r="%g" fill="#0b3d2e"/>' % (x, y, VIA_DRILL / 2))
    for s in shapes:
        f = s.split("~")
        if f[0] == "TRACK" and f[2] in ("3", "10"):
            o.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="%s"/>'
                     % (f[4], "#f2f0e6" if f[2] == "3" else "#ff66ff", f[1]))
        elif f[0] == "TEXT":
            o.append('<text x="%s" y="%s" font-family="DejaVu Sans" font-size="%s" '
                     'fill="#f2f0e6">%s</text>' % (f[2], f[3], f[9], f[10]))
        elif f[0] == "HOLE":
            o.append('<circle cx="%s" cy="%s" r="%g" fill="#0b3d2e" stroke="#f2f0e6" '
                     'stroke-width="0.4"/>' % (f[1], f[2], float(f[3]) / 2))
    o.append("</svg>")
    return "\n".join(o)


svg = preview()
with open(os.path.join(pcbdir, SLUG + "-pcb.svg"), "w") as f:
    f.write(svg)
try:
    import cairosvg
    cairosvg.svg2png(bytestring=svg.encode(),
                     write_to=os.path.join(pcbdir, SLUG + "-pcb.png"),
                     output_width=int(BW * 4), output_height=int(BH * 4))
except Exception as exc:
    print("PNG preview skipped:", exc)

print("board %.1f x %.1f mm | %d footprints | %d pads | %d tracks | %d vias"
      % (BW * 0.254, BH * 0.254, len(FP_SPANS), len(pads), len(ROUTED), len(VIAS)))
print("ratsnest %.0f -> %.0f mm (placement optimisation)" % (BEFORE / MM, AFTER / MM))
if FAILED:
    print("UNROUTED:", FAILED)
if ISSUES:
    print("DRC PROBLEMS (%d):" % len(ISSUES))
    for p in ISSUES[:20]:
        print("  -", p)
else:
    print("verified: all nets connected, all clearances >= %.0f mil, "
          "no unrouted nets, pads match the schematic exactly" % (CLEAR * 10))
