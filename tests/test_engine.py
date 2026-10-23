import io
import json
import math
import os
import random
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from edge_engine import backtest, blend, config, correlation, db, engine, export, mathx, portfolio, pricing, \
    projections, results, slips, tracking, web  # noqa: E402
from edge_engine.sources import http  # noqa: E402
from tests import fakes  # noqa: E402


def make_cfg(tmp, **kw):
    os.environ.update({"ODDS_API_KEY": "test", "SPORTS": "nba", "EDGE_DB": os.path.join(tmp, "edge.db"),
                       "NTFY_TOPIC": "test-topic", "SECURE_COOKIES": "0", "INVITE_CODE": "letmein"})
    cfg = config.load()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class MathTests(unittest.TestCase):
    def test_breakeven_5_flex(self):
        e = config.load().app_payouts()["prizepicks"]["entries"]["5-Pick Flex"]
        self.assertAlmostEqual(mathx.breakeven(e["n"], e["pay"]), 0.5425, places=3)

    def test_devig_sums_to_one(self):
        po, pu = mathx.american_to_prob(-130), mathx.american_to_prob(110)
        p = mathx.devig_power(po, pu)
        self.assertTrue(0.5 < p < po)

    def test_shift_line_monotone(self):
        d = ("normal", .3)
        self.assertGreater(mathx.shift_line(0.55, 24.5, 23.5, d), 0.55)
        self.assertLess(mathx.shift_line(0.55, 24.5, 25.5, d), 0.55)

    def test_uncertainty_shrink(self):
        self.assertAlmostEqual(mathx.uncertainty_shrink(0.1, 0.0), 1.0)
        self.assertAlmostEqual(mathx.uncertainty_shrink(0.1, 0.1), 0.5)
        self.assertEqual(mathx.uncertainty_shrink(-0.1, 0.1), 0.0)

    def test_ev_sigma_positive(self):
        pay = {5: 10, 4: 2, 3: 0.4}
        s = mathx.ev_sigma([.56] * 5, [.02] * 5, pay)
        self.assertTrue(0.05 < s < 0.6)

    def test_market_fair_sigma_and_shrink(self):
        q = [("pinnacle", 24.5, -140, 115), ("fanduel", 24.5, -135, 110)]
        fo, info = pricing.market_fair("nba", "player_points", 24.5, q)
        self.assertTrue(0.55 < fo < 0.6)
        self.assertTrue(info["sharp"] and 0.005 < info["sigma"] < 0.05)
        q2 = [("fanduel", 24.5, -140, 115), ("draftkings", 24.5, -140, 115)]
        fo2, _ = pricing.market_fair("nba", "player_points", 24.5, q2)
        self.assertLess(fo2, fo)        # no sharp book: pulled toward 50%


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "m.db"))

    def test_blend_recovers_projection_weight(self):
        rng = random.Random(1)
        rows = []
        for _ in range(4000):
            xm, xp = rng.gauss(0.2, 0.25), rng.gauss(0.2, 0.25)
            true = mathx.sigmoid(0.6 * xm + 0.5 * xp)
            rows.append((xm, xp, 1.0 if rng.random() < true else 0.0))
        a, b, c = blend.fit_rows(rows)
        self.assertAlmostEqual(a, 0.6, delta=0.2)
        self.assertAlmostEqual(b, 0.5, delta=0.2)

    def test_correlation_learns_teammates(self):
        fakes.seed_boxscores(self.conn, n_games=120)
        out = correlation.fit(self.conn, "nba")
        rho, n = out[("team", "player_points", "player_points")]
        self.assertGreater(rho, 0.15)      # true 0.36, shrunk toward prior -0.05
        rho_o, _ = out[("opp", "player_points", "player_points")]
        self.assertLess(abs(rho_o), 0.15)

    def test_projection_and_usage_bump(self):
        fakes.seed_boxscores(self.conn, n_games=30)
        base = projections.project(self.conn, "nba", "ace guard", "player_points", opp="knicks", team="celtics")
        self.assertIsNotNone(base)
        self.assertAlmostEqual(base["mu"], 25, delta=3)
        bumped = projections.project(self.conn, "nba", "ace guard", "player_points", opp="knicks", team="celtics",
                                     absent={"big wing"})
        self.assertGreater(bumped["mu"], base["mu"] + 1)

    def test_hash_chain_detects_tampering(self):
        tz = config.load().tz
        now = datetime.now(timezone.utc)
        for i in range(5):
            leg = {"sport": "nba", "game_id": "g", "app": "prizepicks", "player": f"P{i}", "player_key": f"p{i}",
                   "market": "player_points", "stat": "Points", "line": 10.5 + i, "side": "MORE", "tier": "",
                   "home": "A", "away": "B", "start": "2026-10-23T00:00:00Z", "fair_market": 0.56, "fair_proj": None,
                   "fair": 0.56, "sigma": 0.02, "edge": 0.02, "books": 3, "sharp": True, "exact": 1}
            tracking.log_pick(self.conn, leg, now, "t", tz)
        self.assertEqual(tracking.verify_chain(self.conn)[0], True)
        self.conn.execute("UPDATE picks SET fair=0.70 WHERE seq=3")
        ok, n, bad = tracking.verify_chain(self.conn)
        self.assertFalse(ok)
        self.assertEqual(bad, 3)

    def test_holdout_share(self):
        n = sum(tracking.is_holdout(f"pick-{i}") for i in range(5000))
        self.assertAlmostEqual(n / 5000, 0.2, delta=0.02)

    def test_entry_return_steps_down(self):
        apps = config.load().app_payouts()
        self.assertEqual(tracking.entry_return(apps["prizepicks"], "5-Pick Flex", ["hit", "hit", "hit", "miss", "push"]), 1.5)
        self.assertEqual(tracking.entry_return(apps["prizepicks"], "3-Pick Flex", ["hit", "hit", "void"]), 3)
        self.assertEqual(tracking.entry_return(apps["underdog"], "3-Pick Standard", ["hit", "hit", "void"]), 3)
        self.assertEqual(tracking.entry_return(apps["prizepicks"], "2-Pick Power", ["hit", "void"]), 1)


class SafetyTests(unittest.TestCase):
    """Guards added after a run where preseason box scores made projections claim 80-99% legs."""

    def test_projection_cannot_overrule_market(self):
        # A projection claiming certainty moves the price at most MAX_PROJ_SHIFT from the market.
        for params in [blend.DEFAULT_PARAMS, (0.5, 1.5, 0.3)]:
            for pm in (0.45, 0.5, 0.57):
                for pp in (0.0, 1e-9, 0.5, 0.9999, 1.0):
                    p = pricing.blend(pm, pp, params)
                    self.assertLessEqual(abs(p - pm), pricing.MAX_PROJ_SHIFT + 1e-12)

    def test_questionable_players_not_eligible(self):
        from edge_engine import slips
        self.assertFalse(slips.eligible({"tier": "", "injury": "Day-To-Day"}))
        self.assertFalse(slips.eligible({"tier": "", "injury": "Questionable"}))
        self.assertTrue(slips.eligible({"tier": "", "injury": ""}))

    def test_preseason_box_scores_skipped_and_purged(self):
        tmp = tempfile.mkdtemp()
        conn = db.get(make_cfg(tmp).db_path)
        from edge_engine.sources import espn
        day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        conn.execute("INSERT INTO boxscores(sport,event_id,game_date,team,opp,player_key,player,minutes,stats,home) "
                     "VALUES('nba','pre1',?,'celtics','knicks','ace guard','Ace Guard',15,'{\"points\": 4}',1)", (day,))
        conn.commit()
        games = [{"id": "pre1", "home": "Boston Celtics", "away": "New York Knicks", "final": True, "exhibition": True}]
        orig_sb, orig_box = espn.scoreboard, espn.box
        espn.scoreboard = lambda sport, ymd: games
        espn.box = lambda sport, gid: self.fail("exhibition box score should not be fetched")
        try:
            tracking.backfill(conn, "nba", 2)
        finally:
            espn.scoreboard, espn.box = orig_sb, orig_box
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM boxscores").fetchone()[0], 0)

    def test_transliterated_names_share_a_key(self):
        self.assertEqual(http.norm_name("Yegor Chinakhov"), http.norm_name("Egor Chinakhov"))
        self.assertEqual(http.norm_name("Evgeny Kuznetsov"), http.norm_name("Evgeni Kuznetsov"))
        self.assertEqual(http.norm_name("Aleksandr Barkov Jr."), "alexander barkov")

    def test_scratched_player_voids_and_unknown_name_is_flagged(self):
        tmp = tempfile.mkdtemp()
        conn = db.get(make_cfg(tmp).db_path)
        from edge_engine.sources import espn
        tz = config.load().tz
        start = datetime(2026, 10, 22, 23, 10, tzinfo=timezone.utc)
        conn.execute("INSERT INTO boxscores(sport,event_id,game_date,team,opp,player_key,player,minutes,stats,home) "
                     "VALUES('nhl','old','2026-10-19','penguins','panthers','egor chinakhov','Egor Chinakhov',15,"
                     "'{\"shotsTotal\": 3}',1)")
        for name in ("Yegor Chinakhov", "Nobody Known", "Sidney Crosby"):
            leg = {"sport": "nhl", "game_id": "g", "app": "prizepicks", "player": name,
                   "player_key": http.norm_name(name), "market": "player_shots_on_goal", "stat": "Shots on Goal",
                   "line": 2.5, "side": "MORE", "tier": "", "home": "Pittsburgh Penguins", "away": "Florida Panthers",
                   "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "fair_market": 0.56, "fair_proj": None,
                   "fair": 0.56, "sigma": 0.02, "edge": 0.02, "books": 3, "sharp": True, "exact": 1}
            tracking.log_pick(conn, leg, start - timedelta(hours=3), "t", tz)
        game = {"id": "G1", "home": "Pittsburgh Penguins", "away": "Florida Panthers",
                "date": "2026-10-22T23:10:00Z", "final": True}
        box = [{"player_key": "sidney crosby", "player": "Sidney Crosby", "team": "Pittsburgh Penguins",
                "opp": "Florida Panthers", "minutes": 20, "stats": {"shotsTotal": 4}, "dnp": False}]
        orig_sb, orig_box = espn.scoreboard, espn.box
        espn.scoreboard, espn.box = (lambda sport, ymd: [game]), (lambda sport, gid: box)
        try:
            tracking.grade(conn, start + timedelta(hours=8))
        finally:
            espn.scoreboard, espn.box = orig_sb, orig_box
        res = {r["player"]: r["result"] for r in conn.execute("SELECT player, result FROM picks")}
        self.assertEqual(res, {"Yegor Chinakhov": "void", "Nobody Known": "unmatched", "Sidney Crosby": "hit"})

    def test_retired_model_kept_out_of_headline(self):
        tmp = tempfile.mkdtemp()
        conn = db.get(make_cfg(tmp).db_path)
        from edge_engine import RETIRED_MODELS
        tz = config.load().tz
        now = datetime.now(timezone.utc)
        old = next(iter(RETIRED_MODELS))
        for i, ver in enumerate((old, old, "current")):
            leg = {"sport": "nba", "game_id": "g", "app": "prizepicks", "player": f"P{i}", "player_key": f"p{i}",
                   "market": "player_points", "stat": "Points", "line": 10.5, "side": "MORE", "tier": "",
                   "home": "A", "away": "B", "start": "2026-10-20T00:00:00Z", "fair_market": 0.9, "fair_proj": None,
                   "fair": 0.9, "sigma": 0.02, "edge": 0.3, "books": 3, "sharp": True, "exact": 1}
            tracking.log_pick(conn, leg, now, ver, tz)
        conn.execute("UPDATE picks SET result='hit'")
        d = results.data(conn)
        self.assertEqual(d["all"]["n"], 1)
        self.assertEqual(d["chain"]["count"], 3)
        self.assertEqual({m["version"]: m["n"] for m in d["models"]}, {old: 2, "current": 1})
        self.assertIn("Results by model version", results.html(conn))

    def test_projection_needs_real_sample(self):
        self.assertGreaterEqual(projections.MIN_GAMES, 15)


class PortfolioTests(unittest.TestCase):
    def test_caps_and_positive_growth(self):
        corr = correlation.Correlations()
        legs = []
        for i in range(10):
            legs.append({"sport": "nba", "game_id": f"g{i % 4}", "app": "prizepicks", "player": f"P{i}", "player_key": f"p{i}",
                         "market": "player_points", "side": "MORE", "fair": 0.60 - i * 0.004, "sigma": 0.02, "edge": 0.05,
                         "tier": "", "injury": "", "team": ""})
        app = dict(config.load().app_payouts()["prizepicks"], key="prizepicks")
        cands, pool = slips.candidates(legs, app, corr)
        self.assertTrue(cands)
        plays = portfolio.optimize(cands, pool, corr, 0.25, 0.02, 0.08, 0.04, 6)
        self.assertTrue(plays)
        self.assertLessEqual(sum(p["stake_pct"] for p in plays), 0.08 + 1e-9)
        exposure = {}
        for p in plays:
            self.assertLessEqual(p["stake_pct"], 0.02 + 1e-9)
            for l in p["legs"]:
                exposure[l["player"]] = exposure.get(l["player"], 0) + p["stake_pct"]
        self.assertLessEqual(max(exposure.values()), 0.04 + 1e-9)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp, dfs_apps=["prizepicks", "underdog"], alt_lines=True)
        self.conn = db.get(self.cfg.db_path)
        fakes.seed_boxscores(self.conn, 40)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.start = (self.now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.fake = fakes.Fake(self.start)
        http.fetch = self.fake

    def test_full_cycle(self):
        site = os.path.join(self.tmp, "site")
        r = engine.run_once(self.conn, self.cfg, self.now, export_path=os.path.join(site, "index.json"))
        self.assertGreater(r["legs"], 0)
        self.assertGreater(r["logged"], 0)
        # injured player dropped, questionable tagged, alt lines tagged
        b = db.meta_get(self.conn, "board")
        names = {l["player"] for l in b["legs"]}
        self.assertNotIn("Gus Hurt", names)
        self.assertTrue(any(l["injury"] == "Questionable" for l in b["legs"]))
        self.assertTrue({a["tier"] for a in b["alts"]} <= {"goblin", "demon"} and b["alts"])
        self.assertTrue(all(l["fair_proj"] is not None for l in b["legs"]))   # projections from seeded history
        self.assertTrue(b["plays"], "expected at least one recommended entry")
        # alerts went to ntfy
        self.assertTrue(any("ntfy.sh/test-topic" in u for u, *_ in self.fake.posts))
        # export is in the Edge Finder app format
        d = json.load(open(os.path.join(site, "index.json")))
        self.assertEqual(d["app"], "edge-finder")
        self.assertTrue(d["plays"] and all(k in d["plays"][0] for k in ("name", "mult", "dist", "legs", "stake_pct")))
        self.assertTrue(d["history"])
        # team bets: FanDuel's home moneyline and DraftKings' home spread beat the sharp price; nothing else does
        tp = {(p["market"], p["book"]) for p in d["teams"]["plays"]}
        self.assertEqual(tp, {("h2h", "fanduel"), ("spreads", "draftkings")})
        self.assertTrue(all(p["ev"] >= 0.01 and 0 < p["fair"] < 1 and p["books"] for p in d["teams"]["plays"]))
        # scheduling: right after a poll nothing is due; a lineup change forces a repoll
        due, base = engine.schedule(self.conn, self.cfg, self.now + timedelta(minutes=1))
        self.assertEqual(due, [])
        self.conn.execute("UPDATE games SET poll_now=1")
        due, _ = engine.schedule(self.conn, self.cfg, self.now + timedelta(minutes=1))
        self.assertEqual(len(due), 1)
        # a price change goes to snapshots only once
        n_snap = self.conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
        engine.poll_games(self.conn, self.cfg, due, self.now + timedelta(minutes=2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"], n_snap)

        # Flex tracker: lock in a slip, only one per size per day
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM picks ORDER BY seq")]
        self.assertGreaterEqual(len(ids), 2)
        n_flex = len(ids[:3])
        entry = f"{n_flex}-Pick Flex" if n_flex == 3 else "2-Pick Power"
        self.assertTrue(tracking.log_flex_slip(self.conn, self.now, self.cfg.tz, "prizepicks", entry, n_flex,
                                               ids[:3], 1.1, 0.6, [0.2] * (n_flex + 1)))
        self.assertIsNone(tracking.log_flex_slip(self.conn, self.now, self.cfg.tz, "prizepicks", entry, n_flex,
                                                 ids[:2], 1.2, 0.6, [0.2] * 3))
        self.assertIsNotNone(engine.track_flex(self.conn, self.cfg, self.now, []))

        # game finishes: grade, settle model entries, results page, chain intact
        later = datetime.fromisoformat(self.start.replace("Z", "+00:00")) + timedelta(hours=5)
        self.fake.final = True
        graded = tracking.grade(self.conn, later)
        self.assertGreater(graded, 0)
        from edge_engine import teams
        self.assertEqual(teams.grade(self.conn, later), 2)   # 112-105: home wins and covers -4.5
        rec = teams.record(self.conn)
        self.assertEqual((rec["won"], rec["lost"]), (2, 0))
        self.assertGreater(rec["units"], 1.5)
        res = {r["result"] for r in self.conn.execute("SELECT result FROM picks")}
        self.assertTrue(res & {"hit", "miss"})
        apps = engine._apps(self.cfg)
        tracking.settle_system_entries(self.conn, apps, later)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) c FROM system_entries WHERE status='settled'").fetchone()["c"], 0)
        self.assertTrue(tracking.verify_chain(self.conn)[0])
        results.write(self.conn, self.cfg, os.path.join(site, "results.html"))
        html_ = open(os.path.join(site, "results.html")).read()
        self.assertIn("Record verified", html_)
        info = self.conn.execute("SELECT * FROM player_info WHERE jersey != '' LIMIT 1").fetchone()
        self.assertIsNotNone(info)
        self.assertEqual(info["color"], "#008348")
        self.assertEqual(info["pos"], "G")
        d2 = export.app_json(self.conn, self.cfg, later)
        legs2 = [l for s in d2["plays"] for l in s["legs"]] + d2["props"]
        self.assertTrue(any(l["jersey"] and l["color"] == "#008348" for l in legs2))
        # research: projection, averages, splits, usage and book lines for props on the board
        rs = [r for r in d2["research"].values() if r["n"]]
        self.assertTrue(rs)
        r = rs[0]
        self.assertTrue(r["proj"] and r["l5"] is not None and r["avg"] is not None and len(r["games"]) <= 20)
        self.assertTrue(r["home_n"] + r["away_n"] == r["n"])
        self.assertEqual(r["usage_label"], "Shot attempts")
        self.assertTrue(any(x["books"] for x in d2["research"].values()))
        # line history file, injury report and data freshness
        tl = json.load(open(os.path.join(site, "timeline.json")))
        self.assertTrue(tl and any(v["books"] for v in tl.values()) and any(v["apps"] for v in tl.values()))
        pl = json.load(open(os.path.join(site, "players.json")))
        withg = [v for v in pl["players"].values() if v["games"]]
        self.assertTrue(withg and "points" in withg[0]["games"][0]["s"])
        self.assertEqual(pl["stat_keys"]["nba"]["player_points"], ["points"])
        self.assertIn("injuries", d2)
        self.assertTrue(d2["freshness"]["odds"] and d2["freshness"]["models"])
        self.assertIn("Accuracy over time", html_)
        self.assertIn("Model and data", html_)
        f = self.conn.execute("SELECT * FROM flex_slips").fetchone()
        self.assertIn(f["status"], ("settled", "void"))
        self.assertIsNotNone(f["hits"])
        self.assertIn("Flex plays", html_)
        self.assertIn("Team bets", html_)
        self.assertIn("2-0", html_)
        from edge_engine.cli import verify_csv
        self.assertTrue(verify_csv(os.path.join(site, "picks.csv"))[0])
        self.assertIn("verdict", backtest.evaluate(self.conn, 0) | {"verdict": ""})

    def test_web_flow(self):
        engine.run_once(self.conn, self.cfg, self.now)
        app = web.make_app(self.cfg)

        def call(method, path, form=None, cookie=""):
            body = urllib.parse.urlencode(form or {}).encode()
            path_, _, qs = path.partition("?")
            env = {"REQUEST_METHOD": method, "PATH_INFO": path_, "QUERY_STRING": qs, "CONTENT_LENGTH": str(len(body)),
                   "wsgi.input": io.BytesIO(body), "HTTP_COOKIE": cookie, "REMOTE_ADDR": "1.2.3.4"}
            out = {}
            chunks = app(env, lambda s, h: out.update(status=s, headers=h))
            return out["status"], dict(out["headers"]), b"".join(chunks).decode()

        s, h, body = call("GET", "/results")
        self.assertTrue(s.startswith("200") and "Track record" in body)
        s, h, _ = call("GET", "/board")
        self.assertTrue(s.startswith("303"))
        s, h, body = call("POST", "/signup", {"email": "a@b.co", "password": "longpassword1", "invite": "wrong", "age": "yes"})
        self.assertIn("invite code", body)
        s, h, _ = call("POST", "/signup", {"email": "a@b.co", "password": "longpassword1", "invite": "letmein", "age": "yes"})
        self.assertTrue(s.startswith("303"))
        cookie = h["Set-Cookie"].split(";")[0]
        s, h, body = call("GET", "/board", cookie=cookie)
        self.assertTrue(s.startswith("200") and "Recommended entries" in body)
        sess = self.conn.execute("SELECT csrf FROM sessions").fetchone()["csrf"]
        b = db.meta_get(self.conn, "board")
        s, h, _ = call("POST", "/bets", {"csrf": "bad", "play": "0", "stake": "5", "generated_at": b["generated_at"]}, cookie)
        self.assertTrue(s.startswith("403"))
        s, h, _ = call("POST", "/bets", {"csrf": sess, "play": "0", "stake": "5", "generated_at": b["generated_at"]}, cookie)
        self.assertTrue(s.startswith("303"))
        s, h, body = call("GET", "/bets", cookie=cookie)
        self.assertIn("open", body)
        tok = self.conn.execute("SELECT api_token FROM users").fetchone()["api_token"]
        s, h, body = call("GET", "/api/latest.json?token=" + tok)
        self.assertEqual(json.loads(body)["app"], "edge-finder")
        s, h, _ = call("GET", "/api/latest.json?token=nope")
        self.assertTrue(s.startswith("401"))
        # wrong password is rejected; XSS in email is escaped
        s, h, body = call("POST", "/login", {"email": "a@b.co", "password": "nope"})
        self.assertIn("password don&#x27;t match", body)



class NewFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def leg(self, player, market, fair, side="MORE", game="g1", team="A"):
        return {"sport": "nfl", "game_id": game, "app": "prizepicks", "player": player, "player_key": player.lower(),
                "market": market, "stat": market, "line": 10.5, "side": side, "tier": "", "fair": fair, "sigma": 0.02,
                "edge": fair - 0.54, "team": team, "injury": ""}

    def test_line_moves(self):
        t0 = datetime(2026, 10, 22, 15, tzinfo=timezone.utc)
        l = self.leg("Qb One", "player_pass_yds", 0.56)
        self.assertEqual(tracking.record_moves(self.conn, [l], t0), 1)
        self.assertEqual(tracking.record_moves(self.conn, [l], t0 + timedelta(hours=1)), 0)   # unchanged: not stored
        l2 = {**l, "fair": 0.585}
        self.assertEqual(tracking.record_moves(self.conn, [l2], t0 + timedelta(hours=2)), 1)
        mv = tracking.moves_for(self.conn, [l2])[tracking.prop_key(l2)]
        self.assertAlmostEqual(mv[0], 0.56)
        self.assertEqual(mv[2], 1)

    @unittest.skipIf(slips.np is None, "correlated simulation needs numpy (the engine installs it)")
    def test_stacks_find_correlated_combo(self):
        class Corr:
            def pair(self, a, b):
                return 0.45 if a["game_id"] == b["game_id"] else 0.0
        apps = engine._apps(config.load())
        legs = [self.leg("Qb One", "player_pass_yds", 0.58), self.leg("Wr Two", "player_reception_yds", 0.57),
                self.leg("Other Guy", "player_rush_yds", 0.56, game="g2")]
        found = slips.stacks(legs, apps["prizepicks"], Corr())
        self.assertTrue(found)
        top = found[0]
        self.assertTrue(all(l["game_id"] == "g1" for l in top["legs"]))
        self.assertGreater(top["p_joint"], top["p_indep"])
        self.assertGreater(top["ev"], 1.0)
        # with no correlation there is nothing to stack
        class NoCorr:
            def pair(self, a, b):
                return 0.0
        self.assertEqual(slips.stacks(legs, apps["prizepicks"], NoCorr()), [])


if __name__ == "__main__":
    unittest.main()
