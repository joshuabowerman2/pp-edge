"""The engine: poll prices on a credit budget, price every pick'em line, log picks, build the day's
entries, react to lineup news, grade, and publish the board."""
import json
import math
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import MODEL_VERSION, alerts, blend, correlation, mathx, portfolio, pricing, projections, slips, tracking
from .db import meta_get, meta_set
from .markets import LABELS, SPORTS, injury_level
from .sources import espn, odds_api
from .sources.http import canon_team

iso = tracking.iso


def _apps(cfg):
    apps = cfg.app_payouts()
    for k, a in apps.items():
        a["key"] = k
        main = a["entries"].get(a.get("main") or "") or (next(iter(a["entries"].values())) if a["entries"] else None)
        a["breakeven"] = mathx.breakeven(main["n"], main["pay"]) if main else None
    return apps


def markets_for(cfg, sport):
    _, core, extra = SPORTS[sport]
    m = core + (extra if cfg.all_markets else [])
    return m + ([x + "_alternate" for x in core] if cfg.alt_lines else [])


# ------------------------------------------------------------------ polling
def refresh_events(conn, cfg, sport, now):
    evs = odds_api.fetch_events(SPORTS[sport][0], cfg.odds_api_key, cfg.window_hours)
    for ev in evs:
        conn.execute("""INSERT INTO games(id,sport,home,away,start) VALUES(?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET start=excluded.start""",
                     (ev["id"], sport, ev.get("home_team", ""), ev.get("away_team", ""), ev.get("commence_time", "")))
    meta_set(conn, f"events_refreshed:{sport}", iso(now))
    return evs


def _factor(minutes_to_start):
    """Poll more often as a game gets close: prices move most in the last hours."""
    if minutes_to_start > 360:
        return 4.0
    if minutes_to_start > 120:
        return 1.5
    if minutes_to_start > 30:
        return 1.0
    return 0.5


def schedule(conn, cfg, now):
    """Games due for a poll, and the base interval that keeps daily credit use under budget."""
    games = conn.execute("SELECT * FROM games WHERE start > ? AND start < ?",
                         (iso(now), iso(now + timedelta(hours=cfg.window_hours)))).fetchall()
    if not games:
        return [], None
    cost = {s: odds_api.call_cost(len(markets_for(cfg, s)), len(cfg.books)) for s in cfg.sports}
    mins = {g["id"]: max((tracking.parse_time(g["start"]) - now).total_seconds() / 60, 0) for g in games}
    need = sum(1440 * cost.get(g["sport"], 5) / _factor(mins[g["id"]]) for g in games)   # credits/day at base=1 min
    base = max(cfg.min_poll_minutes, need / max(cfg.daily_credit_budget, 1))
    due = []
    for g in games:
        if g["sport"] not in cfg.sports:
            continue
        last = tracking.parse_time(g["last_polled"]) if g["last_polled"] else None
        interval = base * _factor(mins[g["id"]])
        if g["poll_now"] or last is None or (now - last).total_seconds() / 60 >= interval:
            due.append(g)
    return due, base


def poll_games(conn, cfg, games, now):
    n_lines, used = 0, 0
    for g in games:
        try:
            data, usage = odds_api.fetch_event_odds(SPORTS[g["sport"]][0], g["id"], cfg.odds_api_key,
                                                    cfg.books, markets_for(cfg, g["sport"]))
        except Exception as e:
            print(f"   odds {g['away']} @ {g['home']}: {e}")
            code = getattr(e, "code", None)
            if code in (401, 429):
                raise
            continue
        ev = {"id": g["id"], "home_team": g["home"], "away_team": g["away"], "commence_time": g["start"]}
        n_lines += odds_api.store_event(conn, g["sport"], ev, data, now)["dfs"]
        try:
            used += int(usage.get("last") or 0)
        except ValueError:
            pass
        if usage.get("remaining") is not None:
            meta_set(conn, "credits_remaining", usage["remaining"])
    day = now.astimezone(cfg.tz).strftime("%Y-%m-%d")
    spent = meta_get(conn, "credits_spent", {}) or {}
    spent[day] = spent.get(day, 0) + used
    meta_set(conn, "credits_spent", {k: v for k, v in spent.items() if k >= (now - timedelta(days=30)).strftime("%Y-%m-%d")})
    return n_lines


def refresh_injuries(conn, cfg, sport, now):
    """Update injuries; games with a newly ruled-out player get repriced right away. Returns changes."""
    try:
        cur = espn.injuries(sport)
    except Exception as e:
        print(f"   injuries {sport}: {e}")
        return []
    old = {r["player_key"]: r["status"] for r in conn.execute("SELECT player_key, status FROM injuries WHERE sport=?", (sport,))}
    changes = []
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM injuries WHERE sport=?", (sport,))
    for k, (name, status) in cur.items():
        conn.execute("INSERT INTO injuries VALUES(?,?,?,?,?)", (sport, k, name, status, iso(now)))
        if injury_level(status) != injury_level(old.get(k, "")):
            changes.append({"player_key": k, "player": name, "status": status, "was": old.get(k, "")})
    conn.execute("COMMIT")
    for c in changes:
        if injury_level(c["status"]) < 3:
            continue
        team = conn.execute("SELECT team FROM rosters WHERE sport=? AND player_key=?", (sport, c["player_key"])).fetchone()
        if team:
            for g in conn.execute("SELECT id, home, away FROM games WHERE sport=? AND start > ?", (sport, iso(now))).fetchall():
                if team["team"] in (canon_team(g["home"]), canon_team(g["away"])):
                    conn.execute("UPDATE games SET poll_now=1 WHERE id=?", (g["id"],))
    return changes


def refresh_rosters(conn, sport, now, hours=36):
    teams = set()
    for g in conn.execute("SELECT home, away FROM games WHERE sport=? AND start > ? AND start < ?",
                          (sport, iso(now), iso(now + timedelta(hours=hours)))):
        teams |= {g["home"], g["away"]}
    if not teams:
        return 0
    try:
        r = espn.rosters(sport, teams)
    except Exception as e:
        print(f"   rosters {sport}: {e}")
        return 0
    conn.execute("BEGIN IMMEDIATE")
    for k, team in r.items():
        conn.execute("INSERT OR REPLACE INTO rosters VALUES(?,?,?,?)", (sport, k, canon_team(team), iso(now)))
    conn.execute("COMMIT")
    meta_set(conn, f"rosters_refreshed:{sport}", iso(now))
    return len(r)


# ------------------------------------------------------------------ pricing
def price_all(conn, cfg, now, apps=None):
    """Every active pick'em line in the window, priced. Logs picks at or above break-even."""
    apps = apps or _apps(cfg)
    rows = conn.execute("""SELECT d.*, g.sport, g.home, g.away, g.start FROM dfs_lines d JOIN games g ON g.id=d.game_id
                           WHERE d.active=1 AND g.start > ? AND g.start < ?""",
                        (iso(now), iso(now + timedelta(hours=cfg.window_hours)))).fetchall()
    inj = {(r["sport"], r["player_key"]): r["status"] for r in conn.execute("SELECT sport, player_key, status FROM injuries")}
    ros = {(r["sport"], r["player_key"]): r["team"] for r in conn.execute("SELECT sport, player_key, team FROM rosters")}
    params = {s: blend.params(conn, s) for s in cfg.sports}
    proj_cache, absent_cache, legs, n_logged = {}, {}, [], 0
    for r in rows:
        if r["app"] not in apps or r["app"] not in cfg.dfs_apps:
            continue
        sport, pk = r["sport"], r["player_key"]
        status = inj.get((sport, pk), "")
        if injury_level(status) >= 3:
            continue
        fo, info = pricing.market_fair(sport, r["market"], r["point"],
                                       pricing.quotes_for(conn, r["game_id"], r["market"], pk), cfg.min_books)
        if fo is None:
            continue
        team = ros.get((sport, pk), "")
        home, away = canon_team(r["home"]), canon_team(r["away"])
        opp = away if team == home else home if team == away else ""
        if team and team not in absent_cache:
            absent_cache[team] = projections.absent_players(conn, sport, team)
        ck = (sport, pk, r["market"])
        if ck not in proj_cache:
            proj_cache[ck] = projections.project(conn, sport, pk, r["market"], opp=opp, team=team,
                                                 absent=absent_cache.get(team, set()))
        po = pricing.proj_over(proj_cache[ck], r["point"], sport, r["market"])
        p_over = pricing.blend(fo, po, params[sport]) if sport in params else fo
        side = "MORE" if r["tier"] or p_over >= 0.5 else "LESS"
        orient = (lambda p: p) if side == "MORE" else (lambda p: None if p is None else 1 - p)
        be = apps[r["app"]]["breakeven"]
        fair = orient(p_over)
        leg = {"sport": sport, "game_id": r["game_id"], "app": r["app"], "player": r["player"], "player_key": pk,
               "market": r["market"], "stat": LABELS.get(r["market"], r["market"]), "line": r["point"],
               "side": side, "tier": r["tier"], "home": r["home"], "away": r["away"], "start": r["start"],
               "team": team, "fair_market": orient(fo), "fair_proj": orient(po), "fair": fair,
               "proj_mu": proj_cache[ck]["mu"] if proj_cache[ck] else None,
               "sigma": info["sigma"], "edge": (fair - be) if be else None, "books": info["books"],
               "sharp": info["sharp"], "exact": info["exact"], "quotes": info["quotes"], "injury": status}
        legs.append(leg)
        if be and not r["tier"] and leg["edge"] >= cfg.log_edge:
            if tracking.log_pick(conn, leg, now, MODEL_VERSION, cfg.tz, cfg.holdout_share):
                n_logged += 1
    return legs, n_logged


# ------------------------------------------------------------------ board
def build_board(conn, cfg, now, legs, apps=None):
    apps = apps or _apps(cfg)
    corr = correlation.Correlations(conn)
    all_cands, pool = [], []
    for key in cfg.dfs_apps:
        a = apps.get(key)
        if not a or not a["entries"]:
            continue
        app_legs = [l for l in legs if l["app"] == key and l["edge"] is not None]
        cands, app_pool = slips.candidates(app_legs, a, corr)
        offset = len(pool)
        pool += app_pool
        for c in cands:
            c["pool_idx"] = [i + offset for i in c["pool_idx"]]
        all_cands += cands
    plays = portfolio.optimize(all_cands, pool, corr, cfg.kelly_mult, cfg.max_stake_pct, cfg.day_cap_pct,
                               cfg.player_cap_pct, cfg.max_plays)
    for p in plays:
        ids = []
        for l in p["legs"]:
            pid = tracking.pick_id(l, cfg.tz)
            if not conn.execute("SELECT 1 FROM picks WHERE id=?", (pid,)).fetchone():
                tracking.log_pick(conn, l, now, MODEL_VERSION, cfg.tz, cfg.holdout_share)
            conn.execute("UPDATE picks SET in_play=1 WHERE id=?", (pid,))
            ids.append(pid)
        p["pick_ids"] = ids
        p["system_id"] = tracking.log_system_entry(conn, now, p["app"], p["entry"], ids, p["stake_pct"], p["ev"], p["sigma"])
    flex = track_flex(conn, cfg, now, all_cands)
    board = {"generated_at": iso(now), "model_version": MODEL_VERSION, "flex_logged": flex,
             "plays": [_entry_json(p, apps) for p in plays],
             "entries": [_entry_json(c, apps) for c in sorted(all_cands, key=lambda c: c["ev"], reverse=True)[:40]],
             "legs": [_leg_json(l) for l in sorted([l for l in legs if not l["tier"] and l["edge"] is not None],
                                                   key=lambda l: l["edge"], reverse=True)[:200]],
             "alts": [_leg_json(l) for l in sorted([l for l in legs if l["tier"]], key=lambda l: l["fair"], reverse=True)[:80]],
             "best_app": portfolio.best_app([l for l in legs if l["edge"] is not None], apps)[:60],
             "watch": watch_list(conn, now), "credits_remaining": meta_get(conn, "credits_remaining"),
             "blend": {s: blend.params(conn, s) for s in cfg.sports}}
    meta_set(conn, "board", board)
    return board, plays


def track_flex(conn, cfg, now, cands):
    """Log the best positive Flex slip of each size, once per day, so each size builds its own record."""
    best = {}
    for c in cands:
        if "Flex" not in c["entry"] or c["ev"] <= 1.0:
            continue
        k = (c["app"], c["entry"])
        if k not in best or c["ev"] > best[k]["ev"]:
            best[k] = c
    logged = []
    for (app, entry), c in sorted(best.items(), key=lambda kv: kv[1]["n"]):
        ids = []
        for l in c["legs"]:
            pid = tracking.pick_id(l, cfg.tz)
            if not conn.execute("SELECT 1 FROM picks WHERE id=?", (pid,)).fetchone():
                tracking.log_pick(conn, l, now, MODEL_VERSION, cfg.tz, cfg.holdout_share)
            ids.append(pid)
        if tracking.log_flex_slip(conn, now, cfg.tz, app, entry, c["n"], ids, c["ev"], c["cash"], c["dist"]):
            logged.append(entry)
    return logged


def _leg_json(l):
    keep = ["sport", "game_id", "app", "player", "player_key", "market", "stat", "line", "side", "tier", "home", "away",
            "start", "team", "fair_market", "fair_proj", "fair", "proj_mu", "sigma", "edge", "books", "sharp", "exact",
            "injury", "quotes"]
    out = {k: l.get(k) for k in keep}
    for k in ("fair_market", "fair_proj", "fair", "sigma", "edge", "proj_mu"):
        if out[k] is not None:
            out[k] = round(out[k], 4)
    return out


def _entry_json(e, apps):
    return {"app": e["app"], "app_label": apps[e["app"]]["label"], "entry": e["entry"], "num": e.get("num", 1),
            "n": e["n"], "pay": {str(k): v for k, v in e["pay"].items()}, "ev": round(e["ev"], 4),
            "sigma": round(e["sigma"], 4), "cash": round(e["cash"], 4), "kelly": round(e["kelly"], 4),
            "dist": [round(x, 5) for x in e["dist"]], "correlated": e["correlated"],
            "stake_pct": round(e.get("stake_pct", 0), 5), "system_id": e.get("system_id"),
            "pick_ids": e.get("pick_ids", []), "legs": [_leg_json(l) for l in e["legs"]]}


def watch_list(conn, now):
    out = []
    for p in conn.execute("SELECT * FROM picks WHERE in_play=1 AND result='' AND start > ?", (iso(now),)):
        why = []
        if injury_level(p["injury"]) >= 1:
            why.append(f"listed {p['injury']}")
        if p["app_line_now"] is not None:
            why.append(f"line now {p['app_line_now']:g}")
        if p["clv"] is not None and p["clv"] <= -0.02:
            why.append(f"books moved against it ({p['clv'] * 100:+.1f} pts)")
        if why:
            out.append({"pick_id": p["id"], "player": p["player"], "stat": p["stat"], "line": p["line"], "side": p["side"],
                        "app": p["app"], "start": p["start"], "why": why})
    return out


# ------------------------------------------------------------------ alerts
def send_alerts(conn, cfg, legs, plays, watch, injury_changes, now):
    lines = []
    for l in sorted([l for l in legs if l["edge"] is not None and not l["tier"]], key=lambda l: l["edge"], reverse=True):
        if l["edge"] < cfg.alert_edge:
            break
        if alerts.once(conn, "leg:" + tracking.pick_id(l, cfg.tz)):
            lines.append(f"{l['player']} {l['side']} {l['line']:g} {l['stat']} ({l['app']}): fair {l['fair'] * 100:.1f}%, "
                         f"edge {l['edge'] * 100:+.1f} pts")
    for p in plays:
        if p.get("system_id") and alerts.once(conn, "play:" + p["system_id"]):
            lines.append(f"PLAY {p['entry']} on {p['app']} ({(p['ev'] - 1) * 100:+.1f}% EV, stake {p['stake_pct'] * 100:.2f}%): "
                         + "; ".join(f"{l['player']} {l['side']} {l['line']:g}" for l in p["legs"]))
    for w in watch:
        if alerts.once(conn, "watch:" + w["pick_id"] + ":" + ",".join(w["why"])):
            lines.append(f"WATCH {w['player']} {w['side']} {w['line']:g} {w['stat']}: {', '.join(w['why'])}")
    for c in injury_changes:
        if injury_level(c["status"]) >= 3 and alerts.once(conn, f"inj:{c['player_key']}:{c['status']}:{iso(now)[:10]}"):
            if conn.execute("SELECT 1 FROM picks WHERE player_key=? AND result='' AND start > ?",
                            (c["player_key"], iso(now))).fetchone():
                lines.append(f"OUT: {c['player']} ({c['status']}). Affected lines repriced.")
    if lines:
        alerts.send(cfg, f"Edge Engine: {len(lines)} update{'s' if len(lines) > 1 else ''}", "\n".join(lines[:12]),
                    cfg.public_url or None)
    return len(lines)


# ------------------------------------------------------------------ cycles
def nightly(conn, cfg):
    out = {}
    for s in cfg.sports:
        out[s] = {"blend": blend.fit(conn, s)}
        try:
            out[s]["correlations"] = len(correlation.fit(conn, s))
        except Exception as e:
            out[s]["correlations"] = f"error: {e}"
    meta_set(conn, "nightly", {"at": iso(datetime.now(timezone.utc)), "result": {k: str(v) for k, v in out.items()}})
    return out


def run_once(conn, cfg, now=None, export_path=None):
    """One full pass: everything in the window is polled. Used by the scheduled GitHub Action."""
    now = now or datetime.now(timezone.utc)
    apps = _apps(cfg)
    injury_changes = []
    for s in cfg.sports:
        refresh_events(conn, cfg, s, now)
        refresh_rosters(conn, s, now)
        injury_changes += refresh_injuries(conn, cfg, s, now)
    games = conn.execute("SELECT * FROM games WHERE start > ? AND start < ?",
                         (iso(now), iso(now + timedelta(hours=cfg.window_hours)))).fetchall()
    poll_games(conn, cfg, [g for g in games if g["sport"] in cfg.sports], now)
    return _after_poll(conn, cfg, now, apps, injury_changes, export_path)


def _after_poll(conn, cfg, now, apps, injury_changes, export_path):
    legs, n_logged = price_all(conn, cfg, now, apps)
    tracking.update_closing(conn, now)
    board, plays = build_board(conn, cfg, now, legs, apps)
    n_graded = tracking.grade(conn, now)
    tracking.settle_system_entries(conn, apps, now)
    n_alerts = send_alerts(conn, cfg, legs, plays, board["watch"], injury_changes, now)
    if export_path:
        from . import export
        export.write_app_json(conn, cfg, export_path, now)
    return {"legs": len(legs), "logged": n_logged, "plays": len(plays), "graded": n_graded, "alerts": n_alerts}


def run_forever(conn, cfg, tick=30, export_path=None, results_path=None):
    """Always-on loop for a server. Polls on the credit budget, reacts to lineup news within minutes."""
    apps = _apps(cfg)
    last = {"events": {}, "rosters": {}, "inj": 0.0, "grade": 0.0, "night": ""}
    print(f"Edge Engine running: sports {cfg.sports}, budget {cfg.daily_credit_budget:g} credits/day")
    while True:
        try:
            now = datetime.now(timezone.utc)
            t = time.time()
            changes = []
            for s in cfg.sports:
                if t - last["events"].get(s, 0) > 1800:
                    refresh_events(conn, cfg, s, now)
                    last["events"][s] = t
                if t - last["rosters"].get(s, 0) > 6 * 3600:
                    refresh_rosters(conn, s, now)
                    last["rosters"][s] = t
            if t - last["inj"] > 600:
                for s in cfg.sports:
                    changes += refresh_injuries(conn, cfg, s, now)
                last["inj"] = t
            due, base = schedule(conn, cfg, now)
            if due:
                poll_games(conn, cfg, due, now)
                r = _after_poll(conn, cfg, now, apps, changes, export_path)
                print(f"{now:%H:%M} polled {len(due)} games (base {base:.1f} min): {r}")
            elif changes:
                _after_poll(conn, cfg, now, apps, changes, export_path)
            if t - last["grade"] > 1800:
                tracking.grade(conn, now)
                tracking.settle_system_entries(conn, apps, now)
                if results_path:
                    from . import results
                    results.write(conn, cfg, results_path)
                last["grade"] = t
            local = now.astimezone(cfg.tz)
            if local.hour == 9 and last["night"] != local.strftime("%Y-%m-%d"):
                print("Nightly model fit:", nightly(conn, cfg))
                last["night"] = local.strftime("%Y-%m-%d")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if getattr(e, "code", None) == 401:
                raise SystemExit("The Odds API rejected the key.")
            traceback.print_exc()
            time.sleep(60)
        time.sleep(tick)
