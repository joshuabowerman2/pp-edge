#!/usr/bin/env python3
"""
PrizePicks Edge Finder (auto)

Each run:
  1. Pulls today's PrizePicks lines and the same props from sharp and US sportsbooks
     (all through The Odds API).
  2. Prices every PrizePicks leg at its own line, even when the books post a
     different number, using a stat model to shift the books' line.
  3. Trusts sharp books most, shrinks legs no sharp book has priced, and drops
     legs where the books disagree.
  4. Builds 3/4/5/6-pick Flex slips, simulating same-game correlation.
  5. Recommends the day's plays for your goal with a stake for each.
  6. Logs picks to history/picks.csv, records the closing line on later runs,
     and grades finished games from ESPN box scores.
  7. Writes <out>.json (site/index.json on GitHub Pages) for the Edge Finder app:
     today's plays, every slip, the priced props, and the last 60 days of graded
     legs, so the app can log entries and grade them.
  8. Drops players listed out on ESPN's injury report and flags questionable ones.
  9. Looks up rosters so same-game correlation knows teammates from opponents.
 10. Watches logged legs for injuries and line moves before games start.
 11. Tunes its own safety discounts from your graded history once there is enough.
 12. With --alt-lines, prices PrizePicks goblin and demon lines too.
 13. Sends a phone (ntfy) or Discord alert with the plays if NTFY_TOPIC or
     DISCORD_WEBHOOK is set.
MLB legs are graded from the MLB Stats API; other sports from ESPN box scores.

Runs on GitHub Actions (see .github/workflows/daily.yml), or by hand:
  ODDS_API_KEY=yourkey python prizepicks_edge.py --sports nba nhl --bankroll 500
Optional: numpy (for same-game correlation). Without it, legs are treated as independent.
"""
import argparse, csv, html, json, math, os, re, sys, time, unicodedata, webbrowser
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone
from itertools import combinations
from zoneinfo import ZoneInfo

try:
    import numpy as np
except ImportError:
    np = None

# ================================================================ settings ===
# Payouts per number of hits. Check these against your PrizePicks app.
ENTRIES = {
    "2-Pick Power": (2, {2: 3}),
    "3-Pick Power": (3, {3: 5}),
    "4-Pick Power": (4, {4: 10}),
    "5-Pick Power": (5, {5: 20}),
    "6-Pick Power": (6, {6: 37.5}),
    "3-Pick Flex":  (3, {3: 3, 2: 1}),
    "4-Pick Flex":  (4, {4: 6, 3: 1.5}),
    "5-Pick Flex":  (5, {5: 10, 4: 2, 3: 0.4}),
    "6-Pick Flex":  (6, {6: 25, 5: 2, 4: 0.4}),
}
FLEX = ["3-Pick Flex", "4-Pick Flex", "5-Pick Flex", "6-Pick Flex"]

# PrizePicks plus 9 sportsbooks = 10 books, which The Odds API bills as one region.
BOOKS = ["prizepicks", "pinnacle", "betonlineag", "fanduel", "draftkings", "betmgm",
         "williamhill_us", "espnbet", "betrivers", "bovada"]
SHARP = {"pinnacle", "betonlineag"}
BOOK_WEIGHTS = {"pinnacle": 4, "betonlineag": 2.5, "fanduel": 1.5, "draftkings": 1.2}
CONVERTED_WEIGHT = 0.6     # a book quoted at a different line counts this much
NO_SHARP_SHRINK = 0.75     # with no sharp book, keep this share of the edge over 50%
EST_ONLY_SHRINK = 0.70     # with no book at the exact PrizePicks line, keep this share
MAX_SPREAD = 0.10          # drop a leg if books' fair % differ by more than this

# Core markets keep credit use low. --all-markets adds the extras.
SPORTS = {
    "nba":  ("basketball_nba",
             ["player_points", "player_rebounds", "player_assists", "player_threes",
              "player_points_rebounds_assists"],
             ["player_points_rebounds", "player_points_assists", "player_rebounds_assists",
              "player_blocks", "player_steals", "player_turnovers"]),
    "wnba": ("basketball_wnba",
             ["player_points", "player_rebounds", "player_assists", "player_points_rebounds_assists"], []),
    "nfl":  ("americanfootball_nfl",
             ["player_pass_yds", "player_rush_yds", "player_reception_yds", "player_receptions"],
             ["player_pass_tds", "player_pass_completions", "player_pass_attempts",
              "player_rush_attempts", "player_rush_reception_yds"]),
    "cfb":  ("americanfootball_ncaaf",
             ["player_pass_yds", "player_rush_yds", "player_reception_yds"], ["player_receptions"]),
    "nhl":  ("icehockey_nhl", ["player_shots_on_goal", "player_points"],
             ["player_assists", "player_total_saves", "player_blocked_shots"]),
    "mlb":  ("baseball_mlb", ["pitcher_strikeouts", "batter_total_bases", "batter_hits"],
             ["pitcher_hits_allowed", "pitcher_outs", "batter_hits_runs_rbis"]),
}

LABELS = {"player_points": "Points", "player_rebounds": "Rebounds", "player_assists": "Assists",
          "player_threes": "3-PT Made", "player_points_rebounds_assists": "Pts+Rebs+Asts",
          "player_points_rebounds": "Pts+Rebs", "player_points_assists": "Pts+Asts",
          "player_rebounds_assists": "Rebs+Asts", "player_blocks": "Blocks", "player_steals": "Steals",
          "player_turnovers": "Turnovers", "player_pass_yds": "Pass Yards", "player_rush_yds": "Rush Yards",
          "player_reception_yds": "Receiving Yards", "player_receptions": "Receptions",
          "player_pass_tds": "Pass TDs", "player_pass_completions": "Pass Completions",
          "player_pass_attempts": "Pass Attempts", "player_rush_attempts": "Rush Attempts",
          "player_rush_reception_yds": "Rush+Rec Yds", "player_shots_on_goal": "Shots on Goal",
          "player_total_saves": "Goalie Saves", "player_blocked_shots": "Blocked Shots",
          "pitcher_strikeouts": "Pitcher Ks", "batter_total_bases": "Total Bases", "batter_hits": "Hits",
          "pitcher_hits_allowed": "Hits Allowed", "pitcher_outs": "Pitching Outs",
          "batter_hits_runs_rbis": "Hits+Runs+RBIs"}

# Stat model for shifting a book's line to the PrizePicks line.
# ("poisson",) for low counts; ("normal", cv) where std dev = cv * mean.
DIST = {
    "player_points": ("normal", .30), "player_rebounds": ("normal", .40), "player_assists": ("normal", .45),
    "player_points_rebounds_assists": ("normal", .25), "player_points_rebounds": ("normal", .28),
    "player_points_assists": ("normal", .30), "player_rebounds_assists": ("normal", .35),
    "player_threes": ("poisson",), "player_blocks": ("poisson",), "player_steals": ("poisson",),
    "player_turnovers": ("poisson",),
    "player_pass_yds": ("normal", .25), "player_rush_yds": ("normal", .45),
    "player_reception_yds": ("normal", .55), "player_receptions": ("poisson",),
    "player_pass_tds": ("poisson",), "player_pass_completions": ("normal", .20),
    "player_pass_attempts": ("normal", .15), "player_rush_attempts": ("normal", .30),
    "player_rush_reception_yds": ("normal", .40),
    "player_shots_on_goal": ("poisson",), "player_total_saves": ("normal", .22),
    "player_blocked_shots": ("poisson",),
    "pitcher_strikeouts": ("poisson",), "batter_total_bases": ("poisson",), "batter_hits": ("poisson",),
    "pitcher_hits_allowed": ("poisson",), "pitcher_outs": ("normal", .20),
    "batter_hits_runs_rbis": ("poisson",),
}
# NHL "points" is goals + assists, a low count.
SPORT_DIST = {("nhl", "player_points"): ("poisson",), ("nhl", "player_assists"): ("poisson",)}

# Rough correlation between two different players' stats in the same game, both on MORE.
# Team isn't known, so these blend teammates and opponents and are kept small on purpose.
CORR = {
    ("player_pass_yds", "player_reception_yds"): .30, ("player_pass_yds", "player_receptions"): .20,
    ("player_pass_completions", "player_receptions"): .25,
    ("player_pass_completions", "player_reception_yds"): .25,
    ("player_pass_yds", "player_rush_yds"): -.10, ("player_reception_yds", "player_reception_yds"): .05,
    ("player_points", "player_points"): .05, ("player_assists", "player_points"): .05,
    ("player_points_rebounds_assists", "player_points_rebounds_assists"): .05,
    ("player_rebounds", "player_rebounds"): -.05,
    ("player_shots_on_goal", "player_shots_on_goal"): .05, ("player_shots_on_goal", "player_total_saves"): .10,
    ("batter_hits", "pitcher_strikeouts"): -.10, ("batter_total_bases", "pitcher_strikeouts"): -.10,
}
# When rosters are known: correlation for two players on the SAME team, and on OPPOSING teams.
# Pairs are sorted market names. Rough, deliberately modest estimates.
CORR_TEAM = {
    "nfl": {("player_pass_yds", "player_reception_yds"): .45, ("player_pass_yds", "player_receptions"): .30,
            ("player_pass_completions", "player_receptions"): .35, ("player_pass_completions", "player_reception_yds"): .35,
            ("player_pass_yds", "player_rush_yds"): -.10, ("player_reception_yds", "player_reception_yds"): -.05,
            ("player_rush_yds", "player_rush_yds"): -.15},
    "nba": {("player_points", "player_points"): -.05, ("player_assists", "player_points"): .10,
            ("player_points_rebounds_assists", "player_points_rebounds_assists"): -.03,
            ("player_rebounds", "player_rebounds"): -.10},
    "wnba": {("player_points", "player_points"): -.05, ("player_assists", "player_points"): .10,
             ("player_rebounds", "player_rebounds"): -.10},
    "nhl": {("player_shots_on_goal", "player_shots_on_goal"): .05, ("player_points", "player_points"): .20},
    "mlb": {("batter_hits", "batter_hits"): .10, ("batter_total_bases", "batter_total_bases"): .10},
}
CORR_OPP = {
    "nfl": {("player_pass_yds", "player_reception_yds"): .10, ("player_pass_yds", "player_receptions"): .05,
            ("player_pass_yds", "player_pass_yds"): .15, ("player_reception_yds", "player_reception_yds"): .10,
            ("player_pass_yds", "player_rush_yds"): .05},
    "nba": {("player_points", "player_points"): .10, ("player_assists", "player_points"): .05,
            ("player_points_rebounds_assists", "player_points_rebounds_assists"): .10,
            ("player_rebounds", "player_rebounds"): -.05},
    "wnba": {("player_points", "player_points"): .10, ("player_rebounds", "player_rebounds"): -.05},
    "nhl": {("player_shots_on_goal", "player_total_saves"): .35, ("player_points", "player_points"): -.05,
            ("player_shots_on_goal", "player_shots_on_goal"): .05},
    "mlb": {("batter_hits", "pitcher_strikeouts"): -.20, ("batter_strikeouts", "pitcher_strikeouts"): .30,
            ("batter_total_bases", "pitcher_strikeouts"): -.15, ("batter_hits", "pitcher_hits_allowed"): .25,
            ("batter_total_bases", "pitcher_hits_allowed"): .20},
}

ODDS_BASE = "https://api.the-odds-api.com/v4"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_PATH = {"nba": "basketball/nba", "wnba": "basketball/wnba", "nfl": "football/nfl",
             "cfb": "football/college-football", "nhl": "hockey/nhl"}
ESPN_INFO = {**ESPN_PATH, "mlb": "baseball/mlb"}   # injuries and rosters
MLB_BASE = "https://statsapi.mlb.com/api/v1"
# market -> (stat group, MLB Stats API fields summed)
MLB_KEYS = {"pitcher_strikeouts": ("pitching", ["strikeOuts"]), "pitcher_hits_allowed": ("pitching", ["hits"]),
            "pitcher_outs": ("pitching", ["outs"]), "batter_hits": ("batting", ["hits"]),
            "batter_total_bases": ("batting", ["totalBases"]),
            "batter_hits_runs_rbis": ("batting", ["hits", "runs", "rbi"])}
# Injury status words -> 3 out, 2 doubtful, 1 questionable.
INJ_LEVEL = {"out": 3, "injured reserve": 3, "-day il": 3, "suspended": 3, "inactive": 3,
             "doubtful": 2, "questionable": 1, "day-to-day": 1, "day to day": 1}
LOCAL_TZ = ZoneInfo(os.environ.get("REPORT_TZ", "America/Chicago"))
EASTERN = ZoneInfo("America/New_York")

# ==================================================================== math ===
def american_to_prob(a):
    a = float(a)
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)

def devig_power(p_over, p_under):
    lo, hi = 0.5, 3.0
    for _ in range(60):
        k = (lo + hi) / 2
        if p_over ** k + p_under ** k > 1: lo = k
        else: hi = k
    return p_over ** ((lo + hi) / 2)

def devig_mult(p_over, p_under):
    return p_over / (p_over + p_under)

def hit_dist(ps):
    d = [1.0]
    for p in ps:
        n = [0.0] * (len(d) + 1)
        for k, v in enumerate(d):
            n[k] += v * (1 - p); n[k + 1] += v * p
        d = n
    return d

def payout_mult(dist, pay):
    return sum(v * pay.get(k, 0) for k, v in enumerate(dist))

def breakeven(n, pay):
    lo, hi = 0.3, 0.95
    for _ in range(60):
        m = (lo + hi) / 2
        if payout_mult(hit_dist([m] * n), pay) < 1: lo = m
        else: hi = m
    return (lo + hi) / 2

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def norm_ppf(p):
    lo, hi = -8.0, 8.0
    for _ in range(80):
        m = (lo + hi) / 2
        if norm_cdf(m) < p: lo = m
        else: hi = m
    return (lo + hi) / 2

def poisson_cdf(k, mu):
    if k < 0:
        return 0.0
    term = total = math.exp(-mu)
    for i in range(1, int(k) + 1):
        term *= mu / i; total += term
    return min(total, 1.0)

def model_over(mu, line, dist):
    """P(over), ignoring pushes, for a stat with mean mu."""
    if dist[0] == "poisson":
        over = 1 - poisson_cdf(math.floor(line), mu)
        under = poisson_cdf(math.ceil(line) - 1, mu)
    else:
        sd = max(dist[1] * mu, 0.5)
        if float(line).is_integer():
            over, under = 1 - norm_cdf((line + .5 - mu) / sd), norm_cdf((line - .5 - mu) / sd)
        else:
            over, under = 1 - norm_cdf((line - mu) / sd), norm_cdf((line - mu) / sd)
    tot = over + under
    return over / tot if tot > 0 else 0.5

def shift_line(p_over, from_line, to_line, dist):
    """Fair P(over to_line), given fair P(over from_line)."""
    lo, hi = 0.01, max(3 * from_line, from_line + 10)
    for _ in range(70):
        mu = (lo + hi) / 2
        if model_over(mu, from_line, dist) < p_over: lo = mu
        else: hi = mu
    return model_over((lo + hi) / 2, to_line, dist)

def max_shift(line, dist):
    return 1.0 if dist[0] == "poisson" else max(1.0, 0.12 * line)

def kelly_fraction(dist, pay):
    """Fraction of bankroll that maximizes long-run growth for one slip."""
    outs = [(p, pay.get(k, 0) - 1) for k, p in enumerate(dist) if p > 0]
    if sum(p * r for p, r in outs) <= 0:
        return 0.0
    lo, hi = 0.0, 0.999
    for _ in range(60):
        f = (lo + hi) / 2
        if sum(p * r / (1 + f * r) for p, r in outs) > 0: lo = f
        else: hi = f
    return lo

# ================================================================ fetching ===
def norm_name(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[.\-'’]", " ", s.lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pp-edge/2.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()), dict(r.headers)

def fetch_sport(sport_key, markets, api_key, hours):
    """Returns (pp_lines, quotes, credits_left, n_games).
    quotes: {(player, market): [(book, point, over, under), ...]} at every point the books post."""
    now = datetime.now(timezone.utc)
    qs = urllib.parse.urlencode({"apiKey": api_key,
        "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    events, _ = get_json(f"{ODDS_BASE}/sports/{sport_key}/events?{qs}")
    pp, quotes, left = [], {}, None
    for ev in events:
        qs = urllib.parse.urlencode({"apiKey": api_key, "bookmakers": ",".join(BOOKS),
                                     "markets": ",".join(markets), "oddsFormat": "american"})
        try:
            data, hdr = get_json(f"{ODDS_BASE}/sports/{sport_key}/events/{ev['id']}/odds?{qs}")
        except urllib.error.HTTPError as e:
            print(f"   skipped {ev.get('away_team')} @ {ev.get('home_team')}: HTTP {e.code}")
            if e.code in (401, 429):
                raise
            continue
        left = {k.lower(): v for k, v in hdr.items()}.get("x-requests-remaining", left)
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        for bk in data.get("bookmakers", []):
            for mk in bk.get("markets", []):
                # Alternate markets ("player_points_alternate") hold PrizePicks goblins and demons
                # and the books' alternate lines. Both are filed under the base market.
                alt = mk["key"].endswith("_alternate")
                base = mk["key"][:-len("_alternate")] if alt else mk["key"]
                sides = {}
                for o in mk.get("outcomes", []):
                    if o.get("point") is None or not o.get("description"):
                        continue
                    s = sides.setdefault((norm_name(o["description"]), float(o["point"])),
                                         {"player": o["description"]})
                    s[o["name"].lower()] = o["price"]
                for (pn, point), s in sides.items():
                    if bk["key"] == "prizepicks":
                        # The Odds API posts demons at even odds (+100) and goblins at the default price.
                        tier = ("demon" if s.get("over") == 100 else "goblin") if alt else ""
                        pp.append({"pkey": (pn, base), "player": s["player"], "market": base, "tier": tier,
                                   "line": point, "home": home, "away": away,
                                   "game": f"{away} @ {home}", "start": ev.get("commence_time", "")})
                    elif "over" in s and "under" in s:
                        quotes.setdefault((pn, base), []).append((bk["key"], point, s["over"], s["under"]))
        time.sleep(0.2)
    return pp, quotes, left, len(events)

# ================================================================= pricing ===
def price_leg(sport, market, line, qlist, method, min_books):
    """Fair P(over) at `line` from all books. Returns (fair_over, info) or (None, reason)."""
    devig = devig_power if method == "power" else devig_mult
    dist = SPORT_DIST.get((sport, market)) or DIST.get(market, ("normal", .35))
    best = {}
    for book, point, o, u in qlist or []:
        if book not in best or abs(point - line) < abs(best[book][0] - line):
            best[book] = (point, o, u)
    est, weights, used, exact, conv = [], [], [], 0, 0
    for book, (point, o, u) in best.items():
        p = devig(american_to_prob(o), american_to_prob(u))
        w = BOOK_WEIGHTS.get(book, 1)
        if point == line:
            exact += 1
        elif abs(point - line) <= max_shift(line, dist):
            p = shift_line(p, point, line, dist); w *= CONVERTED_WEIGHT; conv += 1
        else:
            continue
        est.append(p); weights.append(w); used.append((book, point, o, u))
    if len(est) < min_books:
        return None, "few books"
    spread = max(est) - min(est)
    if spread > MAX_SPREAD:
        return None, "books disagree"
    fo = sum(p * w for p, w in zip(est, weights)) / sum(weights)
    raw = fo
    sharp = any(b in SHARP for b, *_ in used)
    if not sharp:
        fo = 0.5 + (fo - 0.5) * NO_SHARP_SHRINK
    if exact == 0:
        fo = 0.5 + (fo - 0.5) * EST_ONLY_SHRINK
    return fo, {"books": len(est), "exact": exact, "converted": conv, "sharp": sharp, "spread": spread,
                "raw": raw, "quotes": ", ".join(f"{b} {pt:g} {o:+d}/{u:+d}" for b, pt, o, u in used)}

def injury_level(status):
    s = (status or "").lower()
    return max([v for k, v in INJ_LEVEL.items() if k in s], default=0)

def build_rows(sport, pp_lines, quotes, method, be, min_books, drops, injuries=None, teams=None):
    """Price every PrizePicks line. Goblin/demon lines come back with `tier` set and
    are MORE-only; their edge is not comparable to standard lines, so they never go in slips."""
    injuries, teams = injuries or {}, teams or {}
    rows, seen = [], set()
    for p in pp_lines:
        tier = p.get("tier", "")
        k = (p["pkey"], p["line"], tier)
        if k in seen:
            continue
        seen.add(k)
        status = injuries.get(p["pkey"][0], "")
        if injury_level(status) >= 3:
            drops["player out"] = drops.get("player out", 0) + 1
            continue
        fo, info = price_leg(sport, p["market"], p["line"], quotes.get(p["pkey"]), method, min_books)
        if fo is None:
            drops[info] = drops.get(info, 0) + 1
            continue
        raw = info.pop("raw")
        side = "MORE" if tier or fo >= 0.5 else "LESS"
        fp, rawp = (fo, raw) if side == "MORE" else (1 - fo, 1 - raw)
        rows.append({**p, **info, "sport": sport, "stat": LABELS.get(p["market"], p["market"]),
                     "side": side, "fair": fp, "raw": rawp, "edge": fp - be,
                     "injury": status, "team": teams.get(p["pkey"][0], "")})
    return rows

# ============================================================== flex slips ===
def pair_corr(a, b):
    if a["game"] != b["game"] or norm_name(a["player"]) == norm_name(b["player"]):
        return 0.0
    pair = tuple(sorted((a["market"], b["market"])))
    ta, tb = a.get("team"), b.get("team")
    if ta and tb:
        rho = (CORR_TEAM if ta == tb else CORR_OPP).get(a.get("sport", ""), {}).get(pair, 0.0)
    else:
        rho = CORR.get(pair, 0.0)
    sa = 1 if a["side"] == "MORE" else -1
    sb = 1 if b["side"] == "MORE" else -1
    return rho * sa * sb

_RNG = np.random.default_rng(7) if np else None
_Z = {}

def slip_dist(legs, sims=20000):
    """Hit-count distribution. Uses a Gaussian copula when same-game legs are correlated."""
    ps = [l["fair"] for l in legs]
    n = len(legs)
    C = [[1.0 if i == j else pair_corr(legs[i], legs[j]) for j in range(n)] for i in range(n)]
    if np is None or all(C[i][j] == 0 for i in range(n) for j in range(n) if i != j):
        return hit_dist(ps), False
    try:
        L = np.linalg.cholesky(np.array(C))
    except np.linalg.LinAlgError:
        return hit_dist(ps), False
    if n not in _Z:
        _Z[n] = _RNG.standard_normal((sims, n))
    Z = _Z[n] @ L.T
    hits = (Z < np.array([norm_ppf(p) for p in ps])).sum(axis=1)
    counts = np.bincount(hits, minlength=n + 1) / sims
    return [float(c) for c in counts], True

def build_slips(rows, pool_size=18, per_size=5, max_per_game=2, max_reuse=2):
    pool = sorted([r for r in rows if r["fair"] > 0.5 and not r.get("tier") and injury_level(r.get("injury")) < 2],
                  key=lambda r: r["fair"], reverse=True)[:pool_size]
    m = len(pool)
    bad_pair = {(i, j) for i in range(m) for j in range(i + 1, m) if pair_corr(pool[i], pool[j]) < -0.02}
    out = {}
    for name in FLEX:
        n, pay = ENTRIES[name]
        if m < n:
            out[name] = []
            continue
        cands = []
        for combo in combinations(range(m), n):
            legs = [pool[i] for i in combo]
            if len({norm_name(l["player"]) for l in legs}) < n:
                continue
            games = {}
            for l in legs:
                games[l["game"]] = games.get(l["game"], 0) + 1
            if max(games.values()) > max_per_game:
                continue
            if any((i, j) in bad_pair for i, j in combinations(combo, 2)):
                continue
            cands.append((payout_mult(hit_dist([l["fair"] for l in legs]), pay), combo))
        cands.sort(key=lambda c: c[0], reverse=True)
        rescored = []
        for _, combo in cands[:400]:
            legs = [pool[i] for i in combo]
            d, corr = slip_dist(legs)
            rescored.append((payout_mult(d, pay), combo, d, corr))
        rescored.sort(key=lambda c: c[0], reverse=True)
        chosen, used = [], {}
        for mult, combo, d, corr in rescored:
            if any(used.get(i, 0) >= max_reuse for i in combo):
                continue
            if any(len(set(combo) & set(c[1])) > n // 2 for c in chosen):
                continue
            chosen.append((mult, combo, d, corr))
            for i in combo:
                used[i] = used.get(i, 0) + 1
            if len(chosen) == per_size:
                break
        out[name] = [{"name": name, "num": k, "mult": mult, "legs": [pool[i] for i in combo], "dist": d,
                      "pay": pay, "n": n, "corr": corr,
                      "cash": sum(d[h] for h in pay if pay[h] >= 1),
                      "kelly": kelly_fraction(d, pay)}
                     for k, (mult, combo, d, corr) in enumerate(chosen, 1)]
    return out

def choose_plays(slips, goal, max_plays, bankroll, kelly_mult=0.25, cap=0.02, day_cap=0.08):
    """Pick the day's plays and a stake for each."""
    pos = [s for ss in slips.values() for s in ss if s["mult"] > 1.0]
    key = (lambda s: s["mult"]) if goal == "profit" else (lambda s: (s["cash"], s["mult"]))
    plays, leg_use = [], {}
    for s in sorted(pos, key=key, reverse=True):
        ids = [(norm_name(l["player"]), l["market"]) for l in s["legs"]]
        if any(leg_use.get(i, 0) >= 3 for i in ids):
            continue
        plays.append(s)
        for i in ids:
            leg_use[i] = leg_use.get(i, 0) + 1
        if len(plays) == max_plays:
            break
    for s in plays:
        s["stake_pct"] = min(s["kelly"] * kelly_mult, cap)
    total = sum(s["stake_pct"] for s in plays)
    if total > day_cap:
        for s in plays:
            s["stake_pct"] *= day_cap / total
    for s in plays:
        s["stake"] = round(s["stake_pct"] * bankroll, 2) if bankroll else None
        s["play"] = True
    return plays

# ================================================================ tracking ===
HIST_FIELDS = ["id", "logged_at", "date", "sport", "player", "market", "stat", "line", "side",
               "pick_fair", "close_fair", "clv", "in_play", "home", "away", "start",
               "result", "actual", "graded_at",
               "raw_fair", "sharp", "exact", "pp_line_now", "injury"]

def load_history(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []

def save_history(path, hist):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HIST_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(hist)

def parse_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None

def leg_id(r):
    t = parse_time(r["start"])
    day = t.astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if t else ""
    return f"{day}|{r['sport']}|{norm_name(r['player'])}|{r['market']}|{float(r['line']):g}|{r['side']}", day

def update_closing(hist, quotes_by_sport, method, now, pp_now=None, injuries_by_sport=None):
    """Reprice every not-yet-started logged leg at its own line. The last price before
    the game starts is the closing line; CLV = closing fair % - fair % when picked.
    Also records whether PrizePicks moved the line, and the player's current injury status."""
    pp_now, injuries_by_sport = pp_now or {}, injuries_by_sport or {}
    n = 0
    for h in hist:
        if h.get("result"):
            continue
        t = parse_time(h["start"])
        q = quotes_by_sport.get(h["sport"])
        if not t or t <= now or q is None:
            continue
        name = norm_name(h["player"])
        if h["sport"] in injuries_by_sport:
            h["injury"] = injuries_by_sport[h["sport"]].get(name, "")
        lines = pp_now.get((h["sport"], name, h["market"]))
        if lines:
            line = float(h["line"])
            h["pp_line_now"] = "" if line in lines else f"{min(lines, key=lambda x: abs(x - line)):g}"
        fo, _ = price_leg(h["sport"], h["market"], float(h["line"]), q.get((name, h["market"])), method, 1)
        if fo is None:
            continue
        fair = fo if h["side"] == "MORE" else 1 - fo
        h["close_fair"] = f"{fair:.4f}"
        h["clv"] = f"{fair - float(h['pick_fair']):.4f}"
        n += 1
    return n

def watch_list(hist, now, clv_drop=-0.02):
    """Logged legs in a play that haven't started and now look worse: injury news,
    a moved PrizePicks line, or books moving against the pick."""
    out = []
    for h in hist:
        t = parse_time(h.get("start", ""))
        if h.get("result") or h.get("in_play") != "1" or not t or t <= now:
            continue
        why = []
        if injury_level(h.get("injury")) >= 1:
            why.append(f"listed {h['injury']}")
        if h.get("pp_line_now"):
            why.append(f"PrizePicks line now {h['pp_line_now']}")
        if h.get("clv") and float(h["clv"]) <= clv_drop:
            why.append(f"books moved against it ({float(h['clv']) * 100:+.1f} pts)")
        if why:
            out.append({"player": h["player"], "stat": h["stat"], "line": h["line"], "side": h["side"],
                        "start": h["start"], "sport": h["sport"], "market": h["market"], "why": why})
    return out

def log_picks(hist, rows, play_keys, min_edge, now):
    index = {h["id"]: h for h in hist}
    added = 0
    for r in rows:
        if r.get("tier"):
            continue
        key = (norm_name(r["player"]), r["market"], r["line"], r["side"])
        if r["edge"] < min_edge and key not in play_keys:
            continue
        lid, day = leg_id(r)
        if lid in index:
            if key in play_keys:
                index[lid]["in_play"] = "1"
            continue
        h = {"id": lid, "logged_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "date": day, "sport": r["sport"],
             "player": r["player"], "market": r["market"], "stat": r["stat"], "line": f"{r['line']:g}",
             "side": r["side"], "pick_fair": f"{r['fair']:.4f}", "close_fair": "", "clv": "",
             "in_play": "1" if key in play_keys else "0", "home": r["home"], "away": r["away"],
             "start": r["start"], "result": "", "actual": "", "graded_at": "",
             "raw_fair": f"{r['raw']:.4f}", "sharp": "1" if r["sharp"] else "0",
             "exact": "1" if r["exact"] else "0", "pp_line_now": "", "injury": r.get("injury", "")}
        hist.append(h); index[lid] = h; added += 1
    return added

# ESPN box-score keys that make up each market.
GRADE_KEYS = {
    "basketball": {"player_points": ["points"], "player_rebounds": ["rebounds"], "player_assists": ["assists"],
                   "player_threes": ["threePointFieldGoalsMade"],
                   "player_points_rebounds_assists": ["points", "rebounds", "assists"],
                   "player_points_rebounds": ["points", "rebounds"], "player_points_assists": ["points", "assists"],
                   "player_rebounds_assists": ["rebounds", "assists"], "player_blocks": ["blocks"],
                   "player_steals": ["steals"], "player_turnovers": ["turnovers"]},
    "football": {"player_pass_yds": ["passingYards"], "player_rush_yds": ["rushingYards"],
                 "player_reception_yds": ["receivingYards"], "player_receptions": ["receptions"],
                 "player_pass_tds": ["passingTouchdowns"], "player_pass_completions": ["completions"],
                 "player_pass_attempts": ["passingAttempts"], "player_rush_attempts": ["rushingAttempts"],
                 "player_rush_reception_yds": ["rushingYards", "receivingYards"],
                 "player_pass_interceptions": ["interceptions"]},
    "hockey": {"player_shots_on_goal": ["shotsTotal"], "player_points": ["goals", "assists"],
               "player_assists": ["assists"], "player_total_saves": ["saves"],
               "player_blocked_shots": ["blockedShots"]},
}

def team_match(a, b):
    na, nb = norm_name(a), norm_name(b)
    ta, tb = na.split(), nb.split()
    if not ta or not tb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    # Nicknames shared by two teams (Red Sox / White Sox) need the word before too.
    if ta[-1] in {"sox"}:
        return ta[-2:] == tb[-2:]
    return ta[-1] == tb[-1]

def judge(actual, line, side):
    if actual == line:
        return "push"
    return "hit" if (actual > line) == (side == "MORE") else "miss"

def parse_mlb_box(box):
    stats = {}
    for side in ("home", "away"):
        for p in box.get("teams", {}).get(side, {}).get("players", {}).values():
            name = norm_name(p.get("person", {}).get("fullName", ""))
            if name:
                stats[name] = p.get("stats", {})
    return stats

def grade_mlb(h, t, boards, boxes):
    """(result, actual) for one MLB leg from the MLB Stats API, or None if not final yet."""
    spec = MLB_KEYS.get(h["market"])
    if not spec:
        return None
    ymd = t.astimezone(EASTERN).strftime("%Y-%m-%d")
    bkey = ("mlb", ymd)
    if bkey not in boards:
        try:
            sched = get_json(f"{MLB_BASE}/schedule?sportId=1&date={ymd}")[0]
            boards[bkey] = [g for d in sched.get("dates", []) for g in d.get("games", [])]
        except Exception as e:
            print(f"   MLB schedule {ymd}: {e}")
            boards[bkey] = []
    cands = []
    for g in boards[bkey]:
        tm = g.get("teams", {})
        if team_match(tm.get("home", {}).get("team", {}).get("name", ""), h["home"]) and \
           team_match(tm.get("away", {}).get("team", {}).get("name", ""), h["away"]):
            gt = parse_time(g.get("gameDate", ""))
            cands.append((abs((gt - t).total_seconds()) if gt else 1e12, g))
    if not cands:
        return None
    g = min(cands, key=lambda c: c[0])[1]          # doubleheaders: the game nearest the listed start
    if g.get("status", {}).get("abstractGameState") != "Final":
        return None
    pk = ("mlb", g["gamePk"])
    if pk not in boxes:
        try:
            boxes[pk] = parse_mlb_box(get_json(f"{MLB_BASE}/game/{g['gamePk']}/boxscore")[0])
        except Exception as e:
            print(f"   MLB box score {g['gamePk']}: {e}")
            return None
    stats = boxes[pk]
    name = norm_name(h["player"])
    if name not in stats:
        return ("unmatched", "")
    group, keys = spec
    grp = stats[name].get(group) or {}
    if not grp:
        return ("void", "")                        # on the roster but didn't bat or pitch
    actual = sum(float(grp.get(k) or 0) for k in keys)
    return (judge(actual, float(h["line"]), h["side"]), f"{actual:g}")

def parse_box(summary):
    stats, dnp = {}, set()
    for team in summary.get("boxscore", {}).get("players", []):
        for grp in team.get("statistics", []):
            keys = grp.get("keys", [])
            for ath in grp.get("athletes", []):
                name = norm_name(ath.get("athlete", {}).get("displayName", ""))
                if not name:
                    continue
                if ath.get("didNotPlay"):
                    dnp.add(name); continue
                d = stats.setdefault(name, {})
                for k, v in zip(keys, ath.get("stats", [])):
                    sep = "/" if "/" in k else "-" if "-" in k else None
                    pairs = zip(k.split(sep), str(v).split(sep)) if sep else [(k, v)]
                    for kk, vv in pairs:
                        try:
                            d[kk] = float(str(vv).replace("+", ""))
                        except ValueError:
                            pass
    return stats, dnp

def grade_history(hist, now, max_days=4):
    """Grade finished legs from ESPN box scores. Returns number graded."""
    boards, boxes, graded = {}, {}, 0
    for h in hist:
        if h.get("result") or (h["sport"] not in ESPN_PATH and h["sport"] != "mlb"):
            continue
        t = parse_time(h["start"])
        if not t or now - t < timedelta(hours=4) or now - t > timedelta(days=max_days):
            if t and now - t > timedelta(days=max_days):
                h["result"] = "ungraded"
            continue
        if h["sport"] == "mlb":
            out = grade_mlb(h, t, boards, boxes)
            if out is None:
                continue
            h["result"], h["actual"] = out
            h["graded_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            graded += 1
            continue
        path = ESPN_PATH[h["sport"]]
        family = path.split("/")[0]
        keys = GRADE_KEYS.get(family, {}).get(h["market"])
        if not keys:
            continue
        ymd = t.astimezone(EASTERN).strftime("%Y%m%d")
        bkey = (h["sport"], ymd)
        if bkey not in boards:
            extra = "&groups=80&limit=400" if h["sport"] == "cfb" else "&limit=200"
            try:
                boards[bkey] = get_json(f"{ESPN_BASE}/{path}/scoreboard?dates={ymd}{extra}")[0].get("events", [])
            except Exception as e:
                print(f"   ESPN scoreboard {h['sport']} {ymd}: {e}")
                boards[bkey] = []
        event = None
        for ev in boards[bkey]:
            comp = (ev.get("competitions") or [{}])[0]
            teams = {c.get("homeAway"): c.get("team", {}).get("displayName", "") for c in comp.get("competitors", [])}
            if team_match(teams.get("home", ""), h["home"]) and team_match(teams.get("away", ""), h["away"]):
                event = ev; break
        if not event or not event.get("status", {}).get("type", {}).get("completed"):
            continue
        if event["id"] not in boxes:
            try:
                boxes[event["id"]] = parse_box(get_json(f"{ESPN_BASE}/{path}/summary?event={event['id']}")[0])
            except Exception as e:
                print(f"   ESPN box score {event['id']}: {e}")
                continue
        stats, dnp = boxes[event["id"]]
        name = norm_name(h["player"])
        if name not in stats:
            h["result"] = "void" if name in dnp else "unmatched"
        else:
            actual = sum(stats[name].get(k, 0.0) for k in keys)
            h["actual"] = f"{actual:g}"
            h["result"] = judge(actual, float(h["line"]), h["side"])
        h["graded_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        graded += 1
    return graded

def track_summary(hist, now):
    def stats(rows):
        g = [h for h in rows if h.get("result") in ("hit", "miss")]
        c = [float(h["clv"]) for h in rows if h.get("clv")]
        return {"n": len(g), "hits": sum(h["result"] == "hit" for h in g),
                "exp": sum(float(h["pick_fair"]) for h in g) / len(g) if g else None,
                "clv_n": len(c), "clv": sum(c) / len(c) if c else None,
                "beat": sum(x > 0 for x in c) / len(c) if c else None}
    cutoff = (now - timedelta(days=30)).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    out = [("All time", stats(hist)), ("Last 30 days", stats([h for h in hist if h["date"] >= cutoff])),
           ("Played legs only", stats([h for h in hist if h.get("in_play") == "1"]))]
    for sp in sorted({h["sport"] for h in hist}):
        out.append((sp.upper(), stats([h for h in hist if h["sport"] == sp])))
    return out

# ======================================================= injuries & rosters ===
def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)

def fetch_injuries(sport):
    """{normalized player name: status} from ESPN's injury report. Empty if unavailable."""
    path = ESPN_INFO.get(sport)
    if not path:
        return {}
    try:
        data = get_json(f"{ESPN_BASE}/{path}/injuries")[0]
    except Exception as e:
        print(f"   injuries {sport}: {e}")
        return {}
    out = {}
    for d in _walk(data):
        ath, st = d.get("athlete"), d.get("status")
        if isinstance(st, dict):
            st = st.get("name") or st.get("description") or st.get("type")
        if isinstance(ath, dict) and ath.get("displayName") and isinstance(st, str) and st:
            name = norm_name(ath["displayName"])
            if injury_level(st) >= injury_level(out.get(name, "")):
                out[name] = st
    return out

def fetch_rosters(sport, team_names):
    """{normalized player name: team name as The Odds API spells it} for teams playing today."""
    path = ESPN_INFO.get(sport)
    if not path or not team_names:
        return {}
    try:
        data = get_json(f"{ESPN_BASE}/{path}/teams?limit=100")[0]
    except Exception as e:
        print(f"   teams {sport}: {e}")
        return {}
    teams = [d["team"] for d in _walk(data)
             if isinstance(d.get("team"), dict) and d["team"].get("id") and d["team"].get("displayName")]
    out = {}
    for tn in sorted(team_names):
        tm = next((t for t in teams if team_match(t["displayName"], tn)), None)
        if not tm:
            continue
        try:
            roster = get_json(f"{ESPN_BASE}/{path}/teams/{tm['id']}/roster")[0]
        except Exception as e:
            print(f"   roster {tn}: {e}")
            continue
        for a in roster.get("athletes", []):
            group = a.get("items") if isinstance(a, dict) and isinstance(a.get("items"), list) else [a]
            for p in group:
                if isinstance(p, dict) and p.get("displayName"):
                    out[norm_name(p["displayName"])] = tn
        time.sleep(0.1)
    return out

# ================================================================= tuning ===
def tune_shrinks(hist, min_legs=150, prior=300):
    """Fit the no-sharp and estimated-line discounts to your graded legs (lowest log loss),
    then blend toward the defaults by sample size. Needs raw_fair, so only legs logged
    after this version count."""
    legs = []
    for h in hist:
        if h.get("result") not in ("hit", "miss") or not h.get("raw_fair"):
            continue
        legs.append((float(h["raw_fair"]), h["result"] == "hit", h.get("sharp") == "0", h.get("exact") == "0"))
    n_ns, n_est = sum(l[2] for l in legs), sum(l[3] for l in legs)
    out = {"n": len(legs), "n_no_sharp": n_ns, "n_est": n_est, "applied": False,
           "no_sharp": NO_SHARP_SHRINK, "est_only": EST_ONLY_SHRINK}
    if len(legs) < min_legs or max(n_ns, n_est) < 30:
        return out
    grid = [x / 20 for x in range(4, 21)]          # 0.20 .. 1.00
    best = None
    for a in grid:
        for b in grid:
            ll = 0.0
            for raw, hit, ns, est in legs:
                p = 0.5 + (raw - 0.5) * (a if ns else 1) * (b if est else 1)
                p = min(max(p, 0.01), 0.99)
                ll -= math.log(p if hit else 1 - p)
            if best is None or ll < best[0]:
                best = (ll, a, b)
    _, a, b = best
    # A discount with fewer than 30 legs behind it keeps its default.
    w_ns = n_ns / (n_ns + prior) if n_ns >= 30 else 0.0
    w_est = n_est / (n_est + prior) if n_est >= 30 else 0.0
    out.update(applied=True, fit_no_sharp=a, fit_est_only=b,
               no_sharp=round(NO_SHARP_SHRINK + (a - NO_SHARP_SHRINK) * w_ns, 3),
               est_only=round(EST_ONLY_SHRINK + (b - EST_ONLY_SHRINK) * w_est, 3))
    return out

# ========================================================== notifications ===
def notify(plays, watch, report_url, always=False):
    """Phone push through ntfy.sh (NTFY_TOPIC) and/or a Discord webhook (DISCORD_WEBHOOK)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not (topic or hook) or not (plays or watch or always):
        return
    lines = [f"{s['name']} {(s['mult'] - 1) * 100:+.1f}%"
             + (f" (${s['stake']:.2f})" if s.get("stake") else "") + ": "
             + "; ".join(f"{l['player']} {l['side']} {l['line']:g} {l['stat']}" for l in s["legs"])
             for s in plays[:5]] or ["No positive plays this run."]
    for w in watch[:5]:
        lines.append(f"Watch: {w['player']} {w['side']} {w['line']} {w['stat']}: {', '.join(w['why'])}")
    if report_url:
        lines.append(report_url)
    msg = "\n".join(lines)[:1900]
    title = f"Edge Finder: {len(plays)} play{'' if len(plays) == 1 else 's'}" + (f", {len(watch)} to watch" if watch else "")
    if topic:
        headers = {"Title": title, "Tags": "chart_with_upwards_trend"}
        if report_url:
            headers["Click"] = report_url
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}", data=msg.encode(), headers=headers), timeout=15)
            print("Sent ntfy alert.")
        except Exception as e:
            print(f"ntfy alert failed: {e}")
    if hook:
        try:
            urllib.request.urlopen(urllib.request.Request(
                hook, data=json.dumps({"content": f"**{title}**\n{msg}"}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "pp-edge/2.0"}), timeout=15)
            print("Sent Discord alert.")
        except Exception as e:
            print(f"Discord alert failed: {e}")

# ================================================================== output ===
def local_time(iso, fmt="%a %-I:%M %p"):
    t = parse_time(iso)
    return t.astimezone(LOCAL_TZ).strftime(fmt) if t else ""

def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sport", "player", "stat", "line", "pick", "fair_pct", "edge_pct", "books", "sharp",
                    "converted", "game", "start", "odds"])
        for r in rows:
            w.writerow([r["sport"], r["player"], r["stat"], r["line"], r["side"], round(r["fair"] * 100, 2),
                        round(r["edge"] * 100, 2), r["books"], int(r["sharp"]), r["converted"], r["game"],
                        r["start"], r["quotes"]])

# ---- app data (read by the Edge Finder web app) ----
def leg_json(l):
    return {"sport": l["sport"], "player": l["player"], "market": l["market"], "stat": l["stat"],
            "line": l["line"], "side": l["side"], "fair": round(l["fair"], 4), "game": l["game"],
            "start": l["start"], "books": l["books"], "sharp": bool(l["sharp"]), "exact": l["exact"],
            "injury": l.get("injury", ""), "team": l.get("team", ""), "tier": l.get("tier", "")}

def slip_json(s):
    return {"name": s["name"], "num": s["num"], "n": s["n"],
            "pay": {str(k): v for k, v in s["pay"].items()},
            "mult": round(s["mult"], 4), "cash": round(s["cash"], 4), "kelly": round(s["kelly"], 4),
            "dist": [round(x, 5) for x in s["dist"]], "corr": bool(s["corr"]),
            "stake_pct": round(s.get("stake_pct", 0.0), 4), "play": bool(s.get("play")),
            "legs": [leg_json(l) for l in s["legs"]]}

def write_json(path, rows, slips, plays, hist, now, entry, be, notes, days=60, max_props=150,
               alts=(), watch=(), tuning=None):
    """Everything the Edge Finder app imports: plays, slips, props, goblin/demon lines,
    the watch list, tuning results and recent history."""
    cutoff = (now - timedelta(days=days)).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    data = {
        "app": "edge-finder", "version": 2,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry": entry, "breakeven": round(be, 5), "notes": notes,
        "payouts": {k: {"n": n, "pay": {str(h): m for h, m in pay.items()}} for k, (n, pay) in ENTRIES.items()},
        "plays": [slip_json(s) for s in plays],
        "slips": [slip_json(s) for ss in slips.values() for s in ss if not s.get("play")],
        "props": [{**leg_json(r), "edge": round(r["edge"], 4)} for r in rows[:max_props]],
        "alts": [leg_json(r) for r in list(alts)[:80]],
        "watch": list(watch),
        "tuning": tuning or {},
        "history": [{k: h.get(k, "") for k in HIST_FIELDS} for h in hist if h.get("date", "") >= cutoff],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))

CSS = """
:root{--bg:#f3f5f8;--panel:#fff;--line:#d9dee7;--ink:#131822;--muted:#5d6676;--accent:#2a55c0;--good:#17784a;--gbg:#e3f4ea;--warn:#9a6411;--wbg:#fbf0dc;--bad:#b0291d;--bbg:#fbe6e3}
@media (prefers-color-scheme:dark){:root{--bg:#0f131a;--panel:#171c26;--line:#2a3242;--ink:#e7ebf2;--muted:#98a2b4;--accent:#7c9cf0;--good:#5fd197;--gbg:#16301f;--warn:#e8b45c;--wbg:#33260f;--bad:#f08478;--bbg:#3a1a17;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font:700 2rem/1 "Arial Narrow",system-ui;margin:0 0 6px}
h2{font:700 1.4rem/1.1 "Arial Narrow",system-ui;margin:28px 0 10px}
h3{font:700 1.15rem/1.1 "Arial Narrow",system-ui;margin:20px 0 0}
.meta{color:var(--muted);margin:0 0 12px}.muted{color:var(--muted)}.small{font-size:.82rem}
.box{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.pos{color:var(--good)}.neg{color:var(--bad)}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.wide{min-width:760px}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.r{text-align:right;font-variant-numeric:tabular-nums}small{display:block;color:var(--muted)}
.side{font-weight:700}.MORE{color:var(--good)}.LESS{color:var(--accent)}
.chip{font:600 .8rem ui-monospace,monospace;padding:2px 8px;border-radius:99px;white-space:nowrap}
.good{background:var(--gbg);color:var(--good)}.warn{background:var(--wbg);color:var(--warn)}.bad{background:var(--bbg);color:var(--bad)}
.tag{white-space:nowrap;font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--warn);margin-left:6px}
.odds{font:.75rem ui-monospace,monospace;color:var(--muted);max-width:360px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-top:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;min-width:0}
.card.dim{opacity:.6}.card.play{border-color:var(--good);box-shadow:inset 0 0 0 1px var(--good)}
.ch{display:flex;justify-content:space-between;align-items:center;gap:8px}
.card ol{margin:8px 0 0;padding-left:20px}.card li{margin:3px 0}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.foot{color:var(--muted);font-size:.82rem;margin-top:20px;max-width:80ch}
"""

def pct(x, d=1):
    return "–" if x is None else f"{x * 100:.{d}f}%"

FAVICON = ("data:image/svg+xml," + urllib.parse.quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#10161F"/>'
    '<polyline points="96,360 196,330 276,348 380,199" fill="none" stroke="#E8B23A" stroke-width="58" stroke-linecap="round" stroke-linejoin="round"/>'
    '<polygon points="416,148 413,244 327,184" fill="#E8B23A" stroke="#E8B23A" stroke-width="28" stroke-linejoin="round"/>'
    '<polyline points="96,360 196,330 276,348 380,199" fill="none" stroke="#34D27B" stroke-width="40" stroke-linecap="round" stroke-linejoin="round"/>'
    '<polygon points="416,148 413,244 327,184" fill="#34D27B" stroke="#34D27B" stroke-width="10" stroke-linejoin="round"/></svg>'))

def inj_tag(status):
    return f"<span class='tag'>{html.escape(status)}</span>" if status else ""

def leg_li(l):
    tags = ("<span class='tag'>est. line</span>" if l["exact"] == 0 else "") + \
           ("" if l["sharp"] else "<span class='tag'>no sharp</span>") + inj_tag(l.get("injury"))
    return (f"<li><span class='side {l['side']}'>{l['side']}</span> {html.escape(l['player'])} "
            f"<span class='muted'>{html.escape(l['stat'])} {l['line']:g} · {l['fair'] * 100:.1f}%</span>{tags}</li>")

def slip_card(s):
    n, d, pay = s["n"], s["dist"], s["pay"]
    tiers = " · ".join(f"{h}/{n} hit {d[h] * 100:.0f}% ({pay[h]:g}x)" for h in sorted(pay, reverse=True) if pay[h] > 0)
    stake = ""
    if s.get("play"):
        amt = f"${s['stake']:.2f} · " if s.get("stake") is not None else ""
        stake = f"<br><b class='pos'>Play: {amt}{s['stake_pct'] * 100:.1f}% of bankroll</b>"
    corr = "<br>Same-game correlation included" if s["corr"] else ""
    cls = "play" if s.get("play") else ("" if s["mult"] > 1 else "dim")
    return (f"<div class='card {cls}'><div class='ch'><b>{s['name']} #{s['num']}</b>"
            f"<span class='chip {'good' if s['mult'] > 1 else 'bad'}'>{(s['mult'] - 1) * 100:+.1f}%</span></div>"
            f"<ol>{''.join(leg_li(l) for l in s['legs'])}</ol>"
            f"<p class='muted small' style='margin:8px 0 0'>{tiers}<br>Stake back or more: {s['cash'] * 100:.0f}%"
            f"{corr}{stake}</p></div>")

def write_html(path, rows, slips, plays, goal, bankroll, track, recent, entry, be, min_edge, notes, window_end,
               json_name="", alts=(), watch=(), tuning=None):
    stamp = datetime.now(LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p %Z")
    pos = [s for ss in slips.values() for s in ss if s["mult"] > 1]
    best_ev = max(pos, key=lambda s: s["mult"], default=None)
    best_cash = max(pos, key=lambda s: (s["cash"], s["mult"]), default=None)
    goal_txt = "highest average return" if goal == "profit" else "winning most often"

    # Today's plays
    if plays:
        total_pct = sum(s["stake_pct"] for s in plays)
        total_amt = f" (${sum(s['stake'] for s in plays):.2f})" if bankroll else ""
        play_html = (f"<p class='meta'>Picked for {goal_txt}. Total stake {total_pct * 100:.1f}% of bankroll{total_amt}. "
                     "Stakes are a quarter of the Kelly amount, capped at 2% per slip and 8% per day.</p>"
                     "<div class='cards'>" + "".join(slip_card(s) for s in plays) + "</div>")
    else:
        play_html = "<p class='box'>No positive Flex slips this run. Sitting out is the right play.</p>"
    goals = ""
    if best_ev:
        goals = ("<div class='two' style='margin-top:12px'>"
                 f"<div class='box'><b>Highest average return</b><br>{best_ev['name']} #{best_ev['num']}: "
                 f"<b class='pos'>{(best_ev['mult'] - 1) * 100:+.1f}%</b>, stake back {best_ev['cash'] * 100:.0f}% of the time</div>"
                 f"<div class='box'><b>Wins most often</b><br>{best_cash['name']} #{best_cash['num']}: "
                 f"stake back <b>{best_cash['cash'] * 100:.0f}%</b> of the time, "
                 f"<b class='pos'>{(best_cash['mult'] - 1) * 100:+.1f}%</b> average return</div></div>")

    # All slips by size
    sections = []
    for name, ss in slips.items():
        if ss:
            n_pos = sum(s["mult"] > 1 for s in ss)
            sections.append(f"<h3>{name} <span class='muted small' style='display:inline;font-weight:400'>"
                            f"{n_pos} of {len(ss)} positive</span></h3><div class='cards'>"
                            + "".join(slip_card(s) for s in ss) + "</div>")
    slips_html = "".join(sections) or "<p class='meta'>Not enough playable legs for a Flex slip.</p>"

    # Props table
    def chip(e):
        c = "good" if e >= min_edge else "warn" if e >= 0 else "bad"
        return f"<span class='chip {c}'>{e * 100:+.1f}</span>"
    trs = "".join(
        f"<tr><td>{html.escape(r['sport'].upper())}</td><td><b>{html.escape(r['player'])}</b>"
        f"{inj_tag(r.get('injury'))}"
        f"<small>{html.escape(r['stat'])} {r['line']:g} · {html.escape(r['game'])} · {local_time(r['start'])}</small></td>"
        f"<td class='side {r['side']}'>{r['side']}</td><td class='r'>{r['books']}"
        f"{'' if r['sharp'] else '<small>no sharp</small>'}{'<small>' + str(r['converted']) + ' est.</small>' if r['converted'] else ''}</td>"
        f"<td class='r'>{r['fair'] * 100:.1f}%</td><td class='r'>{chip(r['edge'])}</td>"
        f"<td class='odds'>{html.escape(r['quotes'])}</td></tr>" for r in rows)

    # Track record
    def pts(x, d):
        return "–" if x is None else f"{x * 100:+.{d}f}"
    trows = "".join(
        f"<tr><td>{label}</td><td class='r'>{s['n']}</td><td class='r'>{pct(s['hits'] / s['n'] if s['n'] else None)}</td>"
        f"<td class='r'>{pct(s['exp'])}</td><td class='r'>{pts(s['clv'], 2)}</td>"
        f"<td class='r'>{pct(s['beat'], 0)}</td></tr>" for label, s in track)
    res_cls = {"hit": "good", "miss": "bad"}
    rrows = "".join(
        f"<tr><td>{html.escape(h['date'])}</td><td><b>{html.escape(h['player'])}</b><small>{html.escape(h['stat'])} "
        f"{h['line']}</small></td><td class='side {h['side']}'>{h['side']}</td><td class='r'>{h['actual'] or '–'}</td>"
        f"<td><span class='chip {res_cls.get(h['result'], 'warn')}'>{h['result']}</span></td>"
        f"<td class='r'>{pct(float(h['pick_fair']))}</td>"
        f"<td class='r'>{pts(float(h['clv']) if h['clv'] else None, 1)}</td></tr>"
        for h in recent)
    app_link = (f' · <a href="{html.escape(json_name)}" download>Download app data (JSON) for Edge Finder</a>'
                if json_name else "")

    watch_html = ""
    if watch:
        watch_html = ("<h2>Watch list</h2><p class='meta'>Legs in today's plays that changed since they were picked. "
                      "Check them in PrizePicks before lock and swap them out if needed.</p><div class='scroll'><table><thead>"
                      "<tr><th>Prop</th><th>Pick</th><th>What changed</th></tr></thead><tbody>" + "".join(
                          f"<tr><td><b>{html.escape(w['player'])}</b><small>{html.escape(w['stat'])} {w['line']} · "
                          f"{local_time(w['start'])}</small></td><td class='side {w['side']}'>{w['side']}</td>"
                          f"<td>{html.escape('; '.join(w['why']))}</td></tr>" for w in watch) + "</tbody></table></div>")
    alt_html = ""
    if alts:
        alt_html = ("<h2>Goblins and demons</h2><p class='meta'>Alternate PrizePicks lines, MORE only. Their payouts differ from "
                    "standard picks and change with your entry, so compare each fair % with the break-even for the payout "
                    "PrizePicks shows you (the app's EV calculator does this).</p><div class='scroll'><table class='wide'><thead>"
                    "<tr><th>Type</th><th>Prop</th><th class='r'>Fair MORE</th><th class='r'>Books</th></tr></thead><tbody>" + "".join(
                        f"<tr><td><span class='chip {'good' if a['tier'] == 'goblin' else 'warn'}'>{a['tier']}</span></td>"
                        f"<td><b>{html.escape(a['player'])}</b><small>{html.escape(a['stat'])} {a['line']:g} · {html.escape(a['game'])} · "
                        f"{local_time(a['start'])}</small></td><td class='r'>{a['fair'] * 100:.1f}%</td><td class='r'>{a['books']}</td></tr>"
                        for a in alts[:40]) + "</tbody></table></div>")
    tune_txt = ""
    if tuning:
        if tuning.get("applied"):
            tune_txt = (f" Discounts tuned from {tuning['n']} of your graded legs: no-sharp legs keep "
                        f"{tuning['no_sharp'] * 100:.0f}% of their edge, estimated lines {tuning['est_only'] * 100:.0f}%.")
        else:
            tune_txt = (f" Discounts use the defaults until 150 graded legs with full detail are logged "
                        f"({tuning.get('n', 0)} so far).")

    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>PrizePicks Edge Report</title>
<link rel="icon" href="{FAVICON}"><link rel="apple-touch-icon" href="apple-touch-icon.png"><style>{CSS}</style></head><body><div class="wrap">
<h1>PrizePicks Edge Report</h1>
<p class="meta">Updated {stamp} · games starting before {window_end} · {len(rows)} priced props ·
{entry} break-even {be * 100:.2f}% per leg</p>
{f'<p class="meta">{html.escape(notes + tune_txt)}</p>' if notes or tune_txt else ''}
{watch_html}

<h2>Today's plays</h2>
{play_html}
{goals}
{alt_html}

<h2>Track record</h2>
<p class="meta">Hit rate should land near the expected rate if the fair odds are right. Closing line value (CLV) is the
earliest sign of a real edge: if your picks keep getting more likely by game time, the books moved toward you.</p>
<div class="scroll"><table><thead><tr><th>Group</th><th class="r">Graded</th><th class="r">Hit rate</th>
<th class="r">Expected</th><th class="r">Avg CLV (pts)</th><th class="r">Beat close</th></tr></thead><tbody>
{trows or "<tr><td colspan=6>No picks logged yet.</td></tr>"}</tbody></table></div>
{('<h3>Recent results</h3><div class="scroll" style="margin-top:10px"><table class="wide"><thead><tr><th>Date</th><th>Prop</th><th>Pick</th><th class="r">Actual</th><th>Result</th><th class="r">Fair</th><th class="r">CLV</th></tr></thead><tbody>' + rrows + '</tbody></table></div>') if rrows else ''}
<p class="meta small" style="margin-top:8px"><a href="picks.csv">Download full pick history (CSV)</a>{app_link}</p>

<h2>All Flex slips</h2>
{slips_html}

<h2>All props</h2>
<div class="scroll"><table class="wide"><thead><tr><th>Sport</th><th>Prop</th><th>Pick</th><th class="r">Books</th>
<th class="r">Fair</th><th class="r">Edge</th><th>Book line and odds (over/under)</th></tr></thead>
<tbody>{trs or "<tr><td colspan=7>No PrizePicks lines could be priced this run. Props often post later in the day.</td></tr>"}</tbody></table></div>

<p class="foot">Fair % is the vig-free win chance implied by sportsbook prices, leaning on sharp books. "est. line" means
no book posted the exact PrizePicks number, so the price was shifted from a nearby line with a stat model. "no sharp"
legs are shrunk toward 50%, and so are legs priced only from estimated lines. Correlation figures are rough estimates. Positive slips still lose often; the edge shows up
over many entries. Lines move, so recheck each line in the app before entering, and swap in another leg if a line changed
or PrizePicks blocks a combo. Problem gambling help: 1-800-GAMBLER.</p>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)

# ==================================================================== main ===
def main():
    global NO_SHARP_SHRINK, EST_ONLY_SHRINK
    ap = argparse.ArgumentParser(description="Rank today's PrizePicks lines and build Flex plays.")
    ap.add_argument("--sports", nargs="+", default=["nba"], choices=SPORTS.keys())
    ap.add_argument("--entry", default="5-Pick Flex", choices=ENTRIES.keys(), help="entry used for the edge column")
    ap.add_argument("--method", default="power", choices=["power", "mult"])
    ap.add_argument("--min-books", type=int, default=2)
    ap.add_argument("--min-edge", type=float, default=1.0, help="edge in points to flag green and log")
    ap.add_argument("--hours", type=float, default=24, help="games starting within this many hours")
    ap.add_argument("--all-markets", action="store_true", help="more stat types (uses more credits)")
    ap.add_argument("--alt-lines", action="store_true",
                    help="also price PrizePicks goblins and demons (about doubles credit use)")
    ap.add_argument("--slips", type=int, default=5, help="Flex slips per size")
    ap.add_argument("--goal", default="profit", choices=["profit", "consistency"],
                    help="profit = highest average return; consistency = win more often")
    ap.add_argument("--max-plays", type=int, default=5)
    ap.add_argument("--bankroll", type=float, default=float(os.environ.get("BANKROLL") or 0))
    ap.add_argument("--history", default="history/picks.csv")
    ap.add_argument("--history-days", type=int, default=60, help="days of history in the app data file")
    ap.add_argument("--no-grade", action="store_true")
    ap.add_argument("--no-injuries", action="store_true", help="skip ESPN injury reports")
    ap.add_argument("--no-rosters", action="store_true", help="skip roster lookups for teammate correlation")
    ap.add_argument("--no-tune", action="store_true", help="always use the default discounts")
    ap.add_argument("--notify-always", action="store_true", help="send an alert even with no plays")
    ap.add_argument("--report-url", default=os.environ.get("REPORT_URL", ""))
    ap.add_argument("--key", default=os.environ.get("ODDS_API_KEY"))
    ap.add_argument("--out", default="edge_report")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if not a.key:
        sys.exit("No Odds API key. Get one at https://the-odds-api.com and set ODDS_API_KEY.")
    now = datetime.now(timezone.utc)
    n, pay = ENTRIES[a.entry]
    be = breakeven(n, pay)
    min_edge = a.min_edge / 100
    if np is None:
        print("numpy not installed: same-game correlation is off.")

    # Tune the discounts from past results before pricing anything.
    hist = load_history(a.history)
    tuning = tune_shrinks(hist)
    if tuning["applied"] and not a.no_tune:
        NO_SHARP_SHRINK, EST_ONLY_SHRINK = tuning["no_sharp"], tuning["est_only"]
        print(f"Tuned discounts from {tuning['n']} legs: no sharp {NO_SHARP_SHRINK}, est. line {EST_ONLY_SHRINK}")
    elif a.no_tune:
        tuning = {**tuning, "applied": False}

    rows, notes, left, quotes_by_sport, drops = [], [], None, {}, {}
    pp_now, injuries_by_sport = {}, {}
    for sport in a.sports:
        sport_key, core, extra = SPORTS[sport]
        markets = core + (extra if a.all_markets else [])
        if a.alt_lines:
            markets = markets + [m + "_alternate" for m in core]
        print(f"[{sport.upper()}] fetching {len(markets)} markets...", end=" ", flush=True)
        try:
            pp, quotes, rem, n_games = fetch_sport(sport_key, markets, a.key, a.hours)
        except urllib.error.HTTPError as e:
            msg = {401: "the API key was rejected", 429: "out of API credits or rate limited"}.get(e.code, f"HTTP {e.code}")
            print(f"failed: {msg}")
            notes.append(f"{sport.upper()}: {msg}.")
            continue
        except urllib.error.URLError as e:
            print(f"failed: {e.reason}")
            notes.append(f"{sport.upper()}: couldn't reach The Odds API.")
            continue
        left = rem or left
        quotes_by_sport[sport] = quotes
        print(f"{n_games} games, {len(pp)} PrizePicks lines")
        if n_games and not pp:
            notes.append(f"{sport.upper()}: no PrizePicks lines posted yet.")
        for p in pp:
            if not p.get("tier"):
                pp_now.setdefault((sport, p["pkey"][0], p["market"]), set()).add(p["line"])
        injuries = {} if a.no_injuries else fetch_injuries(sport)
        if not a.no_injuries:
            injuries_by_sport[sport] = injuries
            print(f"   {len(injuries)} players on the {sport.upper()} injury report")
        teams = {}
        if not a.no_rosters and pp:
            teams = fetch_rosters(sport, {p["home"] for p in pp} | {p["away"] for p in pp})
            print(f"   rosters: {len(teams)} players matched to teams")
        rows += build_rows(sport, pp, quotes, a.method, be, a.min_books, drops, injuries, teams)

    alts = sorted([r for r in rows if r.get("tier")], key=lambda r: r["fair"], reverse=True)
    rows = sorted([r for r in rows if not r.get("tier")], key=lambda r: r["edge"], reverse=True)
    if drops:
        notes.append("Skipped: " + ", ".join(f"{v} {k}" for k, v in drops.items()) + ".")
    if left is not None:
        notes.append(f"Odds API credits left: {left}.")

    slips = build_slips(rows, per_size=a.slips)
    plays = choose_plays(slips, a.goal, a.max_plays, a.bankroll)

    # tracking
    n_close = update_closing(hist, quotes_by_sport, a.method, now, pp_now, injuries_by_sport)
    play_keys = {(norm_name(l["player"]), l["market"], l["line"], l["side"]) for s in plays for l in s["legs"]}
    n_new = log_picks(hist, rows, play_keys, min_edge, now)
    n_graded = 0 if a.no_grade else grade_history(hist, now)
    save_history(a.history, hist)
    watch = watch_list(hist, now)
    print(f"History: {n_new} new picks, {n_close} closing prices updated, {n_graded} graded, {len(watch)} to watch.")
    track = track_summary(hist, now)
    recent = sorted([h for h in hist if h.get("result") in ("hit", "miss", "push", "void")],
                    key=lambda h: h["start"], reverse=True)[:30]

    print(f"\n{'PICK':<5} {'EDGE':>6} {'FAIR':>6}  PROP")
    for r in rows[:20]:
        print(f"{r['side']:<5} {r['edge'] * 100:+6.1f} {r['fair'] * 100:5.1f}%  {r['player']} {r['stat']} "
              f"{r['line']:g} ({r['books']} books{', est.' if r['exact'] == 0 else ''}{'' if r['sharp'] else ', no sharp'}"
              f"{', ' + r['injury'] if r.get('injury') else ''})")
    print("\nTODAY'S PLAYS" if plays else "\nNo positive Flex slips this run.")
    for s in plays:
        amt = f" ${s['stake']:.2f}" if s.get("stake") is not None else ""
        print(f"{s['name']} #{s['num']}: {(s['mult'] - 1) * 100:+.1f}% avg return, "
              f"stake back {s['cash'] * 100:.0f}%, stake {s['stake_pct'] * 100:.1f}%{amt}")
        for l in s["legs"]:
            print(f"    {l['side']:<4} {l['player']} {l['stat']} {l['line']:g} ({l['fair'] * 100:.1f}%)")
    for w in watch:
        print(f"WATCH {w['player']} {w['side']} {w['line']} {w['stat']}: {', '.join(w['why'])}")

    out_dir = os.path.dirname(a.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    window_end = (now + timedelta(hours=a.hours)).astimezone(LOCAL_TZ).strftime("%a %-I:%M %p")
    write_csv(rows, a.out + ".csv")
    write_json(a.out + ".json", rows, slips, plays, hist, now, a.entry, be, " ".join(notes),
               days=a.history_days, alts=alts, watch=watch, tuning=tuning)
    write_html(a.out + ".html", rows, slips, plays, a.goal, a.bankroll, track, recent,
               a.entry, be, min_edge, " ".join(notes), window_end, json_name=os.path.basename(a.out) + ".json",
               alts=alts, watch=watch, tuning=tuning)
    print(f"\nSaved {a.out}.html, {a.out}.csv and {a.out}.json")
    notify(plays, watch, a.report_url, a.notify_always)
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(a.out + ".html"))

if __name__ == "__main__":
    main()
