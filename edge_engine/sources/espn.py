"""ESPN's public site API: injuries, rosters, scoreboards and box scores (free, no key)."""
from ..markets import ESPN_BASE, ESPN_PATH
from . import http


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def injuries(sport):
    """{player_key: (player name, status)}. Defensive about the document shape."""
    path = ESPN_PATH.get(sport)
    if not path:
        return {}
    data, _ = http.get_json(f"{ESPN_BASE}/{path}/injuries")
    from ..markets import injury_level
    out = {}
    for d in _walk(data):
        ath, st = d.get("athlete"), d.get("status")
        if isinstance(st, dict):
            st = st.get("name") or st.get("description") or st.get("type")
        if isinstance(ath, dict) and ath.get("displayName") and isinstance(st, str) and st:
            k = http.norm_name(ath["displayName"])
            if k not in out or injury_level(st) >= injury_level(out[k][1]):
                out[k] = (ath["displayName"], st)
    return out


def _hex(c):
    c = str(c or "").strip().lstrip("#")
    return "#" + c.lower() if len(c) == 6 and all(x in "0123456789abcdefABCDEF" for x in c) else ""


def rosters(sport, team_names):
    """{player_key: team name as given in team_names}."""
    return {k: v["team"] for k, v in roster_details(sport, team_names).items()}


def roster_details(sport, team_names):
    """{player_key: {team, jersey, pos, color, alt}}: team colors from ESPN's team list, jersey number and
    position from each roster."""
    path = ESPN_PATH.get(sport)
    if not path or not team_names:
        return {}
    data, _ = http.get_json(f"{ESPN_BASE}/{path}/teams?limit=100")
    teams = [d["team"] for d in _walk(data)
             if isinstance(d.get("team"), dict) and d["team"].get("id") and d["team"].get("displayName")]
    out = {}
    for tn in sorted(team_names):
        tm = next((t for t in teams if http.team_match(t["displayName"], tn)), None)
        if tm:
            color, alt = _hex(tm.get("color")), _hex(tm.get("alternateColor"))
        if not tm:
            continue
        try:
            r, _ = http.get_json(f"{ESPN_BASE}/{path}/teams/{tm['id']}/roster")
        except Exception:
            continue
        for a in r.get("athletes", []):
            group = a.get("items") if isinstance(a, dict) and isinstance(a.get("items"), list) else [a]
            for p in group:
                if isinstance(p, dict) and p.get("displayName"):
                    pos = p.get("position")
                    pos = (pos.get("abbreviation") or "") if isinstance(pos, dict) else ""
                    out[http.norm_name(p["displayName"])] = {"team": tn, "jersey": str(p.get("jersey") or ""), "pos": pos,
                                                             "color": color, "alt": alt}
    return out


def all_rosters(sport, pause=0.05):
    """Every player on every team's roster: {player_key: {name, team, jersey, pos, color, alt}}.
    For the app's player directory (past-bet entry), so it isn't limited to teams playing today."""
    import time
    path = ESPN_PATH.get(sport)
    if not path:
        return {}
    data, _ = http.get_json(f"{ESPN_BASE}/{path}/teams?limit=100")
    teams = [d["team"] for d in _walk(data)
             if isinstance(d.get("team"), dict) and d["team"].get("id") and d["team"].get("displayName")]
    out, seen = {}, set()
    for tm in teams:
        if tm["id"] in seen:
            continue
        seen.add(tm["id"])
        try:
            r, _ = http.get_json(f"{ESPN_BASE}/{path}/teams/{tm['id']}/roster")
        except Exception:
            continue
        color, alt = _hex(tm.get("color")), _hex(tm.get("alternateColor"))
        for a in r.get("athletes", []):
            group = a.get("items") if isinstance(a, dict) and isinstance(a.get("items"), list) else [a]
            for p in group:
                if isinstance(p, dict) and p.get("displayName"):
                    pos = p.get("position")
                    pos = (pos.get("abbreviation") or "") if isinstance(pos, dict) else ""
                    out[http.norm_name(p["displayName"])] = {"name": p["displayName"], "team": tm["displayName"],
                                                             "jersey": str(p.get("jersey") or ""), "pos": pos,
                                                             "color": color, "alt": alt}
        if pause:
            time.sleep(pause)
    return out


def scoreboard(sport, ymd):
    path = ESPN_PATH[sport]
    extra = "&groups=80&limit=400" if sport == "cfb" else "&limit=200"
    data, _ = http.get_json(f"{ESPN_BASE}/{path}/scoreboard?dates={ymd}{extra}")
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c.get("team", {}).get("displayName", "") for c in comp.get("competitors", [])}
        score = {}
        for c in comp.get("competitors", []):
            try:
                score[c.get("homeAway")] = float(c.get("score"))
            except (TypeError, ValueError):
                pass
        season = ev.get("season") or {}
        # ESPN season types: 1 preseason, 2 regular season, 3 postseason, 4 all-star/off-season.
        stype = season.get("type")
        exhibition = stype in (1, 4, "1", "4") or str(season.get("slug", "")).lower() in ("preseason", "off-season")
        out.append({"id": ev["id"], "home": teams.get("home", ""), "away": teams.get("away", ""),
                    "exhibition": exhibition,
                    "date": ev.get("date", ""), "home_score": score.get("home"), "away_score": score.get("away"),
                    "final": bool(ev.get("status", {}).get("type", {}).get("completed"))})
    return out


def _minutes(v):
    try:
        s = str(v)
        if ":" in s:
            m, sec = s.split(":")[:2]
            return float(m) + float(sec) / 60
        return float(s)
    except ValueError:
        return None


def box(sport, event_id):
    """[{player_key, player, team, opp, minutes, stats{key: value}, dnp}] from ESPN's game summary."""
    data, _ = http.get_json(f"{ESPN_BASE}/{ESPN_PATH[sport]}/summary?event={event_id}")
    teams = [t.get("team", {}).get("displayName", "") for t in data.get("boxscore", {}).get("players", [])]
    out = {}
    for ti, team in enumerate(data.get("boxscore", {}).get("players", [])):
        tname = team.get("team", {}).get("displayName", "")
        opp = next((t for t in teams if t != tname), "")
        for grp in team.get("statistics", []):
            keys = grp.get("keys", [])
            for ath in grp.get("athletes", []):
                name = ath.get("athlete", {}).get("displayName", "")
                k = http.norm_name(name)
                if not k:
                    continue
                row = out.setdefault(k, {"player_key": k, "player": name, "team": tname, "opp": opp,
                                         "minutes": None, "stats": {}, "dnp": False})
                if ath.get("didNotPlay"):
                    row["dnp"] = True
                    continue
                for key, v in zip(keys, ath.get("stats", [])):
                    if key == "minutes":
                        row["minutes"] = _minutes(v)
                    sep = "/" if "/" in key else "-" if "-" in key else None
                    pairs = zip(key.split(sep), str(v).split(sep)) if sep else [(key, v)]
                    for kk, vv in pairs:
                        try:
                            row["stats"][kk] = float(str(vv).replace("+", ""))
                        except ValueError:
                            pass
    return list(out.values())
