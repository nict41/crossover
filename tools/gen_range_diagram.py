#!/usr/bin/env python3
"""
Draw the crossover tuning-range diagram for the two variants in this repo.

Two panels:
  A  the frequency range each pot can sweep, per variant, on a log axis
  B  what the summed response does as the two crossover points approach and
     cross over each other

Outputs docs/crossover-ranges.svg and .png.
"""

import math
import os

# --- palette (validated categorical slots 1 and 2, light surface) ---------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
S1 = "#2a78d6"          # VR1 - High/Mid
S2 = "#eb6834"          # VR2 - Mid/Low
WARN = "#fab219"        # status: caution (always with icon + label)
BORDER = "rgba(11,11,11,0.10)"
FONT = "DejaVu Sans, system-ui, sans-serif"

W, H = 1180, 800

VARIANTS = [
    ("Stock  ESP P148", [("VR1", "High / Mid", 683.0, 4820.0, S1),
                         ("VR2", "Mid / Low", 68.3, 482.0, S2)], None),
    ("Retuned  (this repo)", [("VR1", "High / Mid", 195.3, 1026.0, S1),
                              ("VR2", "Mid / Low", 60.7, 256.7, S2)], (195.3, 256.7)),
]

FMIN, FMAX = 30.0, 12000.0
PX0, PX1 = 250, 1110


def fx(f):
    return PX0 + (math.log10(f) - math.log10(FMIN)) / \
        (math.log10(FMAX) - math.log10(FMIN)) * (PX1 - PX0)


def hz(f):
    return "%.2f kHz" % (f / 1000) if f >= 1000 else "%.0f Hz" % f


# --- panel B data: what happens as the two crossover points converge ------
def outputs(f, f1, f2):
    s = 1j * 2 * math.pi * f
    T1, T2 = 1 / (2 * math.pi * f1), 1 / (2 * math.pi * f2)
    D1, D2 = (s * T1 + 1) ** 2, (s * T2 + 1) ** 2
    high = -(s * T1) ** 2 / D1
    lp1 = -1 / D1
    mid = (-(s * T2) ** 2 / D2) * lp1
    low = -((-1 / D2) * lp1)
    return high, mid, low


def db(x):
    return 20 * math.log10(abs(x)) if abs(x) > 1e-12 else -99.0


def worst_sum_error(ratio, f2=200.0):
    f1, worst = f2 * ratio, 0.0
    for i in range(900):
        f = 10 * 10 ** (3.5 * i / 899)
        e = db(sum(outputs(f, f1, f2)))
        if abs(e) > abs(worst):
            worst = e
    return worst


out = []


def add(s):
    out.append(s)


def text(x, y, s, size=13, fill=INK2, anchor="start", weight="normal",
         spacing=None):
    ls = ' letter-spacing="%s"' % spacing if spacing else ""
    add('<text x="%.1f" y="%.1f" font-family="%s" font-size="%d" fill="%s" '
        'text-anchor="%s" font-weight="%s"%s>%s</text>'
        % (x, y, FONT, size, fill, anchor, weight, ls, s))


add('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
    'viewBox="0 0 %d %d">' % (W, H, W, H))
add('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
add('<rect x="0.5" y="0.5" width="%.1f" height="%.1f" fill="none" '
    'stroke="%s" stroke-width="1"/>' % (W - 1, H - 1, "#e5e4de"))

# ---------------- header --------------------------------------------------
text(48, 60, "Crossover tuning ranges", size=27, fill=INK, weight="bold")
text(48, 88, "ESP Project 148 3-way state variable crossover — the span each "
     "frequency pot can sweep, for both variants in this repo.", size=14)

# ---------------- panel A -------------------------------------------------
AY = 150
text(48, AY - 22, "A.  WHAT EACH POT COVERS", size=12, fill=MUTED,
     weight="bold", spacing="1.2")

# axis gridlines
TICKS = [30, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
AXBOT = AY + 232
for t in TICKS:
    x = fx(t)
    add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
        'stroke-width="1"/>' % (x, AY + 6, x, AXBOT, GRID))
    text(x, AXBOT + 20, hz(t).replace(".00", ""), size=12, fill=MUTED,
         anchor="middle")
add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
    'stroke-width="1"/>' % (PX0, AXBOT, PX1, AXBOT, AXIS))
text((PX0 + PX1) / 2, AXBOT + 44, "frequency (log scale)", size=12, fill=MUTED,
     anchor="middle")

for i, (lab, colour) in enumerate([("VR1 — high/mid crossover", S1),
                                   ("VR2 — mid/low crossover", S2)]):
    lx = 700 + i * 210
    add('<rect x="%d" y="%d" width="11" height="11" rx="3" fill="%s"/>'
        % (lx, AY - 34, colour))
    text(lx + 18, AY - 24, lab, size=12, fill=INK2)

BAR = 26
row_y = AY + 24
for vname, bars, overlap in VARIANTS:
    grp_top = row_y - 6
    if overlap:
        ox0, ox1 = fx(overlap[0]), fx(overlap[1])
        oh = 2 * BAR + 22
        add('<clipPath id="ovl"><rect x="%.1f" y="%.1f" width="%.1f" '
            'height="%.1f"/></clipPath>' % (ox0, grp_top - 4, ox1 - ox0, oh))
        add('<g clip-path="url(#ovl)">')
        add('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" '
            'fill-opacity="0.16"/>' % (ox0, grp_top - 4, ox1 - ox0, oh, WARN))
        for dx in range(0, int(ox1 - ox0) + 40, 8):     # texture, not colour alone
            add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                'stroke-width="1" stroke-opacity="0.55"/>'
                % (ox0 + dx, grp_top - 4, ox0 + dx - oh, grp_top - 4 + oh, WARN))
        add('</g>')
    text(48, row_y + 17, vname, size=14, fill=INK, weight="bold")
    for label, band, f_lo, f_hi, colour in bars:
        x0, x1 = fx(f_lo), fx(f_hi)
        add('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="4" '
            'fill="%s"/>' % (x0, row_y, x1 - x0, BAR - 4, colour))
        add('<rect x="%.1f" y="%.1f" width="10" height="%d" rx="4" '
            'fill="%s"/>' % (x0, row_y, BAR - 4, colour))
        text(48, row_y + 38, "", size=1)
        # direct label inside the bar, on the surface colour
        text(x0 + 14, row_y + 16, "%s   %s" % (label, band), size=12.5,
             fill=SURFACE, weight="bold")
        text(x0 - 12, row_y + 16, hz(f_lo), size=12, fill=INK2, anchor="end")
        text(x1 + 12, row_y + 16, hz(f_hi), size=12, fill=INK2)
        row_y += BAR + 6
    if overlap:
        cx = (fx(overlap[0]) + fx(overlap[1])) / 2
        ty = grp_top + 2 * BAR + 32
        add('<path d="M %.1f %.1f L %.1f %.1f L %.1f %.1f Z" fill="%s"/>'
            % (cx - 46, ty + 1, cx - 36, ty + 1, cx - 41, ty - 8, "#b07d00"))
        text(cx - 41, ty, "!", size=8, fill=SURFACE, anchor="middle",
             weight="bold")
        text(cx - 30, ty, "ranges overlap, %s–%s"
             % (hz(overlap[0]).split()[0], hz(overlap[1])), size=12,
             fill="#7a5600", weight="bold")
    row_y += 30

# ---------------- panel B -------------------------------------------------
BY = 530
text(48, BY - 22, "B.  WHAT HAPPENS WHEN THE TWO POINTS CONVERGE", size=12,
     fill=MUTED, weight="bold", spacing="1.2")
text(48, BY + 6, "Worst error in the summed", size=13, fill=INK, weight="bold")
text(48, BY + 24, "response (HIGH+MID+LOW)", size=13, fill=INK, weight="bold")
text(48, BY + 48, "The mid band is squeezed", size=12, fill=INK2)
text(48, BY + 65, "between the two roll-offs;", size=12, fill=INK2)
text(48, BY + 82, "as they approach, it thins", size=12, fill=INK2)
text(48, BY + 99, "and the sum dips.", size=12, fill=INK2)
text(48, BY + 128, "Worst case of the retuned", size=12, fill=INK2)
text(48, BY + 145, "ranges (195 Hz vs 257 Hz):", size=12, fill=INK2)
text(48, BY + 166, "-5.2 dB", size=15, fill=INK, weight="bold")

BX0, BX1 = 380, 1110
BY0, BY1 = BY, BY + 170
RMIN, RMAX = 0.5, 16.0
EMIN = -8.0


def bx(r):
    return BX0 + (math.log10(r) - math.log10(RMIN)) / \
        (math.log10(RMAX) - math.log10(RMIN)) * (BX1 - BX0)


def by(e):
    return BY0 + (e / EMIN) * (BY1 - BY0)


for e in [0, -2, -4, -6, -8]:
    y = by(e)
    add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
        'stroke-width="1"/>' % (BX0, y, BX1, y, GRID))
    text(BX0 - 12, y + 4, "%d dB" % e, size=11.5, fill=MUTED, anchor="end")

# inverted region (f1 < f2) shaded
add('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" '
    'fill-opacity="0.13"/>' % (bx(RMIN), BY0, bx(1.0) - bx(RMIN),
                               BY1 - BY0, WARN))
for i, line in enumerate(["mid/low set", "ABOVE high/mid"]):
    text(bx(RMIN) + 10, BY0 + 20 + i * 15, line, size=11.5, fill="#7a5600",
         weight="bold")
add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
    'stroke-width="1.5"/>' % (bx(1.0), BY0 - 6, bx(1.0), BY1, "#b07d00"))

pts = []
r = RMIN
while r <= RMAX + 1e-9:
    pts.append((bx(r), by(worst_sum_error(r))))
    r *= 1.06
add('<polyline points="%s" fill="none" stroke="%s" stroke-width="2" '
    'stroke-linejoin="round"/>' % (" ".join("%.1f,%.1f" % p for p in pts), S1))

for r, note, dy in [(0.76, "", 24), (3.0, "1.5 octaves apart", 34)]:
    e = worst_sum_error(r)
    add('<circle cx="%.1f" cy="%.1f" r="5" fill="%s" stroke="%s" '
        'stroke-width="2"/>' % (bx(r), by(e), S1, SURFACE))
    text(bx(r) + 11, by(e) + dy, "%+.1f dB" % e, size=12, fill=INK,
         weight="bold")
    if note:
        text(bx(r) + 11, by(e) + dy + 15, note, size=11.5, fill=INK2)

for r in [0.5, 1, 2, 4, 8, 16]:
    text(bx(r), BY1 + 22, ("1 : 1" if r == 1 else
                           ("1 : %g" % (1 / r) if r < 1 else "%g : 1" % r)),
         size=11.5, fill=MUTED, anchor="middle")
add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
    'stroke-width="1"/>' % (BX0, BY1, BX1, BY1, AXIS))
text((BX0 + BX1) / 2, BY1 + 46,
     "separation between the two crossover points  (high/mid : mid/low)",
     size=12, fill=MUTED, anchor="middle")

# ---------------- footer --------------------------------------------------
text(48, H - 34, "Q = 0.5 (Linkwitz-Riley), 12 dB/octave. Both pots are dual-gang "
     "20k; ranges are set by the series resistor and integrator capacitor.",
     size=11.5, fill=MUTED)
text(48, H - 16, "Generated by tools/gen_range_diagram.py — after Rod Elliott, "
     "Elliott Sound Products Project 148.", size=11.5, fill=MUTED)

add("</svg>")

here = os.path.dirname(os.path.abspath(__file__))
docs = os.path.join(os.path.dirname(here), "docs")
os.makedirs(docs, exist_ok=True)
svg = "\n".join(out)
with open(os.path.join(docs, "crossover-ranges.svg"), "w") as f:
    f.write(svg)
try:
    import cairosvg
    cairosvg.svg2png(bytestring=svg.encode(),
                     write_to=os.path.join(docs, "crossover-ranges.png"),
                     output_width=W * 2, output_height=H * 2)
except Exception as exc:      # pragma: no cover
    print("PNG skipped:", exc)
print("wrote docs/crossover-ranges.svg (+ .png)")
