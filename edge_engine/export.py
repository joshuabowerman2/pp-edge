"""Write latest.json in the format the Edge Finder app imports (version 2), so the app keeps working."""
import json
import os
from datetime import timedelta

from .db import meta_get
from .tracking import iso
from . import players, projections, research, results, teams, timeline
from .tracking import moves_for, prop_key

HIST_FIELDS = ["id", "logged_at", "date", "sport", "player", "market", "stat", "line", "side", "pick_fair",
               "close_fair", "clv", "in_play", "home", "away", "start", "result", "actual", "graded_at",
               "raw_fair", "sharp", "exact", "pp_line_now", "injury", "retired"]


def _name(e):
    return e["entry"] if e["app"] == "prizepicks" else f"{e['app_label']} {e['entry']}"


_MOVES = {}
_INFO = {}


def _leg(l):
    info = _INFO.get((l["sport"], l.get("player_key")), {})
    mv = _MOVES.get(prop_key(l)) if l.get("game_id") is not None and not l.get("tier") else None
    return {"sport": l["sport"], "player": l["player"], "player_key": l.get("player_key") or "",
            "jersey": info.get("jersey", ""), "pos": info.get("pos", ""), "color": info.get("color", ""), "alt": info.get("alt", ""),
            "fair_open": round(mv[0], 4) if mv else None, "opened_at": mv[1] if mv else None, "moves": mv[2] if mv else 0, "market": l["market"], "stat": l["stat"], "line": l["line"],
            "side": l["side"], "fair": l["fair"], "game": f"{l['away']} @ {l['home']}", "start": l["start"],
            "books": l["books"], "sharp": bool(l["sharp"]), "exact": l["exact"], "injury": l.get("injury") or "",
            "team": l.get("team") or "", "tier": l.get("tier") or "", "app": l["app"],
            "edge": l.get("edge"), "fair_market": l.get("fair_market"), "fair_proj": l.get("fair_proj"),
            "proj_mu": l.get("proj_mu"), "sigma": l.get("sigma"), "home": l.get("home"), "away": l.get("away"), "market_books": l.get("books")}


def _slip(e, play):
    extra = {k: e[k] for k in ("p_joint", "p_indep", "lift", "rho") if k in e}
    return {**extra, "name": _name(e), "num": e["num"], "n": e["n"], "pay": e["pay"], "mult": e["ev"], "cash": e["cash"],
            "kelly": e["kelly"], "dist": e["dist"], "corr": e["correlated"], "stake_pct": e["stake_pct"],
            "play": play, "app": e["app"], "legs": [_leg(l) for l in e["legs"]]}


def _fmt(x, d=4):
    return "" if x is None else f"{x:.{d}f}"


def app_json(conn, cfg, now, days=60):
    from .engine import _apps
    board = meta_get(conn, "board") or {"plays": [], "entries": [], "legs": [], "alts": [], "watch": []}
    apps = _apps(cfg)
    payouts = {}
    for k, a in apps.items():
        for name, e in a["entries"].items():
            full = name if k == "prizepicks" else f"{a['label']} {name}"
            payouts[full] = {"n": e["n"], "pay": {str(h): m for h, m in e["pay"].items()}}
    play_keys = {(p["app"], p["entry"], p["num"]) for p in board["plays"]}
    all_legs = [l for e in board["plays"] + board["entries"] + board.get("stacks", []) for l in e["legs"]] + board["legs"]
    _INFO.clear()
    _INFO.update({(r["sport"], r["player_key"]): dict(r) for r in conn.execute("SELECT * FROM player_info")})
    _MOVES.clear()
    _MOVES.update(moves_for(conn, all_legs))
    # Research for every player and stat on the board: projection, averages, matchup, games, book lines.
    research_data = research.build(conn, all_legs)
    injuries = injury_report(conn, all_legs)
    cutoff = iso(now - timedelta(days=days))
    hist = []
    from . import RETIRED_MODELS
    for p in conn.execute("SELECT * FROM picks WHERE created_at >= ? ORDER BY seq", (cutoff,)):
        t = p["start"]
        from .tracking import parse_time
        st = parse_time(t)
        hist.append({"id": p["id"], "logged_at": p["created_at"], "date": st.astimezone(cfg.tz).strftime("%Y-%m-%d") if st else "",
                     "sport": p["sport"], "player": p["player"], "market": p["market"], "stat": p["stat"],
                     "line": f"{p['line']:g}", "side": p["side"], "pick_fair": _fmt(p["fair"]),
                     "close_fair": _fmt(p["close_fair"]), "clv": _fmt(p["clv"]), "in_play": str(p["in_play"]),
                     "home": p["home"], "away": p["away"], "start": t,
                     "result": p["result"] if p["result"] in ("hit", "miss", "push", "void") else "",
                     "actual": "" if p["actual"] is None else f"{p['actual']:g}", "graded_at": p["graded_at"] or "",
                     "raw_fair": _fmt(p["fair_market"]), "sharp": str(p["sharp"]), "exact": str(p["exact"]),
                     "pp_line_now": "" if p["app_line_now"] is None else f"{p['app_line_now']:g}", "injury": p["injury"] or "",
                     # Retired-model picks still go to the app so reviews of entries built from them can find
                     # what the model said; the app leaves them out of its track-record stats.
                     "retired": "1" if p["model_version"] in RETIRED_MODELS else ""})
    be = apps["prizepicks"]["breakeven"] if "prizepicks" in apps else 0.5
    return {"app": "edge-finder", "version": 2, "generated_at": board.get("generated_at", iso(now)), "exported_at": iso(now),
            "entry": apps.get("prizepicks", {}).get("main", ""), "breakeven": round(be or 0.5, 5),
            "notes": f"Edge Engine {board.get('model_version', '')}. Credits left: {board.get('credits_remaining')}.",
            "payouts": payouts,
            "plays": [_slip(e, True) for e in board["plays"]],
            "slips": [_slip(e, False) for e in board["entries"] if (e["app"], e["entry"], e["num"]) not in play_keys][:30],
            "props": [{**_leg(l), "edge": l["edge"]} for l in board["legs"]],
            "apps": {k: {"label": a["label"], "breakeven": a.get("breakeven"), "payouts": bool(a["entries"])}
                     for k, a in apps.items() if k in cfg.dfs_apps},
            "alts": [_leg(l) for l in board["alts"]],
            "watch": [{"player": w["player"], "stat": w["stat"], "line": f"{w['line']:g}", "side": w["side"],
                       "start": w["start"], "why": w["why"]} for w in board["watch"]],
            "tuning": {"blend": board.get("blend")}, "history": hist,
            # The model's own record for each Flex size (1 unit per slip), for the app's you-vs-the-model view.
            "stacks": [_slip(e, False) for e in board.get("stacks", [])],
            "research": research_data,
            "injuries": injuries,
            "freshness": freshness(conn),
            "teams": {"plays": teams.board(conn, now), "record": teams.record(conn)},
            "flex": [{k: s[k] for k in ("entry", "n", "slips", "open", "settled", "cashed", "profit", "exp_profit", "roi",
                                         "cash_rate")} for s in results.flex_data(conn)]}


def injury_report(conn, legs):
    """For each team on the board: who is listed on ESPN's injury report (confirmed status), and the model's
    estimate of how absences shift teammates' projections (usage redistribution)."""
    from .markets import injury_level
    from .sources.http import canon_team
    teams = {}
    for l in legs:
        if l.get("team"):
            teams.setdefault((l["sport"], canon_team(l["team"])), []).append(l)
    out = []
    for (sport, team), tl in sorted(teams.items()):
        listed = [{"player": r["player"], "status": r["status"], "level": injury_level(r["status"]), "updated": r["updated_at"]}
                  for r in conn.execute("""SELECT i.player, i.status, i.updated_at FROM injuries i JOIN rosters r
                                           ON r.sport=i.sport AND r.player_key=i.player_key WHERE i.sport=? AND r.team=?""",
                                        (sport, team))]
        listed = [x for x in listed if x["level"] >= 1]
        absent = projections.absent_players(conn, sport, team)
        impact, seen = [], set()
        for l in tl:
            k = (l.get("player_key"), l["market"])
            if not l.get("player_key") or k in seen or l.get("tier"):
                continue
            seen.add(k)
            bump = projections.usage_bump(conn, sport, team, l["player_key"], l["market"], absent) if absent else 0.0
            if bump >= 0.1:
                impact.append({"player": l["player"], "stat": l["stat"], "market": l["market"], "bump": round(bump, 2)})
        if listed or impact:
            out.append({"sport": sport, "team": team, "listed": sorted(listed, key=lambda x: -x["level"]),
                        "impact": sorted(impact, key=lambda x: -x["bump"])})
    return out


def freshness(conn):
    """When each data source was last refreshed, and which model versions made the picks."""
    one = lambda q: (conn.execute(q).fetchone() or [None])[0]
    return {"odds": one("SELECT MAX(last_polled) FROM games"), "injuries": one("SELECT MAX(updated_at) FROM injuries"),
            "boxscores": one("SELECT MAX(game_date) FROM boxscores"), "rosters": one("SELECT MAX(updated_at) FROM rosters"),
            "models": [dict(r) for r in conn.execute("SELECT model_version AS version, COUNT(*) AS picks FROM picks GROUP BY model_version")]}


def write_app_json(conn, cfg, path, now):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    data = app_json(conn, cfg, now)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    # Line history goes in its own file, loaded only when someone opens a player's page.
    from .db import meta_get
    board = meta_get(conn, "board") or {}
    legs = [l for e in board.get("plays", []) + board.get("entries", []) + board.get("stacks", []) for l in e["legs"]] + board.get("legs", [])
    timeline.write(conn, legs, os.path.join(d or ".", "timeline.json"))
    players.write(conn, legs, os.path.join(d or ".", "players.json"))
    # Everyone on every roster, for the app's past-bet form (names, teams, jerseys).
    from . import directory
    directory.write(conn, sorted(set(cfg.sports) | {"nba", "nfl", "nhl", "mlb"}), os.path.join(d or ".", "roster.json"))
