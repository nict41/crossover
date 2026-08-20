/* Exact copper-to-copper geometry, compiled.
 *
 * verify() and split_nets() both walk the same double loop: for every pair
 * of features that share a layer and are near each other, compute the exact
 * gap between their copper and compare it against a threshold.  The pairs
 * they must CONSIDER are O(n^2) - a couple of million on a routed board -
 * and only a handful are near enough to matter, so the Python version
 * carries a numpy bounding-box pre-filter and then pays interpreter cost on
 * each survivor.  Profiling put that at ~10 s of a ~34 s trial, more than
 * the compiled router.
 *
 * This is a straight transcription of the Python in gen_pcb_smd.py, kept
 * expression-for-expression so the two agree bit for bit: same operation
 * order in d_seg_seg's four-way minimum, same subtraction of the two half
 * widths at the end.  gen_pcb_smd.py keeps the Python as the reference
 * implementation and checks this against it (GEOM_CHECK=1).
 *
 * Feature encoding, flat arrays, one entry per feature:
 *   kind  0 = rect, 1 = segment, 2 = point
 *   g     4 doubles: rect x0,y0,x1,y1 | seg ax,ay,bx,by | pt x,y,_,_
 *   hw    half-width of the copper around that geometry
 *   lay   layer bitmask; two features interact only if lay[i] & lay[j]
 *   net   net id; -1 never matches anything
 */
#include <math.h>
#include <stdlib.h>
#include <string.h>

static double d_pt_seg(double px, double py,
                       double ax, double ay, double bx, double by)
{
    double dx = bx - ax, dy = by - ay;
    double L = dx * dx + dy * dy;
    double t;
    if (L == 0.0) {
        t = 0.0;
    } else {
        t = ((px - ax) * dx + (py - ay) * dy) / L;
        if (t < 0.0) t = 0.0;
        if (t > 1.0) t = 1.0;
    }
    return hypot(px - (ax + t * dx), py - (ay + t * dy));
}

static double d_seg_seg(double ax, double ay, double bx, double by,
                        double cx, double cy, double dx, double dy)
{
    /* Same orientation test the Python uses: if the segments properly
     * cross, the distance is exactly zero and no endpoint term is right. */
    double d1 = (dx - cx) * (ay - cy) - (dy - cy) * (ax - cx);
    double d2 = (dx - cx) * (by - cy) - (dy - cy) * (bx - cx);
    double d3 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
    double d4 = (bx - ax) * (dy - ay) - (by - ay) * (dx - ax);
    double m, t;
    if (((d1 > 0.0) != (d2 > 0.0)) && ((d3 > 0.0) != (d4 > 0.0)))
        return 0.0;
    m = d_pt_seg(ax, ay, cx, cy, dx, dy);
    t = d_pt_seg(bx, by, cx, cy, dx, dy);
    if (t < m) m = t;
    t = d_pt_seg(cx, cy, ax, ay, bx, by);
    if (t < m) m = t;
    t = d_pt_seg(dx, dy, ax, ay, bx, by);
    if (t < m) m = t;
    return m;
}

static double d_pt_rect(double px, double py, const double *r)
{
    double ax = r[0] - px, bx = px - r[2];
    double ay = r[1] - py, by = py - r[3];
    if (ax < 0.0) ax = 0.0;
    if (bx > ax) ax = bx;
    if (ay < 0.0) ay = 0.0;
    if (by > ay) ay = by;
    return hypot(ax, ay);
}

static double d_seg_rect(double ax, double ay, double bx, double by,
                         const double *r)
{
    double m, t;
    if (d_pt_rect(ax, ay, r) == 0.0 || d_pt_rect(bx, by, r) == 0.0)
        return 0.0;
    /* corners in the Python's order: (x0,y0) (x1,y0) (x1,y1) (x0,y1) */
    m = d_seg_seg(ax, ay, bx, by, r[0], r[1], r[2], r[1]);
    t = d_seg_seg(ax, ay, bx, by, r[2], r[1], r[2], r[3]);
    if (t < m) m = t;
    t = d_seg_seg(ax, ay, bx, by, r[2], r[3], r[0], r[3]);
    if (t < m) m = t;
    t = d_seg_seg(ax, ay, bx, by, r[0], r[3], r[0], r[1]);
    if (t < m) m = t;
    return m;
}

static double d_rect_rect(const double *p, const double *q)
{
    double x = p[0] - q[2], x2 = q[0] - p[2];
    double y = p[1] - q[3], y2 = q[1] - p[3];
    if (x2 > x) x = x2;
    if (x < 0.0) x = 0.0;
    if (y2 > y) y = y2;
    if (y < 0.0) y = 0.0;
    return hypot(x, y);
}

/* gap(f, g) with f = index i and g = index j, matching the Python's
 * dispatch order exactly. */
static double gap_of(int ki, const double *gi, double hwi,
                     int kj, const double *gj, double hwj)
{
    double d;
    if (ki == 0 && kj == 0)
        d = d_rect_rect(gi, gj);
    else if (ki == 0 && kj == 1)
        d = d_seg_rect(gj[0], gj[1], gj[2], gj[3], gi);
    else if (ki == 1 && kj == 0)
        d = d_seg_rect(gi[0], gi[1], gi[2], gi[3], gj);
    else if (ki == 0 && kj == 2)
        d = d_pt_rect(gj[0], gj[1], gi);
    else if (ki == 2 && kj == 0)
        d = d_pt_rect(gi[0], gi[1], gj);
    else if (ki == 1 && kj == 1)
        d = d_seg_seg(gi[0], gi[1], gi[2], gi[3],
                      gj[0], gj[1], gj[2], gj[3]);
    else if (ki == 1 && kj == 2)
        d = d_pt_seg(gj[0], gj[1], gi[0], gi[1], gi[2], gi[3]);
    else if (ki == 2 && kj == 1)
        d = d_pt_seg(gi[0], gi[1], gj[0], gj[1], gj[2], gj[3]);
    else
        d = hypot(gi[0] - gj[0], gi[1] - gj[1]);
    return d - hwi - hwj;
}

/* One feature's bounding box grown by its own half-width.  The real copper
 * is always inside this, so box-to-box distance is a LOWER BOUND on the
 * gap - which is what makes skipping distant pairs exact rather than
 * approximate. */
static void ebox(int k, const double *g, double hw, double *out)
{
    double x0, y0, x1, y1;
    if (k == 0) {
        x0 = g[0]; y0 = g[1]; x1 = g[2]; y1 = g[3];
    } else if (k == 1) {
        x0 = g[0] < g[2] ? g[0] : g[2];
        x1 = g[0] < g[2] ? g[2] : g[0];
        y0 = g[1] < g[3] ? g[1] : g[3];
        y1 = g[1] < g[3] ? g[3] : g[1];
    } else {
        x0 = x1 = g[0]; y0 = y1 = g[1];
    }
    out[0] = x0 - hw; out[1] = y0 - hw;
    out[2] = x1 + hw; out[3] = y1 + hw;
}

/* Stamp the copper of routed segments into a per-layer boolean mask.
 *
 * pour_connectivity() models the pour from the REAL copper, which means
 * walking every non-GND trace and marking the cells its width covers.  The
 * Python did that by sampling each segment every GRID/2 and stamping a
 * square at each sample - 280000 interpreter round trips on this board, the
 * largest single block of Python left in a production run.
 *
 * This is a transcription, not an improvement: same sampling count, same
 * floor/ceil cell arithmetic, same clamping, so the mask comes out
 * bit-identical.  Rasterising the capsule analytically would be faster
 * still and would NOT be identical, which is not a trade worth making for
 * the checker that decides whether the board can be built.
 *
 * mask is (2, ny, nx) uint8.  layer[i] is 0 or 1, or -1 to stamp both.
 */
void block_segments(int nx, int ny, unsigned char *mask,
                    int nseg, const int *layer,
                    const double *ax, const double *ay,
                    const double *bx, const double *by,
                    const double *hw, double grid)
{
    int s, i, L, y, xa, xb;
    long plane = (long)ny * nx;
    for (s = 0; s < nseg; s++) {
        double dx = bx[s] - ax[s], dy = by[s] - ay[s];
        double len = sqrt(dx * dx + dy * dy);
        int n = (int)(len / (grid / 2.0));
        if (n < 1) n = 1;
        for (i = 0; i <= n; i++) {
            double t = (double)i / (double)n;
            double cx = ax[s] + dx * t, cy = ay[s] + dy * t;
            int a = (int)floor((cx - hw[s]) / grid);
            int b = (int)floor((cy - hw[s]) / grid);
            int c = (int)ceil((cx + hw[s]) / grid);
            int d = (int)ceil((cy + hw[s]) / grid);
            if (a < 0) a = 0;
            if (b < 0) b = 0;
            if (c > nx - 1) c = nx - 1;
            if (d > ny - 1) d = ny - 1;
            if (a > c || b > d) continue;
            for (L = (layer[s] < 0 ? 0 : layer[s]);
                 L <= (layer[s] < 0 ? 1 : layer[s]); L++) {
                unsigned char *p = mask + (long)L * plane;
                for (y = b; y <= d; y++) {
                    xa = y * nx + a;
                    xb = y * nx + c;
                    memset(p + xa, 1, (size_t)(xb - xa + 1));
                }
            }
        }
    }
}


/* All pairs i<j that share a layer, pass the net filter, and whose exact
 * gap is below the threshold.
 *
 *   same_net 0 -> only pairs on DIFFERENT nets, reported when gap < limit-eps
 *                 (the clearance check)
 *   same_net 1 -> only pairs on the SAME net, reported when gap <= eps
 *                 (the connectivity check)
 *
 * Writes 2 ints per hit into out.  Returns the number of hits, or -1 if it
 * would overflow max_out. */
int pair_scan(int n, const int *kind, const double *g, const double *hw,
              const long long *lay, const long long *net,
              double limit, double eps, int same_net,
              int *out, int max_out)
{
    int i, j, nhit = 0;
    double *bx = (double *)malloc((size_t)n * 4 * sizeof(double));
    if (!bx) return -1;
    for (i = 0; i < n; i++)
        ebox(kind[i], g + 4 * i, hw[i], bx + 4 * i);

    for (i = 0; i < n; i++) {
        const double *bi = bx + 4 * i;
        for (j = i + 1; j < n; j++) {
            const double *bj = bx + 4 * j;
            double dx, dy, d;
            if (!(lay[i] & lay[j]))
                continue;
            if (same_net) {
                if (net[i] != net[j]) continue;
            } else {
                if (net[i] == net[j]) continue;
            }
            /* box-to-box distance, the exact lower bound */
            dx = bj[0] - bi[2];
            if (bi[0] - bj[2] > dx) dx = bi[0] - bj[2];
            if (dx < 0.0) dx = 0.0;
            dy = bj[1] - bi[3];
            if (bi[1] - bj[3] > dy) dy = bi[1] - bj[3];
            if (dy < 0.0) dy = 0.0;
            if (dx * dx + dy * dy > limit * limit)
                continue;
            d = gap_of(kind[i], g + 4 * i, hw[i],
                       kind[j], g + 4 * j, hw[j]);
            if (same_net ? (d <= eps) : (d < limit - eps)) {
                if (nhit >= max_out) { free(bx); return -1; }
                out[2 * nhit] = i;
                out[2 * nhit + 1] = j;
                nhit++;
            }
        }
    }
    free(bx);
    return nhit;
}

/* For each feature in A, the SMALLEST index in B that it violates, or -1.
 *
 * "Smallest index" rather than "any": the silkscreen check reports one
 * problem per label and the Python picks the first offender in list order,
 * so anything else here would reorder verify()'s output.  Callers order B
 * to match the order the Python scanned in.  */
void cross_first(int na, const int *ka, const double *ga, const double *hwa,
                 const long long *la,
                 int nb, const int *kb, const double *gb, const double *hwb,
                 const long long *lb,
                 double limit, double eps, int *out)
{
    int i, j;
    double bi[4], *bbs = (double *)malloc((size_t)nb * 4 * sizeof(double));
    if (!bbs) {
        for (i = 0; i < na; i++) out[i] = -1;
        return;
    }
    for (j = 0; j < nb; j++)
        ebox(kb[j], gb + 4 * j, hwb[j], bbs + 4 * j);
    for (i = 0; i < na; i++) {
        out[i] = -1;
        ebox(ka[i], ga + 4 * i, hwa[i], bi);
        for (j = 0; j < nb; j++) {
            const double *bj = bbs + 4 * j;
            double dx, dy;
            if (!(la[i] & lb[j]))
                continue;
            dx = bj[0] - bi[2];
            if (bi[0] - bj[2] > dx) dx = bi[0] - bj[2];
            if (dx < 0.0) dx = 0.0;
            dy = bj[1] - bi[3];
            if (bi[1] - bj[3] > dy) dy = bi[1] - bj[3];
            if (dy < 0.0) dy = 0.0;
            if (dx * dx + dy * dy > limit * limit)
                continue;
            if (gap_of(ka[i], ga + 4 * i, hwa[i],
                       kb[j], gb + 4 * j, hwb[j]) < limit - eps) {
                out[i] = j;
                break;
            }
        }
    }
    free(bbs);
}


/* Does any candidate feature come within `limit` of any foreign one?
 * Two separate arrays, so this is the cand-vs-others shape that
 * path_clearance_ok needs.  Returns 1 on the first violation. */
int cross_any(int na, const int *ka, const double *ga, const double *hwa,
              const long long *la,
              int nb, const int *kb, const double *gb, const double *hwb,
              const long long *lb,
              double limit, double eps)
{
    int i, j;
    double bi[4], bj[4];
    for (i = 0; i < na; i++) {
        ebox(ka[i], ga + 4 * i, hwa[i], bi);
        for (j = 0; j < nb; j++) {
            double dx, dy;
            if (!(la[i] & lb[j]))
                continue;
            ebox(kb[j], gb + 4 * j, hwb[j], bj);
            dx = bj[0] - bi[2];
            if (bi[0] - bj[2] > dx) dx = bi[0] - bj[2];
            if (dx < 0.0) dx = 0.0;
            dy = bj[1] - bi[3];
            if (bi[1] - bj[3] > dy) dy = bi[1] - bj[3];
            if (dy < 0.0) dy = 0.0;
            if (dx * dx + dy * dy > limit * limit)
                continue;
            if (gap_of(ka[i], ga + 4 * i, hwa[i],
                       kb[j], gb + 4 * j, hwb[j]) < limit - eps)
                return 1;
        }
    }
    return 0;
}
