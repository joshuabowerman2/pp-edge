#!/usr/bin/env python3
"""
PrizePicks Edge Finder (auto)

Pulls today's PrizePicks lines and the same player props from sharp and US
sportsbooks (all through The Odds API), removes the vig, and ranks every pick by
edge over the break-even rate for your entry type. Writes an HTML report and CSV.

Runs daily on GitHub Actions (see .github/workflows/daily.yml), or by hand:
  ODDS_API_KEY=yourkey python prizepicks_edge.py --sports nba nhl
"""
import argparse, csv, html, json, os, sys, time, unicodedata, re, webbrowser
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- settings ---
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

# PrizePicks plus 9 sportsbooks = 10 books, which The Odds API bills as one region.
BOOKS = ["prizepicks", "pinnacle", "fanduel", "draftkings", "betmgm",
         "williamhill_us", "espnbet", "betrivers", "betonlineag", "bovada"]
# How much to trust each book when averaging. Sharper books count more.
BOOK_WEIGHTS = {"pinnacle": 3, "betonlineag": 2, "fanduel": 1.5, "draftkings": 1.2}

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

ODDS_BASE = "https://api.the-odds-api.com/v4"
LOCAL_TZ = ZoneInfo(os.environ.get("REPORT_TZ", "America/Chicago"))

# ------------------------------------------------------------------- math ---
def american_to_prob(a):
    a = float(a)
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)

def devig_power(p_over, p_under):
    """Fair over probability, power method: find k with p_o^k + p_u^k = 1."""
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

def slip_multiplier(ps, pay):
    return sum(v * pay.get(k, 0) for k, v in enumerate(hit_dist(ps)))

def breakeven(n, pay):
    lo, hi = 0.3, 0.95
    for _ in range(60):
        m = (lo + hi) / 2
        if slip_multiplier([m] * n, pay) < 1: lo = m
        else: hi = m
    return (lo + hi) / 2

# ----------------------------------------------------------------- fetching ---
def norm_name(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[.\-'’]", " ", s.lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pp-edge/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()), dict(r.headers)

def fetch_sport(sport_key, markets, api_key, hours):
    """Returns (pp_lines, book_quotes, credits_left, n_games)."""
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
        game = f"{ev.get('away_team', '')} @ {ev.get('home_team', '')}"
        start = ev.get("commence_time", "")
        for bk in data.get("bookmakers", []):
            for mk in bk.get("markets", []):
                sides = {}
                for o in mk.get("outcomes", []):
                    if o.get("point") is None or not o.get("description"):
                        continue
                    key = (norm_name(o["description"]), mk["key"], float(o["point"]))
                    s = sides.setdefault(key, {"player": o["description"]})
                    s[o["name"].lower()] = o["price"]
                for key, s in sides.items():
                    if bk["key"] == "prizepicks":
                        pp.append({"key": key, "player": s["player"], "market": key[1], "line": key[2],
                                   "game": game, "start": start})
                    elif "over" in s and "under" in s:
                        quotes.setdefault(key, []).append((bk["key"], s["over"], s["under"]))
        time.sleep(0.2)
    return pp, quotes, left, len(events)

# ------------------------------------------------------------------ ranking ---
def build_rows(pp_lines, quotes, method, be, min_books):
    devig = devig_power if method == "power" else devig_mult
    rows, seen = [], set()
    for p in pp_lines:
        if p["key"] in seen:
            continue
        seen.add(p["key"])
        q = quotes.get(p["key"], [])
        if len(q) < min_books:
            continue
        sw = s = 0.0
        for book, o, u in q:
            w = BOOK_WEIGHTS.get(book, 1)
            s += w * devig(american_to_prob(o), american_to_prob(u)); sw += w
        fo = s / sw
        fp = max(fo, 1 - fo)
        rows.append({**p, "stat": LABELS.get(p["market"], p["market"]),
                     "side": "MORE" if fo >= 0.5 else "LESS", "fair": fp, "edge": fp - be,
                     "books": len(q), "quotes": ", ".join(f"{b} {o:+d}/{u:+d}" for b, o, u in q)})
    rows.sort(key=lambda r: r["edge"], reverse=True)
    return rows

# -------------------------------------------------------------- flex slips ---
FLEX = ["3-Pick Flex", "4-Pick Flex", "5-Pick Flex", "6-Pick Flex"]

def build_slips(rows, pool_size=18, per_size=5, max_per_game=2, max_reuse=2):
    """Best Flex slips by expected return, for each size.
    Rules: one leg per player, at most `max_per_game` legs from one game
    (legs in the same game move together, which hurts a Flex), each leg used
    in at most `max_reuse` slips, and slips of one size share at most half their legs."""
    from itertools import combinations
    pool = sorted([r for r in rows if r["fair"] > 0.5], key=lambda r: r["fair"], reverse=True)[:pool_size]
    out = {}
    for name in FLEX:
        n, pay = ENTRIES[name]
        if len(pool) < n:
            continue
        cands = []
        for combo in combinations(range(len(pool)), n):
            legs = [pool[i] for i in combo]
            if len({norm_name(l["player"]) for l in legs}) < n:
                continue
            games = {}
            for l in legs:
                games[l["game"]] = games.get(l["game"], 0) + 1
            if max(games.values()) > max_per_game:
                continue
            ps = [l["fair"] for l in legs]
            cands.append((slip_multiplier(ps, pay), combo, hit_dist(ps)))
        cands.sort(key=lambda c: c[0], reverse=True)
        chosen, used = [], {}
        for m, combo, dist in cands:
            if any(used.get(i, 0) >= max_reuse for i in combo):
                continue
            if any(len(set(combo) & set(c[1])) > n // 2 for c in chosen):
                continue
            chosen.append((m, combo, dist))
            for i in combo:
                used[i] = used.get(i, 0) + 1
            if len(chosen) == per_size:
                break
        out[name] = [{"mult": m, "legs": [pool[i] for i in combo], "dist": dist, "pay": pay, "n": n}
                     for m, combo, dist in chosen]
    return out

def slips_text(slips):
    lines = []
    for name, ss in slips.items():
        for k, s in enumerate(ss, 1):
            lines.append(f"{name} #{k}: expected return {(s['mult'] - 1) * 100:+.1f}%, "
                         f"all hit {s['dist'][s['n']] * 100:.1f}%")
            for l in s["legs"]:
                lines.append(f"    {l['side']:<4} {l['player']} {l['stat']} {l['line']:g}  ({l['fair'] * 100:.1f}%)")
    return "\n".join(lines)

def slips_html(slips):
    if not any(slips.values()):
        return "<p class='meta'>Not enough playable legs for a Flex slip this run.</p>"
    best = max(((s["mult"], name) for name, ss in slips.items() for s in ss), default=(0, ""))
    head = (f"<p class='slip'>Best Flex size today: <b>{best[1]}</b> at "
            f"<b class='{'pos' if best[0] >= 1 else 'neg'}'>{(best[0] - 1) * 100:+.1f}%</b> expected return. "
            "Only play slips marked positive. A flat stake of about 1% of your bankroll per slip keeps a bad week survivable.</p>")
    sections = []
    for name, ss in slips.items():
        if not ss:
            continue
        cards = []
        for k, s in enumerate(ss, 1):
            pos = s["mult"] >= 1
            n = s["n"]
            paid = [h for h in sorted(s["pay"], reverse=True) if s["pay"][h] > 0]
            odds = " · ".join(f"{h}/{n} hit {s['dist'][h] * 100:.0f}% ({s['pay'][h]:g}x)" for h in paid)
            back = sum(s["dist"][h] for h in s["pay"] if s["pay"][h] >= 1)
            legs = "".join(
                f"<li><span class='side {l['side']}'>{l['side']}</span> {html.escape(l['player'])} "
                f"<span class='muted'>{html.escape(l['stat'])} {l['line']:g} · {l['fair'] * 100:.1f}%</span></li>"
                for l in s["legs"])
            cards.append(
                f"<div class='card{'' if pos else ' dim'}'><div class='ch'><b>{name} #{k}</b>"
                f"<span class='chip {'good' if pos else 'bad'}'>{(s['mult'] - 1) * 100:+.1f}%</span></div>"
                f"<ol>{legs}</ol><p class='muted small'>{odds}<br>Stake back or more: {back * 100:.0f}% of the time</p></div>")
        n_pos = sum(1 for s in ss if s["mult"] >= 1)
        sections.append(f"<h3>{name} <span class='muted small'>{n_pos} of {len(ss)} positive</span></h3>"
                        "<div class='cards'>" + "".join(cards) + "</div>")
    return head + "".join(sections)

# ------------------------------------------------------------------- output ---
def local_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ).strftime("%a %-I:%M %p")
    except Exception:
        return ""

def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sport", "player", "stat", "line", "pick", "fair_pct", "edge_pct", "books", "game", "odds"])
        for r in rows:
            w.writerow([r["sport"], r["player"], r["stat"], r["line"], r["side"], round(r["fair"] * 100, 2),
                        round(r["edge"] * 100, 2), r["books"], r["game"], r["quotes"]])

def write_html(rows, path, entry, be, n, pay, min_edge, notes, slips):
    good = [r for r in rows if r["edge"] >= min_edge]
    slip = slips_html(slips)
    def chip(e):
        c = "good" if e >= min_edge else "warn" if e >= 0 else "bad"
        return f"<span class='chip {c}'>{e * 100:+.1f}</span>"
    trs = "\n".join(
        f"<tr><td>{html.escape(r['sport'].upper())}</td><td><b>{html.escape(r['player'])}</b>"
        f"<small>{html.escape(r['stat'])} {r['line']:g} · {html.escape(r['game'])} · {local_time(r['start'])}</small></td>"
        f"<td class='side {r['side']}'>{r['side']}</td><td class='r'>{r['books']}</td>"
        f"<td class='r'>{r['fair'] * 100:.1f}%</td><td class='r'>{chip(r['edge'])}</td>"
        f"<td class='odds'>{html.escape(r['quotes'])}</td></tr>" for r in rows)
    stamp = datetime.now(LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p %Z")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>PrizePicks Edge Report</title><style>
:root{{--bg:#f3f5f8;--panel:#fff;--line:#d9dee7;--ink:#131822;--muted:#5d6676;--accent:#2a55c0;--good:#17784a;--gbg:#e3f4ea;--warn:#9a6411;--wbg:#fbf0dc;--bad:#b0291d;--bbg:#fbe6e3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f131a;--panel:#171c26;--line:#2a3242;--ink:#e7ebf2;--muted:#98a2b4;--accent:#7c9cf0;--good:#5fd197;--gbg:#16301f;--warn:#e8b45c;--wbg:#33260f;--bad:#f08478;--bbg:#3a1a17;color-scheme:dark}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px 16px}}h1{{font:700 2rem/1 "Arial Narrow",system-ui;margin:0 0 6px}}
.meta{{color:var(--muted);margin:0 0 14px}}.slip{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}}
.pos{{color:var(--good)}}.neg{{color:var(--bad)}}.scroll{{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}}
table{{border-collapse:collapse;width:100%;min-width:760px}}th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}}.r{{text-align:right;font-variant-numeric:tabular-nums}}
small{{display:block;color:var(--muted)}}.side{{font-weight:700}}.MORE{{color:var(--good)}}.LESS{{color:var(--accent)}}
.chip{{font:600 .8rem ui-monospace,monospace;padding:2px 8px;border-radius:99px}}.good{{background:var(--gbg);color:var(--good)}}.warn{{background:var(--wbg);color:var(--warn)}}.bad{{background:var(--bbg);color:var(--bad)}}
.odds{{font:.78rem ui-monospace,monospace;color:var(--muted);max-width:340px}}.foot{{color:var(--muted);font-size:.82rem;margin-top:16px}}
h2{{font:700 1.4rem/1.1 "Arial Narrow",system-ui;margin:24px 0 10px}}.muted{{color:var(--muted)}}.small{{font-size:.82rem;margin:8px 0 0}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-top:12px}}
h3{{font:700 1.15rem/1.1 "Arial Narrow",system-ui;margin:20px 0 0}}h3 .small{{font:400 .82rem system-ui;margin-left:6px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;min-width:0}}.card.dim{{opacity:.6}}
.ch{{display:flex;justify-content:space-between;align-items:center;gap:8px}}.card ol{{margin:8px 0 0;padding-left:20px}}.card li{{margin:3px 0}}
</style></head><body><div class="wrap">
<h1>PrizePicks Edge Report</h1>
<p class="meta">Updated {stamp} · {len(rows)} matched props · {entry} break-even {be * 100:.2f}% per leg, {len(good)} legs at +{min_edge * 100:g} edge or better</p>
{f'<p class="meta">{html.escape(notes)}</p>' if notes else ''}
<h2>Flex plays</h2>
{slip}
<h2>All props</h2>
<div class="scroll"><table><thead><tr><th>Sport</th><th>Prop</th><th>Pick</th><th class="r">Books</th><th class="r">Fair</th><th class="r">Edge</th><th>Book odds (over/under)</th></tr></thead>
<tbody>{trs or "<tr><td colspan=7>No PrizePicks lines matched sportsbook lines this run. Props often post later in the day.</td></tr>"}</tbody></table></div>
<p class="foot">Fair % is the vig-free win chance implied by sportsbook prices. It is an estimate, and positive-edge slips still lose often. Lines move after this report runs, so recheck each line in the app before entering; if a line changed or a Less isn't offered, swap in the next leg from the list. Slips allow at most two legs from one game, because legs in the same game tend to win or lose together. Problem gambling help: 1-800-GAMBLER.</p>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)

# --------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description="Rank today's PrizePicks lines by de-vigged sportsbook edge.")
    ap.add_argument("--sports", nargs="+", default=["nba"], choices=SPORTS.keys())
    ap.add_argument("--entry", default="5-Pick Flex", choices=ENTRIES.keys())
    ap.add_argument("--method", default="power", choices=["power", "mult"])
    ap.add_argument("--min-books", type=int, default=2, help="minimum sportsbooks posting the same line")
    ap.add_argument("--min-edge", type=float, default=1.0, help="edge in percentage points to flag green")
    ap.add_argument("--hours", type=int, default=24, help="games starting within this many hours")
    ap.add_argument("--all-markets", action="store_true", help="more stat types (uses more credits)")
    ap.add_argument("--key", default=os.environ.get("ODDS_API_KEY"))
    ap.add_argument("--out", default="edge_report")
    ap.add_argument("--slips", type=int, default=5, help="Flex slips to build for each size (3, 4, 5, 6 pick)")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if not a.key:
        sys.exit("No Odds API key. Get one at https://the-odds-api.com and set ODDS_API_KEY.")
    n, pay = ENTRIES[a.entry]
    be = breakeven(n, pay)
    print(f"{a.entry}: each leg needs {be * 100:.2f}% to break even\n")

    rows, notes, left = [], [], None
    for sport in a.sports:
        sport_key, core, extra = SPORTS[sport]
        markets = core + (extra if a.all_markets else [])
        print(f"[{sport.upper()}] fetching {len(markets)} markets...", end=" ", flush=True)
        try:
            pp, quotes, rem, n_games = fetch_sport(sport_key, markets, a.key, a.hours)
        except urllib.error.HTTPError as e:
            msg = {401: "the API key was rejected", 429: "out of API credits or rate limited"}.get(e.code, f"HTTP {e.code}")
            print(f"failed: {msg}")
            notes.append(f"{sport.upper()}: {msg}.")
            continue
        left = rem or left
        print(f"{n_games} games, {len(pp)} PrizePicks lines")
        if n_games and not pp:
            notes.append(f"{sport.upper()}: no PrizePicks lines posted yet.")
        r = build_rows(pp, quotes, a.method, be, a.min_books)
        for x in r: x["sport"] = sport
        rows += r

    rows.sort(key=lambda r: r["edge"], reverse=True)
    if left is not None:
        notes.append(f"Odds API credits left: {left}.")
    min_edge = a.min_edge / 100

    print(f"\n{'PICK':<5} {'EDGE':>6} {'FAIR':>6}  PROP")
    for r in rows[:25]:
        flag = "*" if r["edge"] >= min_edge else " "
        print(f"{r['side']:<5} {r['edge'] * 100:+6.1f} {r['fair'] * 100:5.1f}% {flag} "
              f"{r['player']} {r['stat']} {r['line']:g} ({r['books']} books)")

    out_dir = os.path.dirname(a.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    write_csv(rows, a.out + ".csv")
    slips = build_slips(rows, per_size=a.slips)
    if any(slips.values()):
        print("\nFLEX PLAYS\n" + slips_text(slips))
    write_html(rows, a.out + ".html", a.entry, be, n, pay, min_edge, " ".join(notes), slips)
    print(f"\nSaved {a.out}.html and {a.out}.csv")
    for note in notes: print(note)
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(a.out + ".html"))

if __name__ == "__main__":
    main()
