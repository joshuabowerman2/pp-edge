"""Learn how much to trust our projection versus the market, per sport.

Model: logit(p) = c + a*logit(p_market) + b*logit(p_projection), fit by ridge-regularized
logistic regression on graded TRAINING picks only (holdout picks are kept for evaluation).
The ridge pulls toward (a=1, b=0, c=0): market only. The projection earns weight only when the
data shows it adds information."""
from datetime import datetime, timezone

from . import mathx

PRIOR = (1.0, 0.0, 0.0)
RIDGE = 5.0
MIN_PICKS = 200


def training_rows(conn, sport, holdout=0):
    rows = conn.execute("""SELECT fair_market, fair_proj, result FROM picks WHERE sport=? AND holdout=?
                           AND result IN ('hit','miss') AND fair_proj IS NOT NULL AND fair_market IS NOT NULL""",
                        (sport, holdout)).fetchall()
    return [(mathx.logit(r["fair_market"]), mathx.logit(r["fair_proj"]), 1.0 if r["result"] == "hit" else 0.0)
            for r in rows]


def fit_rows(rows, prior=PRIOR, ridge=RIDGE, iters=50):
    """Newton's method for ridge logistic regression on features (x_market, x_proj, 1)."""
    th = list(prior)
    for _ in range(iters):
        g = [ridge * (th[k] - prior[k]) for k in range(3)]
        H = [[ridge if i == j else 0.0 for j in range(3)] for i in range(3)]
        for xm, xp, y in rows:
            x = (xm, xp, 1.0)
            p = mathx.sigmoid(th[0] * xm + th[1] * xp + th[2])
            for i in range(3):
                g[i] += (p - y) * x[i]
                for j in range(3):
                    H[i][j] += p * (1 - p) * x[i] * x[j]
        step = _solve3(H, g)
        th = [th[k] - step[k] for k in range(3)]
        if max(abs(s) for s in step) < 1e-7:
            break
    return tuple(th)


def _solve3(A, b):
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(3):
        piv = max(range(c, 3), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        if abs(M[c][c]) < 1e-12:
            return [0.0, 0.0, 0.0]
        for r in range(3):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, 4):
                    M[r][k] -= f * M[c][k]
    return [M[i][3] / M[i][i] for i in range(3)]


def fit(conn, sport):
    rows = training_rows(conn, sport)
    if len(rows) < MIN_PICKS:
        return None
    a, b, c = fit_rows(rows)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("""INSERT INTO blend_params VALUES(?,?,?,?,?,?) ON CONFLICT(sport) DO UPDATE SET
                    a=excluded.a, b=excluded.b, c=excluded.c, n=excluded.n, updated_at=excluded.updated_at""",
                 (sport, a, b, c, len(rows), now))
    return (a, b, c)


# Before any data: lean mostly on the market, a little on the projection.
DEFAULT_PARAMS = (0.85, 0.15, 0.0)


def params(conn, sport):
    r = conn.execute("SELECT a, b, c FROM blend_params WHERE sport=?", (sport,)).fetchone()
    return (r["a"], r["b"], r["c"]) if r else DEFAULT_PARAMS
