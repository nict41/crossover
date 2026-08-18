#!/usr/bin/env python3
"""
Generate an EasyEDA (Std) importable schematic for the ESP Project 148
3-way State Variable Electronic Crossover.

Reference: https://sound-au.com/project148.htm  (Rod Elliott, ESP)

Outputs (into ../schematic and ../docs relative to this file):
  esp-p148-3way-state-variable-crossover.json        EasyEDA Std schematic (v6 head)
  esp-p148-3way-state-variable-crossover.legacy.json same shapes, legacy string head
  preview.svg / preview.png                          rendering of the same shape data
  netlist.txt                                        netlist extracted back out of the geometry

The schematic is described declaratively (components + orthogonal wire runs on a
10 px grid).  Junction dots are derived automatically, and the netlist is
re-extracted from the finished geometry so the drawing can be checked against the
intended connections rather than trusted blindly.
"""

import json
import os
import re

# --------------------------------------------------------------------------
# id allocation
# --------------------------------------------------------------------------
_next_id = [1]


def gid():
    _next_id[0] += 1
    return "gge%d" % _next_id[0]


WIRE_COLOR = "#008800"
PIN_COLOR = "#880000"
SYM_COLOR = "#000080"
TXT_COLOR = "#000000"

shapes = []          # EasyEDA shape strings
pins = []            # (refdes, pinnum, x, y)
netlabels = []       # (name, x, y)
wires = []           # list of point lists [(x, y), ...]


# --------------------------------------------------------------------------
# Variants.  Only the frequency-setting network differs: the pot gang in series
# with rs sets R, and c is the integrator capacitor, so f = 1 / (2*pi*R*C) with
# R sweeping from rs to rs + 20k.  Q, topology and every other value are shared.
# --------------------------------------------------------------------------
DUAL_ICS = {
    # role:   (refdes, top pin, bottom pin, out pin, power pins)
    "buf":  ("U1A", (3, "+"), (2, "-"), 1, (8, 4)),
    "sum1": ("U1B", (6, "-"), (5, "+"), 7, None),
    "int1": ("U2A", (2, "-"), (3, "+"), 1, (8, 4)),
    "int2": ("U2B", (6, "-"), (5, "+"), 7, None),
    "sum2": ("U3A", (2, "-"), (3, "+"), 1, (8, 4)),
    "int3": ("U3B", (6, "-"), (5, "+"), 7, None),
    "int4": ("U4A", (2, "-"), (3, "+"), 1, (8, 4)),
    "inv":  ("U4B", (6, "-"), (5, "+"), 7, None),
}

# Standard 14-pin quad pinout (TL074 / MC33079 / OPA4134 / LME49740):
#   A out 1, -in 2, +in 3 | V+ 4 | B +in 5, -in 6, out 7
#   C out 8, -in 9, +in 10 | V- 11 | D +in 12, -in 13, out 14
# Filter 1 fills U1 exactly; filter 2 fills U2 exactly.
QUAD_ICS = {
    "buf":  ("U1A", (3, "+"), (2, "-"), 1, (4, 11)),
    "sum1": ("U1B", (6, "-"), (5, "+"), 7, None),
    "int1": ("U1C", (9, "-"), (10, "+"), 8, None),
    "int2": ("U1D", (13, "-"), (12, "+"), 14, None),
    "sum2": ("U2A", (2, "-"), (3, "+"), 1, (4, 11)),
    "int3": ("U2B", (6, "-"), (5, "+"), 7, None),
    "int4": ("U2C", (9, "-"), (10, "+"), 8, None),
    "inv":  ("U2D", (13, "-"), (12, "+"), 14, None),
}

PIN_ROLE = {}      # (refdes, pin) -> "role.function", for cross-variant checks

VARIANTS = [
    dict(slug="esp-p148-3way-state-variable-crossover",
         title="3-Way State Variable Electronic Crossover  -  ESP Project 148",
         rs1="3.3k", c1="10nF", range1="680 Hz - 4.8 kHz",
         rs2="3.3k", c2="100nF", range2="68 Hz - 480 Hz", rq="12k",
         ics=DUAL_ICS, model="NE5532", pkg="DIP-8", pwr="pins 8 / 4",
         packages=["U1", "U2", "U3", "U4"], caps={"C1": ["C1"], "C2": ["C2"], "C3": ["C3"], "C4": ["C4"]}),
    dict(slug="esp-p148-3way-crossover-retuned-200hz-1khz",
         title="3-Way State Variable Electronic Crossover  -  ESP P148, retuned "
               "(195 Hz - 1.03 kHz / 71 Hz - 180 Hz)",
         rs1="4.7k", c1="33nF", range1="195 Hz - 1.03 kHz",
         rs2="13k", c2="68nF", range2="71 Hz - 180 Hz", rq="11k",
         ics=DUAL_ICS, model="NE5532", pkg="DIP-8", pwr="pins 8 / 4",
         packages=["U1", "U2", "U3", "U4"], caps={"C1": ["C1"], "C2": ["C2"], "C3": ["C3"], "C4": ["C4"]}),
    dict(slug="esp-p148-3way-crossover-retuned-quad",
         title="3-Way State Variable Electronic Crossover  -  ESP P148, "
               "retuned, two quad op-amps",
         rs1="4.7k", c1="33nF", range1="195 Hz - 1.03 kHz",
         rs2="13k", c2="68nF", range2="71 Hz - 180 Hz", rq="11k",
         ics=QUAD_ICS, model="MC33079", pkg="DIP-14", pwr="pins 4 / 11",
         packages=["U1", "U2"], caps={"C1": ["C1"], "C2": ["C2"], "C3": ["C3"], "C4": ["C4"]}),
    dict(slug="esp-p148-3way-crossover-retuned-quad-smd",
         title="3-Way State Variable Electronic Crossover  -  ESP P148, "
               "retuned, two quad op-amps, SMD build for JLCPCB assembly",
         rs1="4.7k", c1="33nF", range1="195 Hz - 1.03 kHz",
         rs2="13k", c2="33nF", range2="73 Hz - 186 Hz  (C3, C4 = 2 x 33nF)", rq="11k",
         ics=QUAD_ICS, model="MC33079", pkg="SOIC-14", pwr="pins 4 / 11",
         packages=["U1", "U2"],
         caps={"C1": ["C1"], "C2": ["C2"],
               "C3": ["C3A", "C3B"], "C4": ["C4A", "C4B"]},
         volume_pots=True,
         # SMD variant only.  The through-hole variants are reference
         # builds with hand-placed boards; adding parts to them would
         # disturb those layouts for no benefit to the board actually being
         # manufactured.
         # Bulk decoupling is OFF by default.  It is real but modest, and
         # with both it and the output blocking caps fitted (54 footprints)
         # nothing routed across 320 seed/route-order combinations.  The
         # output caps protect your speakers from a failed op-amp; bulk
         # decoupling shifts an umbilical resonance that mostly wants
         # fixing at the supply end anyway - so when only one of them can
         # fit, it is this one that gives way.  BULK_CAPS=1 turns it on.
         # Both OFF by default, and both are correct circuit changes that
         # the PCB cannot currently take.  Measured, with determinism fixed:
         # 49 footprints route clean about 1 try in 80; add the three output
         # blocking caps (52) and it is 0 in 320; add bulk as well (54) and
         # it is 0 in 320 again.  Shrinking the output caps to a 1210
         # ceramic footprint - a third of the area - did not help either
         # (0 in 160), so it is the extra parts and nets themselves, not
         # their size.  See docs/design-review.md.
         bulk_caps=bool(os.environ.get("BULK_CAPS")),
         output_caps=bool(os.environ.get("OUTPUT_CAPS"))),
]

W_CANVAS, H_CANVAS = 2200, 1600


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
def T(mark, x, y, text, size="7pt", color=SYM_COLOR, anchor="start",
      weight="", style="", family="Verdana", visible=1):
    return "T~%s~%s~%s~0~%s~%s~%s~%s~%s~~comment~%s~%d~%s~%s" % (
        mark, x, y, color, family, size, weight, style, text, visible, anchor, gid())


def PL(points, color=SYM_COLOR, width=1, fill="none"):
    pts = " ".join("%s %s" % (p[0], p[1]) for p in points)
    return "PL~%s~%s~%d~0~%s~%s" % (pts, color, width, fill, gid())


def PG(points, color=SYM_COLOR, width=1, fill="none"):
    pts = " ".join("%s %s" % (p[0], p[1]) for p in points)
    return "PG~%s~%s~%d~0~%s~%s" % (pts, color, width, fill, gid())


def RECT(x, y, w, h, color=SYM_COLOR, width=1, fill="none"):
    return "R~%s~%s~~~%s~%s~%s~%d~0~%s~%s" % (x, y, w, h, color, width, fill, gid())


def PIN(num, x, y, path, rot=0, electric=0, name_at=None, num_at=None):
    """One EasyEDA pin.  (x, y) is the connection dot."""
    seg1 = "P~show~%d~%s~%s~%s~%s~%s" % (electric, num, x, y, rot, gid())
    seg2 = "%s~%s" % (x, y)
    seg3 = "%s~%s" % (path, PIN_COLOR)
    if name_at:
        # visible~x~y~rotation~text~anchor~fontFamily~fontSize
        seg4 = "1~%s~%s~0~%s~%s~~7pt" % (name_at[0], name_at[1], name_at[3], name_at[2])
    else:
        seg4 = "0~%s~%s~0~~start~~" % (x, y)
    if num_at:
        seg5 = "1~%s~%s~0~%s~%s~~7pt" % (num_at[0], num_at[1], num, num_at[2])
    else:
        seg5 = "0~%s~%s~0~%s~start~~" % (x, y, num)
    return "^^".join([seg1, seg2, seg3, seg4, seg5, "0~%s~%s" % (x, y), "0~"])


def LIB(x, y, attrs, sub):
    shapes.append("LIB~%s~%s~%s~~0~%s#@$%s" % (x, y, attrs, gid(), "#@$".join(sub)))


def attrs_of(pairs):
    return "".join("%s`%s`" % (k, v) for k, v in pairs)


def add_pin(ref, num, x, y):
    pins.append((ref, str(num), x, y))


# --------------------------------------------------------------------------
# component symbols
# --------------------------------------------------------------------------
def resistor(ref, value, x, y, vertical=False, label_side=1, above=False,
             package="R_AXIAL-0.4"):
    a = attrs_of([("package", package), ("nameAlias", "Value"),
                  ("Value", value), ("spicePre", "R"), ("spiceSymbolName", "Resistor")])
    sub = []
    if not vertical:
        sub.append(RECT(x - 20, y - 6, 40, 12))
        sub.append(PIN(1, x - 30, y, "M %d %d h 10" % (x - 30, y), 180))
        sub.append(PIN(2, x + 30, y, "M %d %d h -10" % (x + 30, y), 0))
        dy_ref, dy_val = (-24, -12) if above else (-12, 22)
        sub.append(T("P", x, y + dy_ref, ref, anchor="middle"))
        sub.append(T("N", x, y + dy_val, value, anchor="middle"))
        add_pin(ref, 1, x - 30, y)
        add_pin(ref, 2, x + 30, y)
    else:
        sub.append(RECT(x - 6, y - 20, 12, 40))
        sub.append(PIN(1, x, y - 30, "M %d %d v 10" % (x, y - 30), 270))
        sub.append(PIN(2, x, y + 30, "M %d %d v -10" % (x, y + 30), 90))
        sub.append(T("P", x + 12 * label_side, y - 2, ref,
                     anchor="start" if label_side > 0 else "end"))
        sub.append(T("N", x + 12 * label_side, y + 12, value,
                     anchor="start" if label_side > 0 else "end"))
        add_pin(ref, 1, x, y - 30)
        add_pin(ref, 2, x, y + 30)
    LIB(x, y, a, sub)


def capacitor(ref, value, x, y, vertical=False, polar=False, label_side=1,
              above=False, package="C_AXIAL-0.2"):
    a = attrs_of([("package", package), ("nameAlias", "Value"),
                  ("Value", value), ("spicePre", "C"), ("spiceSymbolName", "Capacitor")])
    sub = []
    if not vertical:
        sub.append(PL([(x - 4, y - 12), (x - 4, y + 12)], width=2))
        sub.append(PL([(x + 4, y - 12), (x + 4, y + 12)], width=2))
        sub.append(PIN(1, x - 30, y, "M %d %d h 26" % (x - 30, y), 180))
        sub.append(PIN(2, x + 30, y, "M %d %d h -26" % (x + 30, y), 0))
        dy_ref, dy_val = (-32, -20) if above else (-20, 30)
        sub.append(T("P", x, y + dy_ref, ref, anchor="middle"))
        sub.append(T("N", x, y + dy_val, value, anchor="middle"))
        if polar:
            sub.append(T("L", x - 20, y - 10, "+", anchor="middle", color=SYM_COLOR))
        add_pin(ref, 1, x - 30, y)
        add_pin(ref, 2, x + 30, y)
    else:
        sub.append(PL([(x - 12, y - 4), (x + 12, y - 4)], width=2))
        sub.append(PL([(x - 12, y + 4), (x + 12, y + 4)], width=2))
        sub.append(PIN(1, x, y - 30, "M %d %d v 26" % (x, y - 30), 270))
        sub.append(PIN(2, x, y + 30, "M %d %d v -26" % (x, y + 30), 90))
        sub.append(T("P", x + 18 * label_side, y - 2, ref,
                     anchor="start" if label_side > 0 else "end"))
        sub.append(T("N", x + 18 * label_side, y + 12, value,
                     anchor="start" if label_side > 0 else "end"))
        if polar:
            sub.append(T("L", x - 10, y - 12, "+", anchor="middle"))
        add_pin(ref, 1, x, y - 30)
        add_pin(ref, 2, x, y + 30)
    LIB(x, y, a, sub)


def pot(ref, value, x, y, package="POT-ALPHA-16MM"):
    """Horizontal potentiometer: 1 = left end, 2 = wiper (below), 3 = right end."""
    a = attrs_of([("package", package), ("nameAlias", "Value"),
                  ("Value", value), ("spicePre", "R"),
                  ("spiceSymbolName", "Potentiometer")])
    sub = [RECT(x - 20, y - 6, 40, 12),
           PIN(1, x - 30, y, "M %d %d h 10" % (x - 30, y), 180),
           PIN(3, x + 30, y, "M %d %d h -10" % (x + 30, y), 0),
           PIN(2, x, y + 30, "M %d %d v -12" % (x, y + 30), 90),
           PG([(x - 6, y + 18), (x + 6, y + 18), (x, y + 8)], fill=SYM_COLOR),
           T("P", x, y - 14, ref, anchor="middle"),
           T("N", x, y - 24, value, anchor="middle")]
    add_pin(ref, 1, x - 30, y)
    add_pin(ref, 3, x + 30, y)
    add_pin(ref, 2, x, y + 30)
    LIB(x, y, a, sub)


def opamp(ref, x0, cy, top_pin, bot_pin, out_pin, power=None, model="NE5532",
          package="DIP-8"):
    """Triangle op-amp section.  top_pin/bot_pin are (number, '-' | '+')."""
    a = attrs_of([("package", package), ("nameAlias", "Model"),
                  ("Model", model), ("spicePre", "U"), ("spiceSymbolName", "Opamp")])
    sub = [PG([(x0, cy - 40), (x0, cy + 40), (x0 + 70, cy)])]

    tn, ts = top_pin
    bn, bs = bot_pin
    sub.append(PIN(tn, x0 - 20, cy - 20, "M %d %d h 20" % (x0 - 20, cy - 20), 180,
                   num_at=(x0 - 22, cy - 24, "end")))
    sub.append(PIN(bn, x0 - 20, cy + 20, "M %d %d h 20" % (x0 - 20, cy + 20), 180,
                   num_at=(x0 - 22, cy + 32, "end")))
    sub.append(PIN(out_pin, x0 + 90, cy, "M %d %d h -20" % (x0 + 90, cy), 0,
                   num_at=(x0 + 74, cy - 6, "start")))
    sub.append(T("L", x0 + 8, cy - 14, "−" if ts == "-" else "+",
                 size="9pt", anchor="middle"))
    sub.append(T("L", x0 + 8, cy + 22, "−" if bs == "-" else "+",
                 size="9pt", anchor="middle"))
    add_pin(ref, tn, x0 - 20, cy - 20)
    add_pin(ref, bn, x0 - 20, cy + 20)
    add_pin(ref, out_pin, x0 + 90, cy)

    if power:
        vp, vn = power
        sub.append(PIN(vp, x0 + 20, cy - 60, "M %d %d v 30" % (x0 + 20, cy - 60),
                       270, electric=4, num_at=(x0 + 24, cy - 44, "start")))
        sub.append(PIN(vn, x0 + 20, cy + 60, "M %d %d v -30" % (x0 + 20, cy + 60),
                       90, electric=4, num_at=(x0 + 24, cy + 52, "start")))
        add_pin(ref, vp, x0 + 20, cy - 60)
        add_pin(ref, vn, x0 + 20, cy + 60)

    sub.append(T("P", x0 + 32, cy - 48, ref, anchor="start"))
    sub.append(T("N", x0 + 14, cy + 6, model, anchor="start"))
    LIB(x0 + 35, cy, a, sub)


def header(ref, value, x, y, labels, package="HDR-1X3"):
    """Vertical pin header, pins exit to the right on 30 px pitch."""
    n = len(labels)
    h = 30 * n + 10
    a = attrs_of([("package", package), ("nameAlias", "Value"),
                  ("Value", value), ("spicePre", "J"), ("spiceSymbolName", "Header")])
    sub = [RECT(x - 50, y - 5, 50, h)]
    for i, lab in enumerate(labels):
        py = y + 10 + 30 * i
        sub.append(PIN(i + 1, x + 20, py, "M %d %d h -20" % (x + 20, py), 0,
                       name_at=(x - 45, py + 3, "start", lab),
                       num_at=(x + 4, py - 4, "start")))
        add_pin(ref, i + 1, x + 20, py)
    sub.append(T("P", x - 50, y - 12, ref, anchor="start"))
    sub.append(T("N", x - 10, y - 12, value, anchor="start"))
    LIB(x, y, a, sub)


# --------------------------------------------------------------------------
# wires / labels / flags
# --------------------------------------------------------------------------
def w(*points):
    wires.append(list(points))


def netlabel(name, x, y, anchor="middle", dx=0, dy=-8):
    netlabels.append((name, x, y))
    shapes.append("N~%s~%s~0~%s~%s~%s~%s~%s~%s~Verdana~7pt" %
                  (x, y, "#0000FF", name, gid(), anchor, x + dx, y + dy))


def gnd(x, y):
    """GND net flag; the symbol is drawn downwards from the connection dot."""
    netlabels.append(("GND", x, y))
    dx, dy = x - 330, y - 110
    body = []
    for pts in [[(330, 120), (330, 110)], [(320, 120), (339, 120)],
                [(324, 122), (337, 122)], [(326, 124), (333, 124)],
                [(329, 126), (331, 126)]]:
        body.append("PL~%s~#000000~1~0~none~%s" %
                    (" ".join("%d %d" % (px + dx, py + dy) for px, py in pts), gid()))
    shapes.append("F~part_netLabel_gnD~%s~%s~~%s^^%s~%s^^GND~#000080~%s~%s~0~start~0~"
                  "Verdana~7pt^^%s" % (x, y, gid(), x, y, x - 11, y - 13, "^^".join(body)))


def note(x, y, text, size="9pt", weight="", anchor="start", color="#0000A0"):
    shapes.append(T("L", x, y, text, size=size, color=color, anchor=anchor,
                    weight=weight))


# ==========================================================================
#  T H E   S C H E M A T I C
# ==========================================================================
def amp(cfg, role, x0, cy):
    """Place one op-amp section by its role, with supply labels if it carries them."""
    ref, top, bot, outp, pwr = cfg["ics"][role]
    opamp(ref, x0, cy, top, bot, outp, power=pwr, model=cfg["model"],
          package=cfg["pkg"])
    PIN_ROLE[(ref, str(top[0]))] = "%s.in%s" % (role, top[1])
    PIN_ROLE[(ref, str(bot[0]))] = "%s.in%s" % (role, bot[1])
    PIN_ROLE[(ref, str(outp))] = "%s.out" % role
    if pwr:
        PIN_ROLE[(ref, str(pwr[0]))] = "%s.V+" % role
        PIN_ROLE[(ref, str(pwr[1]))] = "%s.V-" % role
        netlabel("+15V", x0 + 20, cy - 60, anchor="middle", dy=-8)
        netlabel("-15V", x0 + 20, cy + 60, anchor="middle", dy=16)


def cap_bank(cfg, slot, x, y):
    """The integrator capacitor: one part, or two in parallel for the SMD build."""
    refs = cfg["caps"][slot]
    value = cfg["c1"] if slot in ("C1", "C2") else cfg["c2"]
    capacitor(refs[0], value, x, y, above=True)
    for k, ref in enumerate(refs[1:], start=1):
        capacitor(ref, value, x, y + 40 * k, above=True)
        w((x - 30, y + 40 * (k - 1)), (x - 30, y + 40 * k))
        w((x + 30, y + 40 * (k - 1)), (x + 30, y + 40 * k))


def draw(cfg):
    """Place every component and wire for one variant of the crossover."""
    OY = 600        # vertical offset of the second (lower frequency) filter
    # Variants with a volume_pots flag get a per-output attenuator between the
    # filter and its terminal block, so the raw filter output needs a net name
    # distinct from the one the terminal block (and everything downstream)
    # still expects - the pot bridges the two, see the OUTPUT VOLUME CONTROLS
    # section below.
    vsuf = "_PRE" if cfg.get("volume_pots") else ""

    # ---------------- Filter 1: 680 Hz - 4.8 kHz (High / Mid crossover) -------
    netlabel("INPUT", 60, 280, anchor="end", dx=-4, dy=3)
    capacitor("C0", "10uF", 130, 280, polar=True)
    w((60, 280), (100, 280))
    w((160, 280), (240, 280))                       # C0 -> U1A pin 3

    resistor("R1", "10k", 200, 350, vertical=True)
    w((200, 280), (200, 320))
    w((200, 380), (200, 400))
    gnd(200, 400)

    amp(cfg, "buf", 260, 300)
    w((350, 300), (390, 300))
    w((390, 300), (390, 400), (240, 400), (240, 320))       # unity-gain feedback
    w((390, 300), (390, 200), (430, 200))                   # buffer out -> R2

    resistor("R2", "5.6k", 460, 200)
    w((490, 200), (530, 200))

    # summing amplifier U1B
    amp(cfg, "sum1", 590, 300)
    w((530, 100), (530, 280), (570, 280))           # SUM1N spine
    resistor("R6", "5.6k", 600, 100, above=True)                # feedback from 2nd integrator (LP1)
    resistor("R5", "5.6k", 600, 140)                # local negative feedback (HP1)
    w((530, 100), (570, 100))
    w((530, 140), (570, 140))
    w((630, 100), (1580, 100))                      # LP1 rail
    w((630, 140), (1680, 140))                      # HP1 rail
    w((680, 300), (720, 300), (720, 140))           # U1B out -> HP1 rail

    w((570, 320), (490, 320), (490, 460))           # SUM1P spine
    resistor("R4", "5.6k", 560, 460)                # feedback from 1st integrator (BP1)
    w((490, 460), (530, 460))
    resistor("R3", cfg["rq"], 490, 530, vertical=True)  # sets Q
    w((490, 460), (490, 500))
    w((490, 560), (490, 580))
    gnd(490, 580)
    note(450, 535, "Set Q", anchor="end")

    # first integrator U2A
    pot("VR1A", "20k", 800, 300)
    w((720, 300), (770, 300))
    w((800, 330), (800, 360), (870, 360), (870, 300))
    w((830, 300), (870, 300))
    w((870, 300), (890, 300))
    resistor("R7", cfg["rs1"], 920, 300)
    amp(cfg, "int1", 1010, 300)
    w((950, 300), (950, 280), (990, 280))
    w((990, 320), (990, 360))
    gnd(990, 360)
    cap_bank(cfg, "C1", 1040, 190)
    w((990, 280), (990, 190), (1010, 190))
    w((1100, 300), (1130, 300))
    w((1130, 300), (1130, 190), (1070, 190))
    w((1130, 300), (1180, 300))
    w((1180, 300), (1180, 460), (590, 460))         # BP1 rail back to R4

    # second integrator U2B
    pot("VR1B", "20k", 1250, 300)
    w((1180, 300), (1220, 300))
    w((1250, 330), (1250, 360), (1320, 360), (1320, 300))
    w((1280, 300), (1320, 300))
    w((1320, 300), (1340, 300))
    resistor("R8", cfg["rs1"], 1370, 300)
    amp(cfg, "int2", 1460, 300)
    w((1400, 300), (1400, 280), (1440, 280))
    w((1440, 320), (1440, 360))
    gnd(1440, 360)
    cap_bank(cfg, "C2", 1490, 190)
    w((1440, 280), (1440, 190), (1460, 190))
    w((1550, 300), (1580, 300))
    w((1520, 190), (1580, 190))
    w((1580, 40), (1580, 340))                      # LP1 spine
    netlabel("LP1", 1580, 340, anchor="middle", dy=16)

    # TP1 null network and High output
    resistor("R11", "10k", 1630, 40)
    w((1580, 40), (1600, 40))
    w((1660, 40), (1680, 40))
    resistor("R10", "10k", 1680, 110, vertical=True)
    w((1680, 80), (1680, 40))
    w((1680, 40), (1680, 20))
    netlabel("TP1", 1680, 20, anchor="middle", dy=-8)
    w((1680, 140), (1720, 140))
    resistor("R9", "100R", 1750, 140)
    w((1780, 140), (1820, 140))
    netlabel("HIGH" + vsuf, 1820, 140, anchor="start", dx=4, dy=3)

    note(1000, 85, "%s  (VR1 sets the High / Mid crossover point)" % cfg["range1"],
         anchor="middle")
    note(60, 60, "FILTER 1 - state variable, Q = 0.5 (Linkwitz-Riley)", weight="bold")

    # ---------------- Filter 2: 68 Hz - 480 Hz (Mid / Low crossover) ----------
    netlabel("LP1", 370, 200 + OY, anchor="end", dx=-4, dy=3)
    resistor("R15", "5.6k", 460, 200 + OY)
    w((370, 200 + OY), (430, 200 + OY))
    w((490, 200 + OY), (530, 200 + OY))

    amp(cfg, "sum2", 590, 300 + OY)
    w((530, 100 + OY), (530, 280 + OY), (570, 280 + OY))
    resistor("R12", "5.6k", 600, 100 + OY, above=True)          # feedback from 2nd integrator (LP2)
    resistor("R16", "5.6k", 600, 140 + OY)          # local negative feedback (HP2)
    w((530, 100 + OY), (570, 100 + OY))
    w((530, 140 + OY), (570, 140 + OY))
    w((630, 100 + OY), (1580, 100 + OY))            # LP2 rail
    w((630, 140 + OY), (1680, 140 + OY))            # HP2 rail
    w((680, 300 + OY), (720, 300 + OY), (720, 140 + OY))

    w((570, 320 + OY), (490, 320 + OY), (490, 460 + OY))
    resistor("R14", "5.6k", 560, 460 + OY)          # feedback from 1st integrator (BP2)
    w((490, 460 + OY), (530, 460 + OY))
    resistor("R13", cfg["rq"], 490, 530 + OY, vertical=True)
    w((490, 460 + OY), (490, 500 + OY))
    w((490, 560 + OY), (490, 580 + OY))
    gnd(490, 580 + OY)
    note(450, 535 + OY, "Set Q", anchor="end")

    pot("VR2A", "20k", 800, 300 + OY)
    w((720, 300 + OY), (770, 300 + OY))
    w((800, 330 + OY), (800, 360 + OY), (870, 360 + OY), (870, 300 + OY))
    w((830, 300 + OY), (870, 300 + OY))
    w((870, 300 + OY), (890, 300 + OY))
    resistor("R17", cfg["rs2"], 920, 300 + OY)
    amp(cfg, "int3", 1010, 300 + OY)
    w((950, 300 + OY), (950, 280 + OY), (990, 280 + OY))
    w((990, 320 + OY), (990, 360 + OY))
    gnd(990, 360 + OY)
    cap_bank(cfg, "C3", 1040, 190 + OY)
    w((990, 280 + OY), (990, 190 + OY), (1010, 190 + OY))
    w((1100, 300 + OY), (1130, 300 + OY))
    w((1130, 300 + OY), (1130, 190 + OY), (1070, 190 + OY))
    w((1130, 300 + OY), (1180, 300 + OY))
    w((1180, 300 + OY), (1180, 460 + OY), (590, 460 + OY))

    pot("VR2B", "20k", 1250, 300 + OY)
    w((1180, 300 + OY), (1220, 300 + OY))
    w((1250, 330 + OY), (1250, 360 + OY), (1320, 360 + OY), (1320, 300 + OY))
    w((1280, 300 + OY), (1320, 300 + OY))
    w((1320, 300 + OY), (1340, 300 + OY))
    resistor("R18", cfg["rs2"], 1370, 300 + OY)
    amp(cfg, "int4", 1460, 300 + OY)
    w((1400, 300 + OY), (1400, 280 + OY), (1440, 280 + OY))
    w((1440, 320 + OY), (1440, 360 + OY))
    gnd(1440, 360 + OY)
    cap_bank(cfg, "C4", 1490, 190 + OY)
    w((1440, 280 + OY), (1440, 190 + OY), (1460, 190 + OY))
    w((1550, 300 + OY), (1580, 300 + OY))
    w((1520, 190 + OY), (1580, 190 + OY))
    w((1580, 40 + OY), (1580, 300 + OY))            # LP2 spine

    # TP2 null network and Mid output
    resistor("R24", "10k", 1630, 40 + OY)
    w((1580, 40 + OY), (1600, 40 + OY))
    w((1660, 40 + OY), (1680, 40 + OY))
    resistor("R23", "10k", 1680, 110 + OY, vertical=True)
    w((1680, 80 + OY), (1680, 40 + OY))
    w((1680, 40 + OY), (1680, 20 + OY))
    netlabel("TP2", 1680, 20 + OY, anchor="middle", dy=-8)
    w((1680, 140 + OY), (1720, 140 + OY))
    resistor("R19", "100R", 1750, 140 + OY)
    w((1780, 140 + OY), (1820, 140 + OY))
    netlabel("MID" + vsuf, 1820, 140 + OY, anchor="start", dx=4, dy=3)

    # output inverter U4B (bass output; U3A already inverts the midrange)
    w((1580, 300 + OY), (1620, 300 + OY))
    resistor("R20", "5.6k", 1650, 300 + OY)
    w((1680, 300 + OY), (1720, 300 + OY), (1720, 280 + OY), (1740, 280 + OY))
    amp(cfg, "inv", 1760, 300 + OY)
    w((1740, 320 + OY), (1740, 360 + OY))
    gnd(1740, 360 + OY)
    resistor("R21", "5.6k", 1790, 200 + OY)
    w((1740, 280 + OY), (1740, 200 + OY), (1760, 200 + OY))
    w((1850, 300 + OY), (1880, 300 + OY))
    w((1880, 300 + OY), (1880, 200 + OY), (1820, 200 + OY))
    w((1880, 300 + OY), (1920, 300 + OY))
    resistor("R22", "100R", 1950, 300 + OY)
    w((1980, 300 + OY), (2020, 300 + OY))
    netlabel("LOW" + vsuf, 2020, 300 + OY, anchor="start", dx=4, dy=3)

    note(1000, 85 + OY, "%s  (VR2 sets the Mid / Low crossover point)" % cfg["range2"],
         anchor="middle")
    note(60, 60 + OY, "FILTER 2 - state variable, Q = 0.5 (Linkwitz-Riley)", weight="bold")

    # ---------------- power input and supply bypassing ------------------------
    header("J1", "PWR", 110, 1280, ["+15V", "GND", "-15V"],
       package="TB-3P-5.08")
    w((130, 1290), (180, 1290))
    netlabel("+15V", 180, 1290, anchor="start", dx=4, dy=3)
    w((130, 1320), (180, 1320))
    gnd(180, 1320)
    w((130, 1350), (180, 1350))
    netlabel("-15V", 180, 1350, anchor="start", dx=4, dy=3)

    n_pkg = len(cfg["packages"])
    if cfg.get("bulk_caps"):
        # The board's only other supply decoupling is 100 nF per rail per
        # IC, and the rails arrive over an umbilical from an off-board
        # supply.  Wiring runs about 1 uH per metre, which resonates with
        # ceramics alone somewhere in the low hundreds of kHz - exactly
        # where an op-amp's supply rejection has fallen away.  A bulk
        # electrolytic at the connector damps it.
        bulk = ["C%d" % (5 + 2 * n_pkg), "C%d" % (6 + 2 * n_pkg)]
        bkx = 250
        netlabel("+15V", bkx, 1240, anchor="middle", dy=-14)
        w((bkx, 1240), (bkx, 1260))
        capacitor(bulk[0], "10uF", bkx, 1290, vertical=True, polar=True,
                  label_side=-1, package="CASE-D5xL5.4")
        w((bkx, 1320), (bkx, 1350))
        w((bkx, 1350), (bkx + 40, 1350))
        gnd(bkx + 40, 1350)
        w((bkx, 1350), (bkx, 1380))
        capacitor(bulk[1], "10uF", bkx, 1410, vertical=True, polar=True,
                  label_side=-1, package="CASE-D5xL5.4")
        w((bkx, 1440), (bkx, 1460))
        netlabel("-15V", bkx, 1460, anchor="middle", dy=16)
        note(bkx, 1208, "BULK", size="7pt", anchor="middle")

    for i, ic in enumerate(cfg["packages"]):
        bx = 350 + i * 170
        netlabel("+15V", bx, 1240, anchor="middle", dy=-14)
        w((bx, 1240), (bx, 1260))
        capacitor("C%d" % (5 + 2 * i), "100nF", bx, 1290, vertical=True)
        w((bx, 1320), (bx, 1350))
        w((bx, 1350), (bx + 50, 1350))
        gnd(bx + 50, 1350)
        w((bx, 1350), (bx, 1380))
        capacitor("C%d" % (6 + 2 * i), "100nF", bx, 1410, vertical=True)
        w((bx, 1440), (bx, 1460))
        netlabel("-15V", bx, 1460, anchor="middle", dy=16)
        note(bx, 1208, "%s  %s" % (ic, cfg["pwr"]), size="7pt",
             anchor="middle")

    # ---------------- signal connectors and test points -------------------
    header("J2", "IN", 1050, 1250, ["IN", "GND"], package="TB-2P-5.08")
    w((1070, 1260), (1130, 1260))
    netlabel("INPUT", 1130, 1260, anchor="start", dx=4, dy=3)
    w((1070, 1290), (1130, 1290))
    gnd(1130, 1290)

    # One 2-way terminal block per output: each amplifier gets its own signal
    # and its own ground return, rather than three signals sharing one ground.
    for k, (ref, nm) in enumerate([("J3", "HIGH"), ("J4", "MID"), ("J5", "LOW")]):
        oy = 1250 + 80 * k
        header(ref, nm, 1270, oy, [nm, "GND"], package="TB-2P-5.08")
        w((1290, oy + 10), (1350, oy + 10))
        netlabel(nm, 1350, oy + 10, anchor="start", dx=4, dy=3)
        w((1290, oy + 40), (1350, oy + 40))
        gnd(1350, oy + 40)

    for i, tp in enumerate(["TP1", "TP2"]):
        header(tp, "TP", 1510, 1250 + 80 * i, [tp], package="TESTPOINT")
        w((1530, 1260 + 80 * i), (1590, 1260 + 80 * i))
        netlabel(tp, 1590, 1260 + 80 * i, anchor="start", dx=4, dy=3)
    note(1050, 1215, "SIGNAL CONNECTORS AND TEST POINTS", weight="bold")

    # ---------------- per-output volume control -------------------------
    # Single-gang attenuator between each filter's output and its terminal
    # block: signal in (left, *_PRE), wiper out (bottom, feeds the terminal
    # block via the unchanged HIGH/MID/LOW net name), ground (right). Audio
    # taper, not linear - this drives the amp directly and gets ridden by
    # ear, not set once and forgotten like VR1/VR2's crossover point.
    if cfg.get("volume_pots"):
        vx = 1830
        # 110px pitch, not 80 like the terminal blocks: each pot's own
        # ref/value text (above) plus its wiper's net label (below) span
        # about 90px, so 80 let the wiper label collide with the next
        # pot's designator text - caught by looking at the render, not
        # by anything that would have failed a DRC-style check.
        for k, (ref, nm) in enumerate([("VR3", "HIGH"), ("VR4", "MID"), ("VR5", "LOW")]):
            oy = 1250 + 110 * k
            pot(ref, "10k log", vx, oy, package="RK097-AUDIO-10K")
            w((vx - 30, oy), (vx - 60, oy))
            netlabel(nm + "_PRE", vx - 60, oy, anchor="end", dx=-4, dy=3)
            w((vx + 30, oy), (vx + 60, oy))
            gnd(vx + 60, oy)
            if cfg.get("output_caps"):
                # Series DC blocking, on the WIPER side of the attenuator.
                # A failed op-amp stuck at a rail can no longer push DC into
                # a DC-coupled power amplifier and from there into a
                # speaker.  10 uF into a 10 k amplifier input puts the
                # corner near 1.3 Hz, far below anything the LOW band
                # carries.
                #
                # Between the filter and the pot would be the better place
                # electrically - it would also keep DC off the pot track,
                # which is what makes a volume control crackle as it wears -
                # but that puts the cap in the middle of a *_PRE net, and
                # those already cross most of the board.  Splitting them
                # cost the board its routability outright: 0 clean boards
                # out of 320 seed/route-order combinations, against 1 in 80
                # without.  Here the cap is local to the pot and terminal it
                # sits between.
                cap = "C%d" % (7 + 2 * n_pkg + k)
                w((vx, oy + 30), (vx, oy + 44))
                capacitor(cap, "10uF", vx, oy + 74, vertical=True, polar=True,
                          label_side=-1, package="CASE-D5xL5.4")
                w((vx, oy + 104), (vx, oy + 118))
                netlabel(nm, vx, oy + 118, anchor="middle", dy=16)
            else:
                w((vx, oy + 30), (vx, oy + 50))
                netlabel(nm, vx, oy + 50, anchor="middle", dy=16)
        note(vx - 30, 1215, "OUTPUT VOLUME (audio taper, wired as attenuator)",
             weight="bold")

    note(60, 1180, "SUPPLY BYPASSING - one 100nF ceramic per rail, at each IC",
         weight="bold")

    # ---------------- title block --------------------------------------------
    note(60, 30, cfg["title"], size="14pt", weight="bold", color="#000000")
    note(60, 1520, "After Rod Elliott, Elliott Sound Products, Project 148 "
         "(https://sound-au.com/project148.htm).  Redrawn for EasyEDA.",
         size="8pt", color="#404040")
    note(60, 1540, "All op-amps %s - %d x %s package%s.  Q = 0.5 Linkwitz-Riley "
         "with R3 / R13 = %s; use 11k2 for exact Q = 0.5, or 5k04 for "
         "Butterworth (Q = 0.707)."
         % (cfg["model"], len(cfg["packages"]), cfg["pkg"],
            "" if len(cfg["packages"]) == 1 else "s", cfg["rq"]),
         size="8pt", color="#404040")
    note(60, 1560, "VR1 and VR2 are dual-gang 20k linear pots wired as rheostats. "
         "TP1 / TP2 null at the crossover frequency and may be omitted."
         + (" VR3/VR4/VR5 are single-gang 10k audio-taper pots wired as "
            "attenuators - per-output volume, not part of the filter."
            if cfg.get("volume_pots") else ""),
         size="8pt", color="#404040")


# ==========================================================================
#  junctions, netlist extraction, output
# ==========================================================================
def split_wires():
    """Insert a real vertex wherever a wire end or pin dot lands mid-segment.

    EasyEDA joins a wire whose endpoint touches another wire, but the junction
    dot is only drawn where segments actually meet, so promote every such touch
    to a proper vertex first.
    """
    marks = set()
    for pts in wires:
        marks.add(pts[0])
        marks.add(pts[-1])
    for _, _, x, y in pins:
        marks.add((x, y))
    for wi, pts in enumerate(wires):
        out = [pts[0]]
        for a, b in zip(pts, pts[1:]):
            inner = [m for m in marks if on_segment(m, a, b)]
            if a[0] == b[0]:
                inner.sort(key=lambda p: p[1], reverse=b[1] < a[1])
            else:
                inner.sort(key=lambda p: p[0], reverse=b[0] < a[0])
            out.extend(inner)
            out.append(b)
        wires[wi] = out


def build_junctions():
    deg = {}
    for pts in wires:
        for i, p in enumerate(pts):
            n = 1 if i in (0, len(pts) - 1) else 2
            deg[p] = deg.get(p, 0) + n
    for ref, num, x, y in pins:
        deg[(x, y)] = deg.get((x, y), 0) + 1
    for name, x, y in netlabels:
        deg[(x, y)] = deg.get((x, y), 0) + 1
    js = []
    for p, d in sorted(deg.items()):
        if d >= 3:
            js.append(p)
    return js


class DSU(dict):
    def find(self, a):
        self.setdefault(a, a)
        while self[a] != a:
            self[a] = self[self[a]]
            a = self[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self[rb] = ra


def on_segment(p, a, b):
    if a == b:
        return False
    if a[0] == b[0] == p[0]:
        return min(a[1], b[1]) < p[1] < max(a[1], b[1])
    if a[1] == b[1] == p[1]:
        return min(a[0], b[0]) < p[0] < max(a[0], b[0])
    return False


def extract_netlist():
    dsu = DSU()
    for pts in wires:
        for p in pts[1:]:
            dsu.union(pts[0], p)
    segs = []
    for pts in wires:
        for a, b in zip(pts, pts[1:]):
            segs.append((a, b))
    verts = set()
    for pts in wires:
        verts.update(pts)
    touches = []
    for p in list(verts) + [(x, y) for _, _, x, y in pins]:
        for a, b in segs:
            if on_segment(p, a, b):
                dsu.union(p, a)
                touches.append((p, a, b))
    named = {}
    for name, x, y in netlabels:
        named.setdefault(dsu.find((x, y)), set()).add(name)
    # merge groups sharing a net label name
    byname = {}
    for root, names in named.items():
        for n in names:
            byname.setdefault(n, []).append(root)
    for n, roots in byname.items():
        for r in roots[1:]:
            dsu.union(roots[0], r)

    nets = {}
    for ref, num, x, y in pins:
        nets.setdefault(dsu.find((x, y)), []).append("%s.%s" % (ref, num))
    out = {}
    for root, members in nets.items():
        names = set()
        for name, x, y in netlabels:
            if dsu.find((x, y)) == root:
                names.add(name)
        label = "/".join(sorted(names)) if names else "N%s_%s" % root
        out[label] = sorted(members, key=lambda s: (re.sub(r"\d+$", "", s), s))
    return out, touches
# --------------------------------------------------------------------------
def svg_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_svg():
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="0 0 %d %d"><rect width="100%%" height="100%%" fill="#fff"/>'
           % (W_CANVAS, H_CANVAS, W_CANVAS, H_CANVAS)]

    def draw(s):
        for part in s.split("#@$"):
            cmd = part.split("~")[0]
            if cmd == "LIB":
                continue
            f = part.split("~")
            if cmd in ("PL", "PG", "W", "B"):
                pts = f[1]
                tag = "polygon" if cmd == "PG" else "polyline"
                out.append('<%s points="%s" stroke="%s" stroke-width="%s" '
                           'fill="%s"/>' % (tag, pts, f[2], f[3],
                                            "none" if f[5] == "none" else f[5]))
            elif cmd == "R":
                out.append('<rect x="%s" y="%s" width="%s" height="%s" stroke="%s" '
                           'stroke-width="%s" fill="%s"/>'
                           % (f[1], f[2], f[5], f[6], f[7], f[8],
                              "none" if f[10] == "none" else f[10]))
            elif cmd == "T":
                if f[13] == "0":
                    continue
                out.append('<text x="%s" y="%s" fill="%s" font-family="Verdana" '
                           'font-size="%s" font-weight="%s" text-anchor="%s">%s</text>'
                           % (f[2], f[3], f[5], f[7] or "7pt", f[8] or "normal",
                              f[14], svg_escape(f[12])))
            elif cmd == "J":
                out.append('<circle cx="%s" cy="%s" r="%s" fill="%s"/>'
                           % (f[1], f[2], f[3], f[4]))
            elif cmd == "N":
                out.append('<circle cx="%s" cy="%s" r="2" fill="%s"/>' % (f[1], f[2], f[4]))
                out.append('<text x="%s" y="%s" fill="%s" font-family="Verdana" '
                           'font-size="7pt" text-anchor="%s">%s</text>'
                           % (f[8], f[9], f[4], f[7], svg_escape(f[5])))
            elif cmd == "P":
                segs = part.split("^^")
                pf = segs[2].split("~")
                out.append('<path d="%s" stroke="%s" stroke-width="1" fill="none"/>'
                           % (pf[0], pf[1]))
                nf = segs[4].split("~")
                if nf[0] == "1":
                    out.append('<text x="%s" y="%s" fill="#000" font-family="Verdana" '
                               'font-size="6pt" text-anchor="%s">%s</text>'
                               % (nf[1], nf[2], nf[5], svg_escape(nf[4])))
                mf = segs[3].split("~")
                if mf[0] == "1":
                    out.append('<text x="%s" y="%s" fill="#666" font-family="Verdana" '
                               'font-size="6pt" text-anchor="%s">%s</text>'
                               % (mf[1], mf[2], mf[5], svg_escape(mf[4])))
            elif cmd == "F":
                segs = part.split("^^")
                mf = segs[2].split("~")
                if mf[5] == "1":
                    out.append('<text x="%s" y="%s" fill="%s" font-size="7pt" '
                               'font-family="Verdana">%s</text>'
                               % (mf[2], mf[3], mf[1], mf[0]))
                for sub in segs[3:]:
                    draw(sub)

    for s in shapes:
        draw(s)
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
def write_bom(filename="bom.csv"):
    seen = {}
    for s in shapes:
        if not s.startswith("LIB~"):
            continue
        attrs = s.split("~")[3]
        kv = attrs.split("`")
        d = dict(zip(kv[0::2], kv[1::2]))
        ref = val = ""
        for part in s.split("#@$"):
            if part.startswith("T~P~"):
                ref = part.split("~")[12]
            elif part.startswith("T~N~"):
                val = part.split("~")[12]
        seen[ref] = (val, d.get("package", ""))
    groups = {}
    for ref, (val, pkg) in seen.items():
        groups.setdefault((val, pkg), []).append(ref)

    def sort_key(r):
        m = re.match(r"([A-Za-z]+)(\d+)([A-Za-z]*)", r)
        return (m.group(1), int(m.group(2)), m.group(3)) if m else (r, 0, "")

    rows = []
    for (val, pkg), refs in groups.items():
        refs.sort(key=sort_key)
        rows.append((len(refs), val, pkg, ", ".join(refs)))
    rows.sort(key=lambda r: (r[2], sort_key(r[3].split(",")[0])))
    notes = {
        "NE5532": "4 x dual op-amp packages (U1-U4); each drawn as two sections",
        "MC33079": "2 x quad op-amp packages (U1 = filter 1, U2 = filter 2); "
                   "each drawn as four sections. Any standard quad pinout fits: "
                   "OPA1644, OPA4134, LME49740, TL074",
        "20k": "2 x dual-gang 20k linear pots (VR1, VR2); wired as rheostats",
        "12k": "Sets filter Q (0.489); 11k2 = exact Q 0.5, 5k04 = Butterworth",
        "11k": "Sets filter Q (0.503); 11k2 = exact Q 0.5, 5k04 = Butterworth",
        "100R": "Output series build-out resistors",
        "TP": "Test points - optional, omit with R10/R11 and R23/R24",
        "IN": "Signal input",
        "HIGH": "Output terminal block to the treble amplifier",
        "MID": "Output terminal block to the midrange amplifier",
        "LOW": "Output terminal block to the bass amplifier",
        "10k": "R10/R11/R23/R24 are the TP1/TP2 null network - optional",
        "10k log": "3 x single-gang 10k audio/log-taper pots (VR3 HIGH, VR4 MID, "
                   "VR5 LOW); wired as an attenuator between the filter output "
                   "and its terminal block, not as a rheostat",
    }
    out = ["Qty,Value,Package,Designators,Notes"]
    for qty, val, pkg, refs in rows:
        out.append('%d,%s,%s,"%s","%s"' % (qty, val, pkg, refs, notes.get(val, "")))
    bomdir = os.path.join(root, "bom")
    os.makedirs(bomdir, exist_ok=True)
    with open(os.path.join(bomdir, filename), "w") as f:
        f.write("\n".join(out) + "\n")
    return len(seen)



# ==========================================================================
#  driver
# ==========================================================================
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(here)
schdir = os.path.join(root, "schematic")
docdir = os.path.join(root, "docs")


def emit(cfg, primary):
    """Build one variant and write its schematic, preview, netlist and BOM."""
    global shapes, pins, netlabels, wires
    shapes, pins, netlabels, wires = [], [], [], []
    PIN_ROLE.clear()
    _next_id[0] = 1

    draw(cfg)
    split_wires()
    junctions = build_junctions()
    for jx, jy in junctions:
        shapes.append("J~%s~%s~2.5~#CC0000~%s" % (jx, jy, gid()))
    for pts in wires:
        shapes.append("W~%s~%s~1~0~none~%s" %
                      (" ".join("%d %d" % (x, y) for x, y in pts), WIRE_COLOR, gid()))

    canvas = ("CA~%d~%d~#FFFFFF~yes~#CCCCCC~10~%d~%d~line~10~pixel~5~0~0"
              % (W_CANVAS, H_CANVAS, W_CANVAS, H_CANVAS))
    doc_modern = {
        "head": {
            "docType": "1",
            "editorVersion": "6.5.46",
            "newgId": True,
            "c_para": {"Prefix Start": "1"},
            "c_spiceCmd": None,
            "hasIdFlag": True,
            "importFlag": 0,
            "transformList": "",
        },
        "canvas": canvas,
        "shape": shapes,
        "BBox": {"x": 0, "y": 0, "width": W_CANVAS, "height": H_CANVAS},
        "colors": {},
    }
    doc_legacy = dict(doc_modern, head="1~1.7.5~~")

    os.makedirs(schdir, exist_ok=True)
    os.makedirs(docdir, exist_ok=True)
    with open(os.path.join(schdir, cfg["slug"] + ".json"), "w") as f:
        json.dump(doc_modern, f, indent=1)
    with open(os.path.join(schdir, cfg["slug"] + ".legacy.json"), "w") as f:
        json.dump(doc_legacy, f, indent=1)

    svg = render_svg()
    with open(os.path.join(schdir, cfg["slug"] + ".svg"), "w") as f:
        f.write(svg)
    try:
        import cairosvg
        cairosvg.svg2png(bytestring=svg.encode(),
                         write_to=os.path.join(schdir, cfg["slug"] + ".png"),
                         output_width=W_CANVAS, output_height=H_CANVAS)
    except Exception as exc:   # pragma: no cover - preview is a convenience only
        print("PNG preview skipped:", exc)

    n_parts = write_bom("bom.csv" if primary else
                        "bom-%s.csv" % cfg["slug"].split("crossover-")[-1])

    nets, touches = extract_netlist()
    lines = ["Netlist extracted from the generated schematic geometry",
             "variant: %s" % cfg["slug"],
             "  High/Mid  %s   R7, R8 = %s   C1, C2 = %s" % (cfg["range1"], cfg["rs1"], cfg["c1"]),
             "  Mid/Low   %s   R17, R18 = %s   C3, C4 = %s" % (cfg["range2"], cfg["rs2"], cfg["c2"]),
             "=" * 56, ""]
    for name in sorted(nets):
        lines.append("%-10s %s" % (name, "  ".join(nets[name])))
    lines.append("")
    lines.append("%d nets, %d parts, %d component pins, %d junctions"
                 % (len(nets), n_parts, len(pins), len(junctions)))
    name = "netlist.txt" if primary else "netlist-%s.txt" % cfg["slug"].split("crossover-")[-1]
    with open(os.path.join(docdir, name), "w") as f:
        f.write("\n".join(lines) + "\n")

    # machine-readable form, consumed by tools/gen_pcb.py
    parts = {}
    for sh in shapes:
        if not sh.startswith("LIB~"):
            continue
        ref = val = pkg = ""
        for k, v in zip(sh.split("~")[3].split("`")[0::2],
                        sh.split("~")[3].split("`")[1::2]):
            if k == "package":
                pkg = v
        for part in sh.split("#@$"):
            if part.startswith("T~P~"):
                ref = part.split("~")[12]
            elif part.startswith("T~N~"):
                val = part.split("~")[12]
        parts[ref] = {"value": val, "package": pkg}
    with open(os.path.join(docdir, "netlist-%s.json" % cfg["slug"]), "w") as f:
        json.dump({"variant": cfg["slug"], "parts": parts,
                   "nets": {k: sorted(v) for k, v in nets.items()}}, f, indent=1)
    print("\n".join(lines))
    if touches:
        print("\nendpoint-on-wire touches (%d):" % len(touches))
        for t in touches:
            print("   ", t)
    return nets


def signal_map(nets, cfg):
    """{pin -> net} for every signal pin, with op-amp pins named by role.

    Supply pins and bypass caps are excluded: a quad-packaged build genuinely
    has fewer of both, while its signal connectivity must be identical.
    """
    # parallel parts (the SMD build's 2 x 33nF) fold onto their slot name:
    # they span the same two nets, so folding keeps the comparison exact
    fold = {r: slot for slot, refs in cfg["caps"].items() for r in refs}
    # The volume_pots variant tees each output through an attenuator that no
    # other variant has: VR3/4/5 don't exist elsewhere (not a divergence to
    # flag, just absent), and the filter side of the tee is named *_PRE
    # rather than HIGH/MID/LOW so the pot has two distinct nets to sit
    # between - normalise that back before comparing, since the filter
    # itself still lands on the exact same net every other variant does.
    volume_refs = {"VR3", "VR4", "VR5"} if cfg.get("volume_pots") else set()
    # Supply caps are excluded from the signal comparison; SIGNAL caps must
    # not be.  These used to be told apart by a regex on the designator
    # ("C5 and up"), which happened to work only because every cap above C4
    # was a bypass - the moment output DC blocking caps were added they
    # would have been silently excluded too, quietly weakening the one
    # check that guarantees the variants agree.  Now it is stated.
    n_pkg = len(cfg["packages"])
    supply_refs = {"C%d" % r for r in range(5, 5 + 2 * n_pkg)}
    if cfg.get("bulk_caps"):
        supply_refs |= {"C%d" % (5 + 2 * n_pkg), "C%d" % (6 + 2 * n_pkg)}
    # The output blocking caps sit in series in each output, so this
    # variant has one more net per output than the others.  Same situation
    # as the volume pots above: fold the cap out and treat both sides as
    # the one signal, which is what it is.
    outcap_refs = ({"C%d" % (7 + 2 * n_pkg + k) for k in range(3)}
                   if cfg.get("output_caps") else set())
    out = {}
    for name, members in nets.items():
        if name.endswith("_PRE"):
            name = name[:-len("_PRE")]
        for m in members:
            ref, _, num = m.rpartition(".")
            if ref in volume_refs or ref in outcap_refs:
                continue
            ref = fold.get(ref, ref)
            alias = PIN_ROLE.get((ref, num))
            if alias:
                if alias.endswith(".V+") or alias.endswith(".V-"):
                    continue
                out[alias] = name
            else:
                if ref in supply_refs:
                    continue
                out["%s.%s" % (ref, num)] = name
    return out


def check_power(nets, cfg):
    """Every package gets both rails and one bypass cap per rail, plus a
    bulk cap per rail where the variant has them."""
    n = len(cfg["packages"])
    want_caps = n + (1 if cfg.get("bulk_caps") else 0)
    for rail in ("+15V", "-15V"):
        caps = [m for m in nets[rail] if m.startswith("C")]
        amps = [m for m in nets[rail] if m.startswith("U")]
        if len(caps) != want_caps or len(amps) != n:
            raise SystemExit("%s: %s has %d caps and %d supply pins, "
                             "expected %d and %d" % (cfg["slug"], rail,
                                                     len(caps), len(amps),
                                                     want_caps, n))


def main():
    reference = None
    for i, cfg in enumerate(VARIANTS):
        nets = emit(cfg, primary=(i == 0))
        check_power(nets, cfg)
        key = signal_map(nets, cfg)
        if reference is None:
            reference = key
        elif key != reference:
            diff = [k for k in set(key) | set(reference)
                    if key.get(k) != reference.get(k)]
            raise SystemExit("variant %s changed signal connectivity at: %s"
                             % (cfg["slug"], sorted(diff)))
        print()
    print("all variants share identical signal connectivity; "
          "supply wiring checked per package")


main()
