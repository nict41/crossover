#!/usr/bin/env python3
"""Routing-aware component placement on an unbounded canvas.

The board size is an OUTPUT of this module, not an input to it.  Parts are
arranged on open canvas and the outline is drawn around the result, which is
the opposite of what this project did for a long time: pick a width and a
height, then search for the size at which the existing arrangement happened
to route.  That search can only ever find a board big enough to hide a
layout problem - it cannot fix one - and the repo's own history is a list of
times it was reached for and the real cause turned out to be structural
(see CLAUDE.md).

What the search optimises, in the order the weights actually favour:

  overlap     courtyards may not intersect.  A penalty rather than a
              rejection, so the anneal can pass through a crowded state to
              reach a better one, but ramped hard enough that the final
              layout is clean - and asserted afterwards regardless.
  escape      every side of a part that carries pads needs clear depth
              beyond it for those pads to break out into.  THIS is the term
              that makes rotation matter: turning a SOIC does not change
              its area or (much) its wirelength, it changes which of its
              sides has seven pins queuing to get out.  Starved escape
              corridors - not board area - are what actually left nets
              unroutable every time it happened here.
  congestion  RUDY: each net spreads an estimated wire demand over its
              bounding box; cells where demand exceeds the tracks that
              physically fit there are penalised.  Catches the case escape
              depth misses, where nothing is locally tight but too many
              nets want the same channel.
  wirelength  half-perimeter per net.  Cheap, and a decent proxy for
              everything not modelled.
  proximity   named pairs that must end up physically next to each other,
              measured pin to pin.  Wirelength does NOT capture this: a
              decoupling capacitor sits on +15V and GND, both of which
              already span the whole board, so moving it makes almost no
              difference to any half-perimeter and the search will happily
              leave it 60 mm from the pin it is supposed to be decoupling.
  size        the actual objective: with the panel row fixing the width, a
              board is better when it is shorter.

Everything is incremental and vectorised - a move touches one part, so only
its overlap, its escape gaps and the handful of nets it belongs to are
recomputed.  A full pass runs in seconds, which is what makes it reasonable
to search placement properly and pay for routing once, rather than the other
way round.
"""

import math
import os
import random

import numpy as np

try:
    import canneal
except ImportError:                     # reference implementation still works
    canneal = None

INF = float("inf")


class Part:
    """One placeable part: its courtyard and pads at each angle it is
    allowed to take, plus whichever constraints apply to it."""

    def __init__(self, ref, geom, rots, group=None, outward=None):
        self.ref = ref
        self.rots = list(rots)
        self.geom = geom                   # {rot: probe() result}
        self.group = group                 # rigid group name, or None
        self.outward = outward             # local-frame edge normal, or None

    def box(self, rot):
        return self.geom[rot]["box"]

    def pads(self, rot):
        return self.geom[rot]["pads"]


SIDES = ((-1, 0), (1, 0), (0, -1), (0, 1))          # left, right, top, bottom


def _rotate(v, q):
    x, y = v
    for _ in range(q % 4):
        x, y = -y, x
    return x, y


def _side_counts(box, pads, outward=None):
    """How many pads break out through each side (left, right, top,
    bottom).

    For a part pinned to a board edge - the panel row, a screw terminal -
    there is no choice about it: everything it connects to is inboard, so
    every pad escapes directly away from that edge, and it needs clear
    depth there in proportion to ALL of its pads.

    For everything else the side is taken from whichever courtyard edge
    each pad is nearest, derived from geometry rather than declared per
    footprint, so it stays right for a part this module has never been told
    anything about.  That rule is wrong for the edge-mounted parts, which
    is why they are special-cased: a pot's outer pins sit only ~7 units
    from the left and right of its courtyard but ~25 from the top, so
    nearest-edge called them sideways escapes - into the gap between two
    pots, where there is plenty of room and nothing to connect to - and the
    upward corridor they actually need went unpriced entirely.  Connections
    to the pot row were the single biggest group of routing failures."""
    if outward is not None:
        return [len(pads) if s == tuple(-v for v in outward) else 0
                for s in SIDES]
    x0, y0, x1, y1 = box
    n = [0, 0, 0, 0]
    for _r, _n, _net, px, py, _w, _h in pads:
        d = (px - x0, x1 - px, py - y0, y1 - py)
        n[d.index(min(d))] += 1
    return n


class Placer:
    def __init__(self, parts, fixed_boxes=(), seed=12345, track_pitch=2.4,
                 cell=10.0, canvas=900.0, near=(), plane_nets=()):
        self.parts = parts
        # Nets carried by a copper plane rather than by traces.  They have
        # no wirelength and make no wire demand, so pricing them as if the
        # router had to draw a tree for them is fiction - and expensive
        # fiction: GND is by far the biggest net on this board, so its
        # half-perimeter and its RUDY demand swamped every signal net and
        # pulled the layout towards a shape that suits a net nobody routes.
        # Their pads still count towards the ESCAPE term, because a ground
        # pin does still have to reach the plane, just not across the
        # board.
        self.plane_nets = set(plane_nets)
        self.n = len(parts)
        self.idx = {p.ref: i for i, p in enumerate(parts)}
        self.rng = random.Random(seed)
        self.track_pitch = track_pitch
        self.fixed_boxes = list(fixed_boxes)

        # --- per-part, per-rotation geometry, flattened for speed --------
        self.rot_opts = [p.rots for p in parts]
        self.boxoff = [{r: p.box(r) for r in p.rots} for p in parts]
        self.padoff = [{r: [(pd[2], pd[3], pd[4]) for pd in p.pads(r)]
                        for r in p.rots} for p in parts]
        # Required clear depth beyond each side, from the pad count facing
        # it: k pads breaking out through one face need somewhere for k
        # tracks to run once they turn, which is k track pitches of depth.
        #
        # Capped, because not all k tracks have to run parallel past the
        # part: some of a SOIC face's pins leave one way along it and some
        # the other, so the depth actually needed is nearer half the pin
        # count than all of it.  Where exactly to cap is not something to
        # reason out from first principles - it was MEASURED by routing the
        # same seed at several caps and comparing failing nets against
        # board area (see docs/pcb-notes-smd.md).  Too low starves IC pin
        # escapes; too high buys nothing and costs real board height.
        # There is a FLOOR as well as a cap.  Pad count alone says a
        # one-pad test point needs a single track's width and can therefore
        # be wedged anywhere it fits - and the router then cannot reach it
        # (TP1 and TP2 both came back unreachable that way, on a board
        # where every IC pin routed fine).  Any side with pads on it needs
        # somewhere to go, whether it has one pad or seven.
        cap = float(os.environ.get("ESC_CAP", 4))
        floor = float(os.environ.get("ESC_FLOOR", 1))
        self.need = []
        for p in parts:
            per = {}
            for r in p.rots:
                out = (_rotate(p.outward, r // 90)
                       if p.outward is not None else None)
                cnt = _side_counts(p.box(r), p.pads(r), out)
                per[r] = [0.0 if c == 0 else max(floor, min(c, cap)) * track_pitch
                          for c in cnt]
            self.need.append(per)

        # --- rigid groups (the front-panel row) --------------------------
        self.groups = {}
        for i, p in enumerate(parts):
            if p.group:
                self.groups.setdefault(p.group, []).append(i)

        # --- nets --------------------------------------------------------
        nets = {}
        for i, p in enumerate(parts):
            for k, (net, _dx, _dy) in enumerate(self.padoff[i][p.rots[0]]):
                if net:
                    nets.setdefault(net, []).append((i, k))
        self.nets = [v for k, v in nets.items()
                     if len(v) > 1 and k not in self.plane_nets]
        self.net_names = [k for k, v in nets.items()
                          if len(v) > 1 and k not in self.plane_nets]
        self.of_part = [[] for _ in parts]
        for ni, mem in enumerate(self.nets):
            for i, _k in mem:
                if ni not in self.of_part[i]:
                    self.of_part[i].append(ni)

        # --- congestion grid ---------------------------------------------
        self.cell = cell
        self.gN = int(canvas / cell)
        self.org = canvas / 2.0
        self.demand = np.zeros((self.gN, self.gN))
        # Tracks that fit across one cell.  The geometric answer is two
        # layers' worth, and at that value the term read exactly zero on
        # every layout ever put through it - which is not a finding about
        # the board, it is a broken gauge: the top layer is largely spoken
        # for by pads, silk keepout and the ground pour long before a cell
        # runs out of room in the abstract.  One layer's worth is the
        # honest figure for how much of it a router can actually have.
        self.supply = float(os.environ.get("SUPPLY", 3.0)) * cell / track_pitch

        # (part a, part b, net, target distance) - see near_penalty().
        self.near = [(self.idx[a], self.idx[b], net, tgt)
                     for a, b, net, tgt in near]

        self.X = np.zeros(self.n)
        self.Y = np.zeros(self.n)
        self.R = [p.rots[0] for p in parts]
        self.BX0 = np.zeros(self.n)
        self.BY0 = np.zeros(self.n)
        self.BX1 = np.zeros(self.n)
        self.BY1 = np.zeros(self.n)
        self.esc = np.zeros(self.n)
        self.net_hpwl = np.zeros(len(self.nets))

        self._swap_pool = {}
        for i, p in enumerate(parts):
            if p.group:
                continue
            bx0, by0, bx1, by1 = p.box(p.rots[0])
            self._swap_pool.setdefault(
                (round(bx1 - bx0, 2), round(by1 - by0, 2)), []).append(i)

    # ------------------------------------------------------------------
    # state maintenance
    # ------------------------------------------------------------------
    def _sync_box(self, i):
        dx0, dy0, dx1, dy1 = self.boxoff[i][self.R[i]]
        self.BX0[i], self.BY0[i] = self.X[i] + dx0, self.Y[i] + dy0
        self.BX1[i], self.BY1[i] = self.X[i] + dx1, self.Y[i] + dy1

    def set_pose(self, i, x, y, rot):
        self.X[i], self.Y[i], self.R[i] = x, y, rot
        self._sync_box(i)

    def pad_xy(self, i, k):
        _net, dx, dy = self.padoff[i][self.R[i]][k]
        return self.X[i] + dx, self.Y[i] + dy

    # ------------------------------------------------------------------
    # cost terms
    # ------------------------------------------------------------------
    def _ov_parts(self, i):
        w = np.minimum(self.BX1, self.BX1[i]) - np.maximum(self.BX0, self.BX0[i])
        h = np.minimum(self.BY1, self.BY1[i]) - np.maximum(self.BY0, self.BY0[i])
        np.maximum(w, 0.0, out=w)
        np.maximum(h, 0.0, out=h)
        return float(np.dot(w, h)) - (self.BX1[i] - self.BX0[i]) * (self.BY1[i] - self.BY0[i])

    def _ov_fixed(self, i):
        a = 0.0
        for fx0, fy0, fx1, fy1 in self.fixed_boxes:
            ow = min(fx1, self.BX1[i]) - max(fx0, self.BX0[i])
            oh = min(fy1, self.BY1[i]) - max(fy0, self.BY0[i])
            if ow > 0 and oh > 0:
                a += ow * oh
        return a

    def overlap_of(self, i):
        """Courtyard overlap AREA between part i and everything else -
        every other part, plus the immovable keepouts.  Area rather than a
        count so the anneal can tell 'nearly apart' from 'right on top of'
        and has a gradient to follow between them.

        Each pair involving i is counted exactly once here, which is what
        makes this the exact delta to the board's total overlap when i (and
        only i) moves."""
        return self._ov_parts(i) + self._ov_fixed(i)

    def total_overlap(self):
        return (sum(self._ov_parts(i) for i in range(self.n)) / 2.0
                + sum(self._ov_fixed(i) for i in range(self.n)))

    def escape_of(self, i):
        """Penalty for pads that have nowhere to break out to.

        For each side, find the nearest neighbouring courtyard directly
        opposite it and compare the clear depth to what that side's pad
        count needs.  Neighbours that do not overlap this part's span on
        the perpendicular axis are not in the way and are skipped, which is
        what stops a part diagonally offset from this one from looking like
        a wall."""
        need = self.need[i][self.R[i]]
        if not any(need):
            return 0.0
        x0, y0, x1, y1 = self.BX0[i], self.BY0[i], self.BX1[i], self.BY1[i]
        span_y = (self.BY0 < y1) & (self.BY1 > y0)
        span_x = (self.BX0 < x1) & (self.BX1 > x0)
        span_y[i] = span_x[i] = False
        gaps = [
            np.min(np.where(span_y & (self.BX1 <= x0), x0 - self.BX1, INF)),
            np.min(np.where(span_y & (self.BX0 >= x1), self.BX0 - x1, INF)),
            np.min(np.where(span_x & (self.BY1 <= y0), y0 - self.BY1, INF)),
            np.min(np.where(span_x & (self.BY0 >= y1), self.BY0 - y1, INF)),
        ]
        for fx0, fy0, fx1, fy1 in self.fixed_boxes:
            if fy0 < y1 and fy1 > y0:
                if fx1 <= x0:
                    gaps[0] = min(gaps[0], x0 - fx1)
                if fx0 >= x1:
                    gaps[1] = min(gaps[1], fx0 - x1)
            if fx0 < x1 and fx1 > x0:
                if fy1 <= y0:
                    gaps[2] = min(gaps[2], y0 - fy1)
                if fy0 >= y1:
                    gaps[3] = min(gaps[3], fy0 - y1)
        return sum(max(0.0, nd - g) ** 2 for nd, g in zip(need, gaps))

    def _pin_on(self, i, net):
        """Position of part i's pad on `net`, or its anchor if it has none."""
        for pnet, dx, dy in self.padoff[i][self.R[i]]:
            if pnet == net:
                return self.X[i] + dx, self.Y[i] + dy
        return self.X[i], self.Y[i]

    def near_penalty(self):
        """How far named pairs are from where they have to be, pin to pin.

        This exists because a decoupling capacitor is invisible to every
        other term: it is two pads on two board-spanning nets, so it costs
        nothing in wirelength wherever it goes, and it is small enough that
        area and congestion barely notice it.  The automated placement duly
        put all four of them 37-63 mm from the op-amp pins they decouple,
        where the trace inductance defeats the capacitor entirely - a
        regression against the hand-placed through-hole board, which has
        them at 11 mm.  Nothing was wrong with the search; it was optimising
        what it had been told to optimise."""
        pen = 0.0
        for i, j, net, tgt in self.near:
            (ax, ay), (bx, by) = self._pin_on(i, net), self._pin_on(j, net)
            d = math.hypot(ax - bx, ay - by)
            if d > tgt:
                pen += (d - tgt) ** 2
        return pen

    def _net_rect(self, ni):
        xs, ys = [], []
        for i, k in self.nets[ni]:
            px, py = self.pad_xy(i, k)
            xs.append(px)
            ys.append(py)
        return min(xs), min(ys), max(xs), max(ys)

    def _rudy(self, ni, sign):
        """Spread a net's wire demand over its bounding box.

        The box is clamped to one congestion CELL, not to 1e-6.  RUDY
        divides by the box AREA, so a net whose pads are collinear - the
        box has zero width or zero height - divided by an area of 1e-6 and
        injected about 1e7 of demand into a single cell.  A net like that
        is not exotic: an op-amp section wired as a follower joins its
        output to its inverting input, two adjacent pins on the SAME side
        of the package, so the box is exactly zero wide.  Adding a quad of
        output buffers created four of them at once and took the placement
        cost from ~2e4 to ~5e7, of which 49999955 was this term - three
        orders of magnitude of noise on top of every real one, and nearly
        constant, so the anneal was optimising almost nothing else.
        Clamping to the cell says the honest thing instead: a net confined
        to one cell demands about one cell's worth of routing."""
        x0, y0, x1, y1 = self._net_rect(ni)
        w, h = max(x1 - x0, self.cell), max(y1 - y0, self.cell)
        a = int((x0 + self.org) / self.cell), int((y0 + self.org) / self.cell)
        b = int((x1 + self.org) / self.cell), int((y1 + self.org) / self.cell)
        a = (max(a[0], 0), max(a[1], 0))
        b = (min(b[0], self.gN - 1), min(b[1], self.gN - 1))
        if b[0] < a[0] or b[1] < a[1]:
            return
        self.demand[a[1]:b[1] + 1, a[0]:b[0] + 1] += sign * (w + h) / (w * h) * self.cell

    def congestion(self):
        return float(np.maximum(self.demand - self.supply, 0.0).sum())

    def extent(self):
        return (float(self.BX0.min()), float(self.BY0.min()),
                float(self.BX1.max()), float(self.BY1.max()))

    def edge_violation(self):
        """Parts that have to reach a board edge - the screw terminals take
        wire from off-board, the panel row IS the panel - are checked by
        asking whether anything got BETWEEN each one and the edge it faces,
        within its own column.

        The obvious test, 'is this part the outermost thing on the board',
        is wrong in a way that quietly wrecks the search: parts that face
        the same edge but have different depths (the dual-gang pots reach
        15 units further than the single-gang ones) can never all be
        outermost, so the penalty never reaches zero and the anneal keeps
        stretching the board trying to pay it off.  What matters is that
        the path out is clear, not that every part ends flush."""
        pen = 0.0
        for i, p in enumerate(self.parts):
            if p.outward is None:
                continue
            ox, oy = self._outward(i)
            if ox:
                across = (self.BY0 < self.BY1[i]) & (self.BY1 > self.BY0[i])
                beyond = (self.BX1 - self.BX1[i]) if ox > 0 else (self.BX0[i] - self.BX0)
            else:
                across = (self.BX0 < self.BX1[i]) & (self.BX1 > self.BX0[i])
                beyond = (self.BY1 - self.BY1[i]) if oy > 0 else (self.BY0[i] - self.BY0)
            across[i] = False
            for j in self.groups.get(p.group, ()):
                across[j] = False       # a rigid group cannot block itself
            d = np.where(across, beyond, 0.0)
            pen += float(np.square(np.maximum(d, 0.0)).sum())
        return pen

    def _outward(self, i):
        ox, oy = self.parts[i].outward
        q = (self.R[i] // 90) % 4
        for _ in range(q):
            ox, oy = -oy, ox
        return ox, oy

    # ------------------------------------------------------------------
    def full_cost(self, w):
        """Recompute every term from scratch.  The anneal keeps all of
        these incrementally, and one of them - the escape penalty - is
        deliberately approximate while it does (moving a part changes its
        neighbours' escape gaps too, and chasing that every move costs more
        than it is worth).  Calling this periodically is what keeps that
        approximation from accumulating into a cost the layout doesn't
        actually have."""
        for i in range(self.n):
            self.esc[i] = self.escape_of(i)
        self.demand[:] = 0.0
        for ni in range(len(self.nets)):
            self._rudy(ni, +1)
            x0, y0, x1, y1 = self._net_rect(ni)
            self.net_hpwl[ni] = (x1 - x0) + (y1 - y0)
        return self.score(w, self.total_overlap())

    def terms(self, w, ov):
        x0, y0, x1, y1 = self.extent()
        return dict(ov=w["ov"] * ov,
                    esc=w["esc"] * float(self.esc.sum()),
                    cong=w["cong"] * self.congestion(),
                    hpwl=w["hpwl"] * float(self.net_hpwl.sum()),
                    h=w["h"] * (y1 - y0),
                    w=w["w"] * max(0.0, (x1 - x0) - w["wfloor"]),
                    edge=w["edge"] * self.edge_violation(),
                    near=w["near"] * self.near_penalty())

    def score(self, w, ov):
        return sum(self.terms(w, ov).values())

    # ------------------------------------------------------------------
    def seed(self, step=4.0):
        """A constructive first layout: take parts in descending pin order
        and drop each at the centroid of whatever it already connects to,
        spiralling outward until it stops overlapping anything.

        The anneal converges from a random start too, but far more slowly
        and to a worse answer - it spends its high-temperature moves
        untangling a mess instead of exploring real alternatives.  Parts
        already positioned by the caller (the panel row) are left alone and
        act as the anchor everything else grows out from."""
        pinned = {i for i, p in enumerate(self.parts) if p.group}
        for i in pinned:
            self._sync_box(i)
        done = set(pinned)
        order = sorted((i for i in range(self.n) if i not in pinned),
                       key=lambda i: -len(self.padoff[i][self.R[i]]))
        for i in order:
            near = [(self.X[j], self.Y[j]) for ni in self.of_part[i]
                    for j, _k in self.nets[ni] if j in done]
            if near:
                cx = sum(p[0] for p in near) / len(near)
                cy = sum(p[1] for p in near) / len(near)
            else:
                cx = float(self.X[list(done)].mean()) if done else 0.0
                cy = float(self.Y[list(done)].mean()) if done else 0.0
            k, ang = 0, 0.0
            while True:
                r = step * math.sqrt(k)
                self.set_pose(i, _snap(cx + r * math.cos(ang)),
                              _snap(cy + r * math.sin(ang)), self.R[i])
                if self.overlap_of(i) < 1e-9:
                    break
                k += 1
                ang += 2.399963          # golden angle - even spiral coverage
            done.add(i)

    # ------------------------------------------------------------------
    def anneal(self, moves=60000, w=None, report=None):
        """Dispatches to the compiled annealer when it is available.

        The Python implementation below is the reference: it is what the
        cost model is written in and what the comments explain.  The C one
        applies the same terms with the same weights, but its RNG differs,
        so the two explore different trajectories and a given SEED does not
        give the same board.  PLACER=py forces this one."""
        w = dict(w or {})
        if canneal is not None and canneal.available():
            ok = canneal.anneal(self, moves, w, self.rng.randrange(1 << 62))
            if not ok:
                self.legalise()
            cost = self.full_cost(w)
            assert self._legal(), "placement search returned an overlapping layout"
            return cost
        return self._anneal_py(moves, w, report)

    def _anneal_py(self, moves, w, report=None):
        cost = self.full_cost(w)
        ov = self.total_overlap()
        best, best_state = (cost if self._legal() else INF), self.snapshot()
        t0, t1 = w.get("t0", 120.0), w.get("t1", 0.5)
        amp0, amp1 = w.get("amp0", 30.0), w.get("amp1", 1.0)
        # The overlap weight starts low - parts have to be able to slide
        # through each other early on or the search can never reorder
        # anything - and is ramped hard, so by the end an overlapping state
        # is never worth accepting.  The best-state filter below only ever
        # records legal layouts anyway; the ramp is what makes the anneal
        # spend its last thousands of moves refining one.
        ov_lo, ov_hi = w["ov"], w["ov"] * 400.0

        for step in range(moves):
            f = step / float(moves)
            temp = t0 * (t1 / t0) ** f
            w["ov"] = ov_lo * (ov_hi / ov_lo) ** f
            # Scored against the CURRENT weights, before anything moves: the
            # overlap weight changes every step, so a total cached from an
            # earlier step goes stale immediately and the accept/reject
            # comparison silently stops meaning anything.  (Carrying `cost`
            # forward froze the search for thousands of moves at a stretch.)
            cost = self.score(w, ov)
            undo = self._propose(amp0 * (amp1 / amp0) ** f)
            if undo is None:
                continue
            moved = undo[0]
            d_ov = sum(self.overlap_of(i) for i in moved) - undo[1]
            for i in moved:
                self.esc[i] = self.escape_of(i)
            for ni in undo[3]:
                self._rudy(ni, +1)
                x0, y0, x1, y1 = self._net_rect(ni)
                self.net_hpwl[ni] = (x1 - x0) + (y1 - y0)
            new = self.score(w, ov + d_ov)
            if new <= cost or self.rng.random() < math.exp((cost - new) / max(temp, 1e-9)):
                cost, ov = new, ov + d_ov
                if new < best and ov < 1e-9:
                    best, best_state = new, self.snapshot()
            else:
                self._undo(undo)
            if report and step % report == 0:
                x0, y0, x1, y1 = self.extent()
                t = self.terms(w, ov)
                print("  %6d T=%7.2f  %.0fx%.0f  ov %8.0f esc %7.0f cong %7.0f "
                      "hpwl %6.0f size %6.0f edge %7.0f"
                      % (step, temp, x1 - x0, y1 - y0, t["ov"], t["esc"],
                         t["cong"], t["hpwl"], t["h"] + t["w"], t["edge"]),
                      flush=True)
            if step % 2000 == 1999:      # re-sync the approximate terms
                self.full_cost(w)
                ov = self.total_overlap()

        self.restore(best_state)
        if not self._legal():
            self.legalise()
        cost = self.full_cost(w)
        assert self._legal(), "placement search returned an overlapping layout"
        return cost

    def legalise(self, step=2.0):
        """Push overlapping parts apart until nothing intersects.

        Needed because `best_state` can only ever be a state the anneal
        actually saw as legal, and there are starts from which it sees
        none - notably the second pass, which introduces the mounting-hole
        keepouts and can therefore make the layout it INHERITS illegal
        before a single move is proposed.  Asserting there just crashes the
        build on a solvable problem."""
        # Rigid groups move as a unit rather than being skipped.  Skipping
        # them left the one case this exists for unfixable: the second pass
        # introduces the mounting-hole keepouts, and it is usually a GROUP
        # (the output terminal row, pinned to an edge and therefore near a
        # corner) that they land on top of.  The assert then fired on a
        # solvable problem.
        units = ([[i] for i in range(self.n) if not self.parts[i].group]
                 + [list(g) for g in self.groups.values()])
        for unit in sorted(units, key=lambda u: -sum(self.overlap_of(i) for i in u)):
            def bad():
                return sum(self.overlap_of(i) for i in unit)
            if bad() < 1e-9:
                continue
            home = [(self.X[i], self.Y[i]) for i in unit]
            k, ang = 0, 0.0
            while bad() > 1e-9 and k < 20000:
                k += 1
                ang += 2.399963
                r = step * math.sqrt(k)
                dx, dy = _snap(r * math.cos(ang)), _snap(r * math.sin(ang))
                for i, (hx, hy) in zip(unit, home):
                    self.set_pose(i, hx + dx, hy + dy, self.R[i])

    def _legal(self):
        return self.total_overlap() < 1e-9

    # ------------------------------------------------------------------
    def _propose(self, amp):
        """Make one random change and return everything needed to undo it.

        `amp` is how far a part may jump, and is on its own schedule rather
        than being read off the temperature.  Tying the two together looks
        natural and is a trap: the temperature has to start high enough to
        accept a costly move, which then means the first few thousand moves
        fling parts hundreds of units apart and the search spends its whole
        budget walking them back.  It reached 1710 x 2443 units that way."""
        kind = self.rng.random()
        if kind < 0.5:
            movable = [i for i in range(self.n) if not self.parts[i].group]
            if not movable:
                return None
            i = self.rng.choice(movable)
            group = [i]
        elif kind < 0.62:
            # Directed move: drop a part where its own connections average
            # out to.  Random walking finds this eventually and slowly;
            # proposing it outright is what lets the anneal spend its
            # budget on the arrangement rather than on transport.
            movable = [i for i in range(self.n)
                       if not self.parts[i].group and self.of_part[i]]
            if not movable:
                return None
            i = self.rng.choice(movable)
            group = [i]
        elif kind < 0.78:
            rotatable = [i for i in range(self.n)
                         if len(self.rot_opts[i]) > 1 and not self.parts[i].group]
            if not rotatable:
                return None
            i = self.rng.choice(rotatable)
            group = [i]
        elif kind < 0.9:
            # Swap two interchangeable parts.  "Interchangeable" means the
            # same courtyard size, which for this board means the same
            # footprint - so a swap always leaves a legal layout legal, and
            # is the only move that can reorder parts wholesale once the
            # overlap weight has ramped up too far for them to slide past
            # each other.
            pool = [v for v in self._swap_pool.values() if len(v) > 1]
            if not pool:
                return None
            i, j = self.rng.sample(self.rng.choice(pool), 2)
            group = [i, j]
        else:
            if not self.groups:
                return None
            group = list(self.rng.choice(list(self.groups.values())))

        before = [(i, self.X[i], self.Y[i], self.R[i]) for i in group]
        ov_before = sum(self.overlap_of(i) for i in group)
        nets = sorted({ni for i in group for ni in self.of_part[i]})
        for ni in nets:
            self._rudy(ni, -1)

        if kind < 0.5:
            i = group[0]
            self.set_pose(i,
                          _snap(self.X[i] + self.rng.gauss(0, amp)),
                          _snap(self.Y[i] + self.rng.gauss(0, amp)),
                          self.R[i])
        elif kind < 0.62:
            i = group[0]
            xs, ys = [], []
            for ni in self.of_part[i]:
                for j, k in self.nets[ni]:
                    if j != i:
                        px, py = self.pad_xy(j, k)
                        xs.append(px)
                        ys.append(py)
            if xs:
                own = self.padoff[i][self.R[i]]
                ox = sum(p[1] for p in own) / len(own)
                oy = sum(p[2] for p in own) / len(own)
                self.set_pose(i, _snap(sum(xs) / len(xs) - ox),
                              _snap(sum(ys) / len(ys) - oy), self.R[i])
        elif kind < 0.78:
            i = group[0]
            r = self.rng.choice([r for r in self.rot_opts[i] if r != self.R[i]])
            self.set_pose(i, self.X[i], self.Y[i], r)
        elif kind < 0.9:
            i, j = group
            self.set_pose(i, before[1][1], before[1][2], self.R[i])
            self.set_pose(j, before[0][1], before[0][2], self.R[j])
        else:
            dx, dy = _snap(self.rng.gauss(0, amp)), _snap(self.rng.gauss(0, amp))
            for i in group:
                self.set_pose(i, self.X[i] + dx, self.Y[i] + dy, self.R[i])

        return (group, ov_before, before, nets)

    def _undo(self, undo):
        group, _ov, before, nets = undo
        for ni in nets:
            self._rudy(ni, -1)
        for i, x, y, r in before:
            self.set_pose(i, x, y, r)
        for i in group:
            self.esc[i] = self.escape_of(i)
        for ni in nets:
            self._rudy(ni, +1)
            x0, y0, x1, y1 = self._net_rect(ni)
            self.net_hpwl[ni] = (x1 - x0) + (y1 - y0)

    # ------------------------------------------------------------------
    def snapshot(self):
        return (self.X.copy(), self.Y.copy(), list(self.R))

    def restore(self, st):
        self.X[:], self.Y[:] = st[0], st[1]
        self.R = list(st[2])
        for i in range(self.n):
            self._sync_box(i)

    def result(self):
        """Final poses, translated so the arrangement's bottom-left corner
        sits at the origin.  The caller adds its own margin and draws the
        outline around this."""
        x0, y0, _x1, _y1 = self.extent()
        return {p.ref: (float(self.X[i] - x0), float(self.Y[i] - y0), self.R[i])
                for i, p in enumerate(self.parts)}


def _snap(v, g=0.5):
    """Pad centres have to land on the routing grid - see the module
    docstring in gen_pcb_smd.py."""
    return round(v / g) * g
