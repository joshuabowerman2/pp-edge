"""Player directory for the app's "Add a past bet" form: every rostered player in each sport with team,
jersey number, position and team colors, so the form can suggest names as you type and show jerseys.

Rosters come from ESPN (free) and are refreshed at most once a day. Players seen in stored box scores but
no longer on a roster (traded, released) are kept with their last team. Written as roster.json next to
index.json and loaded only when someone opens the form."""
import json
from datetime import datetime, timedelta, timezone

from .db import meta_get, meta_set
from .sources import espn


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh(conn, sports, now=None, max_age_hours=20):
    """Refresh each sport's directory if it's older than max_age_hours. Returns {sport: players or error}."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for s in sports:
        last = meta_get(conn, f"directory:{s}")
        try:
            if last and now - datetime.fromisoformat(last.replace("Z", "+00:00")) < timedelta(hours=max_age_hours):
                continue
        except ValueError:
            pass
        try:
            r = espn.all_rosters(s)
        except Exception as e:
            out[s] = f"error: {e}"
            continue
        if not r:
            out[s] = 0
            continue
        conn.execute("BEGIN IMMEDIATE")
        for k, p in r.items():
            conn.execute("INSERT OR REPLACE INTO player_dir VALUES(?,?,?,?,?,?,?,?,?)",
                         (s, k, p["name"], p["team"], p["jersey"], p["pos"], p["color"], p["alt"], _iso(now)))
        meta_set(conn, f"directory:{s}", _iso(now))
        conn.execute("COMMIT")
        out[s] = len(r)
    return out


def build(conn, sports):
    """{"sports": {sport: [[name, team, jersey, pos, color, alt], ...]}}, current rosters first."""
    res = {}
    for s in sports:
        rows, seen = [], set()
        for r in conn.execute("SELECT player_key, name, team, jersey, pos, color, alt FROM player_dir WHERE sport=? ORDER BY name", (s,)):
            seen.add(r["player_key"])
            rows.append([r["name"], r["team"] or "", r["jersey"] or "", r["pos"] or "", r["color"] or "", r["alt"] or ""])
        # Players from box scores who aren't on a current roster, with their latest team and any stored jersey.
        info = {r["player_key"]: r for r in conn.execute("SELECT * FROM player_info WHERE sport=?", (s,))}
        # Team colors by nickname (box scores store "nuggets", rosters "Denver Nuggets").
        colors = {}
        for r in list(conn.execute("SELECT team, color, alt FROM player_dir WHERE sport=? AND color != ''", (s,))) + list(info.values()):
            if r["team"] and r["color"]:
                colors.setdefault(r["team"].lower().split()[-1], (r["team"], r["color"], r["alt"] or ""))
        for r in conn.execute("""SELECT player_key, player, team, MAX(game_date) AS d FROM boxscores
                                 WHERE sport=? AND player IS NOT NULL AND player != '' GROUP BY player_key""", (s,)):
            if r["player_key"] in seen:
                continue
            seen.add(r["player_key"])
            i = info.get(r["player_key"])
            if i:
                rows.append([r["player"], i["team"] or "", i["jersey"] or "", i["pos"] or "", i["color"] or "", i["alt"] or ""])
            else:
                c = colors.get((r["team"] or "").lower().split()[-1] if r["team"] else "")
                rows.append([r["player"], c[0] if c else (r["team"] or ""), "", "", c[1] if c else "", c[2] if c else ""])
        if rows:
            res[s] = rows
    return {"sports": res}


def write(conn, sports, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build(conn, sports), f, separators=(",", ":"))
