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

import croute
import place

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

# Silkscreen line width.  0.8 units = 0.203 mm, against JLCPCB's 0.15 mm
# minimum printable width - below that the fab may thin the line or drop it
# entirely.  This was 0.5 units (0.127 mm) for body outlines and 0.4
# (0.102 mm) for the terminal-block wire-entry marks, i.e. every silk line
# on the board was under the minimum, which nothing in the DRC checks
# because it is a fab limit rather than a geometry error.  EasyEDA's own
# footprint for the RK097 pot (LCSC C470577) draws its outline at 1.0 unit;
# 0.7 keeps real margin over the limit while costing less courtyard than
# matching them exactly.
SILK_W = 0.7

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
# Narrowest strip of ground pour that counts as connected copper.  JLCPCB's
# minimum copper width is 0.127 mm; 1 unit (0.254 mm) leaves real margin and
# stops a hairline neck in the model standing in for a connection the fab
# would never make.
POUR_MIN_W = 1.0
# The clearance the emitted COPPERAREA declares (its clearanceWidth field),
# and therefore the gap the importer holds between the pour and everything
# else.  1 unit = 0.254 mm = 10 mil, comfortably above the 8 mil the rest of
# the board is checked to.
POUR_CLEAR = 1.0

# The seed that produced the committed board.  One constant, read once:
# the placement cache key used to read SEED with its own default ("8")
# while the placer read another (11), which was self-consistent only by
# luck - both defaults applied together or not at all.
SEED = int(os.environ.get("SEED", 1))

# Nets carried by the copper pour instead of by traces.  Ground is one:
# the board already had a GND pour on both layers, and routing GND as a
# net as well made it pay for ground twice - with the routed copy going
# FIRST and taking the best channels.  See pour_connectivity(), which is
# what makes relying on the plane safe.
PLANE_NETS = {"GND"}
# Hole edge to hole edge, as the fab's drill needs it - 0.5 mm, in units.
# A drilling limit, not an electrical one, so it applies between two holes
# on the SAME net just as much as between different ones.
MIN_HOLE_GAP = 0.5 / 0.254
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
# "py" forces the reference Python router; anything else uses the compiled
# kernel when it is available.  See astar().
ROUTER = os.environ.get("ROUTER", "c")

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
# placement transform
#
# Every footprint below draws itself in its own local frame, at rot=0, around
# the origin.  This stack is what puts it somewhere else on the board at some
# other angle, so a footprint never has to know where it ends up.
#
# Before this existed, rotation was a per-footprint feature: fp_chip and
# fp_soic14 each open-coded their own two orientations and nothing else could
# rotate at all, which quietly made rotation un-searchable for two thirds of
# the board.  The placement engine needs to be able to turn ANY part, so the
# turning belongs here, once, rather than in each footprint N times.
#
# Text is the one thing that does not rotate: its ANCHOR moves with the part,
# but the glyphs stay upright.  That is deliberate - silkscreen is read by a
# human holding the board one way up, and sideways designators are a
# legibility regression, which is the exact defect DESIG_SIZE/LABEL_SIZE
# already exist to fix.  It also keeps label_bbox()'s width formula valid in
# board coordinates, so the courtyard math stays correct for free.
# --------------------------------------------------------------------------
_XF = [(0.0, 0.0, 0)]                 # (origin x, origin y, quarter turns)


def xf_push(ox, oy, rot):
    _XF.append((ox, oy, int(round(rot / 90.0)) % 4))


def xf_pop():
    _XF.pop()


def xf_rot():
    return _XF[-1][2]


def _xy(x, y):
    """Local footprint coordinates -> board coordinates."""
    ox, oy, q = _XF[-1]
    if q == 1:
        x, y = -y, x
    elif q == 2:
        x, y = -x, -y
    elif q == 3:
        x, y = y, -x
    return x + ox, y + oy


def _wh(w, h):
    """A quarter turn swaps a rectangle's width and height."""
    return (h, w) if _XF[-1][2] % 2 else (w, h)


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
def pad_rect(ref, num, x, y, net, w, h, layer=TOP):
    x, y = _xy(x, y)
    w, h = _wh(w, h)
    pts = "%g %g %g %g %g %g %g %g" % (x - w / 2, y - h / 2, x + w / 2, y - h / 2,
                                       x + w / 2, y + h / 2, x - w / 2, y + h / 2)
    shapes.append("PAD~RECT~%g~%g~%g~%g~%d~%s~%s~0~%s~0~%s~~~Y"
                  % (x, y, w, h, layer, net, num, pts, gid()))
    pads.append(dict(ref=ref, num=str(num), x=x, y=y, w=w, h=h, layer=layer, net=net))


def pad_tht(ref, num, x, y, net, dia=7.0, hole=2.0):
    x, y = _xy(x, y)
    shapes.append("PAD~ELLIPSE~%g~%g~%g~%g~%d~%s~%s~%g~~0~%s~~~Y"
                  % (x, y, dia, dia, MULTI, net, num, hole / 2, gid()))
    pads.append(dict(ref=ref, num=str(num), x=x, y=y, w=dia, h=dia,
                     layer=MULTI, net=net))


def track(points, layer, width, net=""):
    points = [_xy(*p) for p in points]
    shapes.append("TRACK~%g~%d~%s~%s~%s"
                  % (width, layer, net, " ".join("%g %g" % p for p in points), gid()))


def silk_rect(x0, y0, x1, y1, w=SILK_W):
    track([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], TOPSILK, w)


def silk(x, y, s, size=DESIG_SIZE):
    x, y = _xy(x, y)
    shapes.append("TEXT~L~%g~%g~0.6~0~0~%d~~%g~%s~~~%s"
                  % (x, y, TOPSILK, size, s, gid()))


def silk_ref(x, y, s, size=DESIG_SIZE):
    """Designator text - TEXT~P so EasyEDA treats it as the component name."""
    x, y = _xy(x, y)
    shapes.append("TEXT~P~%g~%g~0.6~0~0~%d~~%g~%s~~~%s"
                  % (x, y, TOPSILK, size, s, gid()))


def _dir(dx, dy):
    """Rotate a direction by the current transform, without translating it."""
    ox, oy = _xy(0, 0)
    px, py = _xy(dx, dy)
    return px - ox, py - oy


def silk_ref_beside(box, s, size, local_dir):
    """Put a designator outside `box` (a local-frame rectangle), on the
    local side `local_dir` - but lay the text out in BOARD space.

    Rotating a part rotates its label's anchor while the glyphs stay
    upright, which means the text can end up extending back across the part
    it names.  That is not cosmetic: silkscreen reserves copper here, so a
    label lying over a pin is a pin that cannot escape.  It is exactly how
    the SOIC designator once blocked pins 13/14 and made them unroutable at
    every board size tried - and the rotation transform reintroduced it at
    other angles until this existed (verify() caught 'silk U2 sits over pad
    U2A.1').

    The side is chosen in the LOCAL frame, so a caller can say "keep this
    clear of the pin rows" once and have it stay true at every angle; where
    that lands on the board, and which way the text then has to be aligned
    to grow away from the part rather than over it, is worked out here."""
    lx0, ly0, lx1, ly1 = box
    pts = [_xy(lx0, ly0), _xy(lx1, ly0), _xy(lx1, ly1), _xy(lx0, ly1)]
    bx0, bx1 = min(p[0] for p in pts), max(p[0] for p in pts)
    by0, by1 = min(p[1] for p in pts), max(p[1] for p in pts)
    ox, oy = _dir(*local_dir)
    w, gap = 0.62 * size * len(s), 2.0
    if ox > 0.5:
        ax, ay = bx1 + gap, (by0 + by1) / 2 + 0.4 * size
    elif ox < -0.5:
        ax, ay = bx0 - gap - w, (by0 + by1) / 2 + 0.4 * size
    elif oy < -0.5:
        ax, ay = bx0, by0 - gap
    else:
        ax, ay = bx0, by1 + gap + size
    xf_push(0.0, 0.0, 0)
    try:
        silk_ref(ax, ay, s, size)
    finally:
        xf_pop()


def label_bbox(ax, ay, text, size=DESIG_SIZE):
    """Matches text_bbox()'s geometry exactly (same magic numbers), so
    courtyard math computed before a label is drawn agrees with the label
    once it actually exists."""
    w_ = 0.62 * size * len(text)
    return (ax, ay - size, ax + w_, ay + 0.25 * size)


def text_bbox(sh):
    """Bounding box of a top-silk TEXT shape, or None.  Shared by the
    courtyard probe, the pre-routing keepout and the post-hoc check in
    verify(), so none of them can drift into disagreeing about what counts
    as an overlap."""
    f = sh.split("~")
    if f[0] != "TEXT" or f[7] != "3":
        return None
    w_ = 0.62 * float(f[9]) * len(f[10])
    return (float(f[2]), float(f[3]) - float(f[9]),
            float(f[2]) + w_, float(f[3]) + 0.25 * float(f[9]), f[10])


def shape_box(sh):
    """(kind, x0, y0, x1, y1) for one emitted shape, or None if it has no
    footprint on the board.  Reading the geometry back off the shapes a
    footprint actually emitted - rather than re-deriving it from the same
    constants a second time - is what stops the courtyard model and the
    drawn part from ever disagreeing.  A hand-maintained second copy is
    exactly how the dual-gang pot came to be drawn with its body hanging
    off the board edge while every check thought it fit."""
    f = sh.split("~")
    if f[0] == "PAD":
        x, y, w, h = (float(f[2]), float(f[3]), float(f[4]), float(f[5]))
        return ("pad", x - w / 2, y - h / 2, x + w / 2, y + h / 2)
    if f[0] == "TRACK":
        if int(f[2]) != TOPSILK:
            return None
        hw = float(f[1]) / 2
        cs = [float(v) for v in f[4].split()]
        xs, ys = cs[0::2], cs[1::2]
        return ("body", min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw)
    b = text_bbox(sh)
    return None if b is None else ("body", b[0], b[1], b[2], b[3])


FP_SPANS = []          # (ref, first shape index, last+1, x, y)


def fp_begin():
    return len(shapes)


def fp_end(ref, mark, x, y):
    FP_SPANS.append((ref, mark, len(shapes)) + _xy(x, y))


def part_record(ref, value, x, y, assembled, pkg):
    """BOM/CPL entry for a footprint, in board coordinates.  The anchor and
    the angle both come from the live placement transform rather than from
    the footprint's own arguments, so a part cannot be drawn at one place
    and reported at another."""
    px, py = _xy(x, y)
    PARTS[ref] = dict(value=value, x=px, y=py, rot=xf_rot() * 90,
                      assembled=assembled, pkg=pkg)


# --------------------------------------------------------------------------
# footprints - all dimensions chosen so pad centres sit on the 0.5 grid
# --------------------------------------------------------------------------
CHIP_GEOM = {"0805": (4.0, 5.6, 4.0), "1210": (5.0, 10.0, 6.0)}


def fp_chip(ref, x, y, net_of, value, kind="0805"):
    """Two-terminal chip part, drawn once in its own frame: pads left and
    right, designator underneath.  There is no `rot` argument any more -
    the placement transform turns it, so the sideways case is the same
    drawing seen from another angle rather than a second hand-written
    layout that has to be kept in step with the first."""
    _m = fp_begin()
    w, h, off = CHIP_GEOM[kind]
    for i, dx in enumerate((-off, off)):
        pad_rect(ref, i + 1, x + dx, y, net_of(ref, i + 1), w, h)
    # Below the body, not beside it: the pads escape along +/-X, so keeping
    # the label out of those two corridors is what matters (same rule that
    # fixed the SOIC label - see fp_soic14).
    silk_ref(x - 4, y - h / 2 - 1.5, ref)
    fp_end(ref, _m, x, y)
    part_record(ref, value, x, y, True, kind)


def fp_soic14(pkg_ref, sections, x, y, net_of):
    """SOIC-14, pins down both sides, drawn ONCE and centred on its own
    anchor.  Which way the pins escape is now the placement transform's
    business, not this function's.

    Two long-standing bugs died with the old two-branch version:

    * The rot=90 branch laid its pads out from `x` rightward, so the
      package centre landed at x+15 while the caller thought it was
      passing a centre - every "centred" SOIC was silently 15 units off.
      Here the pads straddle the anchor at every angle, by construction.
    * The designator has to stay out of the pin-escape corridors, and the
      old code had to re-derive where those were per branch (getting it
      wrong once: the label sat directly over pins 13/14's only channel,
      which made U1D.13 / U2D.13 / U2D.14 unroutable at *every* board size
      tried, for what looked for a long time like a space problem).  In
      the local frame the pins always escape along +/-X, so "above the
      body" is always clear - and rotation carries that guarantee round
      with the part.
    """
    _m = fp_begin()
    pitch, row, pw, ph = 5.0, 10.5, 6.0, 2.5
    ref = pkg_ref.split()[0]
    for i in range(7):
        pad_rect(sections[i + 1], i + 1, x - row, y + (i - 3) * pitch,
                 net_of(sections[i + 1], i + 1), pw, ph)
        n = 8 + i
        pad_rect(sections[n], n, x + row, y + (3 - i) * pitch,
                 net_of(sections[n], n), pw, ph)
    silk_rect(x - 7.5, y - 19, x + 7.5, y + 19)
    track([(x - 7.5, y - 19), (x - 4, y - 19)], TOPSILK, SILK_W)  # pin 1 corner
    # Off the END of the package, never off its sides: the pins escape
    # along local +/-X at every angle, so this is the one direction that is
    # guaranteed not to be somebody's only way out.
    silk_ref_beside((x - row - pw / 2, y - 19, x + row + pw / 2, y + 19),
                    ref, LABEL_SIZE, (0, -1))
    fp_end(ref, _m, x, y)
    part_record(pkg_ref, "MC33079", x, y, True, "SOIC-14")


def fp_elec(ref, x, y, net_of, value):
    _m = fp_begin()
    for i, dx in enumerate((-8.5, 8.5)):
        pad_rect(ref, i + 1, x + dx, y, net_of(ref, i + 1), 8.0, 10.0)
    n, r = 20, 10.0
    track([(x + r * math.cos(2 * math.pi * i / n),
            y + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)],
          TOPSILK, SILK_W)
    silk_ref_beside((x - 12.5, y - r, x + 12.5, y + r), ref, DESIG_SIZE, (0, -1))
    silk(x - 15.5, y + 1, "+")
    fp_end(ref, _m, x, y)
    part_record(ref, value, x, y, True, "CASE-D5xL5.4")


def fp_pads(ref, x, y, net_of, n=3, label="", horiz=False, names=None):
    _m = fp_begin()
    for i in range(n):
        px, py = (x + i * 10.0, y) if horiz else (x, y + i * 10.0)
        pad_tht(ref, i + 1, px, py, net_of(ref, i + 1))
        if names and i < len(names):
            lax = px - 0.31 * LABEL_SIZE * len(names[i])   # centred, per text_bbox's own width formula
            lay = py + 9.5 if horiz else py + 1.6
            silk(lax, lay, names[i], LABEL_SIZE)
    if horiz:
        silk_rect(x - 5, y - 5, x + (n - 1) * 10 + 5, y + 5)
        rlax, rlay = x - 5, y - 7.5
    else:
        silk_rect(x - 5, y - 5, x + 5, y + (n - 1) * 10 + 5)
        rlax, rlay = x + 6.5, y - 2
    silk_ref(rlax, rlay, ref, LABEL_SIZE)
    fp_end(ref, _m, x, y)
    part_record(ref, label or ref, x, y, False, "THT-PAD")


def fp_term(ref, x, y, net_of, n=2, names=None):
    """Screw terminal block, 3.5 mm pitch (14 units = 3.556 mm; the 1.1 mm
    holes absorb the difference).  Wire entry faces the board edge."""
    _m = fp_begin()
    # Centred on the anchor, so turning the block to face a different board
    # edge pivots it about its own middle instead of swinging it away by
    # its own length.
    x0 = x - (n - 1) * 7.0
    for i in range(n):
        px = x0 + i * 14.0
        pad_tht(ref, i + 1, px, y, net_of(ref, i + 1), dia=8.0, hole=4.3)
        if names and i < len(names):
            silk(px - 0.31 * LABEL_SIZE * len(names[i]), y + 15,
                 names[i], LABEL_SIZE)
    x1 = x0 + (n - 1) * 14.0
    silk_rect(x0 - 7, y - 16, x1 + 7, y + 10)
    for i in range(n):                        # wire-entry throats, facing -Y
        silk_rect(x0 + i * 14.0 - 4, y - 14, x0 + i * 14.0 + 4, y - 8)
    # Behind the block, away from the wire entry, so the designator is
    # still readable with wires landed in it.
    silk_ref_beside((x0 - 7, y - 16, x1 + 7, y + 10), ref, LABEL_SIZE, (0, 1))
    fp_end(ref, _m, x, y)
    part_record(ref, "TB-%dP-3.5" % n, x, y, False, "TB-%dP-3.5" % n)


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
    flay = by0 - 3
    ftext = "HIGH/MID" if pkg_ref == "VR1" else "MID/LOW"
    silk(bx0, flay, ftext, LABEL_SIZE)
    rng = RANGES.get(pkg_ref)
    if rng:
        # Its own row, at a smaller size, deliberately narrower than the
        # 52-unit body.  Appending it to the function label instead was
        # tried and cost 15 mm of board height: the footprint's bounding
        # box is PROBED from the shapes it emits, silk included, so a label
        # wider than the part makes the part wider, and five of them on a
        # fixed panel pitch then shove the whole row apart.  Silkscreen is
        # not decoration here in more ways than one.
        silk(bx0, flay - 7, rng, LABEL_SIZE - 1.4)
        silk_ref(bx0, flay - 15, pkg_ref, LABEL_SIZE)
    else:
        silk_ref(bx0, flay - 8, pkg_ref, LABEL_SIZE)
    fp_end(pkg_ref, _m, x, y + 10)
    part_record(pkg_ref, "20k dual", x, y + 10, False, "POT-9MM-DUAL")


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
    flay = by0 - 3
    silk(bx0, flay, label, LABEL_SIZE)
    silk_ref(bx0, flay - 8, ref, LABEL_SIZE)
    fp_end(ref, _m, x, y)
    part_record(ref, "10k log", x, y, False, "POT-9MM-SINGLE")


POT_BOSSES = []          # non-plated locating holes, filled in at placement


# --------------------------------------------------------------------------
# footprint probing
#
# The placement search needs to know how big each part is and where its pads
# sit, at every angle it might be turned to - before anything is committed to
# the board.  Rather than write that down a second time (a second copy is how
# the pot body ended up hanging over the board edge while every check thought
# it fit), each footprint is DRAWN into the live buffers at the origin, the
# geometry is read back off the shapes it actually emitted, and the buffers
# are wound back.  The model is therefore the drawing, by construction.
# --------------------------------------------------------------------------
def probe(draw, rot):
    """Run `draw` at the origin, rotated by `rot`, and return what it drew
    without leaving any of it behind."""
    m_sh, m_pd, m_sp = len(shapes), len(pads), len(FP_SPANS)
    known = set(PARTS)
    xf_push(0.0, 0.0, rot)
    try:
        draw()
    finally:
        xf_pop()
    my_shapes, my_pads = shapes[m_sh:], [dict(p) for p in pads[m_pd:]]
    boxes = [b for b in (shape_box(s) for s in my_shapes) if b]
    del shapes[m_sh:]
    del pads[m_pd:]
    del FP_SPANS[m_sp:]
    for k in set(PARTS) - known:
        del PARTS[k]

    body = [b[1:] for b in boxes if b[0] == "body"]
    x0 = min(b[1] for b in boxes) - CLEAR
    y0 = min(b[2] for b in boxes) - CLEAR
    x1 = max(b[3] for b in boxes) + CLEAR
    y1 = max(b[4] for b in boxes) + CLEAR
    return dict(box=(x0, y0, x1, y1), body=body,
                pads=[(p["ref"], p["num"], p["net"], p["x"], p["y"],
                       p["w"], p["h"]) for p in my_pads])


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


def _compact_range(text):
    """'195 Hz - 1.03 kHz' -> '195Hz-1.03kHz'.  Silkscreen space is
    routing space, so the spaces come out."""
    return (text.split("(")[0].strip()
            .replace(" Hz", "Hz").replace(" kHz", "kHz").replace(" - ", "-"))


# What each frequency pot actually sweeps, taken from the netlist rather
# than written out again here.  A knob marked only "HIGH/MID" tells you
# which crossover it is and nothing about where you can put it; the range
# is the part a person setting the board up needs.  Reading it from the
# netlist means a retune cannot leave the silkscreen lying.
RANGES = {k: _compact_range(v) for k, v in netdoc.get("ranges", {}).items()}

# ==========================================================================
#  layout
#
#  BOARD SIZE IS AN OUTPUT OF THIS SECTION, NOT AN INPUT TO IT.
#
#  For most of this project's history it was the other way round: BOARD_W and
#  BOARD_H were constants, everything else was positioned inside them, and
#  when a net would not route the board was made bigger until it did.  That
#  procedure cannot fix a layout problem - it can only find a size large
#  enough to hide one - and every time it was reached for here the real cause
#  turned out to be structural: pot spacing, route order, a long-haul net, a
#  silkscreen label parked in a pin-escape corridor.  See CLAUDE.md.
#
#  So: arrange the parts on open canvas, optimising for the things that
#  actually decide whether a board routes, then draw the outline around the
#  answer.  tools/place.py has the search; this section says what the parts
#  are, which of them may turn, and which are pinned by something real.
# ==========================================================================
U1S = {1: "U1A", 2: "U1A", 3: "U1A", 4: "U1A", 5: "U1B", 6: "U1B", 7: "U1B",
       8: "U1C", 9: "U1C", 10: "U1C", 11: "U1A", 12: "U1D", 13: "U1D", 14: "U1D"}
U2S = {1: "U2A", 2: "U2A", 3: "U2A", 4: "U2A", 5: "U2B", 6: "U2B", 7: "U2B",
       8: "U2C", 9: "U2C", 10: "U2C", 11: "U2A", 12: "U2D", 13: "U2D", 14: "U2D"}
# U3 is the output-buffer quad.  Its supply pins belong to section D, not
# section A: D is the spare, and it is the one drawn with room around it on
# the schematic sheet for the rail labels.
U3S = {1: "U3A", 2: "U3A", 3: "U3A", 4: "U3D", 5: "U3B", 6: "U3B", 7: "U3B",
       8: "U3C", 9: "U3C", 10: "U3C", 11: "U3D", 12: "U3D", 13: "U3D",
       14: "U3D"}
SOIC_SECTIONS = {"U1": U1S, "U2": U2S, "U3": U3S}

TERMS = {
    "J2": dict(n=2, names=["IN", "GND"]),
    "J1": dict(n=3, names=["+15", "GND", "-15"]),
    "J5": dict(n=2, names=["LOW", "GND"]),
    "J4": dict(n=2, names=["MID", "GND"]),
    "J3": dict(n=2, names=["HI", "GND"]),
}

# Front panel, left to right: LOW ... HIGH, each band's volume trim beside
# the frequency pot it belongs to.  Order is a stated preference; the row
# being one uniformly-pitched line along one edge is a mechanical
# requirement (it is the panel), so these five are a rigid group as far as
# the search is concerned and only move together.
PANEL = ["VR5", "VR2", "VR4", "VR1", "VR3"]
# The three output terminals are a row of their own, in the same LOW -> HIGH
# order as the panel, so each output block sits above the volume pot that
# feeds it.  Left free, they scattered to three different board edges -
# which costs nothing the optimiser can measure and is horrible to wire.
OUTPUTS = ["J5", "J4", "J3"]
OUTPUT_PITCH = 56.0
POT_GANGS = {"VR1": ("VR1A", "VR1B"), "VR2": ("VR2A", "VR2B")}
POT_LABEL = {"VR3": "HIGH VOL", "VR4": "MID VOL", "VR5": "LOW VOL"}
# 80 units = 20.3 mm between knob centres.  This is a human-factors number,
# not a routing one: a knob for a 6 mm shaft is typically 15-20 mm across,
# so anything much under 20 mm pitch has adjacent knobs touching and the
# outer ones unusable.  The pot COURTYARDS would allow ~58 units (14.7 mm),
# which is why this cannot be left to the optimiser to discover - it has no
# model of the thing that actually sets the limit, which is fingers.
PANEL_PITCH = float(os.environ.get("PANEL_PITCH", 80))

# Three short lines, not two long ones.  The title is a real keepout (silk
# reserves top copper), so a 130-unit-wide bar is a bar across whatever part
# of the board it lands on - the placement search, having no wirelength
# reason to put it anywhere in particular, parked the two-line version
# straight across the middle.  Squarer text tucks into a corner instead.
TITLE_LINES = ["ESP P148 3-WAY VARIABLE CROSSOVER",
               "RETUNED QUAD SMD - ONE CHANNEL",
               "%s / %s" % (RANGES.get("VR1", ""), RANGES.get("VR2", ""))]

# Electrolytics are identified by VALUE, not by designator.  C0 used to be
# the only one and was named explicitly; the board now also has bulk supply
# decoupling and per-output DC blocking, all the same 10 uF part, and a
# hardcoded "C0" would have silently placed those as 0805 chip caps.
# A LIST, sorted - not a set.  This is iterated below to assign rotations,
# and a set of strings iterates in an order that changes with Python's
# per-process hash seed.  That changed the order parts were added to ROTS,
# which changed their indices, which changed the sequence of random moves
# the annealer made: the same SEED gave a different board on about one run
# in three.  A search is worthless if its results do not reproduce.
ELECTRO = sorted(r for r in VALUE if VALUE[r] == "10uF")
CHIPS = {r: ("1210" if VALUE[r] == "33nF" else "0805")
         for r in VALUE if r.startswith("R") or
         (r.startswith("C") and r not in ELECTRO)}

# Board-edge clearance for parts, and how far in from the corner each
# mounting hole sits.
EDGE = 5.0
MOUNT_R = 12.6 / 2
BOSS_R = 4.5
MOUNT_INSET = 9.0
MOUNT_KEEP = MOUNT_R + CLEAR + MAX_W / 2


def boxes_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def draw_title():
    for _k, _line in enumerate(TITLE_LINES):
        silk(0, 5.5 * _k, _line)


def drawer(ref):
    """A zero-argument callable that draws `ref` at the local origin.  The
    placement transform is what decides where 'the origin' actually is, so
    nothing below needs to know its own position."""
    if ref in SOIC_SECTIONS:
        return lambda: fp_soic14(ref, SOIC_SECTIONS[ref], 0, 0, N)
    if ref in TERMS:
        return lambda: fp_term(ref, 0, 0, N, **TERMS[ref])
    if ref in ELECTRO:
        return lambda: fp_elec(ref, 0, 0, N, VALUE[ref])
    if ref in ("TP1", "TP2"):
        return lambda: fp_pads(ref, 0, 0, N, n=1, label=ref)
    if ref in POT_GANGS:
        return lambda: fp_pot(ref, POT_GANGS[ref][0], POT_GANGS[ref][1], 0, 0, N)
    if ref in POT_LABEL:
        return lambda: fp_pot_single(ref, 0, 0, N, POT_LABEL[ref])
    if ref == TITLE_REF:
        return draw_title
    return lambda: fp_chip(ref, 0, 0, N, VALUE[ref], CHIPS[ref])


# The title block is a part.  It occupies board area, it has to not sit on
# top of anything, and - like every other piece of silkscreen here - a
# trace cannot run under it (see the silk keepout in the routing section).
# Treating it as furniture that gets dropped in afterwards is how it ended
# up overlapping the pot outlines on the previous board.
TITLE_REF = "#TITLE"

ROTS = {}
for _r in SOIC_SECTIONS:
    ROTS[_r] = (0, 90, 180, 270)      # which way the 14 pins escape
for _r in TERMS:
    ROTS[_r] = (0, 90, 180, 270)      # which board edge the wires enter from
for _r in ELECTRO:
    ROTS[_r] = (0, 90)
ROTS["TP1"] = ROTS["TP2"] = (0,)      # single round pad - rotation is a no-op
ROTS[TITLE_REF] = (0,)
for _r in PANEL:
    ROTS[_r] = (0,)                   # shafts must all face the same way
for _r in CHIPS:
    ROTS[_r] = (0, 90)

GEOM = {ref: {rot: probe(drawer(ref), rot) for rot in rots}
        for ref, rots in ROTS.items()}

POT_W = max(GEOM[r][0]["box"][2] - GEOM[r][0]["box"][0] for r in PANEL)
PANEL_SPAN = 4 * PANEL_PITCH + POT_W

_parts = []
for _ref, _rots in ROTS.items():
    _parts.append(place.Part(
        _ref, GEOM[_ref], _rots,
        group=("panel" if _ref in PANEL else
               "outputs" if _ref in OUTPUTS else None),
        # Screw terminals take wire from off-board, so their entry side has
        # to face out; the panel row has to be the panel.  Both are real
        # mechanical constraints, and both are expressed the same way: name
        # the local-frame direction that must stay clear all the way to the
        # board edge, and let rotation carry it round with the part.
        outward=(0, 1) if _ref in PANEL else ((0, -1) if _ref in TERMS else None)))

# Each supply-bypass capacitor belongs to ONE op-amp's supply pin and has to
# sit next to it: the whole job of a 100 nF bypass is to supply transient
# current through as little loop inductance as possible, and a centimetre of
# trace is already ~10 nH.  Nothing else in the cost function can see this -
# a bypass cap's two nets (+15V/-15V and GND) span the board anyway, so its
# wirelength barely changes wherever it goes - and the search duly scattered
# all four of them 37-63 mm from the pins they are supposed to be
# decoupling, which is no decoupling at all.  The hand-placed through-hole
# board gets them to 11 mm by eye; 40 units is 10.2 mm, pin to pin.
# ON.  This was off for one round, because with it on nothing routed across
# 208 seed/route-order combinations - not because the constraint was wrong
# but because each attempt cost ~30 s and 208 of them was as far as the
# budget went.  Compiling the placement anneal took a trial to ~2.5 s, and
# the very next search found a clean board in 80 trials (SEED=11), which is
# a fair summary of what the performance work was actually for.
BYPASS_NEAR = [("C5", "U1", "+15V", 40.0), ("C6", "U1", "-15V", 40.0),
               ("C7", "U2", "+15V", 40.0), ("C8", "U2", "-15V", 40.0),
               ("C9", "U3", "+15V", 40.0), ("C10", "U3", "-15V", 40.0)]

PLACER = place.Placer(_parts, seed=SEED,
                      track_pitch=MAX_W + CLEAR, near=BYPASS_NEAR,
                      plane_nets=PLANE_NETS)

# The panel row: fixed pitch, fixed order, all on one line.
for _k, _ref in enumerate(PANEL):
    _i = PLACER.idx[_ref]
    PLACER.set_pose(_i, _k * PANEL_PITCH, 0.0, 0)

# Weights are in cost-units per unit of whatever they measure, so what
# matters is their ratios.  Calibrated so that one fully starved pin-escape
# side (a SOIC face with seven pins and nothing but a neighbour's courtyard
# in front of them - worth ~92 before weighting) costs about as much as 60
# units of extra board height.  That is the trade this project kept getting
# wrong by hand in the other direction: it would take the shorter board and
# then spend days discovering the nets no longer route.
WEIGHTS = dict(
    ov=60.0,         # ramped hard by the anneal - see place.anneal()
    esc=40.0,        # pin-escape starvation: the failure this board keeps hitting
    cong=5.0,        # RUDY overflow
    hpwl=0.15,       # wirelength, as a proxy for everything not modelled
    # The size pressure is an env knob because it is the one weight worth
    # re-tuning: the others price routability, which now gets CHECKED by
    # actually routing, so this is the only one still trading against a
    # thing the search cannot see for itself.
    h=float(os.environ.get("W_H", 60)),
    w=120.0,         # width past the panel floor is pure waste - push hard
    wfloor=PANEL_SPAN,
    edge=40.0,       # terminals/panel must have a clear path to their edge
    near=25.0,       # bypass caps must reach their own op-amp's supply pin
    t0=150.0, t1=0.5,
)
# 25000, and like RESTARTS this is not a "more is better" knob.  The
# anneal optimises a SURROGATE for routability; past a point, optimising it
# harder just fits the surrogate more closely, and the surrogate and the
# router disagree.  Measured: 90000 moves produced boards the cost function
# liked and the router did not (0 of 28 seeds verified clean), where 25000
# leaves layouts that route.  Both times this has been checked - here and
# for RESTARTS - the more heavily optimised placement was the worse board.
MOVES = int(os.environ.get("MOVES", 25000))

PLACE_LOG = int(os.environ["PLACE_LOG"]) if "PLACE_LOG" in os.environ else None

# Placement is deterministic in its inputs, and route-order search re-runs
# the generator many times over the SAME placement with a different
# ROUTE_SEED - so without this, a sweep of 8 net orders paid for 8
# identical anneals (~25 s each) to route 8 times (~8 s each).  The cache
# key covers everything the placement depends on, including the probed
# footprint geometry, so editing a footprint or a weight invalidates it
# rather than silently reusing a stale layout.
PLACE_CACHE = os.path.join(ROOT, ".place-cache")


def _place_key():
    import hashlib
    h = hashlib.sha256()
    h.update(repr(sorted((r, sorted(g.items())) for r, g in
                         ((r, {k: v["box"] for k, v in gg.items()})
                          for r, gg in sorted(GEOM.items())))).encode())
    h.update(repr(sorted(netdoc["nets"].items())).encode())
    h.update(repr(sorted((k, v) for k, v in WEIGHTS.items())).encode())
    # BYPASS_NEAR belongs in the key: it changes the placement, so without
    # it here, switching the constraint on silently reuses a layout
    # computed without it.
    h.update(repr((MOVES, RESTARTS, PANEL_PITCH, OUTPUT_PITCH, EDGE,
                   MOUNT_INSET, MOUNT_KEEP, SEED,
                   os.environ.get("ESC_CAP"), os.environ.get("ESC_FLOOR"),
                   os.environ.get("SUPPLY"), BYPASS_NEAR,
                   sorted(PLANE_NETS),
                   os.environ.get("PLACER"))).encode())
    return h.hexdigest()[:32]


def _cached_pose():
    if os.environ.get("NO_PLACE_CACHE"):
        return None
    f = os.path.join(PLACE_CACHE, _place_key() + ".json")
    if not os.path.exists(f):
        return None
    try:
        d = json.load(open(f))
    except ValueError:
        return None
    return ({k: tuple(v) for k, v in d["pose"].items()}, d["before"], d["after"])


def _save_pose(pose, before, after):
    if os.environ.get("NO_PLACE_CACHE"):
        return
    try:
        os.makedirs(PLACE_CACHE, exist_ok=True)
        json.dump(dict(pose={k: list(v) for k, v in pose.items()},
                       before=before, after=after),
                  open(os.path.join(PLACE_CACHE, _place_key() + ".json"), "w"))
    except OSError:
        pass
# 2, and this is NOT a "more is better" knob.  The restart loop keeps the
# lowest-COST placement, and cost is a surrogate for routability, not a
# measurement of it: raising this to 4 found a placement the surrogate
# scored better and the router did visibly worse on (7 DRC problems where
# best-of-2 had none).  Picking a board is done by routing candidates -
# see tools/find_board.py - not by making this number bigger.
RESTARTS = int(os.environ.get("RESTARTS", 2))


def run_placement(rng_seed):
    """One full placement attempt.

    Pass 1 sizes the board without knowing where the mounting holes will
    be - they are at the corners of an outline that does not exist yet.
    Pass 2 reserves those corners and re-runs from the pass-1 answer.  Two
    cheap passes rather than one, because the alternative (a keepout
    anchored to a board whose size is still moving) makes every incremental
    cost in the search subtly wrong."""
    PLACER.rng.seed(rng_seed)
    PLACER.fixed_boxes = []
    for _i, _ref in enumerate(PANEL):
        PLACER.set_pose(PLACER.idx[_ref], _i * PANEL_PITCH, 0.0, 0)
    for _i, _ref in enumerate(OUTPUTS):
        PLACER.set_pose(PLACER.idx[_ref],
                        PANEL_SPAN / 2 - OUTPUT_PITCH + _i * OUTPUT_PITCH, -170.0, 0)
    PLACER.seed()
    PLACER.anneal(moves=MOVES // 3, w=WEIGHTS, report=PLACE_LOG)
    x0, y0, x1, y1 = PLACER.extent()
    PLACER.fixed_boxes = [
        (cx - MOUNT_KEEP, cy - MOUNT_KEEP, cx + MOUNT_KEEP, cy + MOUNT_KEEP)
        for cx in (x0 - EDGE + MOUNT_INSET, x1 + EDGE - MOUNT_INSET)
        for cy in (y0 - EDGE + MOUNT_INSET, y1 + EDGE - MOUNT_INSET)]
    return PLACER.anneal(moves=MOVES, w=WEIGHTS, report=PLACE_LOG)


PLACER.seed()
PLACER.full_cost(WEIGHTS)
BEFORE = float(PLACER.net_hpwl.sum())
_HIT = _cached_pose()

# The anneal lands in a different local minimum from each start, and the
# spread between them is worth more than the same time spent on a longer
# single run.  Restarts are seconds; routing is minutes - so it is much
# cheaper to search placement properly and route once than to route a
# mediocre placement and go looking for a board size that rescues it.
_best = (float("inf"), None, None)
for _try in range(0 if _HIT else RESTARTS):
    _cost = run_placement(SEED + 1000 * _try)
    _x0, _y0, _x1, _y1 = PLACER.extent()
    print("  placement %d/%d: cost %.0f, %.1f x %.1f mm"
          % (_try + 1, RESTARTS, _cost, (_x1 - _x0 + 2 * EDGE) * 0.254,
             (_y1 - _y0 + 2 * EDGE) * 0.254), flush=True)
    if _cost < _best[0]:
        _best = (_cost, PLACER.snapshot(), float(PLACER.net_hpwl.sum()))
if _HIT:
    POSE, BEFORE, AFTER = _HIT
    print("  placement: reusing the cached layout for these inputs")
else:
    PLACER.restore(_best[1])
    PLACER.full_cost(WEIGHTS)
    AFTER = _best[2]
    POSE = PLACER.result()
    _save_pose(POSE, BEFORE, AFTER)
_bx1 = max(POSE[r][0] + GEOM[r][POSE[r][2]]["box"][2] for r in POSE)
_by1 = max(POSE[r][1] + GEOM[r][POSE[r][2]]["box"][3] for r in POSE)

# THE BOARD SIZE, at last: whatever the arrangement turned out to need, plus
# an edge margin, rounded up to a whole unit so the outline lands on the
# routing grid.
BW = math.ceil(_bx1 + 2 * EDGE)
BH = math.ceil(_by1 + 2 * EDGE)
MOUNT_HOLES = [(MOUNT_INSET, MOUNT_INSET), (BW - MOUNT_INSET, MOUNT_INSET),
               (MOUNT_INSET, BH - MOUNT_INSET), (BW - MOUNT_INSET, BH - MOUNT_INSET)]

# ---- commit the placement: draw every part where the search put it -------
for _ref in sorted(POSE):
    _x, _y, _rot = POSE[_ref]
    xf_push(_x + EDGE, _y + EDGE, _rot)
    try:
        drawer(_ref)()
    finally:
        xf_pop()
    _b = GEOM[_ref][_rot]["box"]
    placed.append((_x + EDGE + _b[0], _y + EDGE + _b[1],
                   _x + EDGE + _b[2], _y + EDGE + _b[3]))

for _ref in POT_GANGS:
    _x, _y, _rot = POSE[_ref]
    # Locating-boss holes, measured clear of every pad by verify()'s own
    # geometry rather than nested inside the drawn body outline - a real
    # boss position for this part is not known (no verified dual-gang
    # datasheet), so agreeing with the pads beats agreeing with a guess.
    POT_BOSSES.extend([(_x + EDGE - 14, _y + EDGE - 22),
                       (_x + EDGE + 14, _y + EDGE - 22)])


# ==========================================================================
#  routing
# ==========================================================================
NX, NY = int(BW / GRID), int(BH / GRID)
# One contiguous (2, NY, NX) block with per-layer VIEWS into it, rather
# than two independent arrays.  The views alias the same memory, so
# occ[L][y, x] = v still works exactly as before, but the compiled router
# can be handed OCC directly instead of np.stack()-ing the two layers on
# every single net - which at this grid size was copying ~10 MB per search.
OCC = np.zeros((2, NY, NX), dtype=np.int32)
occ = [OCC[0], OCC[1]]
# A cell can fall inside the keep-out of more than one net.  Recording only the
# first claimant would let the second net route there, so contested cells are
# flagged separately and are passable to nobody.
CONTESTED = np.zeros((2, NY, NX), dtype=np.uint8)
contested = [CONTESTED[0], CONTESTED[1]]
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
    """Every pad reserves for the WIDEST trace that might pass it, plus a
    grid margin.

    IC pads used to be a deliberate exception, reserving only for their own
    net's width and skipping the grid margin, to keep the channel between
    adjacent SOIC pins open.  Two things were wrong with that.  The channel
    is not actually open: SOIC pads are 2.5 units apart and a signal trace
    needs 2.8, so nothing legal ever fit there and the exception bought
    nothing.  And the exception applied to the pad's WHOLE perimeter, not
    just the inter-pin gap, so a trace running past an IC pin sat exactly
    on the clearance limit with nothing left for grid quantisation to eat -
    which is why every layout the placement search produced came back with
    the same 7.4-7.8 mil violations at U1A.1 and U2B.7 against an 8 mil
    rule.  The hand-tuned board never tripped it only because no trace
    happened to take that route.

    What eats the margin is the 45 degree chamfering: it moves a finished
    centreline off the grid nodes the router checked it on, and the
    analysis showing a chamfer stays inside the reserved margin assumed the
    FULL margin, which is the one thing IC pads did not have."""
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
    """Shortest DRC-legal path for one net, from any source cell to any
    target cell.

    Dispatches to the compiled kernel in router.c when it built, and to the
    Python implementation below otherwise (ROUTER=py forces the latter).

    The two apply the same legality rules - same neighbour set, same via
    rule, same 1.02x weighted heuristic - but they are not guaranteed to
    return the SAME path: that heuristic is deliberately inadmissible, so
    which of several equal-cost routes comes out first depends on
    tie-breaking, and the C version settles nodes with a closed set where
    the Python one re-pops them.  What is guaranteed is that any path
    either produces obeys the same clearance rules, and the independent
    geometric verifier re-checks whatever comes back regardless of which
    router found it.  Compare them on failing-net count, not path equality.

    This exists because routing was the whole program's bottleneck at
    minutes per run, which made it far too expensive to use as feedback -
    so placement quality got guessed at through hand-tuned proxy terms
    instead of measured.  Same board, same settings: ~20 minutes of
    Python A* becomes ~20 seconds."""
    if ROUTER != "py" and croute.available():
        return croute.route(
            OCC, CONTESTED,
            None if blocked_extra is None
            else np.ascontiguousarray(np.stack(blocked_extra), dtype=np.uint8),
            None, None, nid, relaxed,
            int(math.ceil((VIA_EXTRA + extra) / GRID)), 0.0, 24.0,
            sources, targets, tgt_xy, NY, NX)
    return astar_py(sources, targets, nid, tgt_xy, extra, blocked_extra, relaxed)


def astar_py(sources, targets, nid, tgt_xy, extra=0.0, blocked_extra=None,
             relaxed=False):
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


# Route order is the single most influential thing about this router, because
# it is single-pass with no rip-up: whichever net claims a corridor first
# keeps it.  The tiers below are the part that is reasoned about; WITHIN a
# tier the order was previously just whatever sorted() did, which is an
# arbitrary choice presented as if it were a decision.
#
# ROUTE_SEED makes that arbitrary part searchable instead.  It only permutes
# nets that the tiers rank equally, so it cannot undo the ordering that is
# actually justified, and ROUTE_SEED=0 reproduces the old behaviour exactly.
# Worth having because the measured clean rate over placements alone is only
# a couple of percent - the same layout often routes cleanly under one order
# and not another, and searching that is far cheaper than searching seeds.
_ROUTE_SEED = int(os.environ.get("ROUTE_SEED", 0))
_ORDER_JITTER = {}
if _ROUTE_SEED:
    import random as _r
    _rng = _r.Random(_ROUTE_SEED)
    _ORDER_JITTER = {n: _rng.random() for n in netdoc["nets"]}


def _pad_span(name):
    pts = [(p["x"], p["y"]) for p in pads if p["net"] == name]
    if len(pts) < 2:
        return 0.0
    return max(max(p[0] for p in pts) - min(p[0] for p in pts),
               max(p[1] for p in pts) - min(p[1] for p in pts))


# The four widest-reaching signal nets on THIS board, measured.  Four
# because that is how many the old hardcoded list promoted (LP1 plus the
# three *_PRE nets), and promoting more was tried and made things worse.
LONG_HAUL = set(sorted(
    (n for n in netdoc["nets"] if n not in POWER_NETS),
    key=_pad_span, reverse=True)[:4])


# Nets promoted to the front of the route order because a previous attempt
# could not finish them.  Filled in by route_with_ripup().
ROUTE_PRIORITY = []


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

    Which nets those are is now MEASURED from the placement rather than
    listed by name.  It used to name LP1 explicitly, and an earlier attempt
    to generalise it to "any net spanning more than half the board width"
    was reverted for promoting so many nets that it reordered everything
    and produced more failures, not fewer.  Both of those were artefacts of
    a hand-tuned layout that no longer exists: with placement derived per
    run, a hardcoded net name is just a guess about a board that has since
    changed shape.  LONG_HAUL keeps the promotion to the same handful the
    named list did - the widest-reaching nets, whichever they turn out to
    be this time - so the ordering stays as conservative as the version
    that verified clean, without pretending to know the answer in advance."""
    members = netdoc["nets"][n]
    # A net that failed on a previous attempt jumps the whole queue, in the
    # order it failed.  This is the entire mechanism behind rip-up and
    # reroute below: the three tiers under it are hand-reasoned guesses
    # about who will need a channel most, and this one is measurement.
    if n in ROUTE_PRIORITY:
        return (-2, ROUTE_PRIORITY.index(n), 0)
    tier = (-1 if (n in ("GND", "+15V", "-15V") or n in LONG_HAUL)
            else 0 if any(m.startswith("U") for m in members) else 1)
    return (tier, len(members), _ORDER_JITTER.get(n, 0))


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

# GND is NOT routed.  The board carries a ground pour on both layers, every
# SMD ground pad sits in the top pour, every through-hole ground pad ties
# the two pours together by being a plated hole, and stitching vias join
# them elsewhere - so ground is already connected before a single trace is
# drawn.  Routing it as well made the board pay for ground twice, and
# because route_order sends it FIRST it took the best channels on the way.
#
# This is only safe because pour_connectivity() now proves the plane
# actually reaches every ground pad, which nothing checked before.  If that
# check fails, the board is wrong - do not paper over it by routing GND
# again.
_FOREIGN_STATIC = None


def _foreign_static():
    """Copper that cannot move once the parts are placed: pads, mounting
    holes, pot bosses.  Built once and copied.

    pour_connectivity() runs up to a dozen times in a board - six stub
    passes before routing and six after, plus the final check - and
    rebuilding the whole mask each time meant re-rasterising every pad on
    the board to find out something that had not changed since placement.

    Note what is NOT here: silkscreen.  A label is a reason to keep a
    TRACE out, so that "no silk over a trace" holds by construction, and
    no reason at all to keep the pour out - silkscreen printed over a
    ground plane is what every board in the world does.  Building the mask
    from copper rather than from the router's occupancy grid gets that
    right with no special case; the grid version had to be taught it, and
    until it was, U1C.10 and U2B.5 were sealed into pockets whose only
    exit ran under a designator."""
    global _FOREIGN_STATIC
    if _FOREIGN_STATIC is not None:
        return _FOREIGN_STATIC
    m = np.zeros((2, NY, NX), dtype=bool)

    def blk(layers, x0, y0, x1, y1):
        a, b, c, d = cells_in_rect(x0, y0, x1, y1)
        for L in layers:
            m[L, b:d + 1, a:c + 1] = True

    for p in pads:
        if p["net"] == "GND":
            continue                      # the pour joins these, not avoids them
        blk([0, 1] if p["layer"] == MULTI else [p["layer"] - 1],
            p["x"] - p["w"] / 2, p["y"] - p["h"] / 2,
            p["x"] + p["w"] / 2, p["y"] + p["h"] / 2)
    # Holes take copper on both layers whatever net they belong to.
    for hx, hy in MOUNT_HOLES:
        blk([0, 1], hx - MOUNT_R, hy - MOUNT_R, hx + MOUNT_R, hy + MOUNT_R)
    for hx, hy in POT_BOSSES:
        blk([0, 1], hx - BOSS_R, hy - BOSS_R, hx + BOSS_R, hy + BOSS_R)
    _FOREIGN_STATIC = m
    return m


def pour_connectivity():
    """Is the ground pour one piece of copper that reaches every GND pad?

    The board has always emitted a GND pour on both layers and never
    checked it - verify() models pads, tracks and vias only.  That gap is
    why GND also had to be ROUTED as an ordinary net: the plane could not
    be relied on, so ground was paid for twice, and the routed copy went
    first and took the best channels.

    The pour is modelled from the REAL copper on the board - pad
    rectangles, committed traces at their own width, via pads, and the
    holes - each grown by the clearance the emitted COPPERAREA actually
    declares.  It used to be modelled from the router's occupancy grid
    instead, which was easy but wrong by a factor of two: `occ` reserves
    CLEAR + MAX_W/2 around every piece of foreign copper because a TRACE
    routed there needs room for its own half-width, and a pour does not -
    it needs clearance and nothing else.  At the values in use that is
    1.6 units of reserved gap against a true 1.0, so every gap on the
    board read 1.2 units narrower than it is, and the pockets that
    produced "the ground pour does not reach N pads" were mostly an
    artefact of that.

    This is accuracy, not optimism, and it is still bounded on the safe
    side: dilate() grows by a BOX, which is a superset of the disc, so
    the blocked region is if anything slightly too large, and the
    POUR_MIN_W erosion below is unchanged.

    Returns (reached-everything, unreached pads, the reached-cell mask).
    Callers need that last one: to connect a pad the plane missed you have
    to route to copper that is genuinely PART of the plane, not merely to
    the nearest cell that happens to be ground - the pad's own keepout is
    ground, and routing to it connects nothing."""
    foreign = _foreign_static().copy()

    def _block(layers, x0, y0, x1, y1):
        a, b, c, d = cells_in_rect(x0, y0, x1, y1)
        for L in layers:
            foreign[L, b:d + 1, a:c + 1] = True

    for layer, pts, net in ROUTED:
        if net == "GND":
            continue
        hw = net_width(net) / 2
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            n = max(1, int(math.dist((ax, ay), (bx, by)) / (GRID / 2)))
            for i in range(n + 1):
                cx = ax + (bx - ax) * i / n
                cy = ay + (by - ay) * i / n
                _block([layer - 1], cx - hw, cy - hw, cx + hw, cy + hw)
    for vx, vy, vnet in VIAS:
        if vnet != "GND":
            _block([0, 1], vx - VIA_PAD / 2, vy - VIA_PAD / 2,
                   vx + VIA_PAD / 2, vy + VIA_PAD / 2)

    # POUR_CLEAR is the clearanceWidth written into the COPPERAREA shape
    # itself, so this is the gap the importer will actually hold.
    _cc = int(math.ceil(POUR_CLEAR / GRID))
    free = ~np.stack([dilate(foreign[L], _cc) for L in (0, 1)])
    # A pour is only connected where it is WIDE enough to exist.  Without
    # this, a one-cell neck - 0.06 mm - counts as a connection and the fab
    # simply will not make it.  Eroding by POUR_MIN_W/2 means only necks at
    # least that wide survive to carry connectivity.
    narrow = np.stack([dilate(~free[L], int(math.ceil(POUR_MIN_W / 2 / GRID)))
                       for L in (0, 1)])
    poured = (free & ~narrow).astype(np.uint8)
    edge = int(math.ceil(2.0 / GRID))            # the pour inset from the outline
    poured[:, :edge, :] = 0
    poured[:, -edge:, :] = 0
    poured[:, :, :edge] = 0
    poured[:, :, -edge:] = 0

    # Where the two layers are tied together: any plated hole on GND, and
    # every GND via.
    joint = np.zeros((2, NY, NX), dtype=np.uint8)
    for p in pads:
        if p["net"] == "GND" and p["layer"] == MULTI:
            a, b, c, d = cells_in_rect(p["x"] - p["w"] / 2, p["y"] - p["h"] / 2,
                                       p["x"] + p["w"] / 2, p["y"] + p["h"] / 2)
            joint[:, b:d + 1, a:c + 1] = 1
            poured[:, b:d + 1, a:c + 1] = 1
    for vx, vy, vnet in VIAS:
        if vnet == "GND":
            a, b, c, d = cells_in_rect(vx - VIA_PAD / 2, vy - VIA_PAD / 2,
                                       vx + VIA_PAD / 2, vy + VIA_PAD / 2)
            joint[:, b:d + 1, a:c + 1] = 1
            poured[:, b:d + 1, a:c + 1] = 1

    # A routed GND trace is real copper and joins whatever it touches,
    # whether or not it is as wide as the pour's minimum.  It must be put
    # back AFTER the erosion, not before: a stub threading a 0.5 mm channel
    # between two SOIC pads is exactly the geometry the erosion is there to
    # delete, and eroding it away made every plane stub the router
    # committed count for nothing.  R1.2 was routed to the plane on all six
    # passes and reported unreached on all six.
    # Stamped at the trace's real WIDTH, not as a centreline: the flood is
    # 4-connected, and a chamfered 45 degrees segment sampled as single
    # cells steps diagonally, so a centreline the router had committed
    # left a chain of cells the flood could not walk along.  C7.2 and
    # U1D.12 were routed to the plane on every pass and reported unreached
    # on every pass.  Half a trace width is ~3 cells, which also matches
    # what the copper physically covers.
    _tw = int(math.ceil(net_width("GND") / 2 / GRID))
    for layer, pts, net in ROUTED:
        if net != "GND":
            continue
        L = layer - 1
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            n = max(1, int(math.dist((ax, ay), (bx, by)) / (GRID / 2)))
            for i in range(n + 1):
                x = int(round((ax + (bx - ax) * i / n) / GRID))
                y = int(round((ay + (by - ay) * i / n) / GRID))
                poured[L, max(0, y - _tw):y + _tw + 1,
                       max(0, x - _tw):x + _tw + 1] = 1

    gnd_pads = [p for p in pads if p["net"] == "GND"]
    if not gnd_pads:
        return True, []
    cells = {}
    for p in gnd_pads:
        cs = [(L, x, y) for (L, x, y) in core_cells(p)]
        cells["%s.%s" % (p["ref"], p["num"])] = cs
        for L, x, y in cs:
            poured[L, y, x] = 1
    seed = cells["%s.%s" % (gnd_pads[0]["ref"], gnd_pads[0]["num"])]
    seen = croute.flood(poured, joint, seed, NY, NX)
    missed = [k for k, cs in cells.items()
              if not any(seen[L, y, x] for (L, x, y) in cs)]
    return not missed, missed, seen


PLANE_LOG = bool(os.environ.get("PLANE_LOG"))

# ==========================================================================
#  ground plane stubs
# ==========================================================================
# A pour cannot squeeze everywhere.  Between two adjacent SOIC pads there is
# 2.5 units of gap, and a pour needs clearance from both, so the ground pins
# of the op-amps sit in a pocket the plane cannot reach - which is exactly
# why a human drops a via right at such a pin rather than hoping.
#
# So: pour first, then find the ground pads the plane genuinely missed, and
# give each one a SHORT route to copper that is already part of the plane.
# This is the whole of GND's routing - a few stubs a few millimetres long,
# instead of a tree spanning the board.
#
# This runs TWICE, and the first time is the important one.  Running it only
# after the signal nets simply swapped one problem for its mirror image:
# GND used to route FIRST and take the best channels from everything else,
# and running the stubs last left the op-amp ground pins - U1C.10, U2C.10,
# U1D.12, U2B.5 - with nothing but corridors the signal nets had already
# spent.  Which pads sit in a structural pocket is knowable before any
# signal net is routed, because it is a fact about the FOOTPRINTS: pour the
# board with only pads on it, and whatever the plane cannot reach then is a
# geometry problem, not a congestion one.  Those stubs get cut while the
# channels are still free; the second pass then picks up anything that only
# became isolated once the signal nets were in.
def plane_stubs():
    _hopeless = set()
    for _pass in range(6):
        _ok, _missed, _seen = pour_connectivity()
        if _ok:
            break
        _gnid = NETID["GND"]
        _progress = False
        # A pad that found no route at all can only become routable if the
        # plane grew underneath it, which only happens when another stub
        # commits.  Without this the six passes re-run the same hopeless
        # full-grid A* six times over, and a failing A* is the most expensive
        # thing the router does.
        _missed = [m for m in _missed if m not in _hopeless]
        for _name in _missed:
            _ref, _, _num = _name.rpartition(".")
            _p = next((q for q in pads if q["ref"] == _ref and q["num"] == _num), None)
            if _p is None:
                continue
            # Every piece of copper that is genuinely part of the plane and
            # lies near the pad, on EITHER layer, offered to A* at once - plus
            # the pads of every ground pin the plane already reaches.
            #
            # Two mistakes were made here in turn.  Restricting the target to
            # the pad's own layer was the first: around an op-amp's ground pin
            # the top pour is chopped up by the neighbouring pins while the
            # bottom layer is nearly solid ground, and the obvious move - the
            # one a person makes without thinking - is a via straight down.
            # Picking the single NEAREST plane cell was the second: nearest is
            # not cheapest to reach, and a pad whose closest plane cell is
            # walled off simply failed when a cell slightly further along an
            # open channel was free.
            #
            # Reached GROUND PADS are targets in their own right.  Ground is
            # one net: tying a bypass cap's ground pin to the op-amp ground
            # pin beside it is exactly as good as tying it to the pour, and a
            # 40-mil pad is a far easier thing for the router to enter than a
            # sliver of plane that survived the min-width erosion.
            _px, _py = _p["x"] / GRID, _p["y"] / GRID
            _anchored = set()
            for _q in pads:
                if _q["net"] != "GND" or _q is _p:
                    continue
                _qc = core_cells(_q)
                if any(_seen[L, y, x] for (L, x, y) in _qc):
                    _anchored |= _qc
            # One window, not an escalating series.  The reached ground pads
            # are already board-wide targets, so widening the pour window buys
            # very little - and a FAILING A* is the most expensive thing the
            # router does (it drains the queue over the whole reachable grid),
            # so each extra attempt at a pad that has no escape costs seconds.
            _r = 160
            _x0, _x1 = max(0, int(_px - _r)), min(NX, int(_px + _r) + 1)
            _y0, _y1 = max(0, int(_py - _r)), min(NY, int(_py + _r) + 1)
            _tgt, _best = set(_anchored), None
            for _L in (0, 1):
                _ys, _xs = np.nonzero(_seen[_L, _y0:_y1, _x0:_x1])
                if not len(_xs):
                    continue
                _xs, _ys = _xs + _x0, _ys + _y0
                _tgt |= set(zip([_L] * len(_xs), _xs.tolist(), _ys.tolist()))
                _d = (_xs - _px) ** 2 + (_ys - _py) ** 2
                _i = int(np.argmin(_d))
                if _best is None or _d[_i] < _best[0]:
                    _best = (_d[_i], int(_xs[_i]), int(_ys[_i]))
            if not _tgt:
                continue
            _anchor = ((_best[1], _best[2]) if _best else
                       min(_tgt, key=lambda c: (c[1] - _px) ** 2
                           + (c[2] - _py) ** 2)[1:])
            _path = astar(core_cells(_p), _tgt, _gnid, _anchor, 0.0, None)
            _how = "strict"
            if _path is None:
                _path = astar(core_cells(_p), _tgt, _gnid, _anchor, 0.0,
                              None, relaxed=True)
                _how = "relaxed"
            if _path is None:
                _hopeless.add(_name)
                if PLANE_LOG:
                    _cc = core_cells(_p)
                    _free = [(L, x + dx, y + dy) for (L, x, y) in _cc
                             for (dx, dy) in ((1, 0), (-1, 0), (0, 1), (0, -1))
                             if (L, x + dx, y + dy) not in _cc
                             and 0 <= x + dx < NX and 0 <= y + dy < NY
                             and occ[L][y + dy, x + dx] in (0, _gnid)
                             and not contested[L][y + dy, x + dx]]
                    _nd = min(((c[1] - _px) ** 2 + (c[2] - _py) ** 2)
                              for c in _tgt) ** 0.5
                    print("  plane stub %s at (%.1f,%.1f) L%d: no path to "
                          "%d plane cells (nearest %.1f units, %d free "
                          "neighbours, via_ok=%s)"
                          % (_name, _p["x"], _p["y"], _p["layer"], len(_tgt),
                             _nd * GRID, len(_free),
                             any(via_ok(x, y, _gnid) for (L, x, y) in _cc)),
                          flush=True)
                continue
            _r, _v, _pl = path_geometry(_path)
            if path_clearance_ok("GND", _v, _pl):
                commit_path("GND", _gnid, _r, _v, _pl)
                _progress = True
                _hopeless.clear()
                if PLANE_LOG:
                    print("  plane stub %s: %s path committed (%d cells)"
                          % (_name, _how, len(_path)), flush=True)
            elif PLANE_LOG:
                print("  plane stub %s: %s path failed clearance"
                      % (_name, _how), flush=True)
        if not _progress:
            break


# --------------------------------------------------------------------------
#  exact-geometry features, and the split-net test built on them
# --------------------------------------------------------------------------
def _feature_bbox(f):
    """Axis-aligned bounds of a feature's geometry, before its half-width."""
    if f["k"] == "rect":
        return f["g"]
    if f["k"] == "seg":
        (ax, ay), (bx, by) = f["g"]
        return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
    return (f["g"][0], f["g"][1], f["g"][0], f["g"][1])


def _feature_arrays():
    """Every feature's expanded bounding box, net and layer mask, as arrays.

    This is what makes the pairwise checks below affordable.  They are
    genuinely O(n^2) in the pairs they must CONSIDER - roughly 2.9 million
    of them here - but only a handful of those pairs are anywhere near each
    other, and exact segment-to-segment distance is far too expensive to
    spend on the rest (it was 28 of the 38 seconds a full run took).

    Distance between expanded bounding boxes is a lower bound on the real
    gap, so a pair whose boxes are already further apart than the clearance
    rule cannot possibly violate it.  Skipping those is exact, not an
    approximation - the surviving pairs still get the same exact-geometry
    test they always did."""
    n = len(FEATURES)
    x0 = np.empty(n); y0 = np.empty(n); x1 = np.empty(n); y1 = np.empty(n)
    netid = np.empty(n, dtype=np.int64)
    layer = np.empty(n, dtype=np.int64)
    seen = {}
    for i, f in enumerate(FEATURES):
        bx0, by0, bx1, by1 = _feature_bbox(f)
        hw = f["hw"]
        x0[i], y0[i], x1[i], y1[i] = bx0 - hw, by0 - hw, bx1 + hw, by1 + hw
        netid[i] = seen.setdefault(f["net"], len(seen))
        layer[i] = sum(1 << L for L in f["L"])
    return x0, y0, x1, y1, netid, layer


def _near_pairs(i, arr, limit):
    """Indices j > i whose expanded box is within `limit` of feature i."""
    x0, y0, x1, y1, netid, layer = arr
    j = slice(i + 1, None)
    dx = np.maximum(np.maximum(x0[j] - x1[i], x0[i] - x1[j]), 0.0)
    dy = np.maximum(np.maximum(y0[j] - y1[i], y0[i] - y1[j]), 0.0)
    return np.nonzero((dx * dx + dy * dy <= limit * limit)
                      & (layer[j] & layer[i] != 0))[0] + i + 1


# These live here, above the routing section, because the rip-up loop needs
# them.  Scoring an attempt by the ROUTER's own bookkeeping is scoring the
# wrong thing: the router works on a 0.25-unit grid and thinks in cells, and
# it will happily report a net finished that the exact-geometry checker
# then reports in two pieces.  The first rip-up run did exactly that - three
# attempts, "0 unrouted" on the third, and verify() found TP1 split.  The
# loop now optimises the same measure the checker applies, so it cannot
# declare victory on a board the checker will fail.
FEATURES = []
PAD_FEATURES = []


def build_features():
    """Rebuild the exact-geometry feature list from the current copper."""
    FEATURES.clear()
    for p in pads:
        FEATURES.append(dict(net=p["net"], k="rect", hw=0.0,
                             L={1, 2} if p["layer"] == MULTI else {p["layer"]},
                             g=(p["x"] - p["w"] / 2, p["y"] - p["h"] / 2,
                                p["x"] + p["w"] / 2, p["y"] + p["h"] / 2),
                             tag="%s.%s" % (p["ref"], p["num"])))
    PAD_FEATURES[:] = list(FEATURES)
    for layer, pts, name in ROUTED:
        for a, b in zip(pts, pts[1:]):
            FEATURES.append(dict(net=name, k="seg", hw=net_width(name) / 2,
                                 L={layer}, g=(a, b), tag="track"))
    for x, y, name in VIAS:
        FEATURES.append(dict(net=name, k="pt", hw=VIA_PAD / 2, L={1, 2},
                             g=(x, y), tag="via"))


def split_nets():
    """Nets whose pads are not all one piece of touching copper, by exact
    geometry.  Returns {net: [[tag, ...], ...]}."""
    arr = _feature_arrays()
    fnet = arr[4]
    parent = list(range(len(FEATURES)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, f in enumerate(FEATURES):
        for j in _near_pairs(i, arr, 1e-6):
            if fnet[j] == fnet[i] and gap(f, FEATURES[j]) <= 1e-9:
                parent[find(i)] = find(j)
    bypad = {}
    for i, f in enumerate(FEATURES):
        if f in PAD_FEATURES and f["net"] and f["net"] not in PLANE_NETS:
            bypad.setdefault(f["net"], []).append((i, f["tag"]))
    out = {}
    for name, items in bypad.items():
        groups = {}
        for i, tag in items:
            groups.setdefault(find(i), []).append(tag)
        if len(groups) > 1:
            out[name] = list(groups.values())
    return out


# ==========================================================================
#  routing, with rip-up and reroute
# ==========================================================================
# The board is snapshotted here, after every fixed reservation (holes, pad
# keepouts, silkscreen) and before a single trace exists, so a routing
# attempt can be thrown away and redone from a clean grid in O(grid)
# instead of being unpicked.
OCC0 = OCC.copy()
CON0 = CONTESTED.copy()


def reset_routing():
    OCC[:] = OCC0
    CONTESTED[:] = CON0
    ROUTED.clear()
    VIAS.clear()


def route_pass():
    """One complete attempt: plane stubs, every signal net, the relaxed
    retries, plane stubs again, stitching vias.  Returns the nets it could
    not finish."""
    # Cut the structural stubs BEFORE any signal net is routed - see the note
    # above plane_stubs().
    plane_stubs()

    FAILED = []
    RETRY = []      # strict-pass failures, retried once below with relaxed margins
    for name in sorted(netdoc["nets"], key=route_order):
        if name in PLANE_NETS:
            continue
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
    if os.environ.get("RETRY_LOG"):
        print("  strict pass: %d net-targets needed a relaxed retry" % len(RETRY),
              flush=True)
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

    # Second pass: anything the signal nets isolated on their way through.
    plane_stubs()



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
            # via_ok() is net-aware and so will happily put a GND stitching via
            # right up against a GND via the router already placed - fine for
            # copper, but the FAB still has to drill both, and hole-to-hole
            # spacing is a property of the drill, not of the net.  JLCPCB wants
            # 0.5 mm edge to edge; without this a stitching via landed 0.43 mm
            # from a routed one, which no electrical check would ever object to.
            if any(math.dist((_vx, _vy), (_ox, _oy)) < VIA_DRILL + MIN_HOLE_GAP
                   for _ox, _oy, _ in VIAS):
                continue
            if via_ok(_cx, _cy, GND_NID):
                VIAS.append((_vx, _vy, "GND"))
                stamp_disc(MULTI, _vx, _vy, VIA_DIL, GND_NID)
                break


    return FAILED


# --------------------------------------------------------------------------
#  rip-up and reroute
# --------------------------------------------------------------------------
# Why this exists, in one measurement.  An SOIC-14's pads are 2.5 units
# apart edge to edge, and a 12 mil trace needs 1.2 + 2 x 0.8 = 2.8 units to
# pass between two of them.  It does not fit.  Every pin on every IC has to
# escape OUTWARD along a channel it shares with its neighbours, so whichever
# net is routed first takes the channel and the rest are locked out - not
# for want of board area, and not for want of a better placement, but
# because single-pass routing meets a pitch that does not admit a second
# trace.  That is why route_order() has grown three separate special cases,
# and why adding parts kept costing whole search campaigns.
#
# The fix is to let a net that failed take the channel back.  This does NOT
# unpick one net at a time: that is where the negotiated-congestion attempt
# deadlocked, because the net that has to move is usually not either of the
# two in conflict.  It throws the entire route away and runs it again with
# the nets that failed promoted to the front of the order.  Each attempt is
# a full route, which is affordable now one costs seconds, and the order is
# LEARNED from what actually failed rather than guessed by sweeping
# ROUTE_SEED across separate processes.
#
# Every attempt is scored with the same measure the search uses - unrouted
# nets plus ground pads the plane cannot reach - and the best attempt is
# what gets kept, so a round that makes things worse cannot ship.
RIPUP_ROUNDS = int(os.environ.get("RIPUP_ROUNDS", 6))
RIPUP_LOG = bool(os.environ.get("RIPUP_LOG"))


def _crowding_nets(missed):
    """Which nets are sitting on top of a ground pad the plane cannot
    reach.  A pour miss names no net of its own - GND is not routed - so
    without this the loop has nothing to promote and stops on the first
    attempt that routes every signal net but strands a ground pin."""
    out = []
    r = int(math.ceil((VIA_PAD / 2 + CLEAR) / GRID))
    by_id = {v: k for k, v in NETID.items()}
    for name in missed:
        ref, _, num = name.rpartition(".")
        p = next((q for q in pads if q["ref"] == ref and q["num"] == num), None)
        if p is None:
            continue
        cx, cy = int(p["x"] / GRID), int(p["y"] / GRID)
        for L in (0, 1):
            sub = occ[L][max(0, cy - r):cy + r + 1, max(0, cx - r):cx + r + 1]
            for v in np.unique(sub):
                n = by_id.get(int(v))
                if n and n not in PLANE_NETS and n not in out:
                    out.append(n)
    return out


def route_with_ripup():
    """Route the board up to RIPUP_ROUNDS + 1 times, promoting whatever
    failed, and keep the best attempt.

    Each round extends the BEST order found so far, not the last one
    tried.  Extending the last one turns the search into a random walk:
    the first version did that, reached 0 unrouted and 0 split on attempt
    3, and then spent attempts 4, 5 and 6 wandering away from it with a
    priority list that had grown to fifteen nets and no longer meant
    anything.  Hill-climbing from the best keeps every round a variation
    on something that worked."""
    best_score, best, best_priority = None, None, []
    for attempt in range(RIPUP_ROUNDS + 1):
        reset_routing()
        fails = route_pass()
        # Score with the CHECKER's measure, not the router's.  A net the
        # router calls finished can still be in two pieces by exact
        # geometry - it thinks in 0.25-unit cells - and a loop that stops
        # when the router is happy stops one problem short.
        build_features()
        split = split_nets()
        _ok, missed, _seen = pour_connectivity()
        score = len(fails) + len(split) + len(missed)
        if RIPUP_LOG:
            print("  route attempt %d: %d unrouted, %d split, %d pads off "
                  "the plane%s" % (attempt, len(fails), len(split), len(missed),
                                   (" (promoted: %s)" % ", ".join(ROUTE_PRIORITY))
                                   if ROUTE_PRIORITY else ""), flush=True)
        if best_score is None or score < best_score:
            best_score = score
            best_priority = list(ROUTE_PRIORITY)
            best = (list(ROUTED), list(VIAS), list(fails),
                    OCC.copy(), CONTESTED.copy())
        if score == 0:
            break
        # What this attempt could not finish, newest first: each of these
        # has just proved it cannot win its channel from where it sat.
        promote = list(dict.fromkeys(
            [f.split(":")[0] for f in fails] + list(split)))
        if not promote:
            promote = _crowding_nets(missed)
        promote = [n for n in promote if n not in best_priority]
        if not promote:
            break                     # nothing new to learn; stop burning time
        ROUTE_PRIORITY[:] = promote + best_priority
    ROUTED[:], VIAS[:], fails, occ_b, con_b = best
    OCC[:] = occ_b
    CONTESTED[:] = con_b
    return fails


FAILED = route_with_ripup()

# ==========================================================================
#  independent verification - exact geometry, not the router's own bookkeeping
# ==========================================================================
# (d_pt_seg / d_seg_seg / d_pt_rect / d_seg_rect / d_rect_rect / gap moved
# above the routing section - the relaxed retry pass needs them too, to
# check a candidate path for real before committing it, not just hope a
# relaxed pass is safe because it usually is)
build_features()


REPORT = []


def verify():
    problems = []
    arr = _feature_arrays()
    netid = arr[4]
    # -- clearance: every pair of features on a shared layer, different nets
    for i, f in enumerate(FEATURES):
        for j in _near_pairs(i, arr, CLEAR):
            if netid[j] == netid[i]:
                continue
            g = FEATURES[j]
            d = gap(f, g)
            if d < CLEAR - 1e-9:
                problems.append("clearance %.2f mil between %s (%s) and %s (%s)"
                                % (d * 10, f["tag"], f["net"],
                                   g["tag"], g["net"]))
    # -- connectivity: union-find over touching same-net features.  Shared
    # with the rip-up loop, which has to score attempts by exactly this.
    # Plane nets are exempt - proven by the pour check below, not by traces.
    for name, groups in sorted(split_nets().items()):
        problems.append("net %s is in %d pieces: %s"
                        % (name, len(groups), groups))
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
_pour_ok, _pour_missed, _ = pour_connectivity()
if not _pour_ok:
    ISSUES.append("the ground pour does not reach %d pad(s): %s"
                  % (len(_pour_missed), _pour_missed[:8]))

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
    # Same verdict line the full path prints.  Sweep mode used to report
    # its problems without ever stating a count, which let a caller that
    # looked for the count conclude there were none - tools/find_board.py
    # reported 24 boards out of 24 as verifying clean when not one of them
    # did.  Both paths now say the same thing the same way.
    print("DRC PROBLEMS (%d)" % (len(ISSUES) + len(FAILED)))
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
print("placement: %d restarts, net half-perimeter %.0f -> %.0f mm"
      % (RESTARTS, BEFORE / MM, AFTER / MM))
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
