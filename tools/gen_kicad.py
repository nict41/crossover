#!/usr/bin/env python3
"""Emit a KiCad schematic from the SAME geometry gen_schematic.py draws.

Not a re-derivation from the netlist.  `gen_schematic.build()` leaves every
symbol's pins in absolute coordinates, every wire polyline, every net label
and every junction in module globals, and this translates those directly.
Two generators reading one geometry cannot disagree about connectivity; two
generators each laying out a schematic from a netlist certainly can.

Coordinates: EasyEDA schematic units are 10 per 0.1 inch, so one unit is
exactly 0.254 mm and the whole drawing lands on KiCad's 2.54 mm grid with
no rounding.  Sheet space is Y-down in both tools, but SYMBOL space in
KiCad is Y-UP - the classic way a translated symbol comes out mirrored -
so symbol-local geometry is negated in Y and nothing else is.

Symbols are emitted one per INSTANCE (`crossover:R1`, `crossover:C0`, ...)
rather than one per type.  It costs a few hundred lines of file and buys
exactness: every instance is placed at rotation 0 with its graphics already
drawn the way that instance appears, so there is no rotation or mirroring
to get wrong, and no dependency on any library installed on the reader's
machine.

The file is checked before it is written - see verify_connectivity().
"""
import hashlib
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import gen_schematic as gs                                       # noqa: E402

U = 0.254                     # one EasyEDA schematic unit, in mm
MARGIN = 20.0                 # mm of paper around the drawing
TXT = 1.27                    # mm text height
GEN_VERSION = "20231120"      # KiCad 8 schematic format


def num(v):
    """Format a coordinate.  round() happily produces -0.0, which is valid
    S-expression and looks like a bug in a diff."""
    v = round(float(v), 4) + 0.0
    return "%g" % (0.0 if v == 0 else v)


def uid(*parts):
    """A deterministic UUID, so regenerating gives a byte-identical file."""
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:32])


# --------------------------------------------------------------------------
#  read the EasyEDA geometry back
# --------------------------------------------------------------------------
def _pin_path_end(path):
    """Last point of an EasyEDA pin stub path ('M 200 320 v 10')."""
    toks = re.findall(r"[MmLlHhVv]|-?\d+(?:\.\d+)?", path)
    x = y = 0.0
    i = 0
    cmd = None
    while i < len(toks):
        t = toks[i]
        if t in "MmLlHhVv":
            cmd = t
            i += 1
            continue
        v = float(t)
        if cmd in ("M", "L"):
            x, y = v, float(toks[i + 1])
            i += 2
        elif cmd in ("m", "l"):
            x, y = x + v, y + float(toks[i + 1])
            i += 2
        elif cmd == "H":
            x = v
            i += 1
        elif cmd == "h":
            x += v
            i += 1
        elif cmd == "V":
            y = v
            i += 1
        elif cmd == "v":
            y += v
            i += 1
        else:
            i += 1
    return x, y


def read_symbols():
    """[{ref, value, package, origin, prims, pins, ref_at, val_at, texts}]"""
    out = []
    for sh in gs.shapes:
        if not sh.startswith("LIB~"):
            continue
        head, *subs = sh.split("#@$")
        f = head.split("~")
        ox, oy = float(f[1]), float(f[2])
        attrs = dict(re.findall(r"([^`]+)`([^`]*)`", f[3]))
        prims, pins, texts = [], [], []
        ref = val = None
        for s in subs:
            g = s.split("~")
            k = g[0]
            if k == "P":
                segs = s.split("^^")
                p = segs[0].split("~")
                pins.append((p[3], float(p[4]), float(p[5]),
                             segs[2].split("~")[0]))
            elif k == "T":
                item = (g[12], float(g[2]), float(g[3]), g[7], g[14])
                if g[1] == "P":
                    ref = item
                elif g[1] == "N":
                    val = item
                else:
                    texts.append(item)
            elif k == "R":
                prims.append(("rect", float(g[1]), float(g[2]),
                              float(g[1]) + float(g[5]),
                              float(g[2]) + float(g[6])))
            elif k in ("PL", "PG"):
                v = [float(q) for q in g[1].split()]
                prims.append((("poly" if k == "PL" else "pgon"),
                              list(zip(v[0::2], v[1::2]))))
        if ref is None:                      # net flags etc - not a component
            continue
        out.append(dict(ref=ref[0], value=(val[0] if val else ""),
                        package=attrs.get("package", ""),
                        origin=(ox, oy), prims=prims, pins=pins,
                        ref_at=(ref[1], ref[2], ref[4]),
                        val_at=((val[1], val[2], val[4]) if val else None),
                        texts=texts))
    return out


# --------------------------------------------------------------------------
#  emit
# --------------------------------------------------------------------------
class Sheet(object):
    def __init__(self, syms, wires, labels, junctions):
        self.syms, self.wires = syms, wires
        self.labels, self.junctions = labels, junctions
        xs, ys = [], []
        for s in syms:
            for _n, px, py, _p in s["pins"]:
                xs.append(px)
                ys.append(py)
            for p in s["prims"]:
                pts = [(p[1], p[2]), (p[3], p[4])] if p[0] == "rect" else p[1]
                xs += [q[0] for q in pts]
                ys += [q[1] for q in pts]
        for w in wires:
            xs += [p[0] for p in w]
            ys += [p[1] for p in w]
        self.dx = MARGIN - min(xs) * U
        self.dy = MARGIN - min(ys) * U
        self.w = round((max(xs) - min(xs)) * U + 2 * MARGIN, 1)
        self.h = round((max(ys) - min(ys)) * U + 2 * MARGIN, 1)
        self.uuid = uid("sheet", len(syms), len(wires))

    def X(self, x):
        return round(x * U + self.dx, 4)

    def Y(self, y):
        return round(y * U + self.dy, 4)


def eff(size=TXT, hide=False, justify=None):
    j = " (justify %s)" % justify if justify else ""
    return "(effects (font (size %g %g))%s%s)" % (size, size, j,
                                                  " (hide yes)" if hide else "")


def sym_def(sh, s):
    """One lib_symbol, in KiCad's Y-up symbol space."""
    ox, oy = s["origin"]
    name = s["ref"]

    def lx(x):
        return round((x - ox) * U, 4)

    def ly(y):
        return round(-(y - oy) * U, 4)

    L = ['  (symbol "crossover:%s"' % name,
         '    (exclude_from_sim no) (in_bom yes) (on_board yes)',
         '    (property "Reference" "%s" (at 0 0 0) %s)' % (name, eff(hide=True)),
         '    (property "Value" "%s" (at 0 0 0) %s)'
         % (s["value"].replace('"', "'"), eff(hide=True)),
         '    (property "Footprint" "%s" (at 0 0 0) %s)'
         % (s["package"], eff(hide=True)),
         '    (property "Datasheet" "" (at 0 0 0) %s)' % eff(hide=True),
         '    (symbol "%s_0_1"' % name]
    for p in s["prims"]:
        if p[0] == "rect":
            L.append('      (rectangle (start %g %g) (end %g %g) '
                     '(stroke (width 0) (type default)) (fill (type none)))'
                     % (lx(p[1]), ly(p[2]), lx(p[3]), ly(p[4])))
        else:
            pts = " ".join("(xy %g %g)" % (lx(x), ly(y)) for x, y in p[1])
            L.append('      (polyline (pts %s) (stroke (width 0) (type default)) '
                     '(fill (type %s)))'
                     % (pts, "outline" if p[0] == "pgon" else "none"))
    for txt, tx, ty, _size, _anch in s["texts"]:
        L.append('      (text "%s" (at %g %g 0) %s)'
                 % (txt.replace('"', "'"), lx(tx), ly(ty), eff(1.0)))
    L.append('    )')
    L.append('    (symbol "%s_1_1"' % name)
    for num, px, py, path in s["pins"]:
        ex, ey = _pin_path_end(path)
        vx, vy = (ex - px), -(ey - py)               # toward the body, Y-up
        ang = int(round(math.degrees(math.atan2(vy, vx)))) % 360
        ln = round(math.hypot(vx, vy) * U, 4) or 2.54
        L.append('      (pin passive line (at %g %g %d) (length %g)'
                 % (lx(px), ly(py), ang, ln))
        L.append('        (name "~" %s) (number "%s" %s)'
                 % (eff(1.0), num, eff(1.0)))
        L.append('      )')
    L += ['    )', '  )']
    return L


def instance(sh, s):
    ox, oy = s["origin"]
    name, u = s["ref"], uid("inst", s["ref"])
    rt, vt = s["ref_at"], s["val_at"]
    just = {"start": "left", "middle": None, "end": "right"}
    L = ['  (symbol (lib_id "crossover:%s") (at %g %g 0) (unit 1)'
         % (name, sh.X(ox), sh.Y(oy)),
         '    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)',
         '    (uuid "%s")' % u,
         '    (property "Reference" "%s" (at %g %g 0) %s)'
         % (name, sh.X(rt[0]), sh.Y(rt[1]), eff(justify=just.get(rt[2]))),
         '    (property "Value" "%s" (at %g %g 0) %s)'
         % (s["value"].replace('"', "'"),
            sh.X(vt[0]) if vt else sh.X(ox), sh.Y(vt[1]) if vt else sh.Y(oy),
            eff(justify=just.get(vt[2]) if vt else None)),
         '    (property "Footprint" "%s" (at %g %g 0) %s)'
         % (s["package"], sh.X(ox), sh.Y(oy), eff(hide=True)),
         '    (property "Datasheet" "" (at %g %g 0) %s)'
         % (sh.X(ox), sh.Y(oy), eff(hide=True))]
    for num, px, py, _p in s["pins"]:
        L.append('    (pin "%s" (uuid "%s"))' % (num, uid("pin", name, num)))
    L += ['    (instances (project "crossover" (path "/%s" '
          '(reference "%s") (unit 1))))' % (sh.uuid, name),
          '  )']
    return L


def render(sh):
    L = ['(kicad_sch (version %s) (generator "gen_kicad") '
         '(generator_version "8.0")' % GEN_VERSION,
         '  (uuid "%s")' % sh.uuid,
         '  (paper "User" %g %g)' % (sh.w, sh.h),
         '  (title_block (title "ESP P148 3-way variable crossover") '
         '(company "generated by tools/gen_kicad.py"))',
         '  (lib_symbols']
    for s in sorted(sh.syms, key=lambda q: q["ref"]):
        L += sym_def(sh, s)
    L.append('  )')
    for w in sh.wires:
        for a, b in zip(w, w[1:]):
            L.append('  (wire (pts (xy %g %g) (xy %g %g)) '
                     '(stroke (width 0) (type default)) (uuid "%s"))'
                     % (sh.X(a[0]), sh.Y(a[1]), sh.X(b[0]), sh.Y(b[1]),
                        uid("w", a, b)))
    for jx, jy in sh.junctions:
        L.append('  (junction (at %g %g) (diameter 0) (color 0 0 0 0) '
                 '(uuid "%s"))' % (sh.X(jx), sh.Y(jy), uid("j", jx, jy)))
    for name, x, y in sh.labels:
        L.append('  (label "%s" (at %g %g 0) %s (uuid "%s"))'
                 % (name, sh.X(x), sh.Y(y),
                    eff(justify="left bottom"), uid("l", name, x, y)))
    for s in sorted(sh.syms, key=lambda q: q["ref"]):
        L += instance(sh, s)
    L.append(')')
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
#  check the ARTIFACT, not the intention
# --------------------------------------------------------------------------
def sexp(text):
    """Parse S-expressions.  A real parser rather than regexes, because the
    point of this check is to read the file the way KiCad will - and if it
    does not tokenise, that is itself the bug."""
    toks = re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text)
    pos = [0]

    def node():
        out = []
        while pos[0] < len(toks):
            t = toks[pos[0]]
            pos[0] += 1
            if t == "(":
                out.append(node())
            elif t == ")":
                return out
            else:
                out.append(t[1:-1] if t.startswith('"') else t)
        return out
    root = node()
    return root[0] if len(root) == 1 else root


def _kids(nd, tag):
    return [c for c in nd if isinstance(c, list) and c and c[0] == tag]


def _at(nd):
    a = _kids(nd, "at")
    return (float(a[0][1]), float(a[0][2])) if a else None


def verify_connectivity(text, expected):
    """Re-derive the netlist from the emitted file and compare.

    The whole risk in a format translation is that it LOOKS right and wires
    up something else, and nothing here can open KiCad to find out.  So the
    emitted text is parsed back with no reference to the structures that
    produced it, connectivity is rebuilt from the geometry - pins, wire
    segments, collinear touches, labels by name - and checked against the
    netlist gen_schematic derives.  Same method verify() uses on the board:
    measure the artifact.
    """
    root = sexp(text)
    assert root[0] == "kicad_sch", "emitted file is not a kicad_sch"

    local = {}
    for lib in _kids(root, "lib_symbols"):
        for sy in _kids(lib, "symbol"):
            name = sy[1].split(":")[-1]
            got = []
            for sub in _kids(sy, "symbol"):
                for pn in _kids(sub, "pin"):
                    xy = _at(pn)
                    numnd = _kids(pn, "number")
                    got.append((xy[0], xy[1], numnd[0][1]))
            local[name] = got

    abspins, insts = [], []
    for sy in _kids(root, "symbol"):
        lib = _kids(sy, "lib_id")
        if not lib:
            continue
        ref = lib[0][1].split(":")[-1]
        ix, iy = _at(sy)
        insts.append(ref)
        for lx, ly, n in local.get(ref, []):
            abspins.append((ref, n, round(ix + lx, 4), round(iy - ly, 4)))

    segs = []
    for w in _kids(root, "wire"):
        pts = _kids(w, "pts")[0]
        xy = [(float(c[1]), float(c[2])) for c in _kids(pts, "xy")]
        segs += list(zip(xy, xy[1:]))
    labels = [(l[1], _at(l)[0], _at(l)[1]) for l in _kids(root, "label")]

    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def on(p, a, b):
        (px, py), (ax, ay), (bx, by) = p, a, b
        if abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) > 1e-6:
            return False
        return (min(ax, bx) - 1e-6 <= px <= max(ax, bx) + 1e-6 and
                min(ay, by) - 1e-6 <= py <= max(ay, by) + 1e-6)

    for a, b in segs:
        union(a, b)
    pts = ({p for s in segs for p in s} | {(x, y) for _r, _n, x, y in abspins}
           | {(x, y) for _n, x, y in labels})
    for p in pts:
        for a, b in segs:
            if on(p, a, b):
                union(p, a)
    byname = {}
    for n, x, y in labels:
        byname.setdefault(n, []).append(find((x, y)))
    for n, roots in byname.items():
        for r in roots[1:]:
            union(roots[0], r)

    got = {}
    for ref, n, x, y in abspins:
        got.setdefault(find((x, y)), set()).add("%s.%s" % (ref, n))
    name_of = {}
    for n, x, y in labels:
        name_of[find((x, y))] = n
    got_nets = {}
    for rootk, members in got.items():
        got_nets[name_of.get(rootk, "?%s" % (rootk,))] = frozenset(members)

    problems = []
    want = {k: frozenset(v) for k, v in expected.items()}
    if len(abspins) != sum(len(v) for v in want.values()):
        problems.append("recovered %d pins from the file, the schematic has %d"
                        % (len(abspins), sum(len(v) for v in want.values())))
    # Compared as SETS OF NETS: auto-generated names differ by construction
    # on the two sides, and a name is not what makes a net correct.
    if set(want.values()) != set(got_nets.values()):
        for n in sorted(set(want.values()) - set(got_nets.values()))[:6]:
            problems.append("schematic net missing from the KiCad file: %s"
                            % sorted(n))
        for n in sorted(set(got_nets.values()) - set(want.values()))[:6]:
            problems.append("KiCad file has a net the schematic does not: %s"
                            % sorted(n))
    for k, v in want.items():
        if not k.startswith("N") and k in got_nets and got_nets[k] != v:
            problems.append("net %s differs: %s" % (k, sorted(v ^ got_nets[k])))
    return problems, len(got_nets), len(abspins)


def main():
    cfg = [c for c in gs.VARIANTS
           if c["slug"] == "esp-p148-3way-crossover-retuned-quad-smd"][0]
    junctions = gs.build(cfg)
    expected, _touches = gs.extract_netlist()
    syms = read_symbols()
    sh = Sheet(syms, gs.wires, gs.netlabels, junctions)
    text = render(sh)

    problems, nnets, npins = verify_connectivity(text, expected)
    if problems:
        raise SystemExit("gen_kicad: the emitted schematic does not match "
                         "the netlist:\n  " + "\n  ".join(problems))

    outdir = os.path.join(ROOT, "kicad")
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, cfg["slug"])
    with open(base + ".kicad_sch", "w") as f:
        f.write(text)
    with open(os.path.join(outdir, "crossover.kicad_pro"), "w") as f:
        f.write('{\n  "board": {},\n  "meta": {"filename": '
                '"crossover.kicad_pro", "version": 1},\n'
                '  "schematic": {},\n  "sheets": [["%s", "Root"]],\n'
                '  "text_variables": {}\n}\n' % sh.uuid)
    print("kicad/%s.kicad_sch  %d symbols, %d pins, %d wires, %d labels, "
          "%d junctions" % (os.path.basename(base), len(syms), npins,
                            sum(len(w) - 1 for w in gs.wires),
                            len(gs.netlabels), len(junctions)))
    print("  paper %.0f x %.0f mm; %d nets re-derived from the emitted file "
          "and matched against the schematic netlist" % (sh.w, sh.h, nnets))


if __name__ == "__main__":
    main()
