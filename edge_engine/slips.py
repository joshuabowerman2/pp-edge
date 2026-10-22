"""Candidate pick'em entries per app, priced with same-game correlation."""
from itertools import combinations

from . import mathx
from .markets import injury_level
from .sources.http import norm_name

try:
    import numpy as np
except ImportError:   # pragma: no cover
    np = None

_rng = np.random.default_rng(7) if np is not None else None
_Z = {}


def corr_matrix(legs, corr):
    n = len(legs)
    return [[1.0 if i == j else corr.pair(legs[i], legs[j]) for j in range(n)] for i in range(n)]


def entry_dist(legs, corr, sims=20000):
    """Hit-count distribution via a Gaussian copula when legs are correlated. -> (dist, correlated?)"""
    ps = [l["fair"] for l in legs]
    C = corr_matrix(legs, corr)
    n = len(legs)
    if np is None or all(C[i][j] == 0 for i in range(n) for j in range(n) if i != j):
        return mathx.hit_dist(ps), False
    try:
        L = np.linalg.cholesky(np.array(C))
    except np.linalg.LinAlgError:
        return mathx.hit_dist(ps), False
    if n not in _Z:
        _Z[n] = _rng.standard_normal((sims, n))
    Z = _Z[n] @ L.T
    hits = (Z < np.array([mathx.norm_ppf(p) for p in ps])).sum(axis=1)
    return [float(c) for c in np.bincount(hits, minlength=n + 1) / sims], True


def eligible(leg):
    return not leg.get("tier") and injury_level(leg.get("injury")) < 2


def candidates(legs, app_cfg, corr, pool_size=18, per_entry=6, max_per_game=2):
    """Best entries for each of the app's entry types. Each comes back with ev, sigma, kelly."""
    pool = sorted([l for l in legs if eligible(l) and l["fair"] > 0.5], key=lambda l: l["fair"], reverse=True)[:pool_size]
    m = len(pool)
    bad = {(i, j) for i in range(m) for j in range(i + 1, m) if corr.pair(pool[i], pool[j]) < -0.02}
    out = []
    for name, e in app_cfg["entries"].items():
        n, pay = e["n"], e["pay"]
        if m < n:
            continue
        cands = []
        for combo in combinations(range(m), n):
            ls = [pool[i] for i in combo]
            if len({norm_name(l["player"]) for l in ls}) < n:
                continue
            games = {}
            for l in ls:
                games[l["game_id"]] = games.get(l["game_id"], 0) + 1
            if max(games.values()) > max_per_game or any((i, j) in bad for i, j in combinations(combo, 2)):
                continue
            cands.append((mathx.payout_mult(mathx.hit_dist([l["fair"] for l in ls]), pay), combo))
            if len(cands) > 60000:
                break
        cands.sort(key=lambda c: c[0], reverse=True)
        rescored = []
        for _, combo in cands[:300]:
            ls = [pool[i] for i in combo]
            d, correlated = entry_dist(ls, corr)
            rescored.append((mathx.payout_mult(d, pay), combo, d, correlated))
        rescored.sort(key=lambda c: c[0], reverse=True)
        chosen, used = [], {}
        for ev, combo, d, correlated in rescored:
            if any(used.get(i, 0) >= 2 for i in combo) or any(len(set(combo) & set(c[1])) > n // 2 for c in chosen):
                continue
            chosen.append((ev, combo, d, correlated))
            for i in combo:
                used[i] = used.get(i, 0) + 1
            if len(chosen) == per_entry:
                break
        for k, (ev, combo, d, correlated) in enumerate(chosen, 1):
            ls = [pool[i] for i in combo]
            sigma = mathx.ev_sigma([l["fair"] for l in ls], [l["sigma"] for l in ls], pay)
            out.append({"app": app_cfg["key"], "entry": name, "num": k, "n": n, "pay": pay, "legs": ls,
                        "dist": d, "ev": ev, "var": mathx.payout_var(d, pay), "sigma": sigma, "correlated": correlated,
                        "cash": sum(d[h] for h in pay if pay[h] >= 1), "kelly": mathx.kelly_fraction(d, pay),
                        "pool_idx": [pool.index(l) for l in ls]})
    return out, pool
