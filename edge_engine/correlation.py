"""Learn same-game correlations between players' stats from stored box scores.

Each player's stat is turned into a z-score against that player's own average, then z-scores of
teammates (and of opponents) in the same game are multiplied and averaged per market pair.
Estimates are blended with the hand-set priors by sample size and capped at +/-0.6."""
import json
import math
from datetime import datetime, timezone

from .markets import CORR_BLEND, CORR_PRIOR, SPORTS, stat_keys
from .sources.http import canon_team, norm_name

PRIOR_N = 300
MIN_PLAYER_GAMES = 6

try:
    import numpy as np
except ImportError:   # pragma: no cover
    np = None


def _markets(sport):
    _, core, extra = SPORTS[sport]
    return [m for m in core + extra if stat_keys(sport, m)]


def fit(conn, sport, max_rows=60000):
    markets = _markets(sport)
    keys = {m: stat_keys(sport, m) for m in markets}
    rows = conn.execute("""SELECT event_id, team, player_key, stats FROM boxscores WHERE sport=?
                           ORDER BY game_date DESC LIMIT ?""", (sport, max_rows)).fetchall()
    vals = {}   # (player, market) -> list of (event, team, value)
    for r in rows:
        st = json.loads(r["stats"])
        for m in markets:
            vs = [st.get(k) for k in keys[m]]
            if all(v is not None for v in vs):
                vals.setdefault((r["player_key"], m), []).append((r["event_id"], r["team"], sum(vs)))
    z = {}      # event -> list of (player, team, market, z)
    for (p, m), obs in vals.items():
        if len(obs) < MIN_PLAYER_GAMES:
            continue
        xs = [o[2] for o in obs]
        mu = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
        if sd <= 1e-9:
            continue
        for ev, team, x in obs:
            z.setdefault(ev, []).append((p, team, m, (x - mu) / sd))
    acc = {}    # (relation, m1, m2) -> [sum, n]
    for ev, items in z.items():
        for i in range(len(items)):
            pi, ti, mi, zi = items[i]
            for j in range(i + 1, len(items)):
                pj, tj, mj, zj = items[j]
                if pi == pj:
                    continue
                rel = "team" if ti == tj else "opp"
                a, b = sorted((mi, mj))
                s = acc.setdefault((rel, a, b), [0.0, 0])
                s[0] += zi * zj
                s[1] += 1
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    conn.execute("BEGIN IMMEDIATE")
    for (rel, a, b), (s, n) in acc.items():
        est = s / n
        prior = CORR_PRIOR.get(rel, {}).get(sport, {}).get((a, b), 0.0)
        rho = max(-0.6, min(0.6, (n * est + PRIOR_N * prior) / (n + PRIOR_N)))
        out[(rel, a, b)] = (rho, n)
        conn.execute("""INSERT INTO correlations VALUES(?,?,?,?,?,?,?) ON CONFLICT(sport,relation,m1,m2)
                        DO UPDATE SET rho=excluded.rho, n=excluded.n, updated_at=excluded.updated_at""",
                     (sport, rel, a, b, rho, n, now))
    conn.execute("COMMIT")
    return out


class Correlations:
    """Lookup used when pricing slips: learned value if present, else priors."""
    def __init__(self, conn=None):
        self.table = {}
        if conn is not None:
            for r in conn.execute("SELECT sport, relation, m1, m2, rho FROM correlations"):
                self.table[(r["sport"], r["relation"], r["m1"], r["m2"])] = r["rho"]

    def pair(self, a, b):
        """Correlation of two legs' outcomes (signed by their sides)."""
        if a["game_id"] != b["game_id"]:
            return 0.0
        sa = 1 if a["side"] == "MORE" else -1
        sb = 1 if b["side"] == "MORE" else -1
        if norm_name(a["player"]) == norm_name(b["player"]):
            # Same player on two apps or two stats (only matters for the day's portfolio;
            # entries never hold the same player twice).
            if a["market"] == b["market"]:
                return 0.97 * sa * sb
            ka, kb = set(stat_keys(a["sport"], a["market"]) or []), set(stat_keys(b["sport"], b["market"]) or [])
            return (0.8 if ka & kb else 0.3) * sa * sb
        m1, m2 = sorted((a["market"], b["market"]))
        ta, tb = a.get("team"), b.get("team")
        if ta and tb:
            rel = "team" if canon_team(ta) == canon_team(tb) else "opp"
            rho = self.table.get((a["sport"], rel, m1, m2))
            if rho is None:
                rho = CORR_PRIOR.get(rel, {}).get(a["sport"], {}).get((m1, m2), 0.0)
        else:
            rho = CORR_BLEND.get((m1, m2), 0.0)
        return rho * sa * sb
