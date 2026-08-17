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

# 4.0 units = 1.016mm text height, comfortably over JLCPCB's stated
# ~1.0mm legibility minimum (was 2.2-2.6 units = 0.56-0.66mm - about
# half that, flagged by an independent DFM review as a real risk
# specifically because the connector pin-name labels are how this
# board gets wired without the schematic to hand).  Used for R/C
# designators, which nobody needs to read at a glance while wiring the
# board up - LABEL_SIZE below is for everything that IS read that way.
DESIG_SIZE = 4.0

# Bigger than DESIG_SIZE, for every label that isn't an R or C value:
# connector/pot designators (J*, TP*, U*, VR*) and every functional
# pin-name label (IN, GND, +15V, HIGH VOL, HIGH/MID, ...).  Those are the
# text a person actually reads while wiring the board up or setting a
# knob, not while probing a specific resistor - worth the extra size and
# the board area it costs.
LABEL_SIZE = 5.0

CLEAR = 0.8                           # 8 mil clearance

# Trace width by purpose, not one width for everything.  Signal traces carry a
# few mA at most; +15V/-15V/GND carry the combined supply current of both quad
# op-amps and (for GND) the audio return path, so they get twice the copper -
# lower resistance and inductance, and a trace that reads as a supply rail
# rather than a signal line.
#
# IPC-2221 (external layer, 1oz Cu, dT=10C, I = 0.048*dT^0.44*(w[mil]*1.378)^0.725)
# gives ~886 mA at 10 mil and ~1.25 A at 16 mil - either is >15x the ~58 mA
# worst-case combined quiescent+load current of two MC33079 packages, so
# current capacity was never the binding constraint at any width considered.
# The real reason to size these near their pads' short dimension (40 mil for
# an 0805 pad) rather than the current-only minimum: a trace noticeably
# thinner than about 1/3 of the pad it lands on reads as an error, not a
# design choice, and is worth a few mm of board area to avoid.
SIG_W = 1.2                           # 12 mil - default signal trace (1:3.3 vs an 0805 pad)
PWR_W = 1.6                           # 16 mil - +15V, -15V, GND      (1:2.5 vs an 0805 pad)
POWER_NETS = {"+15V", "-15V", "GND"}


def net_width(name):
    return PWR_W if name in POWER_NETS else SIG_W


MAX_W = max(SIG_W, PWR_W)
VIA_PAD, VIA_DRILL = 2.8, 1.2         # 0.20mm annular ring (was 0.15mm - no margin)
# Routing grid.  Pad centres all land on 0.5 (see the module docstring), so
# 0.5 is the coarsest grid that still puts every pad exactly on a routing
# node - but it is NOT equivalent to 0.25, because the grid also sets how
# many parallel channels fit between obstacles.  Measured on the same
# layout: 0.5 runs ~3x faster (1m47 vs 5m35) and routes strictly worse
# (6 nets left in pieces vs 4).
#
# So 0.25 is the production setting, and GRID=0.5 is a deliberately
# cheaper, strictly more pessimistic filter for board-size searches: a
# size that comes back clean at 0.5 will almost certainly be clean at
# 0.25, while a size that fails at 0.5 may still be fine - which makes it
# useful for finding candidates fast, and useless for ruling them out.
# Combined with running four sizes at once (scratchpad psweep.sh), that is
# the ~12x that makes sweeping practical; every candidate still gets
# confirmed at 0.25 before it becomes the committed board.
GRID = float(os.environ.get("GRID", 0.25))

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


def silk(x, y, s, size=DESIG_SIZE):
    shapes.append("TEXT~L~%g~%g~0.6~0~0~%d~~%g~%s~~~%s"
                  % (x, y, TOPSILK, size, s, gid()))


def silk_ref(x, y, s, size=DESIG_SIZE):
    """Designator text - TEXT~P so EasyEDA treats it as the component name."""
    shapes.append("TEXT~P~%g~%g~0.6~0~0~%d~~%g~%s~~~%s"
                  % (x, y, TOPSILK, size, s, gid()))


def label_bbox(ax, ay, text, size=DESIG_SIZE):
    """Matches text_bbox()'s geometry exactly (same magic numbers), so
    courtyard math computed before a label is drawn agrees with the label
    once it actually exists."""
    w_ = 0.62 * size * len(text)
    return (ax, ay - size, ax + w_, ay + 0.25 * size)


def chip_courtyard(ref, x, y, kind, rot):
    """The exact box fp_chip's pads + designator label occupy, with
    clearance.  Shared with slot_box so a slot judged free here is
    guaranteed free when the real footprint lands there."""
    w, h, off = {"0805": (4.0, 5.6, 4.0), "1210": (5.0, 10.0, 6.0)}[kind]
    pw, ph = (w, h) if rot == 0 else (h, w)
    if rot == 0:
        px0, px1 = x - (off + pw / 2), x + (off + pw / 2)
        py0, py1 = y - ph / 2, y + ph / 2
        lx0, ly0, lx1, ly1 = label_bbox(x - 4, y - ph / 2 - 1.5, ref)
    else:
        px0, px1 = x - pw / 2, x + pw / 2
        py0, py1 = y - (off + ph / 2), y + (off + ph / 2)
        lx0, ly0, lx1, ly1 = label_bbox(x + pw / 2 + off + 1.0, y - 0.8, ref)
    x0, y0 = min(px0, lx0), min(py0, ly0)
    x1, y1 = max(px1, lx1), max(py1, ly1)
    return (x0 - CLEAR, y0 - CLEAR, x1 + CLEAR, y1 + CLEAR)


FP_SPANS = []          # (ref, first shape index, last+1, x, y)


def fp_begin():
    return len(shapes)


def fp_end(ref, mark, x, y):
    FP_SPANS.append((ref, mark, len(shapes), x, y))


# --------------------------------------------------------------------------
# footprints - all dimensions chosen so pad centres sit on the 0.5 grid
# --------------------------------------------------------------------------
def fp_chip(ref, x, y, net_of, value, kind="0805", rot=0):
    """rot=0: pads left/right, offset along X.  rot=1: pads top/bottom,
    offset along Y.  Courtyard (incl. the designator label) comes from
    chip_courtyard() using these exact same label anchor points, so the
    two can't drift apart."""
    _m = fp_begin()
    w, h, off = dict(**{"0805": (4.0, 5.6, 4.0), "1210": (5.0, 10.0, 6.0)})[kind]
    d = [(-off, 0), (off, 0)] if rot == 0 else [(0, -off), (0, off)]
    pw, ph = (w, h) if rot == 0 else (h, w)
    for i, (dx, dy) in enumerate(d):
        pad_rect(ref, i + 1, x + dx, y + dy, net_of(ref, i + 1), pw, ph)
    if rot == 0:
        silk_ref(x - 4, y - ph / 2 - 1.5, ref)
    else:
        # designator goes to the side, clear of the pads regardless of its
        # own vertical position, since the pads don't extend past pw/2 in X
        silk_ref(x + pw / 2 + off + 1.0, y - 0.8, ref)
    box = chip_courtyard(ref, x, y, kind, rot)
    fp_end(ref, _m, x, y)
    PARTS[ref] = dict(value=value, x=x, y=y, rot=rot * 90, assembled=True,
                      pkg=kind)
    return box


def fp_soic14(pkg_ref, sections, x, y, net_of, rot=0):
    """SOIC-14.  rot=0: pins down the sides, package tall, pins escape sideways.
    rot=90: pins along top and bottom, package wide, pins escape vertically."""
    _m = fp_begin()
    pitch, row = 5.0, 10.5
    ref = pkg_ref.split()[0]
    if rot == 0:
        pw, ph = 6.0, 2.5
        for i in range(7):
            pad_rect(sections[i + 1], i + 1, x - row, y + i * pitch,
                     net_of(sections[i + 1], i + 1), pw, ph)
            n = 8 + i
            pad_rect(sections[n], n, x + row, y + (6 - i) * pitch,
                     net_of(sections[n], n), pw, ph)
        silk_rect(x - 7.5, y - 4, x + 7.5, y + 34)
        track([(x - 7.5, y - 4), (x - 4, y - 4)], TOPSILK, 0.5)
        lax, lay = x - 7.5, y - 6.5
        silk_ref(lax, lay, ref, LABEL_SIZE)
        cx, cy = x, y + 15
        pad_box = (x - row - pw / 2, y - ph / 2, x + row + pw / 2, y + 30 + ph / 2)
    else:
        pw, ph = 2.5, 6.0
        for i in range(7):
            pad_rect(sections[i + 1], i + 1, x + i * pitch, y + row,
                     net_of(sections[i + 1], i + 1), pw, ph)
            n = 8 + i
            pad_rect(sections[n], n, x + (6 - i) * pitch, y - row,
                     net_of(sections[n], n), pw, ph)
        silk_rect(x - 4, y - 7.5, x + 34, y + 7.5)
        track([(x - 4, y + 7.5), (x - 4, y + 4)], TOPSILK, 0.5)
        # Beside the package, not above it.  Above (the old x-4, y-16) put
        # the label's silk keepout directly over the escape corridor for
        # pins 13 and 14 - which at rot=90 are the top-LEFT pins, right
        # where the label sat - and every top-row pin escapes vertically by
        # definition of this rotation.  That is why U1D.13 / U2D.13 / U2D.14
        # came back unroutable at *every* board size tried during the
        # width-and-layout rework: not a space problem at all, a label
        # parked in the one channel those pins can use.  Right-aligned to
        # end 6 units left of the body, vertically centred on it, so it
        # clears both pin rows entirely (pins span x+0..x+30; body silk
        # starts at x-4).
        lax = x - 6 - 0.62 * LABEL_SIZE * len(ref)
        lay = y + 0.4 * LABEL_SIZE
        silk_ref(lax, lay, ref, LABEL_SIZE)
        cx, cy = x + 15, y
        pad_box = (x - 4, y - row - ph / 2, x + 34, y + row + ph / 2)
    fp_end(ref, _m, cx, cy)
    PARTS[pkg_ref] = dict(value="MC33079", x=cx, y=cy, rot=rot,
                          assembled=True, pkg="SOIC-14")
    lbl = label_bbox(lax, lay, ref, LABEL_SIZE)
    return (min(pad_box[0], lbl[0]) - CLEAR, min(pad_box[1], lbl[1]) - CLEAR,
            max(pad_box[2], lbl[2]) + CLEAR, max(pad_box[3], lbl[3]) + CLEAR)


def fp_elec(ref, x, y, net_of, value):
    _m = fp_begin()
    for i, dx in enumerate((-8.5, 8.5)):
        pad_rect(ref, i + 1, x + dx, y, net_of(ref, i + 1), 8.0, 10.0)
    n, r = 20, 10.0
    track([(x + r * math.cos(2 * math.pi * i / n),
            y + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)],
          TOPSILK, 0.5)
    rlax, rlay = x - 10, y - 12
    silk_ref(rlax, rlay, ref)
    plax, play = x - 15.5, y + 1
    silk(plax, play, "+")
    fp_end(ref, _m, x, y)
    PARTS[ref] = dict(value=value, x=x, y=y, rot=0, assembled=True,
                      pkg="CASE-D5xL5.4")
    pad_box = (x - 8.5 - 4, y - 5, x + 8.5 + 4, y + 5)
    circle_box = (x - r, y - r, x + r, y + r)
    rlbl, plbl = label_bbox(rlax, rlay, ref), label_bbox(plax, play, "+")
    x0 = min(pad_box[0], circle_box[0], rlbl[0], plbl[0])
    y0 = min(pad_box[1], circle_box[1], rlbl[1], plbl[1])
    x1 = max(pad_box[2], circle_box[2], rlbl[2], plbl[2])
    y1 = max(pad_box[3], circle_box[3], rlbl[3], plbl[3])
    return (x0 - CLEAR, y0 - CLEAR, x1 + CLEAR, y1 + CLEAR)


def fp_pads(ref, x, y, net_of, n=3, label="", horiz=False, names=None):
    _m = fp_begin()
    label_boxes = []
    for i in range(n):
        px, py = (x + i * 10.0, y) if horiz else (x, y + i * 10.0)
        pad_tht(ref, i + 1, px, py, net_of(ref, i + 1))
        if names and i < len(names):
            lax = px - 0.31 * LABEL_SIZE * len(names[i])   # centred, per text_bbox's own width formula
            lay = py + 9.5 if horiz else py + 1.6
            silk(lax, lay, names[i], LABEL_SIZE)
            label_boxes.append(label_bbox(lax, lay, names[i], LABEL_SIZE))
    if horiz:
        silk_rect(x - 5, y - 5, x + (n - 1) * 10 + 5, y + 5)
        rlax, rlay = x - 5, y - 7.5
        e = (x - 5, y - 5, x + (n - 1) * 10 + 5, y + 5)
    else:
        silk_rect(x - 5, y - 5, x + 5, y + (n - 1) * 10 + 5)
        rlax, rlay = x + 6.5, y - 2
        e = (x - 5, y - 5, x + 5, y + (n - 1) * 10 + 5)
    silk_ref(rlax, rlay, ref, LABEL_SIZE)
    label_boxes.append(label_bbox(rlax, rlay, ref, LABEL_SIZE))
    fp_end(ref, _m, x, y)
    PARTS[ref] = dict(value=label or ref, x=x, y=y, rot=0, assembled=False,
                      pkg="THT-PAD")
    x0, y0, x1, y1 = e
    for lb in label_boxes:
        x0, y0 = min(x0, lb[0]), min(y0, lb[1])
        x1, y1 = max(x1, lb[2]), max(y1, lb[3])
    return (x0 - CLEAR, y0 - CLEAR, x1 + CLEAR, y1 + CLEAR)


def fp_term(ref, x, y, net_of, n=2, names=None):
    """Screw terminal block, 3.5 mm pitch (14 units = 3.556 mm; the 1.1 mm
    holes absorb the difference).  Wire entry faces the board edge."""
    _m = fp_begin()
    label_boxes = []
    for i in range(n):
        pad_tht(ref, i + 1, x + i * 14.0, y, net_of(ref, i + 1), dia=8.0, hole=4.3)
        if names and i < len(names):
            lax = x + i * 14.0 - 0.31 * LABEL_SIZE * len(names[i])
            lay = y + 15
            silk(lax, lay, names[i], LABEL_SIZE)
            label_boxes.append(label_bbox(lax, lay, names[i], LABEL_SIZE))
    x1 = x + (n - 1) * 14.0
    silk_rect(x - 7, y - 16, x1 + 7, y + 10)
    for i in range(n):
        silk_rect(x + i * 14.0 - 4, y - 14, x + i * 14.0 + 4, y - 8, 0.4)
    rlax, rlay = x - 7, y - 18.5
    silk_ref(rlax, rlay, ref, LABEL_SIZE)
    label_boxes.append(label_bbox(rlax, rlay, ref, LABEL_SIZE))
    fp_end(ref, _m, (x + x1) / 2, y)
    PARTS[ref] = dict(value="TB-%dP-3.5" % n, x=(x + x1) / 2, y=y, rot=0,
                      assembled=False, pkg="TB-%dP-3.5" % n)
    x0, y0, x1b, y1 = x - 9, y - 18, x1 + 9, y + 12
    for lb in label_boxes:
        x0, y0 = min(x0, lb[0]), min(y0, lb[1])
        x1b, y1 = max(x1b, lb[2]), max(y1, lb[3])
    return (x0 - CLEAR, y0 - CLEAR, x1b + CLEAR, y1 + CLEAR)


def fp_pot(pkg_ref, gang_a, gang_b, x, y, net_of):
    """Board-mount 9 mm dual-gang pot.

    Six terminals in two rows of three on 0.2 in (5.08 mm) centres - the
    near-universal 9 mm dual pattern (Alps RK09K12, RV09 dual and the many
    equivalents).  Real parts are on 5.00 mm; the 0.08 mm/pitch difference is
    absorbed by the 1.2 mm holes.  VERIFY against your pot's datasheet.
    Two non-plated holes take the locating bosses.

    Body width here (52 units) is NOT the same number as the single-gang
    RK097 footprint's (37 units, see fp_pot_single) - a first pass tried
    that, reasoning "same mechanical family, same diameter," and it was
    wrong: this pattern's own pads sit on a 5.08 mm pitch (40 units,
    pin-to-pin) against RK097's 2.5 mm, so it is a physically bigger part,
    not the same body with more elements stacked inside, and a 37-unit
    body doesn't even reach the outer pads. 52 units keeps the same margin
    beyond the pad row (6 units, matching CLEAR + a bit) that the original
    circle-based courtyard did, before this session tightened it to
    something that turned out too small. Height still extrapolates the
    single-gang footprint's per-gang top/bottom margins across both rows,
    since no verified *dual*-gang drawing was found either way.
    """
    _m = fp_begin()
    for i, dx in enumerate((-20.0, 0.0, 20.0)):
        pad_tht(gang_a, i + 1, x + dx, y, net_of(gang_a, i + 1), dia=7.0, hole=4.7)
        pad_tht(gang_b, i + 1, x + dx, y + 20.0, net_of(gang_b, i + 1),
                dia=7.0, hole=4.7)
    # body bottom y+35, not y+40: the dual-gang body depth is an
    # extrapolation either way (no verified dual-gang drawing found), and
    # y+40 was deep enough to push the outline off the board edge at the
    # pot row's verified-clean position.  It only has to enclose the two
    # pad rows (deepest pad edge is y+23.5), so trimming it is free -
    # and far better than moving the pots, which broke their pin escapes.
    bx0, by0, bx1, by1 = x - 26, y - 8, x + 26, y + 35
    silk_rect(bx0, by0, bx1, by1)
    flax, flay = bx0, by0 - 3
    ftext = "HIGH/MID" if pkg_ref == "VR1" else "MID/LOW"
    silk(flax, flay, ftext, LABEL_SIZE)
    rlax, rlay = bx0, flay - 8
    silk_ref(rlax, rlay, pkg_ref, LABEL_SIZE)
    fp_end(pkg_ref, _m, x, y + 10)
    PARTS[pkg_ref] = dict(value="20k dual", x=x, y=y + 10, rot=0,
                          assembled=False, pkg="POT-9MM-DUAL")
    rlbl = label_bbox(rlax, rlay, pkg_ref, LABEL_SIZE)
    flbl = label_bbox(flax, flay, ftext, LABEL_SIZE)
    x0 = min(bx0, rlbl[0], flbl[0])
    y0 = min(by0, rlbl[1], flbl[1])
    x1 = max(bx1, rlbl[2], flbl[2])
    y1 = max(by1, rlbl[3], flbl[3])
    return (x0 - CLEAR, y0 - CLEAR, x1 + CLEAR, y1 + CLEAR)


def fp_pot_single(ref, x, y, net_of, label):
    """Single-gang volume pot: Alps RK097111080R, LCSC C470577 - the real
    part chosen for this board (see docs/pcb-notes-smd.md), not a generic
    stand-in.  Pad AND body geometry both pulled from LCSC/EasyEDA's own
    footprint for this exact part number (its `/api/products/C470577/svgs`
    PCB-layer drawing, viewed directly as a rendered PNG, not just parsed
    as raw coordinates): 3 terminals on a 2.5 mm pitch, ~1.9 mm pad,
    ~1.2 mm hole - drawn here on 2.54 mm (0.1 in), the nearest 0.5-unit
    grid point, absorbing the same sub-0.1 mm slop the dual-gang footprint
    already does between its real 5.00 mm pitch and its drawn 5.08 mm.

    The body is a compact ~9.5 x 7 mm rectangle sitting mostly *above* the
    pad row, not the ~17 mm circle drawn here previously - that circle was
    two independent guesses stacked on each other (the dual-gang
    footprint's pitch AND an AliExpress-listing body size), and both were
    wrong: the real part's actual drawing is nothing like a circle, and is
    barely bigger than the pad row itself. No locating-boss holes: Alps'
    own product page doesn't show anti-rotation pegs for this size/torque
    class. Still worth a last check against the datasheet before ordering.
    """
    _m = fp_begin()
    for i, dx in enumerate((-10.0, 0.0, 10.0)):
        pad_tht(ref, i + 1, x + dx, y, net_of(ref, i + 1), dia=7.5, hole=4.7)
    bx0, by0, bx1, by1 = x - 18.5, y - 8, x + 18.5, y + 20
    silk_rect(bx0, by0, bx1, by1)
    flax, flay = bx0, by0 - 3
    silk(flax, flay, label, LABEL_SIZE)
    rlax, rlay = bx0, flay - 8
    silk_ref(rlax, rlay, ref, LABEL_SIZE)
    fp_end(ref, _m, x, y)
    PARTS[ref] = dict(value="10k log", x=x, y=y, rot=0, assembled=False,
                      pkg="POT-9MM-SINGLE")
    rlbl = label_bbox(rlax, rlay, ref, LABEL_SIZE)
    flbl = label_bbox(flax, flay, label, LABEL_SIZE)
    x0 = min(bx0, rlbl[0], flbl[0])
    y0 = min(by0, rlbl[1], flbl[1])
    x1 = max(bx1, rlbl[2], flbl[2])
    y1 = max(by1, rlbl[3], flbl[3])
    return (x0 - CLEAR, y0 - CLEAR, x1 + CLEAR, y1 + CLEAR)


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
# 288 x 191 (73.2 x 48.5 mm) was the router-imposed floor for the layout
# without front-panel volume controls - found by a bounded sweep after the
# placement, rotation, trace-width and label work above: 287 failed on a
# footprint courtyard collision, 190 on an unroutable net.
#
# Adding VR3/VR4/VR5 (one output-volume pot each, see POTS_SINGLE below)
# needed real width, not just a squeeze: five front-panel controls in a row
# need real pitch between them, and the first attempt at that pitch reused
# VR1/VR2's old 110-unit spacing, wedging the new pots into a 56-unit gap
# with ~1.4 units of clearance on each side - not a courtyard violation, but
# tight enough to starve neighbouring pins' escape routes.  420 x 220 (106.7
# x 55.9 mm) is the smallest size found, after that fix, that both fits the
# new 80-unit-pitch panel row and verifies fully clean; SLOT_PITCH=20 (up
# from 18) was needed alongside it - 18 at this size left one net split in
# two.  This is a real board-area cost for a real feature, not a regression
# in the router or the search: see docs/pcb-notes-smd.md for what actually
# ran into trouble along the way (and how) before landing here.
#
# BOARD WIDTH IS NOW DERIVED, NOT SEARCHED - the change above (420 units)
# came from sweeping board sizes until the OLD layout's anchors (SOICs and
# terminal blocks, both at absolute positions left over from a much
# narrower board) happened to still route, which papered over the real
# problem: those anchors never moved to use the width the pot row was
# already forcing the board to have, so everything piled up on the left
# and the right third of every board this size or larger sat empty - a
# board-size sweep can't fix a layout problem, it can only find a size
# large enough to hide it.  PANEL_PITCH (below) is what actually decides
# how wide the board needs to be: five front-panel controls, uniform
# pitch, so their own geometry sets the minimum, not trial and error.
PANEL_PITCH = 80
# The outermost two controls are single-gang pots (18.5-unit body
# half-width from the board centreline out); pn each needs >=2 units clear
# of the board edge beyond that.
POT_ROW_HALF_W = 2 * PANEL_PITCH + 18.5 + 2
# The pot row's own geometry sets the FLOOR (365 units); the router still
# needs more than the floor to find a way through, so the default is the
# smallest size actually confirmed clean at GRID=0.25 rather than the
# geometric minimum.  Sizes between the two route *almost* cleanly - the
# rearranged layout reached 400x185 (101.6 x 47.0 mm) with four IC-pin
# escapes still failing - so there is very likely a smaller board here;
# see CLAUDE.md before spending another sweep on it.
POT_ROW_MIN_BW = round(2 * POT_ROW_HALF_W) + 4
BW = float(os.environ.get("BOARD_W", max(POT_ROW_MIN_BW, 420)))
# 225, not 220: POT_Y is BH-45 because the dual-gang body reaches 40 units
# below its anchor and BH-40 drew its outline right on the board edge (a
# gap in the checks - they catch TEXT silk leaving the board and copper
# crowding it, but not a body outline drawn as a plain track).  Simply
# moving the pots up 5 units instead left their pins unroutable, so the
# board gets the 5 units back and the pot row stays exactly where it
# verified clean - the board was always 5 units short of containing its
# own pot footprints.
BH = float(os.environ.get("BOARD_H", 220.0))
SLOT_PITCH = int(os.environ.get("SLOT_PITCH", 20))
MOUNT_HOLES = [(9, 9), (BW - 9, 9), (9, BH - 9), (BW - 9, BH - 9)]
MOUNT_R = 12.6 / 2
BOSS_R = 4.5

U1S = {1: "U1A", 2: "U1A", 3: "U1A", 4: "U1A", 5: "U1B", 6: "U1B", 7: "U1B",
       8: "U1C", 9: "U1C", 10: "U1C", 11: "U1A", 12: "U1D", 13: "U1D", 14: "U1D"}
U2S = {1: "U2A", 2: "U2A", 3: "U2A", 4: "U2A", 5: "U2B", 6: "U2B", 7: "U2B",
       8: "U2C", 9: "U2C", 10: "U2C", 11: "U2A", 12: "U2D", 13: "U2D", 14: "U2D"}

# Rotating the quads so their pins escape vertically, into the open space
# above and below rather than into the channel between the two packages, is
# what makes the side-by-side arrangement compact; `IC_LAYOUT=stacked`
# (one quad above the other) is the alternative, kept switchable for a
# sweep, but side+rotated is what every current default assumes.
IC_ROT = int(os.environ.get("IC_ROT", 90))
# U1/U2 centred on the board, not left-anchored: they used to sit at fixed
# x=56/186, tuned for a ~300-unit-wide board that predates the pot row -
# on the current, wider, pot-row-driven board that left the entire right
# third empty (nothing else was anchored there either) rather than using
# the width the pot row already requires. Same 130-unit separation between
# them either way - that number is what's actually tuned, for SOIC-to-SOIC
# routing room, not their absolute position.
if os.environ.get("IC_LAYOUT", "side") == "side":
    # -15 on each: fp_soic14 at rot=90 lays its pads out from `x` rightward,
    # so the package's own centre lands at x+15, not x.  Passing BW/2 +/- 65
    # straight through therefore shifted BOTH packages 15 units right of
    # where they were meant to sit - crowding the right edge and wasting the
    # same 15 units on the left, which is exactly the "everything piles up
    # off-centre" problem this rearrangement exists to remove.  Subtracting
    # the offset makes the pair genuinely symmetric about the centreline.
    # ATTEMPTED AND REVERTED: centring these on the board (BW/2 -/+ 65 - 15,
    # the -15 correcting for fp_soic14's own x+15 centre offset at rot=90)
    # packs the board visibly better - the largest empty rectangle drops
    # from 37x28 mm to 17x27 mm - but costs routability at every size tried
    # between 380 and 440 units, always as IC pin escapes (U1B.7, U2D.13)
    # and pot pins (VR1A.1, VR1B.2/3) left unreachable.  These left-anchored
    # positions are the ones that actually verify clean.  Worth revisiting
    # with a router that can rip up and re-route; see CLAUDE.md.
    U1_X, U2_X = 56, 186
    SOICS = ([("U1", U1S, U1_X, 74), ("U2", U2S, U2_X, 74)] if IC_ROT
             else [("U1", U1S, BW / 2 - 65, 60), ("U2", U2S, BW / 2 + 65, 60)])
else:
    U1_X = U2_X = BW / 2
    SOICS = [("U1", U1S, U1_X, 62), ("U2", U2S, U2_X, 150)]

# Terminal block row, left to right: input, power, then LOW/MID/HIGH -
# mirroring the pot row's own left-to-right LOW...HIGH order below, so
# each output's terminal block roughly lines up above its volume pot,
# instead of the pre-volume-control order (which put HIGH first because
# nothing else determined it). Spread evenly across the actual board
# width - same reasoning as U1/U2 above: fixed absolute spacing tuned for
# an old, narrower board left this row clustered on the left with a large
# unused gap before TP1/TP2, rather than using the width the pot row
# already sets.
J_ROW = ["J2", "J1", "J5", "J4", "J3"]
J_SPECS = {
    "J2": dict(n=2, names=["IN", "GND"]),
    "J1": dict(n=3, names=["+15", "GND", "-15"]),
    "J5": dict(n=2, names=["LOW", "GND"]),
    "J4": dict(n=2, names=["MID", "GND"]),
    "J3": dict(n=2, names=["HI", "GND"]),
}
# ATTEMPTED AND REVERTED alongside the U1/U2 centring above: spreading
# this row evenly across the real board width (J_X0=26 to BW-100) used the
# space far better but was part of the same rearrangement that would not
# route.  Back to the tuned absolute positions that verify clean.  The
# left-to-right ORDER change is kept - it costs nothing and puts each
# output block above the volume pot it feeds.
# Positions are the tuned, verified-clean ones.  Reordering which output
# sits at which position (to mirror the pots' LOW..HIGH order) was tried
# and reverted too: it reads better but perturbs routing, and the pot row
# order - the part actually asked for - is independent of it.
J_X = {"J2": 26, "J1": 68, "J3": 136, "J4": 176, "J5": 216}
FIXED = [
    # y=25.5, not 22: at DESIG_SIZE=4 the "J*" ref label (drawn above the
    # block) needs ~3 extra units of headroom above the block to stay on
    # the board - see fp_term's rlax/rlay.
    (ref, dict(fn=fp_term, **J_SPECS[ref]), J_X[ref], 25.5)
    for ref in J_ROW
] + [
    # 24, not the long-standing 36: moving U1/U2's designator beside the
    # package (see fp_soic14) extends their courtyard ~8 units further
    # left, which at 36 overlapped C0.  The label move is kept because it
    # keeps silk out of the pin-escape corridor - a whole class of
    # unroutable-pin bug - so C0 gives way instead.
    ("C0", dict(fn=fp_elec), 24, 62),
    # BW-relative, not absolute, so a board-width sweep doesn't spuriously
    # run these off the right edge before it ever reaches a routing limit.
    # 30-unit gap, not 22: at LABEL_SIZE the "TP1"/"TP2" ref labels are
    # wide enough that the old 22-unit gap let their courtyards overlap.
    ("TP1", dict(fn=fp_pads, n=1, label="TP1"), BW - 50, 22),
    ("TP2", dict(fn=fp_pads, n=1, label="TP2"), BW - 20, 22),
]
# Five front-panel controls in a row, uniform PANEL_PITCH (defined above,
# where it also sets BW itself), LOW on the left and HIGH on the right:
# LOW vol, VR2 (M/L freq), MID vol, VR1 (H/M freq), HIGH vol.  Every pot -
# single or dual gang - has the same 26-unit body half-width, so a shared
# pitch is what actually matters; the first cut at this used VR1/VR2's OLD
# 110-unit spacing (tuned for just the two of them) and tried to wedge a
# third pot into the 56-unit gap between them, leaving ~1.4 units of
# clearance on each side - not a courtyard violation, but tight enough to
# starve the neighbouring pins' escape routes and leave 3-5 nets unroutable
# across several board sizes before this was traced back to spacing, not
# area.
# The dual-gang body reaches 40 units below its anchor (see fp_pot), so
# BH-40 put its silk outline exactly on the board edge - invisible to the
# checks, which only catch TEXT silk running off the board and copper
# crowding it, not a body outline drawn as a plain track.  BH-45 keeps the
# whole outline on the board with a few units to spare.
POT_Y = BH - 40
POTS = [("VR1", "VR1A", "VR1B", BW / 2 + PANEL_PITCH, POT_Y),
        ("VR2", "VR2A", "VR2B", BW / 2 - PANEL_PITCH, POT_Y)]
POTS_SINGLE = [("VR3", BW / 2 + 2 * PANEL_PITCH, "HIGH VOL"),
               ("VR4", BW / 2, "MID VOL"),
               ("VR5", BW / 2 - 2 * PANEL_PITCH, "LOW VOL")]

# candidate slots for the movable chip parts, 18 x 18 grid over the interior
SLOT_XS = list(range(34, int(BW) - 10, SLOT_PITCH))
# Down to the bottom edge: the strips either side of the pots and the gap
# between them are usable board.  The pot courtyards are already in
# fixed_boxes, so slots that would collide with a pot are dropped anyway.
SLOT_YS = list(range(36, int(BH) - 12, SLOT_PITCH))


def place_fixed():
    for ref, spec, x, y in FIXED:
        fn = spec.pop("fn")
        if fn is fp_elec:
            placed.append(fn(ref, x, y, N, VALUE[ref]))
        elif fn is fp_term:
            placed.append(fn(ref, x, y, N, **spec))
        else:
            placed.append(fn(ref, x, y, N, **spec))
        spec["fn"] = fn
    for pkg, sec, x, y in SOICS:
        placed.append(fp_soic14(pkg, sec, x, y, N, IC_ROT))
    for pkg, ga, gb, x, y in POTS:
        placed.append(fp_pot(pkg, ga, gb, x, y, N))
        # y-22: verified clear of every pad by measurement (d_pt_rect in
        # verify()), not tied to where the silk body outline happens to be
        # drawn - a real boss-peg position isn't known either way (no
        # verified dual-gang datasheet, same caveat as the body outline
        # above), so correctness against the actual pads wins over the
        # cosmetic goal of nesting the boss inside the drawn rectangle.
        # A tighter -4 offset (matching the new, smaller body) put VR1A/
        # VR2A pads only 25.5 mil from the hole against a required 53 mil -
        # caught by verify(), not shipped.
        POT_BOSSES.extend([(x - 14, y - 22), (x + 14, y - 22)])
    for ref, x, label in POTS_SINGLE:
        placed.append(fp_pot_single(ref, x, POT_Y, N, label))


def boxes_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


CHIPS = {r: ("1210" if VALUE[r] == "33nF" else "0805")
         for r in VALUE if r.startswith("R") or
         (r.startswith("C") and r not in ("C0",))}

# Rotation is only explored for 0805 parts.  An 1210, rotated, is 23.6 units
# across (10 unit body + 6 unit lead offset either side + clearance) - wider
# than the 18-unit slot pitch, so two rotated 1210s in adjacent slots would
# overlap by construction, no matter what the local search decides.  A 0805
# stays under the pitch in both orientations (13.6 x 12.0 max), so rotating
# one can never collide with its own neighbouring slot.
ROTATABLE = {r for r, k in CHIPS.items() if k == "0805"}
ROT = {r: 0 for r in CHIPS}


def slot_box(x, y, kind, rot=0, ref="XXX"):
    """Just chip_courtyard() under a different name, kept for callers that
    are asking "is this slot free" before they know which ref will land
    there - ref defaults to a 3-char placeholder ("R17", "C3A", ... are the
    longest real designators here), the worst case for label width, so an
    unresolved query never underestimates the space a part will need."""
    return chip_courtyard(ref, x, y, kind, rot)


def real_box(ref, x, y, rot):
    return slot_box(x, y, CHIPS[ref], rot, ref)


def auto_place():
    """Anchor each part at the centroid of the fixed pads it touches, then
    take the nearest free slot that neither the fixed layout NOR an
    already-placed chip already occupies; afterwards improve by pairwise
    swaps and single-part rotation flips."""
    place_fixed()
    fixed_boxes = list(placed)
    for _hx, _hy in MOUNT_HOLES:      # two of these sit in the pot band
        r = MOUNT_R + CLEAR + 2
        fixed_boxes.append((_hx - r, _hy - r, _hx + r, _hy + r))
    # SOIC pads can only break out sideways, so reserve elbow room beside each
    for _pkg, _sec, _sx, _sy in SOICS:
        fixed_boxes.append((_sx - 8, _sy - 24, _sx + 38, _sy + 24) if IC_ROT
                           else (_sx - 24, _sy - 8, _sx + 24, _sy + 38))
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
            and 18 < s[0] < BW - 12 and 24 < s[1] < BH - 14]

    assign, taken = {}, set()
    placed_boxes = []          # real, rot=0, per-part boxes placed so far
    for ref in sorted(CHIPS, key=lambda r: anchors[r][0]):
        candidates = sorted((s for s in free if s not in taken),
                            key=lambda s: (s[0] - anchors[ref][0]) ** 2 +
                                          (s[1] - anchors[ref][1]) ** 2)
        for s in candidates:
            b = real_box(ref, s[0], s[1], 0)
            if not any(boxes_overlap(b, ob) for ob in placed_boxes):
                assign[ref] = s
                taken.add(s)
                placed_boxes.append(b)
                break
        else:
            # every remaining free slot collides with an already-placed chip
            # (shouldn't happen with the current part count/board size, but
            # fail loudly rather than silently overlapping if it ever does)
            assign[ref] = candidates[0]
            taken.add(candidates[0])
            placed_boxes.append(real_box(ref, candidates[0][0], candidates[0][1], 0))
    return assign, free


def rats(assign, rot=None):
    rot = rot or ROT
    pos = {(p["ref"], p["num"]): (p["x"], p["y"]) for p in pads}
    for ref, (x, y) in assign.items():
        off = 6.0 if CHIPS[ref] == "1210" else 4.0
        if rot.get(ref, 0) == 0:
            pos[(ref, "1")], pos[(ref, "2")] = (x - off, y), (x + off, y)
        else:
            pos[(ref, "1")], pos[(ref, "2")] = (x, y - off), (x, y + off)
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


def placement_valid(assign, rot, fixed_boxes):
    boxes = [real_box(ref, x, y, rot.get(ref, 0)) for ref, (x, y) in assign.items()]
    for b in boxes:
        if any(boxes_overlap(b, fb) for fb in fixed_boxes):
            return False
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if boxes_overlap(a, b):
                return False
    return True


ASSIGN, FREE_SLOTS = auto_place()
_fixed_boxes_for_search = list(placed)
for _hx, _hy in MOUNT_HOLES:
    _r = MOUNT_R + CLEAR + 2
    _fixed_boxes_for_search.append((_hx - _r, _hy - _r, _hx + _r, _hy + _r))
for _pkg, _sec, _sx, _sy in SOICS:
    _fixed_boxes_for_search.append((_sx - 8, _sy - 24, _sx + 38, _sy + 24) if IC_ROT
                                   else (_sx - 24, _sy - 8, _sx + 24, _sy + 38))

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
            if (rats(ASSIGN) < cur - 1e-9 and
                    placement_valid(ASSIGN, ROT, _fixed_boxes_for_search)):
                improved = True
            else:
                ASSIGN[a], ASSIGN[b] = ASSIGN[b], ASSIGN[a]
    for ref in ROTATABLE:
        cur = rats(ASSIGN)
        ROT[ref] = 1 - ROT[ref]
        if (rats(ASSIGN) < cur - 1e-9 and
                placement_valid(ASSIGN, ROT, _fixed_boxes_for_search)):
            improved = True
        else:
            ROT[ref] = 1 - ROT[ref]
AFTER = rats(ASSIGN)
assert placement_valid(ASSIGN, ROT, _fixed_boxes_for_search), \
    "placement search produced an overlapping layout - this should be unreachable"

for ref, (x, y) in sorted(ASSIGN.items()):
    placed.append(fp_chip(ref, x, y, N, VALUE[ref], CHIPS[ref], rot=ROT[ref]))

# split across two lines - a single line at DESIG_SIZE would run past the
# board edge (the old one-line/2.4pt version fit only because it was too
# small to read, the exact defect DESIG_SIZE exists to fix)
silk(26, BH - 10.0, "ESP P148 3-WAY VARIABLE CROSSOVER - RETUNED QUAD SMD")
silk(26, BH - 4.5, "195Hz-1.03kHz / 73-186Hz - ONE CHANNEL")


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
# A pad's keepout has to anticipate the WIDEST trace that might later route
# past it - any pad could have a fat GND run go by - so it must be sized off
# MAX_W, not off the pad's own net.  (An earlier version of this line sized it
# off the pad's own net, which is backwards: the pad's own net tells you what
# connects TO the pad, not what might pass NEAR it.  That under-reserved every
# signal pad by exactly the signal/power half-width difference, and every
# resulting clearance violation in testing was that gap almost to the mil.)
#
# The one deliberate exception is the SOIC-14 pins themselves: at their 50 mil
# pitch, the gap between two adjacent pads is only wide enough for a signal
# trace to thread the midpoint - a 20 mil power trace could never physically
# fit there even ignoring clearance, so reserving MAX_W margin there would
# only block the signal escape route this session already fixed once, for no
# real safety benefit.  So IC pads keep the tight, own-width-based margin;
# everything else (terminals, passives, the pot) gets the full margin.
IC_REFS = set(U1S.values()) | set(U2S.values())


def pad_dilation(ref, net):
    if ref in IC_REFS:
        return CLEAR + net_width(net) / 2
    return CLEAR + MAX_W / 2


VIA_DIL = VIA_PAD / 2 + CLEAR + MAX_W / 2 + GRID
# a via is fatter than a track, so a cell that is safe for a track is not
# necessarily safe for a via - this is the extra margin one needs, sized off
# the thinnest trace since that is the smallest margin any occupied cell is
# guaranteed to already carry
VIA_EXTRA = VIA_PAD / 2 - SIG_W / 2 + GRID


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
    stamp_disc(MULTI, _hx, _hy, MOUNT_R + CLEAR + MAX_W / 2 + GRID, HOLE_NET)
for _hx, _hy in POT_BOSSES:
    stamp_disc(MULTI, _hx, _hy, BOSS_R + CLEAR + MAX_W / 2 + GRID, HOLE_NET)

for p in pads:
    if p["net"]:
        stamp_rect(p["layer"], p["x"] - p["w"] / 2, p["y"] - p["h"] / 2,
                   p["x"] + p["w"] / 2, p["y"] + p["h"] / 2,
                   NETID[p["net"]], pad_dilation(p["ref"], p["net"]))


def text_bbox(sh):
    """Bounding box of a top-silk TEXT shape, or None.  Shared by the
    pre-routing keepout below and the post-hoc check in verify(), so the two
    can't drift apart into disagreeing about what counts as an overlap."""
    f = sh.split("~")
    if f[0] != "TEXT" or f[7] != "3":
        return None
    w_ = 0.62 * float(f[9]) * len(f[10])
    return (float(f[2]), float(f[3]) - float(f[9]),
            float(f[2]) + w_, float(f[3]) + 0.25 * float(f[9]), f[10])


# A pad's own literal footprint (shrunk slightly - the same shrink core_cells
# uses below) must stay reachable no matter what: this is what the silk
# keepout is never allowed to overwrite, only the keepout RING around a pad
# is fair game.  Nothing has been routed yet at this point in the program -
# only keepout reservations exist - so overwriting a ring is free of risk.
CORE_PROTECT = [np.zeros((NY, NX), dtype=bool), np.zeros((NY, NX), dtype=bool)]
for _p in pads:
    _a, _b2, _c, _d = cells_in_rect(_p["x"] - _p["w"] / 2 + 0.4, _p["y"] - _p["h"] / 2 + 0.4,
                                    _p["x"] + _p["w"] / 2 - 0.4, _p["y"] + _p["h"] / 2 - 0.4)
    for _L in ([0, 1] if _p["layer"] == MULTI else [_p["layer"] - 1]):
        CORE_PROTECT[_L][_b2:_d + 1, _a:_c + 1] = True


def stamp_rect_for_silk(layer, x0, y0, x1, y1, netid):
    """Claims every cell in the silk label's box except a pad's protected
    core.  Unlike stamp_rect, this deliberately overwrites (rather than
    contests) any pad keepout RING found there: a component's own future
    trace has no more right to route under its own label than a foreign one
    does, and forcing it out just makes it exit the pad in a slightly
    different direction."""
    a, b, c, d = cells_in_rect(x0, y0, x1, y1)
    for L in ([0, 1] if layer == MULTI else [layer - 1]):
        sub = occ[L][b:d + 1, a:c + 1]
        protect = CORE_PROTECT[L][b:d + 1, a:c + 1]
        sub[~protect] = netid


# Keep top-layer copper out from under every silkscreen label, so "no silk
# over a trace" holds by construction instead of being fixed up label by
# label after the router disagrees with wherever the text landed.  Bottom
# copper is untouched - it's on the other side of the board, so it can't
# visually clash with top silk.
SILK_NET = 2 * 10 ** 6
for _sh in shapes:
    _b = text_bbox(_sh)
    if _b is not None:
        _sm = CLEAR + MAX_W / 2 + GRID
        stamp_rect_for_silk(TOP, _b[0] - _sm, _b[1] - _sm,
                            _b[2] + _sm, _b[3] + _sm, SILK_NET)


def core_cells(p):
    a, b, c, d = cells_in_rect(p["x"] - p["w"] / 2 + 0.4, p["y"] - p["h"] / 2 + 0.4,
                               p["x"] + p["w"] / 2 - 0.4, p["y"] + p["h"] / 2 - 0.4)
    layers = [0, 1] if p["layer"] == MULTI else [p["layer"] - 1]
    return {(L, xx, yy) for L in layers for yy in range(b, d + 1)
            for xx in range(a, c + 1)}


def via_ok(x, y, nid, extra=0.0):
    """A via needs more room than the track that leads to it.

    No relaxed mode, unlike passable(): a via is wider than a track and
    used by everything else that routes near it afterward, so the margin
    it needs is the real one, not the contested heuristic's - a relaxed
    via_ok is what let one retry through with an actual -5 mil (real
    overlap, not a false-positive margin flag) clearance violation before
    this was tightened back up. Trace cells can afford to be optimistic
    and let the geometric verifier be the final word; vias can't, because
    nothing downstream re-checks a via before treating the cells around it
    as claimed.

    Memoised per A* call (VIA_MEMO, cleared in astar()).  This is the
    router's hottest path by a wide margin: it runs once per expanded
    node, does six numpy operations on an 11x11x2 window each time, and
    the same cell gets checked again from every neighbour that reaches it
    - up to four times over.  occ/contested cannot change during a single
    astar() call (stamping happens afterwards, in commit_path), so the
    cache is exactly equivalent to recomputing, not an approximation:
    unlike the expansion cap tried and reverted above, it cannot change
    which board sizes route."""
    key = (x, y, nid, extra)
    hit = VIA_MEMO.get(key)
    if hit is not None:
        return hit
    r = int(math.ceil((VIA_EXTRA + extra) / GRID))
    ok = True
    for L in (0, 1):
        a, b = max(0, x - r), max(0, y - r)
        c, d = min(NX - 1, x + r), min(NY - 1, y + r)
        sub = occ[L][b:d + 1, a:c + 1]
        if np.any(((sub != 0) & (sub != nid)) |
                  contested[L][b:d + 1, a:c + 1]):
            ok = False
            break
    VIA_MEMO[key] = ok
    return ok


def dilate(mask, r_cells):
    """Box-dilate a boolean grid by r_cells in every direction.  A box is a
    safe superset of the disk we actually want, so this stays conservative.
    Shift-and-OR rather than a per-cell scan: O((2r+1)^2) vectorised passes
    over the whole grid, done ONCE per net, instead of a window check redone
    at every single cell A* visits - that per-cell version is what made the
    first attempt at this take minutes instead of seconds."""
    if r_cells <= 0:
        return mask
    out = mask.copy()
    h, w = mask.shape
    for dy in range(-r_cells, r_cells + 1):
        for dx in range(-r_cells, r_cells + 1):
            if dx == 0 and dy == 0:
                continue
            ys, ys_src = slice(max(0, dy), h + min(0, dy)), slice(max(0, -dy), h + min(0, -dy))
            xs, xs_src = slice(max(0, dx), w + min(0, dx)), slice(max(0, -dx), w + min(0, -dx))
            out[ys, xs] |= mask[ys_src, xs_src]
    return out


def passable(L, x, y, nid, blocked_extra=None, relaxed=False):
    """blocked_extra (per layer, precomputed once per net - see main loop)
    widens the check beyond the literal cell.  Pad keepouts near the IC pins
    are deliberately tight (see pad_dilation) so a signal trace can still
    thread between two SOIC pins; a wide net routed nearby must check further
    out itself, since that tight reservation was not sized for it.

    relaxed drops the CONTESTED check: contested marks a still-*empty* cell
    where two different nets' conservative dilated margins merely overlapped,
    not where real copper sits - own_dil already bakes in CLEAR + MAX_W/2 +
    GRID of margin, so a contested cell often still has adequate real
    clearance. Used only for the second-pass retry on nets the strict pass
    couldn't reach; the independent geometric verifier checks exact distances
    on whatever it finds, so a relaxed path is never trusted blind."""
    if not (1 <= x < NX - 1 and 1 <= y < NY - 1):
        return False
    if not relaxed and contested[L][y, x]:
        return False
    v = occ[L][y, x]
    if not (v == 0 or v == nid):
        return False
    if blocked_extra is not None and blocked_extra[L][y, x]:
        return False
    return True


# A failed A* is far more expensive than a successful one: with no path to
# the target it drains the priority queue over the whole reachable grid -
# at GRID=0.25 on a ~400x200 board that is ~2.4M nodes across both layers -
# and it does it again on every relaxed retry round.
#
# This is a SAFETY VALVE, not a tuning knob: it must sit high enough that
# no route which would otherwise succeed ever hits it, or the router
# silently starts depending on it and results change with board size for
# no physical reason.  Both attempts at using it as a speed knob failed
# that test - 300k turned a config with ONE unroutable net into six, and
# even 1.2M (half the grid) still cost real routes.  Congested boards
# genuinely do explore most of the grid before finding a way through, so
# there is no cap that speeds up the hopeless case without also breaking
# the merely-difficult one.  Left effectively disabled: speed comes from
# the via_ok memo below and from running sweeps in parallel, neither of
# which changes a single routing decision.
MAX_EXPAND = int(os.environ.get("MAX_EXPAND", 10 ** 9))


VIA_MEMO = {}


def astar(sources, targets, nid, tgt_xy, extra=0.0, blocked_extra=None, relaxed=False):
    # Safe because occ/contested are only mutated by commit_path, which
    # never runs while a search is in progress - see via_ok's note.
    VIA_MEMO.clear()
    seen, prev = {}, {}
    h = []
    for s in sources:
        if passable(s[0], s[1], s[2], nid, blocked_extra, relaxed):
            key = s
            seen[key] = 0
            heappush(h, (0, key))
    tset = set(targets)
    expanded = 0
    while h:
        expanded += 1
        if expanded > MAX_EXPAND:
            return None
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
            if not passable(nl, nx_, ny_, nid, blocked_extra, relaxed):
                continue
            if nl != L and not via_ok(x, y, nid, extra):
                continue
            ng = g + c
            k = (nl, nx_, ny_)
            if k in seen and seen[k] <= ng:
                continue
            seen[k] = ng
            prev[k] = cur
            # 1.02x on the heuristic (weighted A*, not strict A*): a plain
            # Manhattan heuristic on a mostly-open grid produces huge flat
            # frontiers of equal-f nodes, so the search explores an area
            # closer to O(distance^2) than O(distance) - it degrades toward
            # a blind flood fill exactly where a route has to cross open
            # board rather than hug existing copper.  This is the case that
            # made routing a wider, sparser board (to fit new front-panel
            # controls) take minutes instead of seconds. A 2% inflation
            # trades a usually-unmeasurable amount of extra trace length for
            # a search that actually commits to the goal direction; a valid,
            # DRC-clean route is all that matters here, not a truly shortest
            # one, and the independent post-route verifier checks the real
            # geometry regardless of how the path was found.
            heappush(h, (ng + 1.02 * (abs(nx_ - tgt_xy[0]) + abs(ny_ - tgt_xy[1])), k))
    return None


CHAMFER = 1.5   # 15 mil - 45-degree corner cut length.  Every point stamped
# while routing carries CLEAR + MAX_W/2 + GRID = 1.85 units of margin beyond
# its own half-width; a diagonal cut of length CHAMFER deviates from the
# original right-angle corner by at most CHAMFER/sqrt(2) = 1.06 units, well
# inside that margin, so cutting the corner can never intrude on a
# neighbouring net that routed later and treated the original stamped disc
# as occupied.


def chamfer(pts):
    """Replace each sharp interior 90-degree corner with a short 45-degree
    cut - standard PCB practice (avoids acid traps at etch, reads as
    intentional routing rather than a hand-drawn maze).  Endpoints, which
    land exactly on a pad or via, are left untouched."""
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        (px, py), (cx, cy), (nx_, ny_) = pts[i - 1], pts[i], pts[i + 1]
        d_in = math.hypot(cx - px, cy - py)
        d_out = math.hypot(nx_ - cx, ny_ - cy)
        c = min(CHAMFER, d_in / 2, d_out / 2)
        if c <= 1e-6:
            out.append((cx, cy))
            continue
        out.append((cx - (cx - px) / d_in * c, cy - (cy - py) / d_in * c))
        out.append((cx + (nx_ - cx) / d_out * c, cy + (ny_ - cy) / d_out * c))
    out.append(pts[-1])
    return out


def path_geometry(path):
    """Split a cell path into raw per-layer cell runs, via points, and
    chamfered polylines - no side effects.  Split out from emit_path so the
    relaxed retry pass can inspect a candidate path's real geometry and
    decide whether to commit it, instead of committing on faith."""
    runs, cur = [], [path[0]]
    via_pts = []
    for a, b in zip(path, path[1:]):
        if a[0] != b[0]:
            runs.append(cur)
            via_pts.append((a[1] * GRID, a[2] * GRID))
            cur = [b]
        else:
            cur.append(b)
    runs.append(cur)
    polys = []
    for run in runs:
        if len(run) < 2:
            continue
        pts = [(run[0][1] * GRID, run[0][2] * GRID)]
        for i in range(1, len(run) - 1):
            dx1, dy1 = run[i][1] - run[i - 1][1], run[i][2] - run[i - 1][2]
            dx2, dy2 = run[i + 1][1] - run[i][1], run[i + 1][2] - run[i][2]
            if (dx1, dy1) != (dx2, dy2):
                pts.append((run[i][1] * GRID, run[i][2] * GRID))
        pts.append((run[-1][1] * GRID, run[-1][2] * GRID))
        polys.append((run[0][0] + 1, chamfer(pts)))
    return runs, via_pts, polys


def commit_path(net, nid, runs, via_pts, polys):
    """Stamp occupancy and record the geometry from path_geometry - the
    side-effecting half of what emit_path used to do in one step."""
    own_dil = net_width(net) / 2 + CLEAR + MAX_W / 2 + GRID
    for vx, vy in via_pts:
        VIAS.append((vx, vy, net))
        stamp_disc(MULTI, vx, vy, VIA_DIL, nid)
    for run in runs:
        for c in run:
            stamp_disc(c[0] + 1, c[1] * GRID, c[2] * GRID, own_dil, nid)
    for layer, pts in polys:
        ROUTED.append((layer, pts, net))


def emit_path(path, net, nid):
    """Split a cell path into per-layer polylines, stamping as we go."""
    runs, via_pts, polys = path_geometry(path)
    commit_path(net, nid, runs, via_pts, polys)


def path_clearance_ok(net, via_pts, polys):
    """Exact-geometry pre-commit check for the relaxed retry pass: would
    this candidate path actually violate clearance against any already
    placed copper of a different net?  Only relaxed mode needs this - the
    strict pass never routes through CONTESTED cells in the first place, so
    it can't produce a violation this check would catch; relaxed mode can
    and, once, did (a via at an actual -5 mil overlap, not a false-positive
    CONTESTED flag)."""
    cand = []
    for layer, pts in polys:
        hw = net_width(net) / 2
        for a, b in zip(pts, pts[1:]):
            cand.append(dict(k="seg", hw=hw, L={layer}, g=(a, b)))
    for vx, vy in via_pts:
        cand.append(dict(k="pt", hw=VIA_PAD / 2, L={1, 2}, g=(vx, vy)))
    others = []
    for p in pads:
        if p["net"] and p["net"] != net:
            others.append(dict(k="rect", hw=0.0,
                               L={1, 2} if p["layer"] == MULTI else {p["layer"]},
                               g=(p["x"] - p["w"] / 2, p["y"] - p["h"] / 2,
                                  p["x"] + p["w"] / 2, p["y"] + p["h"] / 2)))
    for layer, pts, name in ROUTED:
        if name == net:
            continue
        hw = net_width(name) / 2
        for a, b in zip(pts, pts[1:]):
            others.append(dict(k="seg", hw=hw, L={layer}, g=(a, b)))
    for x, y, name in VIAS:
        if name == net:
            continue
        others.append(dict(k="pt", hw=VIA_PAD / 2, L={1, 2}, g=(x, y)))
    for c in cand:
        for o in others:
            if c["L"] & o["L"] and gap(c, o) < CLEAR - 1e-9:
                return False
    return True


def route_order(n):
    """Supply rails and long-haul nets first, then IC pins, then everything
    else by size.

    An SOIC pad can only break out sideways, so if the general nets take
    those channels first the op-amp pins are trapped - hence IC-touching
    nets ahead of the rest.  GND/+15V/-15V go even earlier, ahead of that:
    GND alone touches a dozen-plus pads including several IC
    ground-reference pins, and routing it last (its previous position,
    sorted to the end by ascending member count) meant every other IC net
    had already claimed the cells nearest those pins by the time GND got a
    turn - the failure mode that showed up first (U1D.12 or U2D.12, an
    op-amp's grounded '+' input, unreachable) while widening the board for
    the new front-panel pots.  Reversing size order for *every* IC-touching
    net traded that for a new set of casualties among small nets that used
    to route early precisely because they were small (R4.1's net among
    them) - so only the genuinely wide-reaching supply nets jump the queue;
    everything else keeps the original smallest-first order, which is what
    let small point-to-point nets thread through gaps before they closed.

    The *_PRE nets (each filter output to its volume pot) are a second,
    different case that member-count alone can't see: HIGH_PRE/MID_PRE/
    LOW_PRE have only 2 members each - the smallest, lowest-priority nets
    by the general rule - but each one has to cross nearly the full width
    of the board, from the output stage to the front-panel pot row, same as
    a big multi-pad net does for a different reason. Routed last (its
    default position under "smallest first"), a *_PRE net can find every
    cell along that long corridor already claimed by nets that route more
    locally - the actual cause of MID_PRE (VR4.1 to R19.2) reproducibly
    coming back split in two at 420 x 220.

    ATTEMPTED AND REVERTED: generalising this to "any net whose pads span
    more than half the board width", rather than naming LP1 explicitly, is
    tidier and it does subsume the hardcoded case - but it promotes enough
    additional nets to change the whole routing order, which produced MORE
    unroutable nets, and each of those then burns a full grid-exhausting
    relaxed retry, pushing run time past any usable timeout.  The named
    list is uglier and it is what verifies clean.  Revisit only alongside
    a router that can rip up and re-route."""
    members = netdoc["nets"][n]
    if n in ("GND", "+15V", "-15V", "LP1") or n.endswith("_PRE"):
        return (-1, 0)
    return (0 if any(m.startswith("U") for m in members) else 1, len(members))


# Exact-geometry distance helpers, needed here (not just by verify(), far
# below) because the relaxed retry pass checks a candidate path against
# real copper before committing it, rather than trusting relaxed A* to be
# safe on faith - a bare relaxed retry did once produce an actual overlap
# (a via at -5 mil clearance, not just a false-positive CONTESTED flag).
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


PAD_POS = {"%s.%s" % (p["ref"], p["num"]): (p["x"], p["y"]) for p in pads}

FAILED = []
RETRY = []      # strict-pass failures, retried once below with relaxed margins
for name in sorted(netdoc["nets"], key=route_order):
    nid = NETID[name]
    # a wide net can't rely on a foreign pad's keepout being sized for it -
    # that keepout is deliberately tight at the IC pins - so it independently
    # checks a wider neighbourhood while pathfinding.  0 for signal nets keeps
    # them exactly as tight as before, preserving SOIC pin-escape routing.
    # The check is a mask built once per net (cheap - only the 3 power nets
    # need one at all), not recomputed at every cell A* visits.
    route_extra = max(0.0, (net_width(name) - SIG_W) / 2)
    if route_extra > 0:
        r_cells = int(math.ceil(route_extra / GRID))
        blocked_extra = [dilate((occ[L] != 0) & (occ[L] != nid), r_cells)
                         for L in (0, 1)]
    else:
        blocked_extra = None
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
        path = astar(connected, tc, nid, (tx, ty), route_extra, blocked_extra)
        if path is None:
            RETRY.append((name, nid, route_extra, blocked_extra, tgt))
            todo.remove(tgt)
            done.append(tgt)
            continue
        emit_path(path, name, nid)
        connected |= set(path) | tc
        todo.remove(tgt)
        done.append(tgt)

# Second (and third, ...) pass over whatever the strict pass couldn't
# reach.  A miss there often means CONTESTED - not real copper, just two
# nets' conservative dilated margins overlapping over a still-empty cell -
# was the only thing in the way; own_dil already carries CLEAR + MAX_W/2 +
# GRID of real margin, so a contested cell often still has adequate actual
# clearance.  Retried now that the whole board's occupancy is final,
# against every other pad of the same net (not just the ones the first
# pass happened to reach) - and re-tried across a few rounds, since a pad
# that connects on round 1 grows the target for whatever's still isolated
# on round 2 (a straight single retry left one net in two pieces instead
# of five - clear progress, but not yet whole; three rounds cleared it).
# Nothing here is trusted blind: the independent geometric verifier below
# re-measures exact distances on whatever this finds, same as every other
# track on the board.
for _round in range(3):
    if not RETRY:
        break
    still = []
    for name, nid, route_extra, blocked_extra, tgt in RETRY:
        others = [p for p in pads if p["net"] == name and p is not tgt]
        if not others:
            still.append((name, nid, route_extra, blocked_extra, tgt))
            continue
        connected = set()
        for p in others:
            connected |= core_cells(p)
        tc = core_cells(tgt)
        tx, ty = int(round(tgt["x"] / GRID)), int(round(tgt["y"] / GRID))
        path = astar(connected, tc, nid, (tx, ty), route_extra, blocked_extra, relaxed=True)
        if path is None:
            still.append((name, nid, route_extra, blocked_extra, tgt))
            continue
        runs, via_pts, polys = path_geometry(path)
        if path_clearance_ok(name, via_pts, polys):
            commit_path(name, nid, runs, via_pts, polys)
        else:
            still.append((name, nid, route_extra, blocked_extra, tgt))
    RETRY = still
for name, nid, route_extra, blocked_extra, tgt in RETRY:
    FAILED.append("%s: %s.%s unreachable" % (name, tgt["ref"], tgt["num"]))


# ==========================================================================
#  ground stitching vias
# ==========================================================================
# Every through-hole GND pad (terminal blocks, all five pots) already ties
# the top and bottom copper pours together, just by being a plated hole -
# but the SMD-only ground connections (R1/R3/R13's Q-setting/bias returns)
# rely solely on whatever single via the router happened to place while
# routing GND as an ordinary signal.  A deliberate second via right next to
# each one lowers the local ground-plane impedance exactly where the board
# has the least of it already reinforced - most worth doing near R1, the
# input stage's ground reference, where the signal is at its most
# vulnerable to hum pickup before any gain stage.  Added after all routing
# (including retries) is finished, each candidate checked with the same
# strict via_ok() the router itself trusts, so a spot that would actually
# crowd real copper is skipped rather than forced through.
GND_STITCH_REFS = ["R1", "R3", "R13"]
GND_NID = NETID["GND"]
for _ref in GND_STITCH_REFS:
    _p = next((p for p in pads if p["ref"] == _ref and p["net"] == "GND"), None)
    if _p is None:
        continue
    for _dx, _dy in ((3.0, 0), (-3.0, 0), (0, 3.0), (0, -3.0)):
        _vx, _vy = _p["x"] + _dx, _p["y"] + _dy
        _cx, _cy = int(round(_vx / GRID)), int(round(_vy / GRID))
        if via_ok(_cx, _cy, GND_NID):
            VIAS.append((_vx, _vy, "GND"))
            stamp_disc(MULTI, _vx, _vy, VIA_DIL, GND_NID)
            break


# ==========================================================================
#  independent verification - exact geometry, not the router's own bookkeeping
# ==========================================================================
# (d_pt_seg / d_seg_seg / d_pt_rect / d_seg_rect / d_rect_rect / gap moved
# above the routing section - the relaxed retry pass needs them too, to
# check a candidate path for real before committing it, not just hope a
# relaxed pass is safe because it usually is)
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
        FEATURES.append(dict(net=name, k="seg", hw=net_width(name) / 2, L={layer},
                             g=(a, b), tag="track"))
for x, y, name in VIAS:
    FEATURES.append(dict(net=name, k="pt", hw=VIA_PAD / 2, L={1, 2},
                         g=(x, y), tag="via"))


REPORT = []


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
    # -- board utilisation and the largest wasted rectangle.  This is the
    #    "why is there a big empty patch" check, done arithmetically instead
    #    of by squinting at the preview.
    CELL = 5.0
    nx, ny = int(BW / CELL), int(BH / CELL)
    used = np.zeros((ny, nx), dtype=bool)

    def mark(x0, y0, x1, y1):
        used[max(0, int(y0 / CELL)):min(ny, int(y1 / CELL) + 1),
             max(0, int(x0 / CELL)):min(nx, int(x1 / CELL) + 1)] = True

    for b_ in placed:
        mark(*b_)
    for f in FEATURES:
        if f["k"] == "rect":
            mark(*f["g"])
        elif f["k"] == "seg":
            (ax, ay), (bx, by) = f["g"]
            mark(min(ax, bx) - 1, min(ay, by) - 1, max(ax, bx) + 1, max(ay, by) + 1)
        else:
            mark(f["g"][0] - 2, f["g"][1] - 2, f["g"][0] + 2, f["g"][1] + 2)
    for hx, hy in MOUNT_HOLES:
        mark(hx - MOUNT_R, hy - MOUNT_R, hx + MOUNT_R, hy + MOUNT_R)

    # largest all-empty axis-aligned rectangle, by the standard histogram method
    best = (0, 0, 0, 0, 0)
    heights = np.zeros(nx, dtype=int)
    for r in range(ny):
        heights = np.where(used[r], 0, heights + 1)
        stack = []
        for c in range(nx + 1):
            h = heights[c] if c < nx else 0
            start = c
            while stack and stack[-1][1] >= h:
                sc, sh = stack.pop()
                area = sh * (c - sc)
                if area > best[0]:
                    best = (area, sc, r - sh + 1, c, r + 1)
                start = sc
            stack.append((start, h))
    fill = 100.0 * used.sum() / used.size
    wa, wx0, wy0, wx1, wy1 = best
    REPORT.append("board utilisation %.0f%%; largest empty rectangle "
                  "%.0f x %.0f mm at (%.0f, %.0f)"
                  % (fill, (wx1 - wx0) * CELL * 0.254, (wy1 - wy0) * CELL * 0.254,
                     wx0 * CELL * 0.254, wy0 * CELL * 0.254))
    if wa * CELL * CELL > 0.10 * BW * BH:
        problems.append("a single empty rectangle is %.0f%% of the board "
                        "(%.0f x %.0f mm at %.0f,%.0f) - the board can probably "
                        "be smaller or the parts spread better"
                        % (100.0 * wa * CELL * CELL / (BW * BH),
                           (wx1 - wx0) * CELL * 0.254, (wy1 - wy0) * CELL * 0.254,
                           wx0 * CELL * 0.254, wy0 * CELL * 0.254))

    # -- silkscreen: not over exposed pads, not over top-layer copper (traces
    #    or vias - bottom copper is on the other side of the board, so it
    #    can't visually clash with top silk), not off the board.  Covers
    #    plain labels (TEXT~L) and designators (TEXT~P) alike.  This is now a
    #    belt-and-braces check: top-layer copper is kept out from under silk
    #    at routing time (see the SILK_NET keepout, above), so a hit here
    #    means that keepout was bypassed somehow, not the first line of
    #    defence against it.
    silks = [b for b in (text_bbox(sh) for sh in shapes) if b is not None]
    for sx0, sy0, sx1, sy1, txt in silks:
        if sx0 < 1 or sy0 < 1 or sx1 > BW - 1 or sy1 > BH - 1:
            problems.append("silk '%s' runs off the board edge" % txt)
        box = dict(k="rect", hw=0.0, g=(sx0, sy0, sx1, sy1))
        for pd in pads:
            if (sx0 < pd["x"] + pd["w"] / 2 and pd["x"] - pd["w"] / 2 < sx1 and
                    sy0 < pd["y"] + pd["h"] / 2 and pd["y"] - pd["h"] / 2 < sy1):
                problems.append("silk '%s' sits over pad %s.%s"
                                % (txt, pd["ref"], pd["num"]))
                break
        for layer, pts, name in ROUTED:
            if layer != TOP:
                continue
            hit = False
            for a, b in zip(pts, pts[1:]):
                trk = dict(k="seg", hw=net_width(name) / 2, g=(a, b))
                if gap(box, trk) < 0:
                    problems.append("silk '%s' crosses a %s track" % (txt, name))
                    hit = True
                    break
            if hit:
                break
        else:
            for x, y, name in VIAS:
                via = dict(k="pt", hw=VIA_PAD / 2, g=(x, y))
                if gap(box, via) < 0:
                    problems.append("silk '%s' sits over a via (%s)" % (txt, name))
                    break

    # -- copper must not crowd the board edge
    for f in FEATURES:
        g = f["g"]
        xs = ([g[0], g[2]] if f["k"] == "rect" else
              [g[0][0], g[1][0]] if f["k"] == "seg" else [g[0]])
        ys = ([g[1], g[3]] if f["k"] == "rect" else
              [g[0][1], g[1][1]] if f["k"] == "seg" else [g[1]])
        if (min(xs) - f["hw"] < 2 or min(ys) - f["hw"] < 2 or
                max(xs) + f["hw"] > BW - 2 or max(ys) + f["hw"] > BH - 2):
            problems.append("%s (%s) is within 0.5 mm of the board edge"
                            % (f["tag"], f["net"]))
            break

    # -- schematic agreement
    sch = {tuple(m.rpartition(".")[::2]) for mem in netdoc["nets"].values() for m in mem}
    have = {(p["ref"], p["num"]) for p in pads}
    if sch - have:
        problems.append("schematic pins with no pad: %s" % sorted(sch - have))
    if have - sch:
        problems.append("pads with no schematic pin: %s" % sorted(have - sch))

    # -- footprint courtyards must not overlap.  Nothing upstream of this
    #    actually checks that two DIFFERENT components' bodies stay apart -
    #    copper clearance only catches it if their pads happen to collide too.
    #    A rotation or slot change that looks fine electrically can still put
    #    two parts on top of each other visually/mechanically without this.
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if boxes_overlap(a, b):
                problems.append("footprint courtyards overlap: %s / %s"
                                % (tuple(round(v, 1) for v in a),
                                   tuple(round(v, 1) for v in b)))
    return problems


ISSUES = verify()

# SWEEP=1: report the verdict and stop, skipping every artifact write
# (EasyEDA JSON, SVG, the cairosvg PNG render, BOM and CPL).  A board-size
# search only ever reads these three lines, and the files it would write
# are for a size that is about to be thrown away - worse, each sweep run
# would overwrite the real board's committed output files with a candidate
# that may not even verify.
if os.environ.get("SWEEP"):
    print("board %.1f x %.1f mm | %d footprints | %d pads | %d tracks | %d vias"
          % (BW * 0.254, BH * 0.254, len(FP_SPANS), len(pads), len(ROUTED), len(VIAS)))
    for _p in FAILED:
        print("  UNROUTED", _p)
    for _p in ISSUES:
        print("  -", _p)
    raise SystemExit(0)


# ==========================================================================
#  emit copper, outline, pour; wrap footprints as EasyEDA components
# ==========================================================================
for layer, pts, name in ROUTED:
    track(pts, layer, net_width(name), name)
for x, y, name in VIAS:
    shapes.append("VIA~%g~%g~%g~%s~%g~%s" % (x, y, VIA_PAD, name, VIA_DRILL / 2, gid()))

for hx, hy in MOUNT_HOLES:
    shapes.append("HOLE~%g~%g~12.6~%s" % (hx, hy, gid()))
for hx, hy in POT_BOSSES:
    shapes.append("HOLE~%g~%g~9.0~%s" % (hx, hy, gid()))
track([(0, 0), (BW, 0), (BW, BH), (0, BH), (0, 0)], OUTLINE, 0.6)

for L in (TOP, BOT):
    shapes.append("COPPERAREA~%g~%d~GND~%s~1~solid~%s~spoke~none~[]~0~2~1~none"
                  % (PWR_W, L,
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
    "DRCRULE": {"trackWidth": SIG_W, "track2Track": CLEAR, "pad2Pad": CLEAR,
                "track2Pad": CLEAR, "hole2Hole": 1.2, "holeSize": VIA_DRILL,
                "Default": {"trackWidth": SIG_W, "clearance": CLEAR,
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
                    "#d94b3a" if layer == TOP else "#3a6fd9", net_width(name)))
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
for r in REPORT:
    print("  note:", r)
if FAILED:
    print("UNROUTED:", FAILED)
if ISSUES:
    print("DRC PROBLEMS (%d):" % len(ISSUES))
    for p in ISSUES[:20]:
        print("  -", p)
else:
    print("verified: all nets connected, all clearances >= %.0f mil, "
          "no unrouted nets, pads match the schematic exactly" % (CLEAR * 10))
