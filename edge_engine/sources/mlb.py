"""MLB Stats API (free): schedule and box scores with total bases and pitching outs."""
from ..markets import MLB_BASE
from . import http


def schedule(ymd_dash):
    data, _ = http.get_json(f"{MLB_BASE}/schedule?sportId=1&date={ymd_dash}")
    out = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            tm = g.get("teams", {})
            out.append({"id": str(g["gamePk"]), "home": tm.get("home", {}).get("team", {}).get("name", ""),
                        "away": tm.get("away", {}).get("team", {}).get("name", ""), "date": g.get("gameDate", ""),
                        "final": g.get("status", {}).get("abstractGameState") == "Final"})
    return out


def box(game_pk):
    data, _ = http.get_json(f"{MLB_BASE}/game/{game_pk}/boxscore")
    names = {s: data.get("teams", {}).get(s, {}).get("team", {}).get("name", "") for s in ("home", "away")}
    out = []
    for side in ("home", "away"):
        other = "away" if side == "home" else "home"
        for p in data.get("teams", {}).get(side, {}).get("players", {}).values():
            name = p.get("person", {}).get("fullName", "")
            k = http.norm_name(name)
            if not k:
                continue
            stats = {}
            for group in ("batting", "pitching"):
                for kk, vv in (p.get("stats", {}).get(group) or {}).items():
                    try:
                        stats[f"{group}.{kk}"] = float(vv)
                    except (TypeError, ValueError):
                        pass
            out.append({"player_key": k, "player": name, "team": names[side], "opp": names[other],
                        "minutes": None, "stats": stats, "dnp": not stats})
    return out
