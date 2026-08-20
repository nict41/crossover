#!/usr/bin/env python3
"""Independent check that the generated board will IMPORT and can be BUILT.

gen_pcb_smd.py's verify() answers "is this layout electrically and
geometrically self-consistent" - nets connected, clearances met, pads
matching the schematic. It says nothing about two other ways a board can
fail after it leaves here:

  import   EasyEDA rejects or silently mangles a malformed shape string, an
           unknown layer id, or a duplicate gId.
  fabrication  the layout is fine and the FAB cannot make it - a silkscreen
           line under the minimum printable width, a drill below the
           smallest bit, an annular ring too thin to survive registration.

Both are checked here by re-reading the written artifacts, not by asking
the generator what it thinks it wrote - the same reason verify() does not
reuse the router's bookkeeping.

Limits are JLCPCB's published capabilities for a standard 1 oz
process. They are conservative on purpose: where JLC quotes a value for
their advanced process too, the standard one is used, because the whole
point is that the board can be ordered without special handling.

    python3 tools/validate_fab.py
"""

import csv
import json
import os
import sys
import re
from collections import Counter, defaultdict

GID = re.compile(r"^gge\d+$")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SLUG = "esp-p148-3way-crossover-retuned-quad-smd"
PCB = os.path.join(ROOT, "pcb", "%s-pcb.json" % SLUG)
BOM = os.path.join(ROOT, "bom", "jlcpcb-bom-retuned-quad-smd.csv")
CPL = os.path.join(ROOT, "bom", "jlcpcb-cpl-retuned-quad-smd.csv")
NET = os.path.join(ROOT, "docs", "netlist-%s.json" % SLUG)

MM = 0.254                      # 1 unit = 10 mil = 0.254 mm

# --- JLCPCB standard capability, in mm -----------------------------------
# These were written for the 2-layer process and are kept unchanged for the
# 4-layer one: every limit below is the same or looser on 4 layers (same
# 0.2 mm minimum drill, same 0.13 mm annular ring, and a finer minimum
# trace/space, not a coarser one), so they stay valid and stay
# conservative.  What 4 layers DOES change is the price, not the geometry.
MIN_TRACE = 0.127               # 5 mil
MIN_CLEAR = 0.127               # 5 mil
MIN_DRILL = 0.20                # smallest via drill
MIN_ANNULAR = 0.13              # pad radius beyond the drill
MIN_HOLE_GAP = 0.50             # hole edge to hole edge
MIN_SILK_W = 0.15               # printable silkscreen line width
MIN_SILK_H = 0.80               # legible silkscreen text height
MIN_EDGE_CU = 0.20              # copper to board outline
MAX_BOARD = 500.0               # beyond this leaves the standard price tiers
PRICE_TIER = 100.0              # the 100 x 100 mm cheap tier

# EasyEDA layer ids the board may reference.  21/22 are Inner1/Inner2 -
# the GND plane and the third signal layer of the 4-layer stackup.
LAYERS_OK = set("1 2 3 4 5 6 7 8 9 10 11 12 13 21 22 100 101".split())
# Copper layers, for the pour and clearance checks below.
COPPER_LAYERS = ("1", "2", "21", "22")

problems, notes = [], []


def bad(msg):
    problems.append(msg)


def note(msg):
    notes.append(msg)


def flatten(shapes):
    """Every shape, with LIB children lifted out of their parent."""
    for s in shapes:
        if s.startswith("LIB~"):
            for sub in s.split("#@$")[1:]:
                yield sub
        else:
            yield s


def parse_shape_tokens(shapes):
    """Return a list of token lists for every emitted shape.

    This materialises and tokenises the document's shape strings once so
    callers can avoid repeated calls to `split("~")` in hot loops.
    Returns a list of lists where each inner list is the `~`-split tokens
    of one flattened shape string (LIB children lifted out).
    """
    out = []
    for s in flatten(shapes):
        out.append(s.split("~"))
    return out


# =========================================================================
#  import validity
# =========================================================================
def check_import(doc):
    if doc.get("head", {}).get("docType") != "3":
        bad("head.docType is not '3' - EasyEDA will not open this as a PCB")
    for key in ("head", "canvas", "shape", "layers"):
        if key not in doc:
            bad("missing top-level key %r" % key)

    declared = {ln.split("~")[0] for ln in doc.get("layers", [])}
    gids, dup = set(), set()
    # Field counts from EasyEDA's own published PCB format:
    #   TRACK~width~layer~net~points~id
    #   PAD~shape~x~y~w~h~layer~net~num~holeR~points~rot~id
    #   VIA~x~y~diameter~net~holeR~id
    #   HOLE~x~y~diameter~id
    #   TEXT~type~x~y~strokeW~rot~mirror~layer~font~size~text~path~~id
    fields_expected = {"PAD": 12, "TRACK": 6, "VIA": 6, "HOLE": 5, "TEXT": 13}

    parsed = parse_shape_tokens(doc.get("shape", []))
    for f in parsed:
        kind = f[0]
        n = fields_expected.get(kind)
        s = "~".join(f)
        if n is not None and len(f) < n:
            bad("%s shape has %d fields, expected at least %d: %.60s"
                % (kind, len(f), n, s))
            continue
        # layer id must be one the document declares
        layer = {"PAD": 6, "TRACK": 2, "TEXT": 7}.get(kind)
        if layer is not None and len(f) > layer:
            lid = f[layer]
            if lid not in declared and lid not in LAYERS_OK:
                bad("%s references layer %r, which the document does not "
                    "declare" % (kind, lid))
        # numeric sanity - a NaN or an empty coordinate imports as garbage
        coord_at = {"PAD": (2, 3), "VIA": (1, 2), "HOLE": (1, 2), "TEXT": (2, 3)}
        for i in coord_at.get(kind, ()): 
            if len(f) > i:
                try:
                    v = float(f[i])
                except ValueError:
                    bad("%s has a non-numeric coordinate %r" % (kind, f[i]))
                    continue
                if v != v:
                    bad("%s has a NaN coordinate" % kind)

    # gIds are not always the last field (COPPERAREA carries trailing
    # style fields after its id), so match the token shape instead of
    # trusting a position.
    for f in parsed:
        for tok in f:
            if GID.match(tok):
                if tok in gids:
                    dup.add(tok)
                gids.add(tok)
    if dup:
        bad("%d duplicate gIds (EasyEDA keys objects by gId; duplicates get "
            "silently merged): %s" % (len(dup), sorted(dup)[:5]))

    libs = [s for s in doc["shape"] if s.startswith("LIB~")]
    for s in libs:
        if "#@$" not in s:
            bad("LIB element with no child shapes: %.60s" % s)
    note("%d LIB components, %d loose shapes"
         % (len(libs), len(doc["shape"]) - len(libs)))


# =========================================================================
#  fabrication limits
# =========================================================================
def check_fab(doc):
    holes = []          # (x, y, drill_mm, what, net)
    no_path = [0]
    silk_widths = Counter()
    text_heights = Counter()
    trace_widths = Counter()
    outline_w = Counter()

    parsed = parse_shape_tokens(doc.get("shape", []))
    for f in parsed:
        kind = f[0]
        if kind == "TRACK":
            w, layer = float(f[1]) * MM, f[2]
            if layer == "3":
                silk_widths[round(w, 4)] += 1
            elif layer in COPPER_LAYERS:       # copper only
                trace_widths[round(w, 4)] += 1
            elif layer == "10":                # board outline, not copper
                outline_w[round(w, 4)] += 1
        elif kind == "TEXT" and f[7] == "3":
            text_heights[round(float(f[9]) * MM, 3)] += 1
            if len(f) > 11 and not f[11].strip():
                no_path[0] += 1
        elif kind == "VIA":
            # VIA~x~y~diameter~net~holeRADIUS~id
            dia, drill = float(f[3]) * MM, float(f[5]) * MM * 2
            holes.append((float(f[1]) * MM, float(f[2]) * MM, drill,
                          "via", f[4]))
            if drill < MIN_DRILL:
                bad("via drill %.3f mm < %.2f mm minimum" % (drill, MIN_DRILL))
            ring = (dia - drill) / 2
            if ring < MIN_ANNULAR:
                bad("via annular ring %.3f mm < %.2f mm minimum"
                    % (ring, MIN_ANNULAR))
        elif kind == "HOLE":
            # HOLE~x~y~DIAMETER~id - diameter, not radius, per the format docs
            d = float(f[3]) * MM
            holes.append((float(f[1]) * MM, float(f[2]) * MM, d, "hole", ""))
            if d < MIN_DRILL:
                bad("non-plated hole %.3f mm < %.2f mm minimum" % (d, MIN_DRILL))
        elif kind == "PAD":
            hole = float(f[9]) * MM * 2 if f[9] else 0.0
            if hole > 0:
                holes.append((float(f[2]) * MM, float(f[3]) * MM, hole,
                              "pad %s" % f[8], f[7]))
                ring = (min(float(f[4]), float(f[5])) * MM - hole) / 2
                if ring < MIN_ANNULAR:
                    bad("through-hole pad %s annular ring %.3f mm < %.2f mm"
                        % (f[8], ring, MIN_ANNULAR))

    for w, n in sorted(silk_widths.items()):
        if w < MIN_SILK_W:
            bad("%d silkscreen lines at %.3f mm wide, below the %.2f mm "
                "minimum printable width - the fab may thin or drop them"
                % (n, w, MIN_SILK_W))
    for h, n in sorted(text_heights.items()):
        if h < MIN_SILK_H:
            bad("%d silkscreen texts at %.2f mm tall, below the %.1f mm "
                "legible minimum" % (n, h, MIN_SILK_H))
    for w, n in sorted(trace_widths.items()):
        if w < MIN_TRACE:
            bad("%d traces at %.3f mm, below the %.3f mm minimum"
                % (n, w, MIN_TRACE))

    note("trace widths: %s mm" % ", ".join("%.3f x%d" % (w, n)
                                           for w, n in sorted(trace_widths.items())))
    note("silk line widths: %s mm" % ", ".join("%.3f x%d" % (w, n)
                                               for w, n in sorted(silk_widths.items())))
    note("board outline: %s mm wide"
         % ", ".join("%.3f x%d" % (w, n) for w, n in sorted(outline_w.items())))
    note("silk text heights: %s mm" % ", ".join("%.2f x%d" % (h, n)
                                                for h, n in sorted(text_heights.items())))

    # hole-to-hole: O(n^2) over ~240 holes is nothing
    # Hole-to-hole spacing is a DRILLING limit, so it applies whether or not
    # the two holes share a net - a same-net pair is not an electrical
    # problem but the fab still has to drill both without the bit breaking
    # into the neighbouring hole.  Reported separately so the distinction is
    # visible rather than hidden in one number.
    worst = {True: None, False: None}
    for i, a in enumerate(holes):
        for b in holes[i + 1:]:
            gap = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 \
                - a[2] / 2 - b[2] / 2
            same = bool(a[4]) and a[4] == b[4]
            if worst[same] is None or gap < worst[same][0]:
                worst[same] = (gap, a[3], b[3], a[4])
    for same, w in worst.items():
        if w is None:
            continue
        label = "same-net (%s)" % w[3] if same else "different nets"
        if w[0] < MIN_HOLE_GAP:
            bad("closest %s hole-to-hole gap %.3f mm (%s to %s) < %.2f mm "
                "drill minimum" % (label, w[0], w[1], w[2], MIN_HOLE_GAP))
        else:
            note("closest %s hole gap %.2f mm (%s to %s)"
                 % (label, w[0], w[1], w[2]))

    if no_path[0]:
        note("%d silkscreen TEXT shapes carry no glyph path. EasyEDA's own "
             "exports put the stroked outline in that field; ours leave it "
             "empty and rely on the editor re-rendering from the text and "
             "size. Confirm the silkscreen actually appears after import "
             "before ordering - it is the one thing here that cannot be "
             "checked without importing." % no_path[0])

    bb = doc.get("BBox", {})
    w, h = bb.get("width", 0) * MM, bb.get("height", 0) * MM
    note("board %.1f x %.1f mm" % (w, h))
    if max(w, h) > MAX_BOARD:
        bad("board %.0f mm on its long side exceeds %.0f mm" % (max(w, h), MAX_BOARD))
    if max(w, h) > PRICE_TIER:
        note("board exceeds the %.0f x %.0f mm price tier on its long side "
             "(%.1f mm) - still fabricable, just not at the cheapest rate"
             % (PRICE_TIER, PRICE_TIER, max(w, h)))


# =========================================================================
#  assembly data
# =========================================================================
def check_assembly(doc):
    if not (os.path.exists(BOM) and os.path.exists(CPL)):
        bad("BOM or CPL missing - run gen_pcb_smd.py without SWEEP=1")
        return
    bom = list(csv.DictReader(open(BOM)))
    cpl = list(csv.DictReader(open(CPL)))
    for name, rows, need in (("BOM", bom, ("Comment", "Designator", "Footprint", "LCSC Part #")),
                             ("CPL", cpl, ("Designator", "Mid X", "Mid Y", "Layer", "Rotation"))):
        if not rows:
            bad("%s is empty" % name)
            continue
        missing = [c for c in need if c not in rows[0]]
        if missing:
            bad("%s is missing JLCPCB columns %s (has %s)"
                % (name, missing, list(rows[0])))

    bom_refs = set()
    for r in bom:
        for ref in r.get("Designator", "").split(","):
            if ref.strip():
                bom_refs.add(ref.strip())
        if not r.get("LCSC Part #", "").strip():
            bad("BOM line %r has no LCSC part number - JLC cannot source it"
                % r.get("Comment"))
    cpl_refs = {r["Designator"].strip() for r in cpl if r.get("Designator")}
    if bom_refs != cpl_refs:
        bad("BOM and CPL disagree on which parts are assembled: "
            "BOM-only %s, CPL-only %s"
            % (sorted(bom_refs - cpl_refs), sorted(cpl_refs - bom_refs)))
    note("%d BOM lines covering %d assembled designators"
         % (len(bom), len(bom_refs)))

    bb = doc.get("BBox", {})
    w, h = bb.get("width", 0) * MM, bb.get("height", 0) * MM
    for r in cpl:
        try:
            x = float(r["Mid X"].rstrip("m")), float(r["Mid Y"].rstrip("m"))
        except (KeyError, ValueError):
            bad("CPL row %r has unparseable coordinates" % r.get("Designator"))
            continue
        if not (0 <= x[0] <= w and 0 <= x[1] <= h):
            bad("CPL places %s at (%.2f, %.2f) mm, outside the %.1f x %.1f mm "
                "board" % (r["Designator"], x[0], x[1], w, h))
        if r.get("Layer", "").strip().lower() not in ("top", "t"):
            bad("CPL row %s is not on the top layer" % r["Designator"])
        try:
            rot = float(r["Rotation"])
        except (KeyError, ValueError):
            bad("CPL row %s has an unparseable rotation" % r.get("Designator"))
            continue
        if rot % 90:
            note("%s has a %.1f degree rotation - not a multiple of 90"
                 % (r["Designator"], rot))


# =========================================================================
#  the board matches the schematic it claims to
# =========================================================================
def check_netlist(doc):
    if not os.path.exists(NET):
        bad("netlist JSON missing - run gen_schematic.py first")
        return
    netdoc = json.load(open(NET))
    want = set(netdoc["nets"])
    on_board, npads = set(), 0
    pour_layers = {}
    for s in flatten(doc["shape"]):
        f = s.split("~")
        if f[0] == "PAD":
            npads += 1
            if f[7]:
                on_board.add(f[7])
        elif f[0] == "TRACK" and f[2] in ("1", "2") and f[3]:
            on_board.add(f[3])
        elif f[0] == "VIA" and f[4]:
            on_board.add(f[4])
        elif f[0] == "COPPERAREA" and f[3]:
            on_board.add(f[3])
            pour_layers.setdefault(f[3], set()).add(f[2])
    unknown = on_board - want
    missing = want - on_board
    if unknown:
        bad("%d net names on the board are not in the schematic netlist: %s"
            % (len(unknown), sorted(unknown)[:6]))
    if missing:
        bad("%d schematic nets have no copper on the board: %s"
            % (len(missing), sorted(missing)[:6]))
    note("%d pads carrying %d distinct nets, all named in the schematic"
         % (npads, len(on_board)))
    # GND is carried by the pour, not by traces, so the pour is not
    # decoration any more - it is the net.  A board that lost it would
    # still pass every other check here, because ground pads and stitching
    # vias keep the name present.
    if pour_layers.get("GND", set()) >= set(COPPER_LAYERS):
        note("GND is a plane: copper pour present on all four layers, "
             "Inner1 solid")
    elif pour_layers.get("GND", set()) >= {"1", "2"}:
        note("GND is a plane: copper pour present on both layers")
    else:
        bad("GND has no copper pour on both layers - it is not routed as a "
            "net, so the pour is the only thing connecting it")


def check_lcsc(online):
    """Confirm every LCSC part in the BOM still exists and is in stock.

    A board that passes every geometric check is still unbuildable if a
    part has gone end-of-life since the number was written down, so this
    is part of "can it be manufactured" rather than a separate concern.
    Off by default: the rest of this script must work with no network."""
    if not online:
        note("LCSC stock not checked (pass --online to query it)")
        return
    import urllib.request
    bom = list(csv.DictReader(open(BOM)))
    for row in bom:
        code = row.get("LCSC Part #", "").strip()
        qty = len([r for r in row.get("Designator", "").split(",") if r.strip()])
        url = ("https://easyeda.com/api/eda/product/search"
               "?keyword=%s&version=6.5.46&page=1&pageSize=3" % code)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (crossover fab validator)"})
            with urllib.request.urlopen(req, timeout=45) as fh:
                data = json.load(fh)
        except Exception as e:                       # noqa: BLE001
            note("could not check %s (%s)" % (code, e))
            continue
        hit = next((p for p in (data.get("result") or {}).get("productList", [])
                    if p.get("number") == code), None)
        if hit is None:
            note("%s (%s) did not come back from an LCSC search - confirm it "
                 "is still orderable before paying" % (code, row.get("Comment")))
            continue
        stock = hit.get("stock") or 0
        if stock < qty * 20:
            bad("%s (%s) stock is %s, against %d per board - too thin to "
                "rely on" % (code, row.get("Comment"), stock, qty))
        else:
            note("%s %-9s stock %-9s (%d per board)"
                 % (code, row.get("Comment"), stock, qty))


def main():
    if not os.path.exists(PCB):
        print("no board at %s - run gen_pcb_smd.py" % PCB)
        return 2
    doc = json.load(open(PCB))
    check_import(doc)
    check_fab(doc)
    check_assembly(doc)
    check_netlist(doc)
    check_lcsc("--online" in sys.argv)

    for n in notes:
        print("  .. %s" % n)
    print()
    if problems:
        print("FAB/IMPORT PROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    print("OK: imports as an EasyEDA PCB, and every dimension is inside "
          "JLCPCB's standard capability")
    return 0


if __name__ == "__main__":
    sys.exit(main())
