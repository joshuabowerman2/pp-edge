"""The Odds API: events, player-prop odds from sharp books, exchanges, US books and pick'em apps.
Only price changes are written to the snapshot history."""
import urllib.parse
from datetime import datetime, timedelta, timezone

from ..markets import DFS_BOOKS
from . import http

BASE = "https://api.the-odds-api.com/v4"


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_events(sport_key, api_key, hours):
    """Upcoming events. This endpoint does not use quota."""
    now = datetime.now(timezone.utc)
    qs = urllib.parse.urlencode({"apiKey": api_key, "commenceTimeFrom": iso(now),
                                 "commenceTimeTo": iso(now + timedelta(hours=hours))})
    data, _ = http.get_json(f"{BASE}/sports/{sport_key}/events?{qs}")
    return data


def fetch_event_odds(sport_key, event_id, api_key, books, markets):
    qs = urllib.parse.urlencode({"apiKey": api_key, "bookmakers": ",".join(books),
                                 "markets": ",".join(markets), "oddsFormat": "american"})
    data, hdr = http.get_json(f"{BASE}/sports/{sport_key}/events/{event_id}/odds?{qs}")
    usage = {"remaining": hdr.get("x-requests-remaining"), "last": hdr.get("x-requests-last")}
    return data, usage


def call_cost(n_markets, n_books):
    """Credits per event-odds call: markets x regions, where each 10 bookmakers count as one region."""
    return n_markets * max(1, -(-n_books // 10))


def parse_event_odds(data):
    """-> (dfs_lines, quotes). dfs_lines: dicts per pick'em line; quotes: two-sided book prices."""
    dfs, quotes = [], []
    for bk in data.get("bookmakers", []):
        for mk in bk.get("markets", []):
            alt = mk["key"].endswith("_alternate")
            base = mk["key"][:-len("_alternate")] if alt else mk["key"]
            sides = {}
            for o in mk.get("outcomes", []):
                if o.get("point") is None or not o.get("description"):
                    continue
                key = (http.norm_name(o["description"]), float(o["point"]))
                s = sides.setdefault(key, {"player": o["description"]})
                s[o["name"].lower()] = o.get("price")
            for (pk, point), s in sides.items():
                if bk["key"] in DFS_BOOKS:
                    tier = ""
                    if alt:
                        # PrizePicks: demons at even odds, goblins at the default price. Other apps: "alt".
                        tier = ("demon" if s.get("over") == 100 else "goblin") if bk["key"] == "prizepicks" else "alt"
                    dfs.append({"app": bk["key"], "market": base, "player_key": pk, "player": s["player"],
                                "point": point, "tier": tier})
                elif s.get("over") is not None and s.get("under") is not None:
                    quotes.append({"book": bk["key"], "market": base, "player_key": pk, "player": s["player"],
                                   "point": point, "over": int(s["over"]), "under": int(s["under"])})
    return dfs, quotes


def store_event(conn, sport, ev, data, now, partial=False):
    """Write one event's odds: games row, changed quotes (+ snapshot), pick'em lines (+ deactivate gone ones).
    A `partial` poll asked for only some markets, so it updates what came back and removes nothing: lines
    it didn't ask about aren't gone, just not checked."""
    ts = iso(now)
    dfs, quotes = parse_event_odds(data)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if partial:   # a partial poll doesn't count as a full price check of the game
            conn.execute("UPDATE games SET start=? WHERE id=?", (ev.get("commence_time", ""), ev["id"]))
        else:
            conn.execute("""INSERT INTO games(id,sport,home,away,start,last_polled,poll_now) VALUES(?,?,?,?,?,?,0)
                            ON CONFLICT(id) DO UPDATE SET start=excluded.start, last_polled=excluded.last_polled, poll_now=0""",
                         (ev["id"], sport, ev.get("home_team", ""), ev.get("away_team", ""), ev.get("commence_time", ""), ts))
        changed = 0
        for q in quotes:
            old = conn.execute("SELECT over, under FROM quotes WHERE game_id=? AND book=? AND market=? AND player_key=? AND point=?",
                               (ev["id"], q["book"], q["market"], q["player_key"], q["point"])).fetchone()
            if old and old["over"] == q["over"] and old["under"] == q["under"]:
                conn.execute("UPDATE quotes SET updated_at=? WHERE game_id=? AND book=? AND market=? AND player_key=? AND point=?",
                             (ts, ev["id"], q["book"], q["market"], q["player_key"], q["point"]))
                continue
            conn.execute("""INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(game_id,book,market,player_key,point) DO UPDATE SET over=excluded.over,
                            under=excluded.under, updated_at=excluded.updated_at, player=excluded.player""",
                         (ev["id"], q["book"], q["market"], q["player_key"], q["player"], q["point"], q["over"], q["under"], ts))
            conn.execute("INSERT INTO snapshots(ts,game_id,book,market,player_key,point,over,under) VALUES(?,?,?,?,?,?,?,?)",
                         (ts, ev["id"], q["book"], q["market"], q["player_key"], q["point"], q["over"], q["under"]))
            changed += 1
        # Quotes a book stopped posting this poll are stale: drop them so they aren't used.
        books_seen = set() if partial else {q["book"] for q in quotes}
        for b in books_seen:
            conn.execute("DELETE FROM quotes WHERE game_id=? AND book=? AND updated_at<>?", (ev["id"], b, ts))
        apps_seen = set() if partial else {d["app"] for d in dfs}
        for d in dfs:
            conn.execute("""INSERT INTO dfs_lines VALUES(?,?,?,?,?,?,?,?,?,1)
                            ON CONFLICT(game_id,app,market,player_key,point,tier) DO UPDATE SET last_seen=excluded.last_seen, active=1""",
                         (ev["id"], d["app"], d["market"], d["player_key"], d["player"], d["point"], d["tier"], ts, ts))
        for a in apps_seen:
            conn.execute("UPDATE dfs_lines SET active=0 WHERE game_id=? AND app=? AND last_seen<>?", (ev["id"], a, ts))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"quotes": len(quotes), "changed": changed, "dfs": len(dfs)}
