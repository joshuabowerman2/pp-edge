"""The track record.

* Every pick at or above break-even is logged once, with a timestamp and model version.
* Each pick's core fields are hashed together with the previous pick's hash (a hash chain), so
  editing or deleting an old pick breaks every hash after it. `verify_chain` checks this.
* 1 in 5 picks (chosen by hash, not by us) is a holdout pick: models are fit only on the others
  and judged on these.
* Closing line value: the market price is re-taken until the game starts; CLV = close - pick.
* Grading from ESPN (MLB from the MLB Stats API); every graded game's full box score is stored
  for the projection and correlation models."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from . import mathx, pricing
from .markets import ESPN_PATH, stat_keys
from .sources import espn, mlb
from .sources.http import canon_team, norm_name, team_match

CORE = ["id", "created_at", "model_version", "sport", "game_id", "app", "player", "market", "line", "side",
        "tier", "start", "fair_market", "fair_proj", "fair", "edge"]


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def pick_id(leg, tz):
    t = parse_time(leg["start"])
    day = t.astimezone(tz).strftime("%Y-%m-%d") if t else ""
    return f"{day}|{leg['sport']}|{leg['app']}|{leg['player_key']}|{leg['market']}|{leg['line']:g}|{leg['side']}|{leg.get('tier', '')}"


def is_holdout(pid, share=5):
    return int(hashlib.sha256(pid.encode()).hexdigest()[:8], 16) % share == 0


def _hash(prev, rec):
    core = {k: (round(rec[k], 6) if isinstance(rec[k], float) else rec[k]) for k in CORE}
    return hashlib.sha256((prev + json.dumps(core, sort_keys=True)).encode()).hexdigest()


def log_pick(conn, leg, now, model_version, tz, share=5):
    """Insert a pick if this exact line/side hasn't been logged. Returns the id or None."""
    pid = pick_id(leg, tz)
    if conn.execute("SELECT 1 FROM picks WHERE id=?", (pid,)).fetchone():
        return None
    last = conn.execute("SELECT seq, hash FROM picks ORDER BY seq DESC LIMIT 1").fetchone()
    seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, "genesis")
    rec = {"id": pid, "created_at": iso(now), "model_version": model_version, "sport": leg["sport"],
           "game_id": leg["game_id"], "app": leg["app"], "player": leg["player"], "market": leg["market"],
           "line": float(leg["line"]), "side": leg["side"], "tier": leg.get("tier", ""), "start": leg["start"],
           "fair_market": leg["fair_market"], "fair_proj": leg.get("fair_proj"), "fair": leg["fair"],
           "edge": leg["edge"]}
    h = _hash(prev, rec)
    conn.execute("""INSERT INTO picks(id,seq,created_at,model_version,sport,game_id,app,player,player_key,market,stat,
                    line,side,tier,home,away,start,fair_market,fair_proj,fair,sigma,edge,books,sharp,exact,holdout,
                    prev_hash,hash,injury) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (pid, seq, rec["created_at"], model_version, leg["sport"], leg["game_id"], leg["app"], leg["player"],
                  leg["player_key"], leg["market"], leg["stat"], rec["line"], leg["side"], rec["tier"], leg["home"],
                  leg["away"], leg["start"], leg["fair_market"], leg.get("fair_proj"), leg["fair"], leg["sigma"],
                  leg["edge"], leg["books"], int(leg["sharp"]), int(leg["exact"] > 0),
                  int(is_holdout(pid, share)), prev, h, leg.get("injury", "")))
    return pid


def verify_chain(conn):
    """(ok, count, first bad seq or None)."""
    prev, n = "genesis", 0
    for r in conn.execute(f"SELECT seq, prev_hash, hash, {', '.join(CORE)} FROM picks ORDER BY seq"):
        n += 1
        rec = dict(r)
        if r["prev_hash"] != prev or _hash(prev, rec) != r["hash"]:
            return False, n, r["seq"]
        prev = r["hash"]
    return True, n, None


def chain_head(conn):
    r = conn.execute("SELECT seq, hash FROM picks ORDER BY seq DESC LIMIT 1").fetchone()
    return (r["seq"], r["hash"]) if r else (0, "genesis")


def update_closing(conn, now, min_books=1):
    """Re-take the market price for picks whose game hasn't started. The last one before start is the close.
    Also records the app's current line and the player's injury status (for the watch list)."""
    n = 0
    for p in conn.execute("SELECT * FROM picks WHERE result='' AND start > ?", (iso(now),)).fetchall():
        quotes = pricing.quotes_for(conn, p["game_id"], p["market"], p["player_key"])
        fo, info = pricing.market_fair(p["sport"], p["market"], p["line"], quotes, min_books=min_books)
        lines = [r["point"] for r in conn.execute(
            "SELECT point FROM dfs_lines WHERE game_id=? AND app=? AND market=? AND player_key=? AND tier=? AND active=1",
            (p["game_id"], p["app"], p["market"], p["player_key"], p["tier"]))]
        now_line = None if not lines or p["line"] in lines else min(lines, key=lambda x: abs(x - p["line"]))
        inj = conn.execute("SELECT status FROM injuries WHERE sport=? AND player_key=?", (p["sport"], p["player_key"])).fetchone()
        sets = {"app_line_now": now_line, "injury": inj["status"] if inj else ""}
        if fo is not None:
            close = fo if p["side"] == "MORE" else 1 - fo
            sets.update(close_fair=close, clv=close - p["fair_market"], closed_at=iso(now))
            n += 1
        conn.execute(f"UPDATE picks SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), p["id"]))
    return n


def judge(actual, line, side):
    if actual == line:
        return "push"
    return "hit" if (actual > line) == (side == "MORE") else "miss"


def store_box(conn, sport, event_id, game_date, rows, home, away):
    """Save a full box score with canonical team keys."""
    conn.execute("BEGIN IMMEDIATE")
    for r in rows:
        if r["dnp"]:
            continue
        team = home if team_match(r["team"], home) else away if team_match(r["team"], away) else r["team"]
        opp = away if team == home else home if team == away else r["opp"]
        is_home = 1 if team == home else 0 if team == away else None
        conn.execute("""INSERT OR REPLACE INTO boxscores(sport, event_id, game_date, team, opp, player_key, player,
                        minutes, stats, home) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     (sport, str(event_id), game_date, canon_team(team), canon_team(opp), r["player_key"], r["player"],
                      r["minutes"], json.dumps(r["stats"]), is_home))
    conn.execute("COMMIT")


def _find(games, home, away, start):
    cands = [g for g in games if team_match(g["home"], home) and team_match(g["away"], away)]
    if not cands:
        return None
    t = parse_time(start)
    return min(cands, key=lambda g: abs((parse_time(g["date"]) - t).total_seconds()) if t and parse_time(g["date"]) else 0)


def grade(conn, now, max_days=4):
    """Grade finished picks; store each graded game's box score. Returns number graded."""
    open_ = conn.execute("SELECT * FROM picks WHERE result='' AND start < ?", (iso(now - timedelta(hours=4)),)).fetchall()
    boards, boxes, graded = {}, {}, 0
    for p in open_:
        t = parse_time(p["start"])
        if not t:
            continue
        if now - t > timedelta(days=max_days):
            conn.execute("UPDATE picks SET result='ungraded', graded_at=? WHERE id=?", (iso(now), p["id"]))
            continue
        keys = stat_keys(p["sport"], p["market"])
        if not keys or p["sport"] not in ESPN_PATH:
            continue
        from .markets import EASTERN_TZ
        day = t.astimezone(EASTERN_TZ)
        bkey = (p["sport"], day.strftime("%Y%m%d"))
        if bkey not in boards:
            try:
                boards[bkey] = mlb.schedule(day.strftime("%Y-%m-%d")) if p["sport"] == "mlb" else espn.scoreboard(p["sport"], bkey[1])
            except Exception as e:
                print(f"   scoreboard {bkey}: {e}")
                boards[bkey] = []
        g = _find(boards[bkey], p["home"], p["away"], p["start"])
        if not g or not g["final"]:
            continue
        gk = (p["sport"], g["id"])
        if gk not in boxes:
            try:
                rows = mlb.box(g["id"]) if p["sport"] == "mlb" else espn.box(p["sport"], g["id"])
            except Exception as e:
                print(f"   box score {gk}: {e}")
                continue
            boxes[gk] = {r["player_key"]: r for r in rows}
            store_box(conn, p["sport"], g["id"], day.strftime("%Y-%m-%d"), rows, p["home"], p["away"])
        row = boxes[gk].get(p["player_key"])
        if row is None:
            result, actual = "unmatched", None
        elif row["dnp"] or any(k not in row["stats"] for k in keys):
            result, actual = "void", None
        else:
            actual = sum(row["stats"][k] for k in keys)
            result = judge(actual, p["line"], p["side"])
        conn.execute("UPDATE picks SET result=?, actual=?, graded_at=? WHERE id=?", (result, actual, iso(now), p["id"]))
        graded += 1
    return graded


def backfill(conn, sport, days, now=None):
    """Load past box scores so projections and correlations have history from day one."""
    now = now or datetime.now(timezone.utc)
    n = 0
    for d in range(1, days + 1):
        day = (now - timedelta(days=d))
        try:
            games = mlb.schedule(day.strftime("%Y-%m-%d")) if sport == "mlb" else espn.scoreboard(sport, day.strftime("%Y%m%d"))
        except Exception as e:
            print(f"   {sport} {day:%Y-%m-%d}: {e}")
            continue
        for g in games:
            if not g["final"]:
                continue
            if conn.execute("SELECT 1 FROM boxscores WHERE sport=? AND event_id=? LIMIT 1", (sport, str(g["id"]))).fetchone():
                continue
            try:
                rows = mlb.box(g["id"]) if sport == "mlb" else espn.box(sport, g["id"])
            except Exception as e:
                print(f"   box {g['id']}: {e}")
                continue
            store_box(conn, sport, g["id"], day.strftime("%Y-%m-%d"), rows, g["home"], g["away"])
            n += 1
    return n


# ------------------------------------------------------------- line moves
def prop_key(leg):
    return f"{leg['game_id']}|{leg['app']}|{leg['player_key']}|{leg['market']}|{float(leg['line']):g}|{leg['side']}"


def record_moves(conn, legs, now, min_change=0.001):
    """Store each prop's fair % when it is first seen and whenever it changes."""
    n = 0
    for l in legs:
        if l.get("tier") or l.get("fair") is None:
            continue
        k = prop_key(l)
        last = conn.execute("SELECT fair FROM prop_moves WHERE key=? ORDER BY ts DESC LIMIT 1", (k,)).fetchone()
        if last is None or abs(last["fair"] - l["fair"]) >= min_change:
            conn.execute("INSERT OR REPLACE INTO prop_moves(key, ts, fair) VALUES(?,?,?)", (k, iso(now), l["fair"]))
            n += 1
    return n


def moves_for(conn, legs):
    """{prop key: (first fair, first time, number of changes)} for these legs."""
    out = {}
    for l in legs:
        if l.get("tier") or l.get("game_id") is None:
            continue
        k = prop_key(l)
        if k in out:
            continue
        rows = conn.execute("SELECT ts, fair FROM prop_moves WHERE key=? ORDER BY ts", (k,)).fetchall()
        if rows:
            out[k] = (rows[0]["fair"], rows[0]["ts"], len(rows) - 1)
    return out


# ------------------------------------------------------------- system entries
def entry_return(app_cfg, entry, results):
    """Payout multiple for an entry given leg results; voids step down within the entry's family."""
    e = app_cfg["entries"][entry]
    live = [r for r in results if r in ("hit", "miss")]
    hits = live.count("hit")
    if len(live) == e["n"]:
        return float(e["pay"].get(hits, 0))
    n = len(live)
    fam = e.get("family", "")
    alt = next((v for k, v in app_cfg["entries"].items() if fam and v.get("family") == fam and v["n"] == n), None)
    if alt is None and fam == "flex" and n == 2:
        alt = app_cfg["entries"].get("2-Pick Power")
    if n < 2 or alt is None:
        return 1.0
    return float(alt["pay"].get(hits, 0))


def log_system_entry(conn, now, app, entry, pick_ids, stake_pct, ev, sigma):
    eid = f"{iso(now)[:10]}|{app}|{entry}|{'+'.join(sorted(pick_ids))}"
    if conn.execute("SELECT 1 FROM system_entries WHERE id=?", (eid,)).fetchone():
        return None
    last = conn.execute("SELECT seq, hash FROM system_entries ORDER BY seq DESC LIMIT 1").fetchone()
    seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, "genesis")
    body = json.dumps({"id": eid, "t": iso(now), "stake": round(stake_pct, 6), "ev": round(ev, 6)}, sort_keys=True)
    h = hashlib.sha256((prev + body).encode()).hexdigest()
    conn.execute("""INSERT INTO system_entries(id,seq,created_at,app,entry,pick_ids,stake_pct,ev,sigma,prev_hash,hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (eid, seq, iso(now), app, entry, json.dumps(pick_ids),
                                                       stake_pct, ev, sigma, prev, h))
    return eid


def log_flex_slip(conn, now, tz, app, entry, n, pick_ids, ev, cash, dist, per_day=1):
    """Lock in a Flex slip for today if this size doesn't have its slips for the day yet. Never edited after."""
    day = now.astimezone(tz).strftime("%Y-%m-%d")
    if conn.execute("SELECT COUNT(*) c FROM flex_slips WHERE day=? AND app=? AND entry=?",
                    (day, app, entry)).fetchone()["c"] >= per_day:
        return None
    sid = f"{day}|{app}|{entry}|{'+'.join(sorted(pick_ids))}"
    if conn.execute("SELECT 1 FROM flex_slips WHERE id=?", (sid,)).fetchone():
        return None
    last = conn.execute("SELECT seq, hash FROM flex_slips ORDER BY seq DESC LIMIT 1").fetchone()
    seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, "genesis")
    body = json.dumps({"id": sid, "t": iso(now), "ev": round(ev, 6)}, sort_keys=True)
    h = hashlib.sha256((prev + body).encode()).hexdigest()
    conn.execute("""INSERT INTO flex_slips(id,seq,created_at,day,app,entry,n,pick_ids,ev,cash,dist,prev_hash,hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (sid, seq, iso(now), day, app, entry, n, json.dumps(pick_ids), ev, cash,
                  json.dumps([round(x, 5) for x in dist]), prev, h))
    return sid


def settle_flex_slips(conn, apps, now):
    n = 0
    for e in conn.execute("SELECT * FROM flex_slips WHERE status='open'").fetchall():
        ids = json.loads(e["pick_ids"])
        res = [r["result"] for r in conn.execute(
            f"SELECT result FROM picks WHERE id IN ({','.join('?' * len(ids))})", ids)]
        if len(res) != len(ids) or any(r == "" for r in res):
            continue
        hits = res.count("hit")
        if any(r in ("unmatched", "ungraded") for r in res) or e["app"] not in apps:
            conn.execute("UPDATE flex_slips SET status='void', hits=?, ret=1, settled_at=? WHERE id=?",
                         (hits, iso(now), e["id"]))
            continue
        ret = entry_return(apps[e["app"]], e["entry"], res)
        conn.execute("UPDATE flex_slips SET status='settled', hits=?, ret=?, settled_at=? WHERE id=?",
                     (hits, ret, iso(now), e["id"]))
        n += 1
    return n


def settle_system_entries(conn, apps, now):
    settle_flex_slips(conn, apps, now)
    n = 0
    for e in conn.execute("SELECT * FROM system_entries WHERE status='open'").fetchall():
        ids = json.loads(e["pick_ids"])
        res = [r["result"] for r in conn.execute(
            f"SELECT result FROM picks WHERE id IN ({','.join('?' * len(ids))})", ids)]
        if len(res) != len(ids) or any(r == "" for r in res):
            continue
        if any(r in ("unmatched", "ungraded") for r in res):
            conn.execute("UPDATE system_entries SET status='void', ret=1, settled_at=? WHERE id=?", (iso(now), e["id"]))
            continue
        ret = entry_return(apps[e["app"]], e["entry"], res)
        conn.execute("UPDATE system_entries SET status='settled', ret=?, settled_at=? WHERE id=?", (ret, iso(now), e["id"]))
        n += 1
    return n
