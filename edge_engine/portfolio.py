"""Choose the day's entries together, not one at a time.

All candidate legs are simulated jointly (correlated within a game). Entries are added greedily:
each step adds the entry and stake that most increase expected log bankroll growth for the whole
set, so two entries sharing a player are judged together. Stakes are then scaled by the Kelly
multiplier and by the uncertainty in each entry's edge, and capped per entry, per player and per day."""
import math
import random

from . import mathx
from .sources.http import norm_name

try:
    import numpy as np
except ImportError:   # pragma: no cover
    np = None

GRID = [0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.04, 0.06, 0.08]


def simulate_hits(pool, corr, sims=4000, seed=11):
    """sims x legs matrix of hit (1) / miss (0)."""
    n = len(pool)
    if np is not None:
        rng = np.random.default_rng(seed)
        C = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                C[i, j] = C[j, i] = corr.pair(pool[i], pool[j])
        try:
            L = np.linalg.cholesky(C)
        except np.linalg.LinAlgError:
            w, V = np.linalg.eigh(C)                       # nearest positive-definite matrix
            C = V @ np.diag(np.clip(w, 1e-6, None)) @ V.T
            d = np.sqrt(np.diag(C))
            L = np.linalg.cholesky(C / np.outer(d, d))
        Z = rng.standard_normal((sims, n)) @ L.T
        thr = np.array([mathx.norm_ppf(l["fair"]) for l in pool])
        return (Z < thr).astype(np.int8)
    rnd = random.Random(seed)                              # no numpy: independent legs
    return [[1 if rnd.random() < l["fair"] else 0 for l in pool] for _ in range(sims)]


def entry_returns(entry, hits):
    pay = entry["pay"]
    if np is not None:
        k = hits[:, entry["pool_idx"]].sum(axis=1)
        table = np.array([pay.get(h, 0) - 1 for h in range(entry["n"] + 1)], dtype=float)
        return table[k]
    return [pay.get(sum(row[i] for i in entry["pool_idx"]), 0) - 1 for row in hits]


def _growth(total):
    if np is not None:
        if (total <= -1).any():
            return -math.inf
        return float(np.log1p(total).mean())
    if any(t <= -1 for t in total):
        return -math.inf
    return sum(math.log1p(t) for t in total) / len(total)


def optimize(cands, pool, corr, kelly_mult=0.25, max_stake=0.02, day_cap=0.08, player_cap=0.04, max_plays=6):
    """-> list of chosen entries with stake_pct (fraction of bankroll)."""
    cands = [c for c in cands if c["ev"] > 1.0]
    if not cands or not pool:
        return []
    hits = simulate_hits(pool, corr)
    rets = [entry_returns(c, hits) for c in cands]
    sims = len(hits)
    total = np.zeros(sims) if np is not None else [0.0] * sims
    base = 0.0
    chosen, exposure, skip = [], {}, set()
    for _ in range(max_plays * 3):
        if len(chosen) >= max_plays:
            break
        best = None
        for ci, c in enumerate(cands):
            if ci in skip:
                continue
            for f in GRID:
                trial = total + f * rets[ci] if np is not None else [t + f * r for t, r in zip(total, rets[ci])]
                g = _growth(trial)
                if best is None or g > best[0]:
                    best = (g, ci, f)
        if best is None or best[0] <= base + 1e-6:
            break
        g, ci, f = best
        c = cands[ci]
        stake = min(f * kelly_mult * mathx.uncertainty_shrink(c["ev"] - 1, c["sigma"]), max_stake)
        players = [norm_name(l["player"]) for l in c["legs"]]
        skip.add(ci)
        if stake <= 0 or any(exposure.get(p, 0) + stake > player_cap for p in players):
            continue
        chosen.append((ci, stake))
        for p in players:
            exposure[p] = exposure.get(p, 0) + stake
        total = total + f * rets[ci] if np is not None else [t + f * r for t, r in zip(total, rets[ci])]
        base = g
    out = [{**cands[ci], "stake_pct": stake} for ci, stake in chosen]
    tot = sum(e["stake_pct"] for e in out)
    if tot > day_cap:
        for e in out:
            e["stake_pct"] *= day_cap / tot
    return out


def best_app(legs, apps):
    """For props offered on more than one app: which app's line has the most edge."""
    by = {}
    for l in legs:
        if l.get("tier"):
            continue
        by.setdefault((l["game_id"], l["player_key"], l["market"]), []).append(l)
    out = []
    for group in by.values():
        if len({l["app"] for l in group}) < 2:
            continue
        best = max(group, key=lambda l: l["edge"])
        out.append({"player": best["player"], "stat": best["stat"], "best_app": apps[best["app"]]["label"],
                    "line": best["line"], "side": best["side"], "edge": best["edge"],
                    "others": [{"app": apps[l["app"]]["label"], "line": l["line"], "side": l["side"], "edge": l["edge"]}
                               for l in group if l is not best]})
    return sorted(out, key=lambda x: x["edge"], reverse=True)
