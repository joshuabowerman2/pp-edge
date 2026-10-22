"""Sports, markets, stat models, payout tables and box-score keys."""

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
SPORT_DIST = {("nhl", "player_points"): ("poisson",), ("nhl", "player_assists"): ("poisson",)}


def dist_for(sport, market):
    return SPORT_DIST.get((sport, market)) or DIST.get(market, ("normal", .35))


# Hand-set correlation priors. Learned values (correlation.py) replace these as data builds up.
CORR_BLEND = {   # team unknown
    ("player_pass_yds", "player_reception_yds"): .30, ("player_pass_yds", "player_receptions"): .20,
    ("player_pass_completions", "player_receptions"): .25, ("player_pass_completions", "player_reception_yds"): .25,
    ("player_pass_yds", "player_rush_yds"): -.10, ("player_reception_yds", "player_reception_yds"): .05,
    ("player_points", "player_points"): .05, ("player_assists", "player_points"): .05,
    ("player_points_rebounds_assists", "player_points_rebounds_assists"): .05,
    ("player_rebounds", "player_rebounds"): -.05,
    ("player_shots_on_goal", "player_shots_on_goal"): .05, ("player_shots_on_goal", "player_total_saves"): .10,
    ("batter_hits", "pitcher_strikeouts"): -.10, ("batter_total_bases", "pitcher_strikeouts"): -.10,
}
CORR_PRIOR = {
    "team": {
        "nfl": {("player_pass_yds", "player_reception_yds"): .45, ("player_pass_yds", "player_receptions"): .30,
                ("player_pass_completions", "player_receptions"): .35,
                ("player_pass_completions", "player_reception_yds"): .35,
                ("player_pass_yds", "player_rush_yds"): -.10, ("player_reception_yds", "player_reception_yds"): -.05,
                ("player_rush_yds", "player_rush_yds"): -.15},
        "nba": {("player_points", "player_points"): -.05, ("player_assists", "player_points"): .10,
                ("player_points_rebounds_assists", "player_points_rebounds_assists"): -.03,
                ("player_rebounds", "player_rebounds"): -.10},
        "wnba": {("player_points", "player_points"): -.05, ("player_assists", "player_points"): .10,
                 ("player_rebounds", "player_rebounds"): -.10},
        "nhl": {("player_shots_on_goal", "player_shots_on_goal"): .05, ("player_points", "player_points"): .20},
        "mlb": {("batter_hits", "batter_hits"): .10, ("batter_total_bases", "batter_total_bases"): .10},
    },
    "opp": {
        "nfl": {("player_pass_yds", "player_reception_yds"): .10, ("player_pass_yds", "player_receptions"): .05,
                ("player_pass_yds", "player_pass_yds"): .15, ("player_reception_yds", "player_reception_yds"): .10,
                ("player_pass_yds", "player_rush_yds"): .05},
        "nba": {("player_points", "player_points"): .10, ("player_assists", "player_points"): .05,
                ("player_points_rebounds_assists", "player_points_rebounds_assists"): .10,
                ("player_rebounds", "player_rebounds"): -.05},
        "wnba": {("player_points", "player_points"): .10, ("player_rebounds", "player_rebounds"): -.05},
        "nhl": {("player_shots_on_goal", "player_total_saves"): .35, ("player_points", "player_points"): -.05,
                ("player_shots_on_goal", "player_shots_on_goal"): .05},
        "mlb": {("batter_hits", "pitcher_strikeouts"): -.20, ("batter_total_bases", "pitcher_strikeouts"): -.15,
                ("batter_hits", "pitcher_hits_allowed"): .25, ("batter_total_bases", "pitcher_hits_allowed"): .20},
    },
}

# Books: weights when blending prices, and which count as sharp.
SHARP = {"pinnacle", "betonlineag", "novig", "prophetx", "lowvig"}
BOOK_WEIGHTS = {"pinnacle": 4, "betonlineag": 2.5, "novig": 2.5, "prophetx": 2.5, "lowvig": 2,
                "kalshi": 1.5, "polymarket": 1.5, "polymarket_us": 1.5, "fanduel": 1.5, "draftkings": 1.2}
DFS_BOOKS = {"prizepicks", "underdog", "pick6", "dabble_us_dfs", "betr_us_dfs"}

# Pick'em payout tables: hits -> multiplier. Check these in each app; override with APPS_FILE.
# "family" ties entries together so a voided leg steps down to the smaller entry of the same family.
APPS = {
    "prizepicks": {"label": "PrizePicks", "main": "5-Pick Flex", "entries": {
        "2-Pick Power": {"n": 2, "pay": {2: 3}, "family": "power"},
        "3-Pick Power": {"n": 3, "pay": {3: 5}, "family": "power"},
        "4-Pick Power": {"n": 4, "pay": {4: 10}, "family": "power"},
        "5-Pick Power": {"n": 5, "pay": {5: 20}, "family": "power"},
        "6-Pick Power": {"n": 6, "pay": {6: 37.5}, "family": "power"},
        "3-Pick Flex": {"n": 3, "pay": {3: 3, 2: 1}, "family": "flex"},
        "4-Pick Flex": {"n": 4, "pay": {4: 6, 3: 1.5}, "family": "flex"},
        "5-Pick Flex": {"n": 5, "pay": {5: 10, 4: 2, 3: 0.4}, "family": "flex"},
        "6-Pick Flex": {"n": 6, "pay": {6: 25, 5: 2, 4: 0.4}, "family": "flex"}}},
    "underdog": {"label": "Underdog", "main": "3-Pick Standard", "entries": {
        "2-Pick Standard": {"n": 2, "pay": {2: 3}, "family": "standard"},
        "3-Pick Standard": {"n": 3, "pay": {3: 6}, "family": "standard"},
        "4-Pick Standard": {"n": 4, "pay": {4: 10}, "family": "standard"},
        "5-Pick Standard": {"n": 5, "pay": {5: 20}, "family": "standard"}}},
    # Payouts for these vary; add them with APPS_FILE. Their legs are still priced and shown.
    "pick6": {"label": "DraftKings Pick6", "main": "", "entries": {}},
    "dabble_us_dfs": {"label": "Dabble", "main": "", "entries": {}},
}

from zoneinfo import ZoneInfo
EASTERN_TZ = ZoneInfo("America/New_York")   # league schedules are dated in Eastern time

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_PATH = {"nba": "basketball/nba", "wnba": "basketball/wnba", "nfl": "football/nfl",
             "cfb": "football/college-football", "nhl": "hockey/nhl", "mlb": "baseball/mlb"}
MLB_BASE = "https://statsapi.mlb.com/api/v1"

# Market -> stat keys summed from the stored box score (ESPN keys; MLB stored as "group.key").
STAT_KEYS = {
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
                 "player_rush_reception_yds": ["rushingYards", "receivingYards"]},
    "hockey": {"player_shots_on_goal": ["shotsTotal"], "player_points": ["goals", "assists"],
               "player_assists": ["assists"], "player_total_saves": ["saves"],
               "player_blocked_shots": ["blockedShots"]},
    "baseball": {"pitcher_strikeouts": ["pitching.strikeOuts"], "pitcher_hits_allowed": ["pitching.hits"],
                 "pitcher_outs": ["pitching.outs"], "batter_hits": ["batting.hits"],
                 "batter_total_bases": ["batting.totalBases"],
                 "batter_hits_runs_rbis": ["batting.hits", "batting.runs", "batting.rbi"]},
}
FAMILY = {"nba": "basketball", "wnba": "basketball", "nfl": "football", "cfb": "football",
          "nhl": "hockey", "mlb": "baseball"}


def stat_keys(sport, market):
    return STAT_KEYS.get(FAMILY.get(sport, ""), {}).get(market)


INJ_LEVEL = {"out": 3, "injured reserve": 3, "-day il": 3, "suspended": 3, "inactive": 3,
             "doubtful": 2, "questionable": 1, "day-to-day": 1, "day to day": 1}


def injury_level(status):
    s = (status or "").lower()
    return max([v for k, v in INJ_LEVEL.items() if k in s], default=0)
