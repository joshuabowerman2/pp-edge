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


def stacks(legs, app_cfg, corr, per_game=10, min_rho=0.05, top=12, max_use=2):
    """Same-game 2- and 3-leg combos whose legs tend to hit together. Pick'em payouts price legs as
    independent, so a positively correlated combo can be worth more than its legs suggest."""
    entries = {k: e for k, e in app_cfg["entries"].items() if e["n"] in (2, 3)}
    if not entries:
        return []
    by_game = {}
    for l in legs:
        if eligible(l) and l.get("fair") is not None and l["fair"] > 0.45:
            by_game.setdefault(l["game_id"], []).append(l)
    found = []
    for gl in by_game.values():
        gl = sorted(gl, key=lambda l: l["fair"], reverse=True)[:per_game]
        m = len(gl)
        names = [norm_name(l["player"]) for l in gl]
        rho = {(i, j): corr.pair(gl[i], gl[j]) for i in range(m) for j in range(i + 1, m)}
        ok = lambda i, j: names[i] != names[j] and rho[(i, j)] >= min_rho
        combos = [c for c in combinations(range(m), 2) if ok(*c)]
        combos += [c for c in combinations(range(m), 3) if ok(c[0], c[1]) and ok(c[0], c[2]) and ok(c[1], c[2])]
        for combo in combos:
            ls = [gl[i] for i in combo]
            n = len(ls)
            d, correlated = entry_dist(ls, corr)
            if not correlated:
                continue
            best = max(((name, mathx.payout_mult(d, e["pay"]), e) for name, e in entries.items() if e["n"] == n),
                       key=lambda x: x[1], default=None)
            if not best or best[1] <= 1.0:
                continue
            p_ind = 1.0
            for l in ls:
                p_ind *= l["fair"]
            name, ev, e = best
            sigma = mathx.ev_sigma([l["fair"] for l in ls], [l.get("sigma") or 0.03 for l in ls], e["pay"])
            found.append({"app": app_cfg["key"], "entry": name, "n": n, "pay": e["pay"], "legs": ls, "dist": d, "ev": ev,
                          "var": mathx.payout_var(d, e["pay"]), "sigma": sigma, "correlated": True,
                          "cash": sum(d[h] for h in e["pay"] if e["pay"][h] >= 1), "kelly": mathx.kelly_fraction(d, e["pay"]),
                          "p_joint": d[n], "p_indep": p_ind, "lift": d[n] / p_ind - 1 if p_ind else 0.0,
                          "rho": min(rho[p] for p in combinations(combo, 2))})
    found.sort(key=lambda s: s["ev"], reverse=True)
    chosen, use = [], {}
    for s in found:
        ids = [(l["game_id"], norm_name(l["player"]), l["market"]) for l in s["legs"]]
        if any(use.get(i, 0) >= max_use for i in ids):
            continue
        chosen.append(s)
        for i in ids:
            use[i] = use.get(i, 0) + 1
        if len(chosen) == top:
            break
    for k, s in enumerate(chosen, 1):
        s["num"] = k
    return chosen


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
