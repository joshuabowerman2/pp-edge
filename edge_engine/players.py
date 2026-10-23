"""Player game logs for the app's player pages: the last games with each player's main box score stats,
so the app can show a game log, a chart against any line, hit rates and averages.

Written as players.json next to index.json and loaded only when someone opens a player page."""
import json

from .markets import FAMILY, STAT_KEYS

# Box score keys kept per sport family (everything the app's columns and prop markets need).
KEEP = {
    "basketball": ["points", "rebounds", "assists", "threePointFieldGoalsMade", "threePointFieldGoalsAttempted",
                   "fieldGoalsMade", "fieldGoalsAttempted", "freeThrowsMade", "freeThrowsAttempted",
                   "steals", "blocks", "turnovers"],
    "hockey": ["goals", "assists", "shotsTotal", "blockedShots", "saves", "shotsAgainst"],
    "football": ["passingYards", "passingTouchdowns", "completions", "passingAttempts", "interceptions", "rushingYards",
                 "rushingAttempts", "rushingTouchdowns", "receptions", "receivingYards", "receivingTargets",
                 "receivingTouchdowns"],
    "baseball": ["batting.hits", "batting.runs", "batting.rbi", "batting.homeRuns", "batting.totalBases",
                 "batting.atBats", "batting.baseOnBalls", "batting.strikeOuts", "batting.stolenBases",
                 "pitching.strikeOuts", "pitching.outs", "pitching.hits", "pitching.earnedRuns",
                 "pitching.baseOnBalls", "pitching.numberOfPitches"],
}


def key(sport, pk):
    return f"{sport}|{pk}"


def build(conn, legs, max_games=20):
    players, seen = {}, set()
    for l in legs:
        sport, pk = l.get("sport"), l.get("player_key")
        if not pk or (sport, pk) in seen:
            continue
        seen.add((sport, pk))
        fam = FAMILY.get(sport, "")
        keep = KEEP.get(fam)
        if not keep:
            continue
        games = []
        for r in conn.execute("""SELECT game_date, opp, minutes, stats, home FROM boxscores
                                 WHERE sport=? AND player_key=? ORDER BY game_date DESC LIMIT ?""",
                              (sport, pk, max_games)):
            st = json.loads(r["stats"] or "{}")
            s = {k: (int(st[k]) if float(st[k]).is_integer() else st[k]) for k in keep if st.get(k) is not None}
            if not s and not r["minutes"]:
                continue   # listed but didn't play
            games.append({"d": r["game_date"], "o": r["opp"] or "", "h": r["home"],
                          "m": None if r["minutes"] is None else round(r["minutes"], 1), "s": s})
        players[key(sport, pk)] = {"name": l.get("player") or pk, "team": l.get("team") or "", "games": games}
    stat_keys = {s: STAT_KEYS.get(FAMILY.get(s, ""), {}) for s in {k.split("|")[0] for k in players}}
    return {"players": players, "stat_keys": stat_keys}


def write(conn, legs, path):
    with open(path, "w") as f:
        json.dump(build(conn, legs), f, separators=(",", ":"))
