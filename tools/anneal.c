/* Simulated-annealing placement inner loop.
 *
 * A direct port of Placer.anneal() in place.py, for exactly the reason
 * router.c exists: after the router moved to C, PLACEMENT became 85% of a
 * cold run (26.6 s of 31.2 s), and a board search is hundreds of
 * placements, so it dominated the whole workflow.
 *
 * It is not that numpy was the wrong tool - measured, numpy is still ~3x
 * faster than plain Python on these 49-element arrays.  It is that a move
 * costs ~400 us when the arithmetic in it is worth a few, because every
 * term is a handful of numpy calls on tiny arrays and the per-call
 * overhead is the whole cost.  edge_violation() alone was 8.7 s across
 * 133k calls: eight edge-constrained parts, eight numpy operations each.
 *
 * The cost model is deliberately identical to the Python one, term for
 * term, so the two can be compared.  The RNG differs, so a given SEED does
 * NOT give the same layout as the Python version - that is expected, and
 * why place.py stays as the readable reference implementation.
 */

#include <stdlib.h>
#include <string.h>
#include <math.h>

#define INF_D 1e300

/* Sides, board frame: 0 = left, 1 = right, 2 = top, 3 = bottom. */

typedef struct {
    int n, nr, nnets, nfixed, nnear, ngroups;
    const int *rot_base;        /* n+1 : first (part,rot) slot of each part */
    const double *box;          /* nr*4 */
    const double *need;         /* nr*4 */
    const double *pneed;        /* nr*4 : plane-net pads per side */
    const int *pad_base;        /* nr+1 */
    const double *pad_dx, *pad_dy;
    const int *group;           /* n : rigid-group id, or -1 */
    const int *outward;         /* nr : board-frame side index, or -1 */
    const int *net_base;        /* nnets+1 */
    const int *net_part, *net_pk;
    const int *pnet_base;       /* n+1 : nets each part belongs to */
    const int *pnet;
    const double *fixed;        /* nfixed*4 */
    const int *near_a, *near_b, *near_pka, *near_pkb;
    const double *near_tgt;
    double w_ov, w_esc, w_pesc, w_cong, w_hpwl, w_h, w_w, w_wfloor, w_edge, w_near;
    double plane_gap;
    double t0, t1, amp0, amp1, ov_hi_mul;
    double cell, supply;
    int gN;
} Cfg;

typedef struct {
    double *X, *Y;
    int *R;                     /* index of the chosen slot within the part */
    double *BX0, *BY0, *BX1, *BY1;
    double *esc, *pesc, *nethp, *demand;
    double ov;
} St;

/* --- deterministic RNG (xorshift128+), so a run is reproducible -------- */
static unsigned long long rs[2];
static double rnd(void) {
    unsigned long long x = rs[0], y = rs[1];
    rs[0] = y;
    x ^= x << 23;
    rs[1] = x ^ y ^ (x >> 17) ^ (y >> 26);
    return ((rs[1] + y) >> 11) * (1.0 / 9007199254740992.0);
}
static double gauss(double s) {
    double u = rnd(), v = rnd();
    if (u < 1e-12) u = 1e-12;
    return s * sqrt(-2.0 * log(u)) * cos(6.283185307179586 * v);
}
static double snap(double v) { return floor(v / 0.5 + 0.5) * 0.5; }

#define SLOT(c, st, i) ((c)->rot_base[i] + (st)->R[i])

static void sync_box(const Cfg *c, St *s, int i) {
    int pr = SLOT(c, s, i);
    s->BX0[i] = s->X[i] + c->box[pr * 4 + 0];
    s->BY0[i] = s->Y[i] + c->box[pr * 4 + 1];
    s->BX1[i] = s->X[i] + c->box[pr * 4 + 2];
    s->BY1[i] = s->Y[i] + c->box[pr * 4 + 3];
}

static double ov_parts(const Cfg *c, const St *s, int i) {
    double a = 0.0, x0 = s->BX0[i], y0 = s->BY0[i], x1 = s->BX1[i], y1 = s->BY1[i];
    for (int j = 0; j < c->n; j++) {
        if (j == i) continue;
        double w = (s->BX1[j] < x1 ? s->BX1[j] : x1) - (s->BX0[j] > x0 ? s->BX0[j] : x0);
        if (w <= 0) continue;
        double h = (s->BY1[j] < y1 ? s->BY1[j] : y1) - (s->BY0[j] > y0 ? s->BY0[j] : y0);
        if (h <= 0) continue;
        a += w * h;
    }
    return a;
}

static double ov_fixed(const Cfg *c, const St *s, int i) {
    double a = 0.0;
    for (int k = 0; k < c->nfixed; k++) {
        const double *f = c->fixed + k * 4;
        double w = (f[2] < s->BX1[i] ? f[2] : s->BX1[i]) - (f[0] > s->BX0[i] ? f[0] : s->BX0[i]);
        double h = (f[3] < s->BY1[i] ? f[3] : s->BY1[i]) - (f[1] > s->BY0[i] ? f[1] : s->BY0[i]);
        if (w > 0 && h > 0) a += w * h;
    }
    return a;
}

static double overlap_of(const Cfg *c, const St *s, int i) {
    return ov_parts(c, s, i) + ov_fixed(c, s, i);
}

static double total_overlap(const Cfg *c, const St *s) {
    double p = 0.0, f = 0.0;
    for (int i = 0; i < c->n; i++) { p += ov_parts(c, s, i); f += ov_fixed(c, s, i); }
    return p / 2.0 + f;
}

/* Scores BOTH escape terms, because finding the four gaps is a scan over
   every other part and the arithmetic afterwards is a handful of
   multiplications - see Placer.escape_of() for what each one prices. */
static void escape_of(const Cfg *c, St *s, int i) {
    int pr = SLOT(c, s, i);
    const double *nd = c->need + pr * 4;
    const double *pn = c->pneed + pr * 4;
    s->esc[i] = s->pesc[i] = 0.0;
    if (nd[0] == 0 && nd[1] == 0 && nd[2] == 0 && nd[3] == 0 &&
        pn[0] == 0 && pn[1] == 0 && pn[2] == 0 && pn[3] == 0) return;
    double x0 = s->BX0[i], y0 = s->BY0[i], x1 = s->BX1[i], y1 = s->BY1[i];
    double g[4] = { INF_D, INF_D, INF_D, INF_D };
    for (int j = 0; j < c->n; j++) {
        if (j == i) continue;
        if (s->BY0[j] < y1 && s->BY1[j] > y0) {          /* shares a row band */
            if (s->BX1[j] <= x0 && x0 - s->BX1[j] < g[0]) g[0] = x0 - s->BX1[j];
            if (s->BX0[j] >= x1 && s->BX0[j] - x1 < g[1]) g[1] = s->BX0[j] - x1;
        }
        if (s->BX0[j] < x1 && s->BX1[j] > x0) {          /* shares a column */
            if (s->BY1[j] <= y0 && y0 - s->BY1[j] < g[2]) g[2] = y0 - s->BY1[j];
            if (s->BY0[j] >= y1 && s->BY0[j] - y1 < g[3]) g[3] = s->BY0[j] - y1;
        }
    }
    for (int k = 0; k < c->nfixed; k++) {
        const double *f = c->fixed + k * 4;
        if (f[1] < y1 && f[3] > y0) {
            if (f[2] <= x0 && x0 - f[2] < g[0]) g[0] = x0 - f[2];
            if (f[0] >= x1 && f[0] - x1 < g[1]) g[1] = f[0] - x1;
        }
        if (f[0] < x1 && f[2] > x0) {
            if (f[3] <= y0 && y0 - f[3] < g[2]) g[2] = y0 - f[3];
            if (f[1] >= y1 && f[1] - y1 < g[3]) g[3] = f[1] - y1;
        }
    }
    double pen = 0.0, ppen = 0.0;
    for (int k = 0; k < 4; k++) {
        if (nd[k] > 0 && g[k] < nd[k]) { double d = nd[k] - g[k]; pen += d * d; }
        if (pn[k] > 0 && g[k] < c->plane_gap) {
            double d = c->plane_gap - g[k];
            ppen += pn[k] * d * d;
        }
    }
    s->esc[i] = pen;
    s->pesc[i] = ppen;
}

static double edge_violation(const Cfg *c, const St *s) {
    double pen = 0.0;
    for (int i = 0; i < c->n; i++) {
        int side = c->outward[SLOT(c, s, i)];
        if (side < 0) continue;
        int gi = c->group[i];
        for (int j = 0; j < c->n; j++) {
            if (j == i) continue;
            if (gi >= 0 && c->group[j] == gi) continue;   /* a group can't block itself */
            double d;
            if (side == 0 || side == 1) {
                if (!(s->BY0[j] < s->BY1[i] && s->BY1[j] > s->BY0[i])) continue;
                d = (side == 1) ? s->BX1[j] - s->BX1[i] : s->BX0[i] - s->BX0[j];
            } else {
                if (!(s->BX0[j] < s->BX1[i] && s->BX1[j] > s->BX0[i])) continue;
                d = (side == 3) ? s->BY1[j] - s->BY1[i] : s->BY0[i] - s->BY0[j];
            }
            if (d > 0) pen += d * d;
        }
    }
    return pen;
}

static void pin_on(const Cfg *c, const St *s, int i, int pk, double *px, double *py) {
    int pr = SLOT(c, s, i), b = c->pad_base[pr];
    if (pk < 0) { *px = s->X[i]; *py = s->Y[i]; return; }
    *px = s->X[i] + c->pad_dx[b + pk];
    *py = s->Y[i] + c->pad_dy[b + pk];
}

static double near_penalty(const Cfg *c, const St *s) {
    double pen = 0.0;
    for (int k = 0; k < c->nnear; k++) {
        double ax, ay, bx, by;
        pin_on(c, s, c->near_a[k], c->near_pka[k], &ax, &ay);
        pin_on(c, s, c->near_b[k], c->near_pkb[k], &bx, &by);
        double d = hypot(ax - bx, ay - by);
        if (d > c->near_tgt[k]) { d -= c->near_tgt[k]; pen += d * d; }
    }
    return pen;
}

static void net_rect(const Cfg *c, const St *s, int ni, double *r) {
    r[0] = r[1] = INF_D; r[2] = r[3] = -INF_D;
    for (int m = c->net_base[ni]; m < c->net_base[ni + 1]; m++) {
        double px, py;
        pin_on(c, s, c->net_part[m], c->net_pk[m], &px, &py);
        if (px < r[0]) r[0] = px;
        if (py < r[1]) r[1] = py;
        if (px > r[2]) r[2] = px;
        if (py > r[3]) r[3] = py;
    }
}

static void rudy(const Cfg *c, St *s, int ni, double sign) {
    double r[4];
    net_rect(c, s, ni, r);
    double org = c->gN * c->cell / 2.0;
    double w = r[2] - r[0], h = r[3] - r[1];
    /* Clamp to one cell, not to 1e-6.  RUDY divides by the box AREA, so a
       collinear net - an op-amp follower ties its output to its inverting
       input, two adjacent pins on the same side of the package, giving a
       box of exactly zero width - injected ~1e7 of demand into one cell.
       See _rudy() in place.py for the measurement. */
    if (w < c->cell) w = c->cell;
    if (h < c->cell) h = c->cell;
    int a = (int)((r[0] + org) / c->cell), b = (int)((r[1] + org) / c->cell);
    int d = (int)((r[2] + org) / c->cell), e = (int)((r[3] + org) / c->cell);
    if (a < 0) a = 0; if (b < 0) b = 0;
    if (d > c->gN - 1) d = c->gN - 1;
    if (e > c->gN - 1) e = c->gN - 1;
    if (d < a || e < b) return;
    double v = sign * (w + h) / (w * h) * c->cell;
    for (int y = b; y <= e; y++)
        for (int x = a; x <= d; x++) s->demand[y * c->gN + x] += v;
}

static double congestion(const Cfg *c, const St *s) {
    double t = 0.0;
    int N = c->gN * c->gN;
    for (int k = 0; k < N; k++) { double o = s->demand[k] - c->supply; if (o > 0) t += o; }
    return t;
}

static double score(const Cfg *c, St *s, double w_ov, double ov) {
    double x0 = INF_D, y0 = INF_D, x1 = -INF_D, y1 = -INF_D, e = 0.0, pe = 0.0, hp = 0.0;
    for (int i = 0; i < c->n; i++) {
        if (s->BX0[i] < x0) x0 = s->BX0[i];
        if (s->BY0[i] < y0) y0 = s->BY0[i];
        if (s->BX1[i] > x1) x1 = s->BX1[i];
        if (s->BY1[i] > y1) y1 = s->BY1[i];
        e += s->esc[i];
        pe += s->pesc[i];
    }
    for (int k = 0; k < c->nnets; k++) hp += s->nethp[k];
    double wide = (x1 - x0) - c->w_wfloor;
    if (wide < 0) wide = 0;
    return w_ov * ov + c->w_esc * e + c->w_pesc * pe + c->w_cong * congestion(c, s)
         + c->w_hpwl * hp + c->w_h * (y1 - y0) + c->w_w * wide
         + c->w_edge * edge_violation(c, s) + c->w_near * near_penalty(c, s);
}

static void refresh(const Cfg *c, St *s) {
    for (int i = 0; i < c->n; i++) escape_of(c, s, i);
    memset(s->demand, 0, sizeof(double) * c->gN * c->gN);
    for (int k = 0; k < c->nnets; k++) {
        rudy(c, s, k, +1.0);
        double r[4];
        net_rect(c, s, k, r);
        s->nethp[k] = (r[2] - r[0]) + (r[3] - r[1]);
    }
}

int anneal(const Cfg *c, int moves, unsigned long long seed,
           double *X, double *Y, int *R)
{
    St st;
    st.X = X; st.Y = Y; st.R = R;
    st.BX0 = malloc(sizeof(double) * c->n); st.BY0 = malloc(sizeof(double) * c->n);
    st.BX1 = malloc(sizeof(double) * c->n); st.BY1 = malloc(sizeof(double) * c->n);
    st.esc = malloc(sizeof(double) * c->n);
    st.pesc = malloc(sizeof(double) * c->n);
    st.nethp = malloc(sizeof(double) * (c->nnets ? c->nnets : 1));
    st.demand = malloc(sizeof(double) * c->gN * c->gN);
    double *bX = malloc(sizeof(double) * c->n), *bY = malloc(sizeof(double) * c->n);
    int *bR = malloc(sizeof(int) * c->n);
    int *touch = malloc(sizeof(int) * (c->nnets ? c->nnets : 1));
    int *grp = malloc(sizeof(int) * c->n);
    rs[0] = seed * 6364136223846793005ULL + 1442695040888963407ULL;
    rs[1] = seed ^ 0x9E3779B97F4A7C15ULL;
    for (int i = 0; i < 64; i++) rnd();

    for (int i = 0; i < c->n; i++) sync_box(c, &st, i);
    refresh(c, &st);
    double ov = total_overlap(c, &st);
    double ov_lo = c->w_ov, ov_hi = c->w_ov * c->ov_hi_mul;
    double best = (ov < 1e-9) ? score(c, &st, ov_lo, ov) : INF_D;
    memcpy(bX, X, sizeof(double) * c->n);
    memcpy(bY, Y, sizeof(double) * c->n);
    memcpy(bR, R, sizeof(int) * c->n);

    for (int step = 0; step < moves; step++) {
        double f = (double)step / (double)moves;
        double temp = c->t0 * pow(c->t1 / c->t0, f);
        double amp = c->amp0 * pow(c->amp1 / c->amp0, f);
        double w_ov = ov_lo * pow(ov_hi / ov_lo, f);
        double cost = score(c, &st, w_ov, ov);

        /* --- propose ------------------------------------------------- */
        int ng = 0;
        double kind = rnd();
        int i = 0, j = -1;
        if (kind < 0.62) {
            /* Covers both the random walk and the directed move below;
               which one happens is decided when the move is applied. */
            do { i = (int)(rnd() * c->n); } while (c->group[i] >= 0);
            grp[ng++] = i;
        } else if (kind < 0.78) {
            int tries = 0;
            do { i = (int)(rnd() * c->n); tries++; }
            while ((c->group[i] >= 0 ||
                    c->rot_base[i + 1] - c->rot_base[i] < 2) && tries < 200);
            if (c->group[i] >= 0 || c->rot_base[i + 1] - c->rot_base[i] < 2) continue;
            grp[ng++] = i;
        } else if (kind < 0.9) {
            int tries = 0;
            do { i = (int)(rnd() * c->n); j = (int)(rnd() * c->n); tries++; }
            while ((i == j || c->group[i] >= 0 || c->group[j] >= 0 ||
                    fabs((c->box[SLOT(c,&st,i)*4+2] - c->box[SLOT(c,&st,i)*4+0])
                       - (c->box[SLOT(c,&st,j)*4+2] - c->box[SLOT(c,&st,j)*4+0])) > 0.01)
                   && tries < 200);
            if (tries >= 200) continue;
            grp[ng++] = i; grp[ng++] = j;
        } else {
            if (c->ngroups <= 0) continue;
            int want = (int)(rnd() * c->ngroups);
            for (int k = 0; k < c->n; k++) if (c->group[k] == want) grp[ng++] = k;
            if (!ng) continue;
        }

        double oX[8], oY[8]; int oR[8];
        if (ng > 8) continue;
        for (int k = 0; k < ng; k++) { oX[k] = X[grp[k]]; oY[k] = Y[grp[k]]; oR[k] = R[grp[k]]; }
        double ov_before = 0.0;
        for (int k = 0; k < ng; k++) ov_before += overlap_of(c, &st, grp[k]);

        int nt = 0;
        for (int k = 0; k < c->nnets; k++) {
            int hit = 0;
            for (int m = c->net_base[k]; m < c->net_base[k + 1] && !hit; m++)
                for (int q = 0; q < ng; q++) if (c->net_part[m] == grp[q]) { hit = 1; break; }
            if (hit) touch[nt++] = k;
        }
        for (int k = 0; k < nt; k++) rudy(c, &st, touch[k], -1.0);

        if (kind < 0.5) {
            X[i] = snap(X[i] + gauss(amp)); Y[i] = snap(Y[i] + gauss(amp));
        } else if (kind < 0.62) {
            /* Directed move: drop the part where its own connections
               average out to.  A random walk finds this eventually and
               slowly; proposing it outright is what lets the anneal spend
               its budget on the arrangement rather than on transport. */
            double sx = 0.0, sy = 0.0; int cnt = 0;
            for (int q = c->pnet_base[i]; q < c->pnet_base[i + 1]; q++) {
                int ni2 = c->pnet[q];
                for (int m = c->net_base[ni2]; m < c->net_base[ni2 + 1]; m++) {
                    if (c->net_part[m] == i) continue;
                    double px, py;
                    pin_on(c, &st, c->net_part[m], c->net_pk[m], &px, &py);
                    sx += px; sy += py; cnt++;
                }
            }
            if (cnt) {
                int pr = SLOT(c, &st, i);
                int b = c->pad_base[pr], e = c->pad_base[pr + 1];
                double ox = 0.0, oy = 0.0;
                for (int k = b; k < e; k++) { ox += c->pad_dx[k]; oy += c->pad_dy[k]; }
                if (e > b) { ox /= (e - b); oy /= (e - b); }
                X[i] = snap(sx / cnt - ox);
                Y[i] = snap(sy / cnt - oy);
            }
        } else if (kind < 0.78) {
            int nrot = c->rot_base[i + 1] - c->rot_base[i];
            int nr = (int)(rnd() * (nrot - 1));
            R[i] = (R[i] + 1 + nr) % nrot;
        } else if (kind < 0.9) {
            X[i] = oX[1]; Y[i] = oY[1];
            X[j] = oX[0]; Y[j] = oY[0];
        } else {
            double dx = snap(gauss(amp)), dy = snap(gauss(amp));
            for (int k = 0; k < ng; k++) { X[grp[k]] += dx; Y[grp[k]] += dy; }
        }
        for (int k = 0; k < ng; k++) sync_box(c, &st, grp[k]);

        double d_ov = -ov_before;
        for (int k = 0; k < ng; k++) d_ov += overlap_of(c, &st, grp[k]);
        for (int k = 0; k < ng; k++) escape_of(c, &st, grp[k]);
        for (int k = 0; k < nt; k++) {
            rudy(c, &st, touch[k], +1.0);
            double r[4];
            net_rect(c, &st, touch[k], r);
            st.nethp[touch[k]] = (r[2] - r[0]) + (r[3] - r[1]);
        }
        double nv = score(c, &st, w_ov, ov + d_ov);

        if (nv <= cost || rnd() < exp((cost - nv) / (temp > 1e-9 ? temp : 1e-9))) {
            ov += d_ov;
            if (nv < best && ov < 1e-9) {
                best = nv;
                memcpy(bX, X, sizeof(double) * c->n);
                memcpy(bY, Y, sizeof(double) * c->n);
                memcpy(bR, R, sizeof(int) * c->n);
            }
        } else {
            for (int k = 0; k < nt; k++) rudy(c, &st, touch[k], -1.0);
            for (int k = 0; k < ng; k++) { X[grp[k]] = oX[k]; Y[grp[k]] = oY[k]; R[grp[k]] = oR[k]; }
            for (int k = 0; k < ng; k++) sync_box(c, &st, grp[k]);
            for (int k = 0; k < ng; k++) escape_of(c, &st, grp[k]);
            for (int k = 0; k < nt; k++) {
                rudy(c, &st, touch[k], +1.0);
                double r[4];
                net_rect(c, &st, touch[k], r);
                st.nethp[touch[k]] = (r[2] - r[0]) + (r[3] - r[1]);
            }
        }
        if ((step % 2000) == 1999) { refresh(c, &st); ov = total_overlap(c, &st); }
    }

    int ok = (best < INF_D);
    if (ok) {
        memcpy(X, bX, sizeof(double) * c->n);
        memcpy(Y, bY, sizeof(double) * c->n);
        memcpy(R, bR, sizeof(int) * c->n);
    }
    free(st.BX0); free(st.BY0); free(st.BX1); free(st.BY1);
    free(st.esc); free(st.pesc); free(st.nethp); free(st.demand);
    free(bX); free(bY); free(bR); free(touch); free(grp);
    return ok;
}
