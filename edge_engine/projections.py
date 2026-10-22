"""Player projections from our own box-score history.

For each player and stat: an exponentially weighted average of recent games (recent games count
most), pulled toward the player's longer average when the sample is small, adjusted for how much
of that stat the opponent allows, and for teammates ruled out (their usage gets redistributed).
The spread comes from the player's own game-to-game variation, floored by the stat model."""
import json
import math

from .markets import dist_for, injury_level, stat_keys

HALF_LIFE = 8          # games
LOOKBACK = 40          # games
MIN_GAMES = 4
OPP_WINDOW = 15        # opponent games used for the defense factor
OPP_SHRINK = 0.5       # keep half of the measured defense effect
USAGE_SHARE = 0.7      # share of an absent player's production that goes to teammates
USAGE_MIN_MINUTES = 24 # only redistribute for regulars (basketball)


def _value(stats, keys):
    vals = [stats.get(k) for k in keys]
    if any(v is None for v in vals):
        return None
    return sum(vals)


def history(conn, sport, player_key, market, before=None, limit=LOOKBACK):
    keys = stat_keys(sport, market)
    if not keys:
        return []
    q = "SELECT game_date, team, opp, minutes, stats FROM boxscores WHERE sport=? AND player_key=?"
    args = [sport, player_key]
    if before:
        q += " AND game_date < ?"
        args.append(before)
    q += " ORDER BY game_date DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(q, args):
        v = _value(json.loads(r["stats"]), keys)
        if v is not None:
            out.append({"date": r["game_date"], "team": r["team"], "opp": r["opp"], "minutes": r["minutes"], "v": v})
    return out   # newest first


def _ew(values):
    w = [0.5 ** (i / HALF_LIFE) for i in range(len(values))]
    W = sum(w)
    m = sum(x * wi for x, wi in zip(values, w)) / W
    var = sum(wi * (x - m) ** 2 for x, wi in zip(values, w)) / W
    return m, math.sqrt(var), W


def opponent_factor(conn, sport, opp, market, before=None):
    """Stat allowed by `opp` per game vs league average, shrunk toward 1."""
    keys = stat_keys(sport, market)
    if not opp or not keys:
        return 1.0
    args = [sport, opp]
    q = "SELECT event_id, stats FROM boxscores WHERE sport=? AND opp=?"
    if before:
        q += " AND game_date < ?"
        args.append(before)
    per_game = {}
    for r in conn.execute(q + " ORDER BY game_date DESC LIMIT 400", args):
        v = _value(json.loads(r["stats"]), keys)
        if v is not None:
            per_game[r["event_id"]] = per_game.get(r["event_id"], 0) + v
    games = list(per_game.values())[:OPP_WINDOW]
    if len(games) < 5:
        return 1.0
    lg = league_avg(conn, sport, market, before)
    if not lg:
        return 1.0
    f = (sum(games) / len(games)) / lg
    return 1 + (f - 1) * OPP_SHRINK


_league_cache = {}


def league_avg(conn, sport, market, before=None):
    """Average team total of the stat per game."""
    key = (id(conn), sport, market, before)
    if key in _league_cache:
        return _league_cache[key]
    keys = stat_keys(sport, market)
    args = [sport]
    q = "SELECT event_id, team, stats FROM boxscores WHERE sport=?"
    if before:
        q += " AND game_date < ?"
        args.append(before)
    tot = {}
    for r in conn.execute(q + " ORDER BY game_date DESC LIMIT 6000", args):
        v = _value(json.loads(r["stats"]), keys)
        if v is not None:
            tot[(r["event_id"], r["team"])] = tot.get((r["event_id"], r["team"]), 0) + v
    out = sum(tot.values()) / len(tot) if len(tot) >= 10 else None
    _league_cache[key] = out
    return out


def usage_bump(conn, sport, team, player_key, market, absent, before=None):
    """Extra expected production when regular teammates are out: their recent average times
    USAGE_SHARE, split by each remaining player's share of the team's production."""
    if not absent or not team:
        return 0.0
    keys = stat_keys(sport, market)
    rows = conn.execute("""SELECT player_key, minutes, stats FROM boxscores WHERE sport=? AND team=?
                           ORDER BY game_date DESC LIMIT 300""", (sport, team)).fetchall()
    per = {}
    for r in rows:
        v = _value(json.loads(r["stats"]), keys)
        if v is None:
            continue
        d = per.setdefault(r["player_key"], {"v": [], "m": []})
        if len(d["v"]) < 10:
            d["v"].append(v)
            d["m"].append(r["minutes"] or 0)
    avg = {k: sum(d["v"]) / len(d["v"]) for k, d in per.items() if d["v"]}
    mins = {k: sum(d["m"]) / len(d["m"]) for k, d in per.items() if d["m"]}
    gone = [k for k in absent if k in avg and (sport not in ("nba", "wnba") or mins.get(k, 0) >= USAGE_MIN_MINUTES)]
    if not gone or player_key not in avg:
        return 0.0
    pool = sum(v for k, v in avg.items() if k not in absent)
    if pool <= 0:
        return 0.0
    lost = sum(avg[k] for k in gone) * USAGE_SHARE
    return lost * avg[player_key] / pool


def project(conn, sport, player_key, market, opp=None, team=None, absent=(), before=None):
    """{mu, sd, n} or None when there isn't enough history."""
    h = history(conn, sport, player_key, market, before=before)
    if len(h) < MIN_GAMES:
        return None
    vals = [g["v"] for g in h]
    ew, ew_sd, W = _ew(vals)
    long_mean = sum(vals) / len(vals)
    # Small samples lean on the longer average.
    k = 4.0
    mu = (W * ew + k * long_mean) / (W + k)
    mu *= opponent_factor(conn, sport, opp, market, before)
    mu += usage_bump(conn, sport, team or h[0]["team"], player_key, market, absent, before)
    dist = dist_for(sport, market)
    if dist[0] == "poisson":
        sd = math.sqrt(max(mu, 0.01))
    else:
        sd = max(ew_sd, dist[1] * mu * 0.8, 0.5)
    return {"mu": max(mu, 0.0), "sd": sd, "n": len(vals)}


def absent_players(conn, sport, team):
    """Teammates currently listed out."""
    if not team:
        return set()
    rows = conn.execute("""SELECT i.player_key, i.status FROM injuries i JOIN rosters r
                           ON r.sport=i.sport AND r.player_key=i.player_key WHERE i.sport=? AND r.team=?""",
                        (sport, team)).fetchall()
    return {r["player_key"] for r in rows if injury_level(r["status"]) >= 3}
