"""Write latest.json in the format the Edge Finder app imports (version 2), so the app keeps working."""
import json
import os
from datetime import timedelta

from .db import meta_get
from .tracking import iso

HIST_FIELDS = ["id", "logged_at", "date", "sport", "player", "market", "stat", "line", "side", "pick_fair",
               "close_fair", "clv", "in_play", "home", "away", "start", "result", "actual", "graded_at",
               "raw_fair", "sharp", "exact", "pp_line_now", "injury"]


def _name(e):
    return e["entry"] if e["app"] == "prizepicks" else f"{e['app_label']} {e['entry']}"


def _leg(l):
    return {"sport": l["sport"], "player": l["player"], "market": l["market"], "stat": l["stat"], "line": l["line"],
            "side": l["side"], "fair": l["fair"], "game": f"{l['away']} @ {l['home']}", "start": l["start"],
            "books": l["books"], "sharp": bool(l["sharp"]), "exact": l["exact"], "injury": l.get("injury") or "",
            "team": l.get("team") or "", "tier": l.get("tier") or "", "app": l["app"],
            "edge": l.get("edge"), "fair_market": l.get("fair_market"), "fair_proj": l.get("fair_proj")}


def _slip(e, play):
    return {"name": _name(e), "num": e["num"], "n": e["n"], "pay": e["pay"], "mult": e["ev"], "cash": e["cash"],
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
    cutoff = iso(now - timedelta(days=days))
    hist = []
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
                     "pp_line_now": "" if p["app_line_now"] is None else f"{p['app_line_now']:g}", "injury": p["injury"] or ""})
    be = apps["prizepicks"]["breakeven"] if "prizepicks" in apps else 0.5
    return {"app": "edge-finder", "version": 2, "generated_at": board.get("generated_at", iso(now)),
            "entry": apps.get("prizepicks", {}).get("main", ""), "breakeven": round(be or 0.5, 5),
            "notes": f"Edge Engine {board.get('model_version', '')}. Credits left: {board.get('credits_remaining')}.",
            "payouts": payouts,
            "plays": [_slip(e, True) for e in board["plays"]],
            "slips": [_slip(e, False) for e in board["entries"] if (e["app"], e["entry"], e["num"]) not in play_keys][:30],
            "props": [{**_leg(l), "edge": l["edge"]} for l in board["legs"]][:150],
            "alts": [_leg(l) for l in board["alts"]],
            "watch": [{"player": w["player"], "stat": w["stat"], "line": f"{w['line']:g}", "side": w["side"],
                       "start": w["start"], "why": w["why"]} for w in board["watch"]],
            "tuning": {"blend": board.get("blend")}, "history": hist}


def write_app_json(conn, cfg, path, now):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(app_json(conn, cfg, now), f, separators=(",", ":"))
