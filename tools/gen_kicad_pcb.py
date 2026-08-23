#!/usr/bin/env python3
"""Emit a KiCad PCB seeded for an external router: outline + parts, no copper.

What this is for: handing a routing tool a board that is *placed but not
wired*.  So it deliberately contains

  * the committed board outline, at its real size;
  * the EDGE components - the front-panel row and the rear connector row -
    at their committed positions, because those are mechanical and are not
    the router's to move;
  * every other component parked OUTSIDE the outline, on a grid, for the
    tool to place;
  * the full netlist on the pads, so there is a ratsnest to work from;
  * the mounting holes, so nothing routes through a screw;

and deliberately contains NO tracks, NO vias and NO zones - not even the
ground pour, which this project otherwise treats as load-bearing.

Read from the committed `pcb/*.json` rather than by re-running the board
generator, so the export cannot describe a board that was never built.  The
edge rows are read out of gen_pcb_smd.py with `ast`, without executing it.
"""
import ast
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from gen_kicad import sexp, _kids, num, uid                      # noqa: E402

U = 0.254                     # one board unit in mm
SLUG = "esp-p148-3way-crossover-retuned-quad-smd"
GAP = 6.0                     # mm between parked parts
PARK_X = 15.0                 # mm from the board's right edge to the park
SILK_W = 0.15


def edge_refs():
    """PANEL_ORDER + REAR, read out of the board generator, not guessed."""
    tree = ast.parse(open(os.path.join(HERE, "gen_pcb_smd.py")).read())
    got = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("PANEL_ORDER", "REAR")):
            got[node.targets[0].id] = ast.literal_eval(node.value)
    missing = {"PANEL_ORDER", "REAR"} - set(got)
    if missing:
        raise SystemExit("gen_kicad_pcb: could not read %s from gen_pcb_smd.py"
                         % sorted(missing))
    return list(got["PANEL_ORDER"]) + list(got["REAR"])


def read_board():
    d = json.load(open(os.path.join(ROOT, "pcb", SLUG + "-pcb.json")))
    outline = []
    holes = []
    for s in d["shape"]:
        f = s.split("~")
        if s.startswith("TRACK") and f[2] == "10":
            p = [float(v) for v in f[4].split()]
            outline += list(zip(p[0::2], p[1::2]))
        elif s.startswith("HOLE"):
            holes.append((float(f[1]), float(f[2]), float(f[3])))
    parts = []
    for s in d["shape"]:
        if not s.startswith("LIB"):
            continue
        head, *subs = s.split("#@$")
        hf = head.split("~")
        ox, oy = float(hf[1]), float(hf[2])
        attrs = dict(re.findall(r"([^`]+)`([^`]*)`", hf[3]))
        ref, pads, silk = None, [], []
        for t in subs:
            f = t.split("~")
            if f[0] == "PAD":
                pads.append(dict(num=f[8], shape=f[1], x=float(f[2]),
                                 y=float(f[3]), w=float(f[4]), h=float(f[5]),
                                 layer=f[6], net=f[7],
                                 hole=float(f[9]) if f[9] else 0.0))
            elif f[0] == "TRACK" and f[2] == "3":
                p = [float(v) for v in f[4].split()]
                silk.append(list(zip(p[0::2], p[1::2])))
            elif f[0] == "TEXT" and f[1] == "P" and len(f) > 10:
                # TEXT~P is the DESIGNATOR; TEXT~L is a plain label.  Taking
                # the first text that merely LOOKS like a designator named
                # J6 "LD1" after its own first pin label, so the schematic's
                # J6 had no footprint and the board carried a part that does
                # not exist.  Found by importing into a router that reported
                # components missing.
                ref = f[10].strip()
        if ref and pads:
            parts.append(dict(ref=ref, value=attrs.get("Value", ""),
                              package=attrs.get("package", ""),
                              origin=(ox, oy), pads=pads, silk=silk))
    return outline, holes, parts


LAYERS = """  (layers
    (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen") (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user) (39 "F.Mask" user)
    (44 "Edge.Cuts" user) (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
  )"""


def emit():
    edges = set(edge_refs())
    outline, holes, parts = read_board()
    X0 = min(p[0] for p in outline)
    Y0 = min(p[1] for p in outline)
    X1 = max(p[0] for p in outline)
    Y1 = max(p[1] for p in outline)
    BW, BH = (X1 - X0) * U, (Y1 - Y0) * U
    have = {p["ref"] for p in parts}
    unknown = sorted(edges - have)
    if unknown:
        raise SystemExit("gen_kicad_pcb: edge parts not on the board: %s"
                         % unknown)

    def MX(x):
        return (x - X0) * U

    def MY(y):
        return (y - Y0) * U

    # --- park everything that is not an edge part, outside the outline ---
    loose = [p for p in parts if p["ref"] not in edges]
    loose.sort(key=lambda p: (p["package"], p["ref"]))
    for p in loose:
        xs = [q["x"] for q in p["pads"]] + [c[0] for s in p["silk"] for c in s]
        ys = [q["y"] for q in p["pads"]] + [c[1] for s in p["silk"] for c in s]
        p["_w"] = (max(xs) - min(xs)) * U
        p["_h"] = (max(ys) - min(ys)) * U
        p["_dx"] = (min(xs) - p["origin"][0]) * U      # origin -> bbox corner
        p["_dy"] = (min(ys) - p["origin"][1]) * U
    cx, cy, rowh = BW + PARK_X, 0.0, 0.0
    limit = cx + max(BW, 120.0)
    for p in loose:
        if cx + p["_w"] > limit and cx > BW + PARK_X:
            cx, cy, rowh = BW + PARK_X, cy + rowh + GAP, 0.0
        p["_at"] = (cx - p["_dx"], cy - p["_dy"])
        cx += p["_w"] + GAP
        rowh = max(rowh, p["_h"])

    # --- nets --------------------------------------------------------------
    names = sorted({q["net"] for p in parts for q in p["pads"] if q["net"]})
    netid = {n: i + 1 for i, n in enumerate(names)}

    L = ['(kicad_pcb (version 20240108) (generator "gen_kicad_pcb")',
         '  (generator_version "8.0")',
         '  (general (thickness 1.6) (legacy_teardrops no))',
         '  (paper "A3")', LAYERS,
         '  (setup (pad_to_mask_clearance 0))',
         '  (net 0 "")']
    for n in names:
        L.append('  (net %d "%s")' % (netid[n], n))

    # --- board outline -----------------------------------------------------
    corners = [(0, 0), (BW, 0), (BW, BH), (0, BH)]
    for a, b in zip(corners, corners[1:] + corners[:1]):
        L.append('  (gr_line (start %s %s) (end %s %s) (stroke (width 0.1) '
                 '(type solid)) (layer "Edge.Cuts") (uuid "%s"))'
                 % (num(a[0]), num(a[1]), num(b[0]), num(b[1]),
                    uid("edge", a, b)))

    # --- footprints --------------------------------------------------------
    for p in sorted(parts, key=lambda q: q["ref"]):
        on_board = p["ref"] in edges
        ax, ay = ((MX(p["origin"][0]), MY(p["origin"][1])) if on_board
                  else p["_at"])
        L.append('  (footprint "crossover:%s" (layer "F.Cu")' % p["package"])
        L.append('    (uuid "%s") (at %s %s)' % (uid("fp", p["ref"]),
                                                 num(ax), num(ay)))
        L.append('    (attr through_hole)' if any(q["hole"] for q in p["pads"])
                 else '    (attr smd)')
        L.append('    (property "Reference" "%s" (at 0 0 0) (layer "F.SilkS") '
                 '(uuid "%s") (effects (font (size 1 1) (thickness 0.15))))'
                 % (p["ref"], uid("fpref", p["ref"])))
        L.append('    (property "Value" "%s" (at 0 0 0) (layer "F.Fab") '
                 '(uuid "%s") (effects (font (size 1 1) (thickness 0.15))) '
                 '(hide yes))' % (p["value"], uid("fpval", p["ref"])))
        for seg in p["silk"]:
            for a, b in zip(seg, seg[1:]):
                L.append('    (fp_line (start %s %s) (end %s %s) (stroke '
                         '(width %s) (type solid)) (layer "F.SilkS") '
                         '(uuid "%s"))'
                         % (num((a[0] - p["origin"][0]) * U),
                            num((a[1] - p["origin"][1]) * U),
                            num((b[0] - p["origin"][0]) * U),
                            num((b[1] - p["origin"][1]) * U),
                            num(SILK_W), uid("fl", p["ref"], a, b)))
        for q in p["pads"]:
            dx = num((q["x"] - p["origin"][0]) * U)
            dy = num((q["y"] - p["origin"][1]) * U)
            net = '(net %d "%s")' % (netid[q["net"]], q["net"]) if q["net"] else ""
            if q["hole"]:
                d = num(q["hole"] * 2 * U)
                L.append('    (pad "%s" thru_hole circle (at %s %s) '
                         '(size %s %s) (drill %s) (layers "*.Cu" "*.Mask") '
                         '%s (uuid "%s"))'
                         % (q["num"], dx, dy, num(q["w"] * U), num(q["h"] * U),
                            d, net, uid("pad", p["ref"], q["num"])))
            else:
                L.append('    (pad "%s" smd rect (at %s %s) (size %s %s) '
                         '(layers "F.Cu" "F.Paste" "F.Mask") %s (uuid "%s"))'
                         % (q["num"], dx, dy, num(q["w"] * U), num(q["h"] * U),
                            net, uid("pad", p["ref"], q["num"])))
        L.append('  )')

    # --- mounting holes ----------------------------------------------------
    for i, (hx, hy, dia) in enumerate(holes):
        L.append('  (footprint "crossover:MountingHole" (layer "F.Cu")')
        L.append('    (uuid "%s") (at %s %s) (attr through_hole exclude_from_pos_files '
                 'exclude_from_bom)' % (uid("hole", i), num(MX(hx)), num(MY(hy))))
        L.append('    (property "Reference" "H%d" (at 0 0 0) (layer "F.SilkS") '
                 '(uuid "%s") (effects (font (size 1 1) (thickness 0.15))) '
                 '(hide yes))' % (i + 1, uid("holeref", i)))
        L.append('    (pad "" np_thru_hole circle (at 0 0) (size %s %s) '
                 '(drill %s) (layers "F&B.Cu" "*.Mask") (uuid "%s"))'
                 % (num(dia * U), num(dia * U), num(dia * U), uid("holepad", i)))
        L.append('  )')
    L.append(')')
    return "\n".join(L) + "\n", dict(BW=BW, BH=BH, edges=edges, parts=parts,
                                     nets=names, holes=holes)


def verify(text, info):
    """Check the ARTIFACT: it must be placed, netted, and utterly bare."""
    root = sexp(text)
    problems = []
    if root[0] != "kicad_pcb":
        return ["emitted file is not a kicad_pcb"]
    for tag in ("segment", "via", "zone", "arc"):
        n = len(_kids(root, tag))
        if n:
            problems.append("file contains %d %s(s); it must carry no copper"
                            % (n, tag))
    fps = _kids(root, "footprint")
    pads = [pd for fp in fps for pd in _kids(fp, "pad")]
    npads = sum(len(p["pads"]) for p in info["parts"])
    if len(pads) != npads + len(info["holes"]):
        problems.append("%d pads emitted, board has %d plus %d holes"
                        % (len(pads), npads, len(info["holes"])))
    if len(fps) != len(info["parts"]) + len(info["holes"]):
        problems.append("%d footprints, expected %d parts + %d holes"
                        % (len(fps), len(info["parts"]), len(info["holes"])))
    seen = {n[2] for pd in pads for n in _kids(pd, "net")}
    if seen != set(info["nets"]):
        problems.append("pad nets differ from the board's: %s"
                        % sorted(seen ^ set(info["nets"]))[:6])

    # every edge part inside the outline, every other part clear of it
    BW, BH = info["BW"], info["BH"]
    for fp in fps:
        ref = None
        for pr in _kids(fp, "property"):
            if pr[1] == "Reference":
                ref = pr[2]
        at = _kids(fp, "at")[0]
        ax, ay = float(at[1]), float(at[2])
        pd = _kids(fp, "pad")
        if not pd:
            continue
        xs, ys = [], []
        for q in pd:
            qa = _kids(q, "at")[0]
            sz = _kids(q, "size")[0]
            xs += [ax + float(qa[1]) - float(sz[1]) / 2,
                   ax + float(qa[1]) + float(sz[1]) / 2]
            ys += [ay + float(qa[2]) - float(sz[2]) / 2,
                   ay + float(qa[2]) + float(sz[2]) / 2]
        inside = (min(xs) >= -0.01 and max(xs) <= BW + 0.01
                  and min(ys) >= -0.01 and max(ys) <= BH + 0.01)
        clear = min(xs) > BW or min(ys) > BH
        if ref in info["edges"] and not inside:
            problems.append("edge part %s is not inside the outline" % ref)
        if ref not in info["edges"] and ref and ref.startswith("H"):
            continue
        if ref not in info["edges"] and not clear:
            problems.append("%s should be parked outside the outline" % ref)
    return problems


def main():
    text, info = emit()
    problems = verify(text, info)
    if problems:
        raise SystemExit("gen_kicad_pcb: the emitted board is wrong:\n  "
                         + "\n  ".join(problems))
    out = os.path.join(ROOT, "kicad", SLUG + ".kicad_pcb")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(text)
    print("kicad/%s.kicad_pcb  %.1f x %.1f mm outline, %d edge parts placed, "
          "%d parked outside, %d nets, %d mounting holes"
          % (SLUG, info["BW"], info["BH"], len(info["edges"]),
             len(info["parts"]) - len(info["edges"]), len(info["nets"]),
             len(info["holes"])))
    print("  no tracks, no vias, no zones - checked by re-reading the file")


if __name__ == "__main__":
    main()
