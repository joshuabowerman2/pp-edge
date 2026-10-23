"""Fake responses for The Odds API, ESPN and MLB so the engine can be tested offline."""
import json
import random
from datetime import datetime, timedelta, timezone

TEAMS = {"home": "Boston Celtics", "away": "New York Knicks"}
PLAYERS = [("Ace Guard", "home", 24.5), ("Big Wing", "home", 18.5), ("Cal Center", "home", 12.5),
           ("Dee Point", "away", 22.5), ("Eli Forward", "away", 16.5), ("Finn Shooter", "away", 14.5),
           ("Gus Hurt", "away", 20.5), ("Hal Maybe", "home", 10.5)]


class Fake:
    def __init__(self, start, final=False, seed=3):
        self.start = start
        self.final = final
        self.rng = random.Random(seed)
        self.calls = []
        self.posts = []
        self.box_values = {p: line + self.rng.choice([-3, -1, 2, 4]) for p, _, line in PLAYERS}

    def odds(self):
        books = []
        for bk in ["prizepicks", "underdog", "pinnacle", "novig", "fanduel", "draftkings"]:
            outs, alt = [], []
            for j, (p, side, line) in enumerate(PLAYERS):
                if bk in ("prizepicks", "underdog"):
                    l = line if bk == "prizepicks" else line + (0.5 if j % 2 else 0)
                    outs += [{"name": "Over", "description": p, "price": -137, "point": l},
                             {"name": "Under", "description": p, "price": -137, "point": l}]
                else:
                    o = -175 + j * 6 + (3 if bk == "fanduel" else 0)
                    u = 140 - j * 6
                    outs += [{"name": "Over", "description": p, "price": o, "point": line},
                             {"name": "Under", "description": p, "price": u, "point": line}]
            mk = [{"key": "player_points", "outcomes": outs}]
            if bk == "prizepicks":
                mk.append({"key": "player_points_alternate", "outcomes": [
                    {"name": "Over", "description": "Ace Guard", "price": -137, "point": 22.5},
                    {"name": "Over", "description": "Big Wing", "price": 100, "point": 20.5}]})
            books.append({"key": bk, "markets": mk})
        return {"bookmakers": books}

    def box_summary(self):
        players = []
        for side in ("home", "away"):
            athletes = [{"athlete": {"displayName": p}, "stats": ["34:10", str(int(self.box_values[p])), "6", "4", "2"]}
                        for p, s, _ in PLAYERS if s == side and p != "Gus Hurt"]
            if side == "away":
                athletes.append({"athlete": {"displayName": "Gus Hurt"}, "didNotPlay": True, "stats": []})
            players.append({"team": {"displayName": TEAMS[side]},
                            "statistics": [{"keys": ["minutes", "points", "rebounds", "assists", "threePointFieldGoalsMade"],
                                            "athletes": athletes}]})
        return {"boxscore": {"players": players}}

    def team_odds(self):
        h, a = TEAMS["home"], TEAMS["away"]
        def book(key, ml_h, ml_a, sp_h, sp_a, o, u, spread=-4.5, total=221.5):
            return {"key": key, "markets": [
                {"key": "h2h", "outcomes": [{"name": h, "price": ml_h}, {"name": a, "price": ml_a}]},
                {"key": "spreads", "outcomes": [{"name": h, "price": sp_h, "point": spread}, {"name": a, "price": sp_a, "point": -spread}]},
                {"key": "totals", "outcomes": [{"name": "Over", "price": o, "point": total}, {"name": "Under", "price": u, "point": total}]}]}
        return [{"id": "ev1", "home_team": h, "away_team": a, "commence_time": self.start, "bookmakers": [
            book("pinnacle", -190, 165, -108, -112, -110, -110),
            book("novig", -185, 170, -110, -110, -108, -112),
            book("fanduel", -170, 150, -110, -110, -110, -110),      # home moneyline is generous here
            book("draftkings", -200, 160, 105, -125, -105, -115)]}]   # home spread at +105 is generous

    def __call__(self, url, data=None, headers=None, timeout=30):
        self.calls.append(url)
        if data is not None:
            self.posts.append((url, data, headers))
            return {}, {}
        if "/events?" in url:
            return [{"id": "ev1", "home_team": TEAMS["home"], "away_team": TEAMS["away"], "commence_time": self.start}], {}
        if "/odds?" in url and "/events/" not in url:
            return self.team_odds(), {"x-requests-remaining": "18990", "x-requests-last": "3"}
        if "/events/ev1/odds" in url:
            return self.odds(), {"x-requests-remaining": "19000", "x-requests-last": "10"}
        if url.endswith("/injuries"):
            return {"injuries": [{"injuries": [{"status": "Out", "athlete": {"displayName": "Gus Hurt"}},
                                               {"status": "Questionable", "athlete": {"displayName": "Hal Maybe"}}]}]}, {}
        if "/teams?limit" in url:
            return {"sports": [{"leagues": [{"teams": [{"team": {"id": "2", "displayName": "Boston Celtics", "color": "008348", "alternateColor": "ffffff"}},
                                                       {"team": {"id": "18", "displayName": "New York Knicks"}}]}]}]}, {}
        if "/teams/2/roster" in url:
            return {"athletes": [{"displayName": p, "jersey": str(i + 1), "position": {"abbreviation": "G"}}
                                 for i, (p, s, _) in enumerate(PLAYERS) if s == "home"]}, {}
        if "/teams/18/roster" in url:
            return {"athletes": [{"displayName": p} for p, s, _ in PLAYERS if s == "away"]}, {}
        if "/scoreboard" in url:
            return {"events": [{"id": "401", "date": self.start, "status": {"type": {"completed": self.final}},
                                "competitions": [{"competitors": [
                                    {"homeAway": "home", "team": {"displayName": "Boston Celtics"}, "score": "112"},
                                    {"homeAway": "away", "team": {"displayName": "New York Knicks"}, "score": "105"}]}]}]}, {}
        if "/summary?event=401" in url:
            return self.box_summary(), {}
        raise RuntimeError("unexpected url " + url)


def seed_boxscores(conn, n_games=30, seed=5):
    """Synthetic history: each player's points around their line, with a known teammate correlation."""
    import math
    rng = random.Random(seed)
    from edge_engine.sources.http import canon_team, norm_name
    day0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for g in range(n_games):
        team_shock = {"home": rng.gauss(0, 1), "away": rng.gauss(0, 1)}
        date = (day0 + timedelta(days=g)).strftime("%Y-%m-%d")
        for p, side, line in PLAYERS:
            z = 0.6 * team_shock[side] + math.sqrt(1 - 0.36) * rng.gauss(0, 1)   # teammates correlate ~0.36
            pts = max(0, round(line + 0.5 + 5 * z))
            team, opp = canon_team(TEAMS[side]), canon_team(TEAMS["away" if side == "home" else "home"])
            conn.execute("""INSERT INTO boxscores(sport, event_id, game_date, team, opp, player_key, player, minutes, stats, home)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         ("nba", f"h{g}", date, team, opp, norm_name(p), p, 32.0,
                          json.dumps({"points": pts, "rebounds": 5, "assists": 3, "fieldGoalsAttempted": 12 + g % 5}),
                          1 if side == "home" else 0))
