"""Per-prop research for the app: the projection and its parts, recent and season averages, the matchup,
usage, home/away splits, recent games, and every sportsbook's line for the prop with its update time.

Everything here is a model estimate or a historical count. The app labels it that way and shows sample sizes."""
import json

from . import projections
from .markets import FAMILY, stat_keys
from .sources.http import canon_team

# A simple usage measure per sport family: (label, box score keys summed).
USAGE = {"basketball": ("Shot attempts", ["fieldGoalsAttempted"]),
         "football": ("Carries + catches", ["rushingAttempts", "receptions"]),
         "hockey": ("Shots", ["shotsTotal"])}


def _mean(xs):
    return round(sum(xs) / len(xs), 2) if xs else None


def _partial(stats, keys):
    vals = [stats.get(k) for k in keys if stats.get(k) is not None]
    return sum(vals) if vals else None


def key(leg):
    return f"{leg['sport']}|{leg.get('player_key')}|{leg['market']}"


def build(conn, legs, max_games=20):
    out = {}
    for l in legs:
        pk = l.get("player_key")
        k = key(l)
        if not pk or k in out or l.get("tier"):
            continue
        sport, market = l["sport"], l["market"]
        keys = stat_keys(sport, market)
        team = canon_team(l.get("team") or "")
        home, away = canon_team(l.get("home") or ""), canon_team(l.get("away") or "")
        opp = away if team and team == home else home if team and team == away else ""
        games = []
        if keys:
            ulabel, ukeys = USAGE.get(FAMILY.get(sport, ""), ("", []))
            for r in conn.execute("""SELECT game_date, opp, minutes, stats, home FROM boxscores
                                     WHERE sport=? AND player_key=? ORDER BY game_date DESC LIMIT 82""", (sport, pk)):
                st = json.loads(r["stats"])
                v = sum(st.get(x) for x in keys) if all(st.get(x) is not None for x in keys) else None
                if v is None:
                    continue
                games.append({"d": r["game_date"], "v": round(v, 2), "opp": r["opp"] or "",
                              "min": None if r["minutes"] is None else round(r["minutes"], 1),
                              "home": r["home"], "use": None if not ukeys else _partial(st, ukeys)})
        else:
            ulabel = ""
        vals = [g["v"] for g in games]
        proj = projections.project(conn, sport, pk, market, opp=opp or None, team=team or None,
                                   absent=projections.absent_players(conn, sport, team)) if keys else None
        of = projections.opponent_factor(conn, sport, opp, market) if opp and keys else 1.0
        hv = [g["v"] for g in games if g["home"] == 1]
        av = [g["v"] for g in games if g["home"] == 0]
        books = [{"book": r["book"], "line": r["point"], "over": r["over"], "under": r["under"], "at": r["updated_at"]}
                 for r in conn.execute("""SELECT book, point, over, under, updated_at FROM quotes
                                          WHERE game_id=? AND market=? AND player_key=? ORDER BY book, point""",
                                       (l.get("game_id"), market, pk))]
        out[k] = {"n": len(vals), "avg": _mean(vals), "l5": _mean(vals[:5]), "l10": _mean(vals[:10]),
                  "proj": {"mu": round(proj["mu"], 2), "sd": round(proj["sd"], 2), "n": proj["n"]} if proj else None,
                  "opp": opp, "opp_factor": round(of, 3), "usage_label": ulabel,
                  "home_avg": _mean(hv), "home_n": len(hv), "away_avg": _mean(av), "away_n": len(av),
                  "games": games[:max_games], "books": books}
    return out
