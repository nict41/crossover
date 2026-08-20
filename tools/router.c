/* Grid maze router inner loop.
 *
 * This is a direct port of the A* in gen_pcb_smd.py, which was the whole
 * program's bottleneck: pure-Python heap operations over a two-layer grid
 * of ~1500 x 800 cells, where a FAILING search drains the queue across the
 * entire reachable area (~2.4M nodes) and every relaxed retry does it
 * again.  Full runs took minutes, which made routing far too expensive to
 * use as feedback for anything else - so placement quality ended up being
 * guessed at through hand-tuned proxy terms instead of measured.
 *
 * Semantics are deliberately identical to the Python version, including
 * the 1.02x weighted heuristic and the strict-vs-relaxed distinction, so
 * this is a speed change and nothing else.  The independent geometric
 * verifier in gen_pcb_smd.py checks the result either way.
 *
 * Cell state is passed in as the caller's own numpy arrays:
 *   occ        int32, per layer: net id claiming the cell, 0 = free
 *   contested  uint8, per layer: two nets' dilated margins overlap here
 *   blocked    uint8, per layer: this net is too wide for this cell
 *   use        int32, per layer: routed traces currently occupying it,
 *              for negotiated congestion; NULL to ignore
 *   hist       float, per layer: accumulated congestion history
 */

#include <stdlib.h>
#include <string.h>
#include <math.h>

typedef struct { double f; int node; } HeapItem;

typedef struct {
    HeapItem *a;
    long n, cap;
} Heap;

static void heap_push(Heap *h, double f, int node) {
    if (h->n == h->cap) {
        h->cap = h->cap ? h->cap * 2 : 1024;
        h->a = (HeapItem *) realloc(h->a, h->cap * sizeof(HeapItem));
    }
    long i = h->n++;
    h->a[i].f = f;
    h->a[i].node = node;
    while (i > 0) {
        long p = (i - 1) / 2;
        if (h->a[p].f <= h->a[i].f) break;
        HeapItem t = h->a[p]; h->a[p] = h->a[i]; h->a[i] = t;
        i = p;
    }
}

static int heap_pop(Heap *h, double *f) {
    if (h->n == 0) return -1;
    HeapItem top = h->a[0];
    h->a[0] = h->a[--h->n];
    long i = 0;
    for (;;) {
        long l = 2 * i + 1, r = l + 1, m = i;
        if (l < h->n && h->a[l].f < h->a[m].f) m = l;
        if (r < h->n && h->a[r].f < h->a[m].f) m = r;
        if (m == i) break;
        HeapItem t = h->a[m]; h->a[m] = h->a[i]; h->a[i] = t;
        i = m;
    }
    *f = top.f;
    return top.node;
}

typedef struct {
    int NX, NY, NL;
    /* plane_l: a copper layer that exists but is not ROUTABLE - the solid
     * GND plane on a 4-layer board.  It is excluded from the grid rather
     * than filled with an occupying net id, because a through-hole via
     * has to be allowed to pass through it (the plane's antipad is cut by
     * the pour, not by the router) and via_ok() would otherwise refuse
     * every via on the board.  -1 for a board with no such layer. */
    int plane_l;
    const int *occ;              /* [NL][NY][NX] */
    const unsigned char *cont;   /* [NL][NY][NX] */
    const unsigned char *blk;    /* [NL][NY][NX] or NULL */
    const int *use;              /* [NL][NY][NX] or NULL */
    const float *hist;           /* [NL][NY][NX] or NULL */
    int nid, relaxed, via_r;
    double pres_fac;
} Ctx;

#define IDX(c, L, x, y) (((L) * (c)->NY + (y)) * (c)->NX + (x))

static int passable(const Ctx *c, int L, int x, int y) {
    if (L == c->plane_l) return 0;
    if (x < 1 || x >= c->NX - 1 || y < 1 || y >= c->NY - 1) return 0;
    long i = IDX(c, L, x, y);
    if (!c->relaxed && c->cont[i]) return 0;
    int v = c->occ[i];
    if (v != 0 && v != c->nid) return 0;
    if (c->blk && c->blk[i]) return 0;
    return 1;
}

/* A via needs more room than the track leading to it, and unlike passable()
 * it is never relaxed - nothing downstream re-checks a via before treating
 * the cells around it as claimed. */
static int via_ok(const Ctx *c, int x, int y) {
    int r = c->via_r;
    for (int L = 0; L < c->NL; L++) {
        if (L == c->plane_l) continue;
        int a = x - r < 0 ? 0 : x - r, b = y - r < 0 ? 0 : y - r;
        int d = x + r > c->NX - 1 ? c->NX - 1 : x + r;
        int e = y + r > c->NY - 1 ? c->NY - 1 : y + r;
        for (int yy = b; yy <= e; yy++) {
            long row = IDX(c, L, 0, yy);
            for (int xx = a; xx <= d; xx++) {
                int v = c->occ[row + xx];
                if ((v != 0 && v != c->nid) || c->cont[row + xx]) return 0;
            }
        }
    }
    return 1;
}

/* Cost of entering a cell.  Base 1 per step, plus negotiated-congestion
 * terms when `use`/`hist` are supplied: history makes a cell that has been
 * fought over repeatedly permanently less attractive, present cost makes
 * one that is over-used right now expensive.  With both NULL this is
 * exactly the old uniform-cost grid. */
static double cell_cost(const Ctx *c, long i) {
    double base = 1.0;
    if (c->hist) base += c->hist[i];
    if (c->use && c->use[i] > 0) base *= 1.0 + c->pres_fac * c->use[i];
    return base;
}

/* Search state, kept across calls.
 *
 * A route on this board visits a grid of ~2.9M nodes per layer pair, so a
 * fresh malloc + initialisation loop per net was costing ~41 MB of
 * allocation and ~2.9M writes BEFORE the search did anything - about 60
 * times per board, plus retries.  The buffers are therefore allocated once
 * and reused, and `g` is never cleared: a per-call epoch counter marks
 * which entries belong to the current search, so a stale value from a
 * previous net reads as unvisited rather than having to be overwritten.
 *
 * Not thread-safe, deliberately: one board is routed by one thread, and
 * parallel searches are run as separate processes (tools/find_board.py). */
static double *g_cost = NULL;
static int *g_prev = NULL;
static int *g_stamp = NULL;
static unsigned char *g_done = NULL;
static unsigned char *g_tgt = NULL;
static long g_N = 0;
static int g_epoch = 0;

static int ensure_buffers(long N) {
    if (N == g_N) return 1;
    free(g_cost); free(g_prev); free(g_stamp); free(g_done); free(g_tgt);
    g_cost  = (double *) malloc(N * sizeof(double));
    g_prev  = (int *) malloc(N * sizeof(int));
    g_stamp = (int *) calloc(N, sizeof(int));
    g_done  = (unsigned char *) calloc(N, 1);
    g_tgt   = (unsigned char *) calloc(N, 1);
    if (!g_cost || !g_prev || !g_stamp || !g_done || !g_tgt) { g_N = 0; return 0; }
    g_N = N;
    g_epoch = 0;
    return 1;
}

/* Returns path length in nodes, writing (layer, x, y) triples into out.
 * -1 if no path.  `out` must have room for cap triples. */
int route(int NX, int NY, int NL, int plane_l,
          const int *occ, const unsigned char *cont,
          const unsigned char *blk, const int *use, const float *hist,
          int nid, int relaxed, int via_r, double pres_fac, double via_cost,
          const int *src, int nsrc, const int *tgt, int ntgt,
          int tgx, int tgy, int *out, int cap, long max_expand)
{
    Ctx c = { NX, NY, NL, plane_l, occ, cont, blk, use, hist,
              nid, relaxed, via_r, pres_fac };
    long N = (long) NL * NY * NX;
    if (!ensure_buffers(N)) return -1;

    g_epoch++;
    if (g_epoch <= 0) {                 /* wrapped - retire every old stamp */
        memset(g_stamp, 0, N * sizeof(int));
        g_epoch = 1;
    }
    double *g = g_cost;
    int *prev = g_prev;
    int *stamp = g_stamp;
    unsigned char *done = g_done, *is_tgt = g_tgt;
    memset(done, 0, N);

    for (int i = 0; i < ntgt; i++)
        is_tgt[IDX(&c, tgt[3*i], tgt[3*i+1], tgt[3*i+2])] = 1;

    Heap h = { NULL, 0, 0 };
    for (int i = 0; i < nsrc; i++) {
        int L = src[3*i], x = src[3*i+1], y = src[3*i+2];
        if (!passable(&c, L, x, y)) continue;
        long k = IDX(&c, L, x, y);
        if (stamp[k] == g_epoch && g[k] == 0.0) continue;
        g[k] = 0.0; stamp[k] = g_epoch; prev[k] = -1;
        heap_push(&h, 0.0, (int) k);
    }

    int found = -1;
    double f;
    int node;
    long expanded = 0;
    while ((node = heap_pop(&h, &f)) >= 0) {
        if (done[node]) continue;
        /* Expansion cap.  A FAILING search is far more expensive than a
         * successful one - with no path to the target it drains the queue
         * across the whole reachable grid, ~2.9M nodes here - and a board
         * search spends most of its time on boards that fail.
         *
         * This is a SEARCH FILTER, not a production setting: capping makes
         * the router pessimistic (it will give up on some routes it could
         * have found), exactly like GRID=0.5, so a candidate that passes
         * still has to be confirmed by an uncapped run.  Capping it in
         * production was tried twice and reverted both times - it silently
         * broke routable nets.  0 means no cap. */
        if (max_expand > 0 && ++expanded > max_expand) { found = -1; break; }
        done[node] = 1;
        if (is_tgt[node]) { found = node; break; }

        int L = (int) (node / ((long) NY * NX));
        int rem = (int) (node % ((long) NY * NX));
        int y = rem / NX, x = rem % NX;
        double gc = g[node];

        /* Four planar steps, then one layer change per OTHER layer.  A
         * via here is a through-hole one - JLCPCB's standard 4-layer
         * process has no blind or buried vias - so it joins every layer
         * at once and a step to any other layer costs the same single
         * via.  With NL = 2 this is exactly the old `nl = 1 - L`. */
        for (int d = 0; d < 4 + (NL - 1); d++) {
            int nl = L, nx = x, ny = y;
            double step;
            switch (d) {
                case 0: nx = x + 1; step = 1.0; break;
                case 1: nx = x - 1; step = 1.0; break;
                case 2: ny = y + 1; step = 1.0; break;
                case 3: ny = y - 1; step = 1.0; break;
                default: nl = (L + 1 + (d - 4)) % NL; step = via_cost; break;
            }
            if (!passable(&c, nl, nx, ny)) continue;
            if (nl != L && !via_ok(&c, x, y)) continue;
            long k = IDX(&c, nl, nx, ny);
            if (done[k]) continue;
            double ng = gc + step * cell_cost(&c, k);
            if (stamp[k] == g_epoch && ng >= g[k]) continue;
            g[k] = ng; stamp[k] = g_epoch;
            prev[k] = node;
            /* 1.02x weighted heuristic: a plain Manhattan heuristic on a
             * mostly-open grid leaves huge flat frontiers of equal-f nodes
             * and degrades toward a blind flood exactly where a route has
             * to cross open board. A valid DRC-clean route is all that is
             * wanted here, not a provably shortest one. */
            double hh = 1.02 * (fabs((double) (nx - tgx)) + fabs((double) (ny - tgy)));
            heap_push(&h, ng + hh, (int) k);
        }
    }

    int len = -1;
    if (found >= 0) {
        int n = 0;
        for (int cur = found; cur >= 0; cur = prev[cur]) n++;
        if (n <= cap) {
            len = n;
            int i = n - 1;
            for (int cur = found; cur >= 0; cur = prev[cur], i--) {
                int L = (int) (cur / ((long) NY * NX));
                int rem = (int) (cur % ((long) NY * NX));
                out[3*i] = L;
                out[3*i+1] = rem % NX;
                out[3*i+2] = rem / NX;
            }
        }
    }

    for (int i = 0; i < ntgt; i++)      /* leave g_tgt clear for the next call */
        is_tgt[IDX(&c, tgt[3*i], tgt[3*i+1], tgt[3*i+2])] = 0;
    free(h.a);
    return len;
}

/* ------------------------------------------------------------------ *
 * Copper-pour connectivity.
 *
 * The board carries a ground pour on both layers, and nothing checked it:
 * verify() models pads, tracks and vias only, so the pour was emitted and
 * simply hoped for.  That is why GND had to be ROUTED as an ordinary net
 * as well - the plane could not be relied on, so the board paid for ground
 * twice, and the routed copy went first and claimed the best channels.
 *
 * `mask` marks cells the pour can occupy, `joint` marks cells where the two
 * layers are tied together (a via or a plated through-hole).  This floods
 * from the seeds and writes back which cells were reached, so the caller
 * can ask the only question that matters: is every ground pad in the same
 * piece of copper?
 * ------------------------------------------------------------------ */
int flood(int NX, int NY, int NL, const unsigned char *mask,
          const unsigned char *joint, const int *seeds, int nseed,
          unsigned char *seen)
{
    long N = (long) NL * NY * NX;
    int *q = (int *) malloc(N * sizeof(int));
    if (!q) return -1;
    long head = 0, tail = 0;
    for (int i = 0; i < nseed; i++) {
        int L = seeds[3*i], x = seeds[3*i+1], y = seeds[3*i+2];
        if (x < 0 || y < 0 || x >= NX || y >= NY) continue;
        long k = ((long) L * NY + y) * NX + x;
        if (!mask[k] || seen[k]) continue;
        seen[k] = 1;
        q[tail++] = (int) k;
    }
    long reached = tail;
    while (head < tail) {
        int node = q[head++];
        int L = (int) (node / ((long) NY * NX));
        int rem = (int) (node % ((long) NY * NX));
        int y = rem / NX, x = rem % NX;
        for (int d = 0; d < 4 + (NL - 1); d++) {
            int nl = L, nx = x, ny = y;
            switch (d) {
                case 0: nx++; break;
                case 1: nx--; break;
                case 2: ny++; break;
                case 3: ny--; break;
                /* only where the layers meet - a plated hole ties ALL of
                 * them, so every other layer is one step away */
                default: nl = (L + 1 + (d - 4)) % NL; break;
            }
            if (nx < 0 || ny < 0 || nx >= NX || ny >= NY) continue;
            long k = ((long) nl * NY + ny) * NX + nx;
            if (d >= 4 && !joint[((long) L * NY + y) * NX + x]) continue;
            if (!mask[k] || seen[k]) continue;
            seen[k] = 1;
            q[tail++] = (int) k;
            reached++;
        }
    }
    free(q);
    return (int) (reached > 2147483647L ? 2147483647L : reached);
}
