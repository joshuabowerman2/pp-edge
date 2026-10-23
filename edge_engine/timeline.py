"""Line movement history for each prop on the board, written to timeline.json beside the app data file
and loaded by the app only when a player's page opens.

- apps: every pick'em line the app posted for the prop, with when it was first and last seen
- books: each sportsbook's line and odds at every poll where they changed (near the pick'em line)
- fair: the engine's fair % for the prop whenever it changed"""
import json
import os

from .sources.http import norm_name


def _key(l):
    return f"{l['sport']}|{l.get('player_key')}|{l['market']}"


def build(conn, legs, max_book_rows=160):
    out = {}
    for l in legs:
        pk, gid = l.get("player_key"), l.get("game_id")
        k = _key(l)
        if not pk or not gid or k in out or l.get("tier"):
            continue
        market, line = l["market"], float(l["line"])
        near = max(1.5, abs(line) * 0.15)
        apps = [{"app": r["app"], "line": r["point"], "first": r["first_seen"], "last": r["last_seen"], "active": r["active"]}
                for r in conn.execute("""SELECT app, point, first_seen, last_seen, active FROM dfs_lines
                                         WHERE game_id=? AND market=? AND player_key=? AND tier=''
                                         ORDER BY first_seen, app""", (gid, market, pk))]
        books, last = [], {}
        for r in conn.execute("""SELECT ts, book, point, over, under FROM snapshots
                                 WHERE game_id=? AND market=? AND player_key=? AND point BETWEEN ? AND ?
                                 ORDER BY ts, book, point""", (gid, market, pk, line - near, line + near)):
            sig = (r["point"], r["over"], r["under"])
            if last.get((r["book"], r["point"])) == sig:
                continue
            last[(r["book"], r["point"])] = sig
            books.append({"ts": r["ts"], "book": r["book"], "line": r["point"], "over": r["over"], "under": r["under"]})
        if len(books) > max_book_rows:
            books = books[-max_book_rows:]
        prefix = f"{gid}|"
        fair = []
        for r in conn.execute("SELECT key, ts, fair FROM prop_moves WHERE key LIKE ? ORDER BY ts", (prefix + "%",)):
            parts = r["key"].split("|")
            if len(parts) == 6 and parts[2] == pk and parts[3] == market:
                fair.append({"ts": r["ts"], "app": parts[1], "line": float(parts[4]), "side": parts[5], "fair": round(r["fair"], 4)})
        out[k] = {"apps": apps, "books": books, "fair": fair}
    return out


def write(conn, legs, path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build(conn, legs), f, separators=(",", ":"))
