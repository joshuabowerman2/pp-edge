"""Team bets: moneylines, spreads and totals at regular sportsbooks priced against the sharp books.

One Odds API call per sport covers every game (markets x regions credits, so 3 per sport with ten books).
A bet is +EV when a book pays more than the sharp books' fair chance with the vig removed.
Every play is logged the first time it's seen, with its price then, and graded from final scores."""
import hashlib
import json
import urllib.parse
from datetime import timedelta

from .markets import BOOK_WEIGHTS, SHARP, SPORTS
from .mathx import american_to_decimal, american_to_prob, devig_power
from .sources import espn, http, mlb
from .sources.http import canon_team, team_match

# Ten books bill as one region. Sharp books set the fair price; the rest are where you'd bet.
TEAM_BOOKS = ["pinnacle", "novig", "betonlineag", "prophetx", "fanduel", "draftkings", "betmgm",
              "williamhill_us", "espnbet", "fanatics"]
MARKETS = ["h2h", "spreads", "totals"]
MARKET_NAME = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
MIN_EV = 0.01             # show bets at least 1% above fair
ODDS_RANGE = (-400, 400)  # skip heavy favorites and long shots, where small price errors swamp the edge

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_plays (
  id TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, sport TEXT, game_id TEXT, home TEXT, away TEXT, start TEXT,
  market TEXT, outcome TEXT, point REAL, book TEXT, odds INTEGER, fair REAL, ev REAL, sharps TEXT,
  close_odds INTEGER, close_fair REAL, books TEXT, result TEXT DEFAULT '', home_score REAL, away_score REAL, graded_at TEXT);
CREATE INDEX IF NOT EXISTS team_open ON team_plays (result, start);
"""


def ensure(conn):
    conn.executescript(SCHEMA)


def fetch(sport, api_key, start_from, start_to):
    qs = urllib.parse.urlencode({"apiKey": api_key, "bookmakers": ",".join(TEAM_BOOKS), "markets": ",".join(MARKETS),
                                 "oddsFormat": "american", "commenceTimeFrom": start_from, "commenceTimeTo": start_to})
    data, hdr = http.get_json(f"https://api.the-odds-api.com/v4/sports/{SPORTS[sport][0]}/odds?{qs}")
    return data, {"remaining": hdr.get("x-requests-remaining"), "last": hdr.get("x-requests-last")}


def _key(market, o, home):
    """Which outcomes belong to the same bet: a total's point, a spread's home-team point, or the moneyline."""
    if market == "totals":
        return o.get("point")
    if market == "spreads":
        return o.get("point") if o.get("name") == home else -o.get("point") if o.get("point") is not None else None
    return 0


def _devig(prices):
    """{outcome: american odds} -> {outcome: fair chance}. Power method for two-way, proportional for three-way."""
    imp = {k: american_to_prob(v) for k, v in prices.items()}
    if len(imp) == 2:
        a, b = list(imp)
        pa = devig_power(imp[a], imp[b])
        return {a: pa, b: 1 - pa}
    tot = sum(imp.values())
    return {k: v / tot for k, v in imp.items()}


def price_event(ev):
    """-> list of +EV plays for one event. Each book is compared with the sharp books other than itself."""
    home = ev["home_team"]
    lines = {}   # (market, key) -> {book: {outcome: (odds, point)}}
    for bk in ev.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk["key"] not in MARKETS:
                continue
            for o in mk.get("outcomes", []):
                k = _key(mk["key"], o, home)
                if k is None:
                    continue
                lines.setdefault((mk["key"], k), {}).setdefault(bk["key"], {})[o["name"]] = (int(o["price"]), o.get("point"))
    plays = []
    for (market, k), books in lines.items():
        full = {b: v for b, v in books.items() if len(v) >= 2}
        fair_by = {b: _devig({n: x[0] for n, x in v.items()}) for b, v in full.items()}
        for book, outs in full.items():
            sharp = [b for b in fair_by if b in SHARP and b != book]
            if not sharp:
                continue
            w = {b: BOOK_WEIGHTS.get(b, 1) for b in sharp}
            for name, (odds, point) in outs.items():
                if not (ODDS_RANGE[0] <= odds <= ODDS_RANGE[1]):
                    continue
                if any(name not in fair_by[b] for b in sharp):
                    continue
                fair = sum(fair_by[b][name] * w[b] for b in sharp) / sum(w.values())
                ev_ = fair * american_to_decimal(odds) - 1
                if ev_ < MIN_EV:
                    continue
                others = sorted(((b, v[name][0]) for b, v in books.items() if name in v), key=lambda x: -american_to_decimal(x[1]))
                plays.append({"market": market, "outcome": name, "point": point, "book": book, "odds": odds,
                              "fair": round(fair, 4), "ev": round(ev_, 4), "sharps": sharp, "books": others})
    # one row per bet: the best-paying +EV book
    best = {}
    for p in plays:
        bk = (p["market"], p["outcome"], p["point"])
        if bk not in best or p["ev"] > best[bk]["ev"]:
            best[bk] = p
    return list(best.values())


def _id(game_id, p):
    return hashlib.sha256(f"{game_id}|{p['market']}|{p['outcome']}|{p['point']}|{p['book']}".encode()).hexdigest()[:16]


def run(conn, cfg, now, sports=None):
    """Fetch, price and log team bets for every sport with games in the window. Returns (plays, credits used)."""
    ensure(conn)
    t0, t1 = now, now + timedelta(hours=cfg.window_hours)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    out, used = [], 0
    for sport in sports or cfg.sports:
        if sport not in SPORTS:
            continue
        has = conn.execute("SELECT 1 FROM games WHERE sport=? AND start > ? AND start < ? LIMIT 1", (sport, iso(t0), iso(t1))).fetchone()
        if not has:
            continue   # no games in the window: skip the call (and its credits)
        try:
            data, usage = fetch(sport, cfg.odds_api_key, iso(t0), iso(t1))
        except Exception as e:
            print(f"   team odds {sport}: {e}")
            if getattr(e, "code", None) in (401, 429):
                break
            continue
        try:
            used += int(usage.get("last") or 0)
        except ValueError:
            pass
        for ev in data:
            for p in price_event(ev):
                p.update(sport=sport, game_id=ev["id"], home=ev["home_team"], away=ev["away_team"], start=ev["commence_time"])
                p["id"] = _id(ev["id"], p)
                out.append(p)
                row = conn.execute("SELECT id FROM team_plays WHERE id=?", (p["id"],)).fetchone()
                if row:   # seen before: keep the first price; track the latest as the closing price
                    conn.execute("UPDATE team_plays SET last_seen=?, close_odds=?, close_fair=?, books=? WHERE id=?",
                                 (iso(now), p["odds"], p["fair"], json.dumps(p["books"]), p["id"]))
                else:
                    conn.execute("""INSERT INTO team_plays(id, first_seen, last_seen, sport, game_id, home, away, start, market, outcome,
                                    point, book, odds, fair, ev, sharps, close_odds, close_fair, books)
                                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (p["id"], iso(now), iso(now), sport, ev["id"], ev["home_team"], ev["away_team"], ev["commence_time"],
                                  p["market"], p["outcome"], p["point"], p["book"], p["odds"], p["fair"], p["ev"],
                                  json.dumps(p["sharps"]), p["odds"], p["fair"], json.dumps(p["books"])))
    conn.commit()
    out.sort(key=lambda p: -p["ev"])
    return out, used


def _result(r, hs, as_):
    """win / loss / push for a stored play, from the final score."""
    if r["market"] == "totals":
        tot, pt = hs + as_, r["point"]
        if tot == pt:
            return "push"
        return "win" if (tot > pt) == (r["outcome"] == "Over") else "loss"
    if r["outcome"] == "Draw":
        return "win" if hs == as_ else "loss"
    mine, theirs = (hs, as_) if team_match(r["outcome"], r["home"]) else (as_, hs)
    if r["market"] == "spreads":
        mine += r["point"] or 0
    if mine == theirs:
        return "push"
    return "win" if mine > theirs else "loss"


def grade(conn, now, max_days=4):
    """Grade finished team plays from ESPN / MLB final scores (free). Returns the number graded."""
    ensure(conn)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute("SELECT * FROM team_plays WHERE result='' AND start < ?", (iso(now - timedelta(hours=4)),)).fetchall()
    boards, n = {}, 0
    for r in rows:
        start = http_time(r["start"])
        if start is None:
            continue
        if now - start > timedelta(days=max_days):
            conn.execute("UPDATE team_plays SET result='ungraded', graded_at=? WHERE id=?", (iso(now), r["id"]))
            continue
        for day in (start - timedelta(hours=6), start, start + timedelta(hours=6)):   # schedules are dated in US time
            key = (r["sport"], day.strftime("%Y%m%d"))
            if key not in boards:
                try:
                    boards[key] = mlb.schedule(day.strftime("%Y-%m-%d")) if r["sport"] == "mlb" else espn.scoreboard(r["sport"], key[1])
                except Exception as e:
                    print(f"   scores {key}: {e}")
                    boards[key] = []
            g = next((g for g in boards[key] if team_match(g["home"], r["home"]) and team_match(g["away"], r["away"])), None)
            if g and g["final"] and g.get("home_score") is not None and g.get("away_score") is not None:
                res = _result(r, float(g["home_score"]), float(g["away_score"]))
                conn.execute("UPDATE team_plays SET result=?, home_score=?, away_score=?, graded_at=? WHERE id=?",
                             (res, g["home_score"], g["away_score"], iso(now), r["id"]))
                n += 1
                break
    conn.commit()
    return n


def http_time(s):
    from .tracking import parse_time
    return parse_time(s)


def record(conn):
    """Flat 1-unit record of every logged team play, losses included."""
    ensure(conn)
    rows = conn.execute("SELECT * FROM team_plays WHERE result IN ('win','loss','push') ORDER BY start DESC").fetchall()
    w = sum(r["result"] == "win" for r in rows)
    l = sum(r["result"] == "loss" for r in rows)
    pu = sum(r["result"] == "push" for r in rows)
    units = sum((american_to_decimal(r["odds"]) - 1) if r["result"] == "win" else -1 if r["result"] == "loss" else 0 for r in rows)
    staked = w + l
    exp = sum(r["ev"] for r in rows if r["result"] != "push")
    by = {}
    for r in rows:
        b = by.setdefault(MARKET_NAME.get(r["market"], r["market"]), {"w": 0, "l": 0, "p": 0, "units": 0.0})
        b[{"win": "w", "loss": "l", "push": "p"}[r["result"]]] += 1
        b["units"] += (american_to_decimal(r["odds"]) - 1) if r["result"] == "win" else -1 if r["result"] == "loss" else 0
    open_n = conn.execute("SELECT COUNT(*) FROM team_plays WHERE result=''").fetchone()[0]
    recent = [{"sport": r["sport"], "home": r["home"], "away": r["away"], "start": r["start"], "market": r["market"],
               "outcome": r["outcome"], "point": r["point"], "book": r["book"], "odds": r["odds"], "fair": r["fair"],
               "ev": r["ev"], "result": r["result"], "home_score": r["home_score"], "away_score": r["away_score"]}
              for r in rows[:40]]
    return {"won": w, "lost": l, "push": pu, "open": open_n, "units": round(units, 2),
            "roi": round(units / staked, 4) if staked else None, "exp_units": round(exp, 2),
            "by_market": {k: {**v, "units": round(v["units"], 2)} for k, v in by.items()}, "recent": recent}


def board(conn, now):
    """Current +EV plays for games that haven't started (from the latest run), for the app."""
    ensure(conn)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    last = conn.execute("SELECT MAX(last_seen) FROM team_plays").fetchone()[0]
    if not last:
        return []
    rows = conn.execute("SELECT * FROM team_plays WHERE last_seen=? AND start > ?",
                        (last, iso(now))).fetchall()
    out = []
    for r in rows:
        odds, fair = r["close_odds"] or r["odds"], r["close_fair"] or r["fair"]
        out.append({"id": r["id"], "sport": r["sport"], "game_id": r["game_id"], "home": r["home"], "away": r["away"],
                    "start": r["start"], "market": r["market"], "market_name": MARKET_NAME.get(r["market"], r["market"]),
                    "outcome": r["outcome"], "point": r["point"], "book": r["book"], "odds": odds, "fair": fair,
                    "ev": round(fair * american_to_decimal(odds) - 1, 4), "first_odds": r["odds"], "first_seen": r["first_seen"],
                    "sharps": json.loads(r["sharps"] or "[]"), "books": json.loads(r["books"] or "[]")})
    out.sort(key=lambda p: -p["ev"])
    return out
