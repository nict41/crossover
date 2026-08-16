#!/usr/bin/env python3
"""
Generate an EasyEDA (Std) importable PCB for the `retuned-quad` variant.

Reads docs/netlist-esp-p148-3way-crossover-retuned-quad.json (written by
gen_schematic.py) so the board can never drift from the schematic, then places
every footprint, assigns each pad its net, and writes an EasyEDA PCB document.

WHAT THIS PRODUCES
    Board outline, mounting holes, footprints placed by signal flow, every pad
    netted (so the ratsnest is correct on import), silkscreen designators, and
    a bottom-layer ground pour.

WHAT IT DOES NOT PRODUCE
    Routed signal traces.  Route in EasyEDA - by hand or with its autorouter -
    once you have checked the placement.  See docs/pcb-notes.md for why, and
    for the routing order that matters on this board.

Units: EasyEDA PCB coordinates are in 10-mil steps, so 1 unit = 0.254 mm and
one 0.1" pin pitch = 10 units.  Everything below is in those units.
"""

import json
import math
import os

MM = 1000.0 / 25.4 / 10.0          # mm -> units (10 mil)

_next = [100]


def gid():
    _next[0] += 1
    return "gge%d" % _next[0]


TOP, BOT, TOPSILK, BOTSILK, OUTLINE, MULTI = 1, 2, 3, 4, 10, 11

shapes = []
pads = []            # (refdes, pin, x, y)
placed = []          # (refdes, x0, y0, x1, y1) courtyards, for overlap checks


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
def pad(ref, num, x, y, net, w=6.0, h=6.0, hole=1.6, shape="ELLIPSE", rot=0):
    pts = ""
    if shape == "RECT":
        pts = "%g %g %g %g %g %g %g %g" % (x - w / 2, y - h / 2, x + w / 2, y - h / 2,
                                           x + w / 2, y + h / 2, x - w / 2, y + h / 2)
    shapes.append("PAD~%s~%g~%g~%g~%g~%d~%s~%s~%g~%s~%d~%s~~~Y"
                  % (shape, x, y, w, h, MULTI, net, num, hole, pts, rot, gid()))
    pads.append((ref, str(num), x, y))


def track(points, layer=TOPSILK, width=0.8, net=""):
    pts = " ".join("%g %g" % p for p in points)
    shapes.append("TRACK~%g~%d~%s~%s~%s" % (width, layer, net, pts, gid()))


def rect_outline(x0, y0, x1, y1, layer=TOPSILK, width=0.8):
    track([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], layer, width)


def silk_text(x, y, s, size=3.0, layer=TOPSILK, anchor="start"):
    """EasyEDA TEXT: TEXT~type~x~y~strokew~rot~mirror~layer~net~font~text~path~~id"""
    shapes.append("TEXT~L~%g~%g~0.6~0~0~%d~~%g~%s~~~%s"
                  % (x, y, layer, size, s, gid()))


def hole(x, y, dia):
    shapes.append("HOLE~%g~%g~%g~%s" % (x, y, dia, gid()))


# --------------------------------------------------------------------------
# footprints.  Each returns its courtyard so overlaps can be checked.
# --------------------------------------------------------------------------
def fp_axial(ref, x, y, net_of, pitch=40.0, body=25.0, vertical=False):
    """Axial resistor, 0.4in lead pitch by default."""
    if vertical:
        pad(ref, 1, x, y - pitch / 2, net_of(ref, 1))
        pad(ref, 2, x, y + pitch / 2, net_of(ref, 2))
        rect_outline(x - 4, y - body / 2, x + 4, y + body / 2)
        track([(x, y - pitch / 2), (x, y - body / 2)])
        track([(x, y + body / 2), (x, y + pitch / 2)])
        silk_text(x + 6, y - body / 2 - 1, ref)
        return (x - 6, y - pitch / 2 - 3, x + 6, y + pitch / 2 + 3)
    pad(ref, 1, x - pitch / 2, y, net_of(ref, 1))
    pad(ref, 2, x + pitch / 2, y, net_of(ref, 2))
    rect_outline(x - body / 2, y - 4, x + body / 2, y + 4)
    track([(x - pitch / 2, y), (x - body / 2, y)])
    track([(x + body / 2, y), (x + pitch / 2, y)])
    silk_text(x - body / 2, y - 6, ref)
    return (x - pitch / 2 - 6, y - 6, x + pitch / 2 + 6, y + 6)


def fp_cap(ref, x, y, net_of, pitch=20.0, w=10.0, h=8.0):
    """Boxed film / ceramic capacitor, pitch in units (20 = 5 mm)."""
    pad(ref, 1, x - pitch / 2, y, net_of(ref, 1))
    pad(ref, 2, x + pitch / 2, y, net_of(ref, 2))
    rect_outline(x - w / 2, y - h / 2, x + w / 2, y + h / 2)
    silk_text(x - w / 2, y - h / 2 - 2, ref)
    return (x - max(pitch / 2, w / 2) - 3, y - h / 2 - 2,
            x + max(pitch / 2, w / 2) + 3, y + h / 2 + 2)


def fp_radial(ref, x, y, net_of, pitch=20.0, dia=26.0):
    """Radial electrolytic; pin 1 is +."""
    pad(ref, 1, x - pitch / 2, y, net_of(ref, 1), shape="RECT")
    pad(ref, 2, x + pitch / 2, y, net_of(ref, 2))
    n = 24
    track([(x + dia / 2 * math.cos(2 * math.pi * i / n),
            y + dia / 2 * math.sin(2 * math.pi * i / n)) for i in range(n + 1)])
    silk_text(x - dia / 2, y - dia / 2 - 2, ref)
    silk_text(x - pitch / 2 - 5, y - 2, "+")
    return (x - dia / 2 - 2, y - dia / 2 - 2, x + dia / 2 + 2, y + dia / 2 + 2)


def fp_dip(ref_pkg, sections, x, y, net_of, npins=14):
    """DIP-14, pin 1 bottom-left, 0.1in pitch, 0.3in row spacing.

    `sections` maps a pin number to the schematic refdes that owns it, since
    the quad's four sections are drawn as four separate symbols.
    """
    half = npins // 2
    span = (half - 1) * 10.0
    for i in range(half):                       # pins 1..7 along the bottom
        n = i + 1
        pad(sections[n], n, x + i * 10.0, y, net_of(sections[n], n),
            shape="RECT" if n == 1 else "ELLIPSE")
    for i in range(half):                       # pins 8..14 along the top
        n = half + 1 + i
        pad(sections[n], n, x + span - i * 10.0, y - 30.0,
            net_of(sections[n], n))
    rect_outline(x - 6, y - 36, x + span + 6, y + 6)
    track([(x - 6, y - 18), (x - 2, y - 18)])   # pin-1 notch marker
    silk_text(x - 6, y - 42, ref_pkg, size=3.5)
    return (x - 8, y - 38, x + span + 8, y + 8)


def fp_header(ref, x, y, net_of, n=3, vertical=True, label=""):
    """0.1in pin header / wiring pads."""
    for i in range(n):
        px, py = (x, y + i * 10.0) if vertical else (x + i * 10.0, y)
        pad(ref, i + 1, px, py, net_of(ref, i + 1),
            shape="RECT" if i == 0 else "ELLIPSE")
    if vertical:
        rect_outline(x - 5, y - 5, x + 5, y + (n - 1) * 10.0 + 5)
        silk_text(x + 7, y - 3, label or ref)
        return (x - 7, y - 7, x + 7, y + (n - 1) * 10.0 + 7)
    rect_outline(x - 5, y - 5, x + (n - 1) * 10.0 + 5, y + 5)
    silk_text(x - 5, y - 8, label or ref)
    return (x - 7, y - 7, x + (n - 1) * 10.0 + 7, y + 7)


# ==========================================================================
#  board
# ==========================================================================
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SLUG = "esp-p148-3way-crossover-retuned-quad"
net = json.load(open(os.path.join(ROOT, "docs", "netlist-%s.json" % SLUG)))

PIN_NET = {}
for name, members in net["nets"].items():
    for m in members:
        ref, _, num = m.rpartition(".")
        PIN_NET[(ref, num)] = name


def N(ref, num):
    return PIN_NET.get((ref, str(num)), "")


BW, BH = 400.0, 330.0        # 101.6 x 83.8 mm

# ---- placement -----------------------------------------------------------
# Signal flows left to right; filter 1 occupies the upper half, filter 2 the
# lower half, matching the schematic.  Each quad sits amongst its own parts.
U1_SECTIONS = {1: "U1A", 2: "U1A", 3: "U1A", 4: "U1A", 5: "U1B", 6: "U1B",
               7: "U1B", 8: "U1C", 9: "U1C", 10: "U1C", 11: "U1A",
               12: "U1D", 13: "U1D", 14: "U1D"}
U2_SECTIONS = {1: "U2A", 2: "U2A", 3: "U2A", 4: "U2A", 5: "U2B", 6: "U2B",
               7: "U2B", 8: "U2C", 9: "U2C", 10: "U2C", 11: "U2A",
               12: "U2D", 13: "U2D", 14: "U2D"}

def add(box):
    placed.append(box)


# --------------------------------------------------------------------------
# Placement.  Parts that must sit somewhere specific are fixed; the rest are
# assigned to slots within their own functional group, then those assignments
# are optimised for total ratsnest length so the board is short-trace without
# scrambling the logical left-to-right arrangement.
# --------------------------------------------------------------------------
PAD_OFF = {
    "axial-h": [(-20.0, 0.0), (20.0, 0.0)],
    "axial-v": [(0.0, -20.0), (0.0, 20.0)],
    "cap": [(-10.0, 0.0), (10.0, 0.0)],
    "radial": [(-10.0, 0.0), (10.0, 0.0)],
}


def hdr_off(n):
    return [(0.0, 10.0 * i) for i in range(n)]


def dip_off():
    off = {}
    for i in range(7):
        off[i + 1] = (i * 10.0, 0.0)
        off[8 + i] = (60.0 - i * 10.0, -30.0)
    return off


# fixed: (ref, kind, x, y)
FIXED = [("J2", "hdr2", 32, 30), ("C0", "radial", 32, 75),
         ("R1", "axial-v", 32, 125), ("J1", "hdr3", 32, 165),
         ("J3", "hdr4", 32, 225),
         # bypass caps sit hard against their own package, level with the
         # supply pins (V+ pin 4 on the bottom row, V- pin 11 on the top row)
         ("C5", "cap", 235, 115), ("C6", "cap", 235, 85),
         ("C7", "cap", 235, 300), ("C8", "cap", 235, 270),
         ("TP1", "hdr1", 370, 95), ("TP2", "hdr1", 370, 280)]

DIPS = [("U1", 150, 115, U1_SECTIONS), ("U2", 150, 300, U2_SECTIONS)]

# slot groups: parts are permuted within a group only
SLOTS = {
    "f1r": [("axial-h", x, 25) for x in (90, 160, 230, 300, 370)] +
           [("axial-h", x, 52) for x in (210, 280, 350)] +
           [("axial-h", x, 145) for x in (90, 160)],
    "f1c": [("cap", 90, 52), ("cap", 140, 52)],
    "f1p": [("hdr3", 290, 95), ("hdr3", 330, 95)],
    "f2r": [("axial-h", x, 210) for x in (90, 160, 230, 300, 370)] +
           [("axial-h", x, 235) for x in (210, 280, 350)] +
           [("axial-h", x, 254) for x in (210, 280, 350)] +
           [("axial-h", x, 320) for x in (90, 160)],
    "f2c": [("cap", 90, 235), ("cap", 140, 235)],
    "f2p": [("hdr3", 290, 280), ("hdr3", 330, 280)],
}
GROUP_PARTS = {
    "f1r": ["R2", "R5", "R6", "R10", "R11", "R7", "R8", "R9", "R3", "R4"],
    "f1c": ["C1", "C2"],
    "f1p": ["VR1A", "VR1B"],
    "f2r": ["R15", "R16", "R12", "R23", "R24", "R17", "R18", "R19",
            "R22", "R20", "R21", "R13", "R14"],
    "f2c": ["C3", "C4"],
    "f2p": ["VR2A", "VR2B"],
}
for g in SLOTS:
    assert len(SLOTS[g]) == len(GROUP_PARTS[g]), g


def pad_positions(assign):
    """{(ref, pin): (x, y)} for a candidate assignment."""
    pos = {}
    for ref, kind, x, y in FIXED + assign:
        if kind.startswith("hdr"):
            for i, (dx, dy) in enumerate(hdr_off(int(kind[3:]))):
                pos[(ref, str(i + 1))] = (x + dx, y + dy)
        else:
            for i, (dx, dy) in enumerate(PAD_OFF[kind]):
                pos[(ref, str(i + 1))] = (x + dx, y + dy)
    for _, x, y, sections in DIPS:
        for n, (dx, dy) in dip_off().items():
            pos[(sections[n], str(n))] = (x + dx, y + dy)
    return pos


def ratsnest(pos):
    """Total minimum-spanning-tree length over every net, in units."""
    total = 0.0
    for name, members in net["nets"].items():
        pts = [pos[tuple(m.rpartition(".")[::2])] for m in members
               if tuple(m.rpartition(".")[::2]) in pos]
        if len(pts) < 2:
            continue
        if name == "GND":
            continue                      # poured, not routed
        inside, rest = [pts[0]], pts[1:]
        while rest:
            best = min(((math.dist(a, b), j) for j, b in enumerate(rest)
                        for a in inside))
            total += best[0]
            inside.append(rest.pop(best[1]))
    return total


def optimise():
    assign = []
    for g, parts in GROUP_PARTS.items():
        for ref, slot in zip(parts, SLOTS[g]):
            assign.append([ref, slot[0], slot[1], slot[2]])
    idx = {a[0]: i for i, a in enumerate(assign)}
    before = ratsnest(pad_positions([tuple(a) for a in assign]))

    improved = True
    while improved:
        improved = False
        for g, parts in GROUP_PARTS.items():
            for i in range(len(parts)):
                for j in range(i + 1, len(parts)):
                    a, b = idx[parts[i]], idx[parts[j]]
                    cur = ratsnest(pad_positions([tuple(x) for x in assign]))
                    assign[a][1:], assign[b][1:] = assign[b][1:], assign[a][1:]
                    new = ratsnest(pad_positions([tuple(x) for x in assign]))
                    if new < cur - 1e-9:
                        improved = True
                    else:
                        assign[a][1:], assign[b][1:] = assign[b][1:], assign[a][1:]
    after = ratsnest(pad_positions([tuple(a) for a in assign]))
    return [tuple(a) for a in assign], before, after


ASSIGN, RATS_BEFORE, RATS_AFTER = optimise()

EMIT = {"axial-h": lambda r, x, y: fp_axial(r, x, y, N),
        "axial-v": lambda r, x, y: fp_axial(r, x, y, N, vertical=True),
        "cap": lambda r, x, y: fp_cap(r, x, y, N),
        "radial": lambda r, x, y: fp_radial(r, x, y, N, pitch=20, dia=22),
        "hdr1": lambda r, x, y: fp_header(r, x, y, N, n=1, label=r),
        "hdr2": lambda r, x, y: fp_header(r, x, y, N, n=2, label="IN"),
        "hdr3": lambda r, x, y: fp_header(r, x, y, N, n=3, label=r),
        "hdr4": lambda r, x, y: fp_header(r, x, y, N, n=4, label="OUT")}

for ref, kind, x, y in FIXED + list(ASSIGN):
    lab = {"J1": "PWR", "J2": "IN", "J3": "OUT"}.get(ref)
    if kind == "hdr3" and ref == "J1":
        add(fp_header(ref, x, y, N, n=3, label="PWR"))
    else:
        add(EMIT[kind](ref, x, y))
for pkg, x, y, sections in DIPS:
    add(fp_dip("%s  MC33079" % pkg, sections, x, y, N))

# ---- board outline, mounting holes, pour --------------------------------
rect_outline(0, 0, BW, BH, layer=OUTLINE, width=0.6)
for hx, hy in [(12, 12), (BW - 12, 12), (12, BH - 12), (BW - 12, BH - 12)]:
    hole(hx, hy, 12.6)          # M3
silk_text(180, 168, "ESP P148 3-WAY VARIABLE CROSSOVER", size=4.5)
silk_text(180, 178, "RETUNED QUAD  195Hz-1.03kHz / 71-180Hz", size=3.5)
silk_text(180, 187, "PLACEMENT ONLY - SIGNALS NOT ROUTED", size=3.5)

shapes.append("COPPERAREA~1~%d~GND~%s~1~solid~%s~spoke~none~[]~0~2~1~none"
              % (BOT, " ".join("%g %g" % p for p in
                               [(2, 2), (BW - 2, 2), (BW - 2, BH - 2), (2, BH - 2)]),
                 gid()))


# ==========================================================================
#  checks
# ==========================================================================
def check():
    problems = []

    sch_pins = set()
    for members in net["nets"].values():
        for m in members:
            ref, _, num = m.rpartition(".")
            sch_pins.add((ref, num))
    pcb_pins = set((r, n) for r, n, _, _ in pads)
    if sch_pins - pcb_pins:
        problems.append("pins in schematic but not on board: %s"
                        % sorted(sch_pins - pcb_pins))
    if pcb_pins - sch_pins:
        problems.append("pads on board with no schematic pin: %s"
                        % sorted(pcb_pins - sch_pins))

    unnetted = [(r, n) for r, n, _, _ in pads if not N(r, n)]
    if unnetted:
        problems.append("pads with no net: %s" % sorted(unnetted))

    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                problems.append("footprint courtyards overlap: %s / %s" % (a, b))

    seen = {}
    for r, n, x, y in pads:
        if (x, y) in seen:
            problems.append("pads share a position: %s.%s and %s at %g,%g"
                            % (r, n, seen[(x, y)], x, y))
        seen[(x, y)] = "%s.%s" % (r, n)

    for r, n, x, y in pads:
        if not (4 <= x <= BW - 4 and 4 <= y <= BH - 4):
            problems.append("pad %s.%s outside the board at %g,%g" % (r, n, x, y))
    return problems


issues = check()

# ==========================================================================
#  output
# ==========================================================================
doc = {
    "head": {"docType": "3", "editorVersion": "6.5.46", "newgId": True,
             "c_para": {"Prefix Start": "1"}, "hasIdFlag": True,
             "importFlag": 0, "transformList": ""},
    "canvas": "CA~%g~%g~#000000~yes~#FFFFFF~10~%g~%g~line~1~mil~1~45~visible~0.5~0~0"
              % (BW * 2, BH * 2, BW * 2, BH * 2),
    "shape": shapes,
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
    "DRCRULE": {"trackWidth": 1.2, "track2Track": 1.0, "pad2Pad": 1.0,
                "track2Pad": 1.0, "hole2Hole": 1.2, "holeSize": 1.6,
                "Default": {"trackWidth": 1.2, "clearance": 1.0,
                            "viaHoleDiameter": 0.8, "viaDiameter": 2.4}},
    "netColors": {},
}

pcbdir = os.path.join(ROOT, "pcb")
os.makedirs(pcbdir, exist_ok=True)
with open(os.path.join(pcbdir, SLUG + "-pcb.json"), "w") as f:
    json.dump(doc, f, indent=1)


# --- SVG preview (top view, silk + pads + outline) ------------------------
def preview():
    sc = 3.2
    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%g" height="%g" '
         'viewBox="0 0 %g %g">' % (BW * sc, BH * sc, BW, BH),
         '<rect width="%g" height="%g" fill="#0b3d2e"/>' % (BW, BH)]
    for s in shapes:
        f = s.split("~")
        if f[0] == "TRACK":
            o.append('<polyline points="%s" fill="none" stroke="%s" '
                     'stroke-width="%s"/>'
                     % (f[4], "#f2f0e6" if f[2] in ("3", "4") else "#ff00ff", f[1]))
        elif f[0] == "PAD":
            x, y, w, h = float(f[2]), float(f[3]), float(f[4]), float(f[5])
            if f[1] == "RECT":
                o.append('<rect x="%g" y="%g" width="%g" height="%g" fill="#d8a13a"/>'
                         % (x - w / 2, y - h / 2, w, h))
            else:
                o.append('<ellipse cx="%g" cy="%g" rx="%g" ry="%g" fill="#d8a13a"/>'
                         % (x, y, w / 2, h / 2))
            o.append('<circle cx="%g" cy="%g" r="%s" fill="#0b3d2e"/>' % (x, y, f[9]))
        elif f[0] == "HOLE":
            o.append('<circle cx="%s" cy="%s" r="%g" fill="#0b3d2e" '
                     'stroke="#f2f0e6" stroke-width="0.5"/>'
                     % (f[1], f[2], float(f[3]) / 2))
        elif f[0] == "TEXT":
            o.append('<text x="%s" y="%s" font-family="DejaVu Sans" font-size="%s" '
                     'fill="#f2f0e6">%s</text>' % (f[2], f[3], f[9], f[10]))
    o.append("</svg>")
    return "\n".join(o)


svg = preview()
with open(os.path.join(pcbdir, SLUG + "-pcb.svg"), "w") as f:
    f.write(svg)
try:
    import cairosvg
    cairosvg.svg2png(bytestring=svg.encode(),
                     write_to=os.path.join(pcbdir, SLUG + "-pcb.png"),
                     output_width=int(BW * 3.2), output_height=int(BH * 3.2))
except Exception as exc:      # pragma: no cover
    print("PNG preview skipped:", exc)

print("board %.1f x %.1f mm, %d footprints, %d pads, %d nets"
      % (BW / MM, BH / MM, len(placed), len(pads), len(net["nets"])))
print("ratsnest (excl. poured GND): %.0f mm grid placement -> %.0f mm optimised"
      % (RATS_BEFORE / MM, RATS_AFTER / MM))
if issues:
    print("\nPROBLEMS:")
    for p in issues:
        print("  -", p)
else:
    print("checks pass: every schematic pin has a netted pad, no courtyard "
          "overlaps, no coincident pads, all pads inside the outline")
