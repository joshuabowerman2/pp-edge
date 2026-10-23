"""SQLite storage. One file holds the price history, the pick log, box scores, models and users.
WAL mode lets the poller write while the web app reads."""
import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS games (
  id TEXT PRIMARY KEY, sport TEXT, home TEXT, away TEXT, start TEXT,
  last_polled TEXT, poll_now INTEGER DEFAULT 0);

-- Latest price per line; snapshots keep every change (the line history models train on).
CREATE TABLE IF NOT EXISTS quotes (
  game_id TEXT, book TEXT, market TEXT, player_key TEXT, player TEXT, point REAL,
  over INTEGER, under INTEGER, updated_at TEXT,
  PRIMARY KEY (game_id, book, market, player_key, point));
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY, ts TEXT, game_id TEXT, book TEXT, market TEXT, player_key TEXT,
  point REAL, over INTEGER, under INTEGER);
CREATE INDEX IF NOT EXISTS snap_line ON snapshots (game_id, market, player_key, ts);

CREATE TABLE IF NOT EXISTS dfs_lines (
  game_id TEXT, app TEXT, market TEXT, player_key TEXT, player TEXT, point REAL, tier TEXT,
  first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1,
  PRIMARY KEY (game_id, app, market, player_key, point, tier));

-- Every pick the model makes, logged once and never edited except closing price and result.
CREATE TABLE IF NOT EXISTS picks (
  id TEXT PRIMARY KEY, seq INTEGER UNIQUE, created_at TEXT, model_version TEXT,
  sport TEXT, game_id TEXT, app TEXT, player TEXT, player_key TEXT, market TEXT, stat TEXT,
  line REAL, side TEXT, tier TEXT, home TEXT, away TEXT, start TEXT,
  fair_market REAL, fair_proj REAL, fair REAL, sigma REAL, edge REAL, books INTEGER, sharp INTEGER,
  exact INTEGER, holdout INTEGER, prev_hash TEXT, hash TEXT,
  close_fair REAL, clv REAL, closed_at TEXT, in_play INTEGER DEFAULT 0,
  result TEXT DEFAULT '', actual REAL, graded_at TEXT, injury TEXT DEFAULT '', app_line_now REAL);
CREATE INDEX IF NOT EXISTS picks_open ON picks (result, start);

-- Entries the model recommends (its own paper portfolio), also hash-chained.
CREATE TABLE IF NOT EXISTS system_entries (
  id TEXT PRIMARY KEY, seq INTEGER UNIQUE, created_at TEXT, app TEXT, entry TEXT, pick_ids TEXT,
  stake_pct REAL, ev REAL, sigma REAL, prev_hash TEXT, hash TEXT,
  status TEXT DEFAULT 'open', ret REAL, settled_at TEXT);

-- The day's best Flex slip of each size, logged once and tracked at a flat 1 unit each.
CREATE TABLE IF NOT EXISTS flex_slips (
  id TEXT PRIMARY KEY, seq INTEGER UNIQUE, created_at TEXT, day TEXT, app TEXT, entry TEXT, n INTEGER,
  pick_ids TEXT, ev REAL, cash REAL, dist TEXT, prev_hash TEXT, hash TEXT,
  status TEXT DEFAULT 'open', hits INTEGER, ret REAL, settled_at TEXT);
CREATE INDEX IF NOT EXISTS flex_day ON flex_slips (day, app, entry);

-- Each prop's fair % whenever it changes, for the line-move tracker.
CREATE TABLE IF NOT EXISTS prop_moves (key TEXT, ts TEXT, fair REAL, PRIMARY KEY (key, ts));

CREATE TABLE IF NOT EXISTS boxscores (
  sport TEXT, event_id TEXT, game_date TEXT, team TEXT, opp TEXT, player_key TEXT, player TEXT,
  minutes REAL, stats TEXT, home INTEGER, PRIMARY KEY (sport, event_id, player_key));
CREATE INDEX IF NOT EXISTS box_player ON boxscores (sport, player_key, game_date);
CREATE INDEX IF NOT EXISTS box_team ON boxscores (sport, team, game_date);

CREATE TABLE IF NOT EXISTS injuries (sport TEXT, player_key TEXT, player TEXT, status TEXT, updated_at TEXT,
  PRIMARY KEY (sport, player_key));
CREATE TABLE IF NOT EXISTS rosters (sport TEXT, player_key TEXT, team TEXT, updated_at TEXT,
  PRIMARY KEY (sport, player_key));

CREATE TABLE IF NOT EXISTS player_info (sport TEXT, player_key TEXT, team TEXT, jersey TEXT, pos TEXT, color TEXT,
  alt TEXT, updated_at TEXT, PRIMARY KEY (sport, player_key));
CREATE TABLE IF NOT EXISTS player_dir (sport TEXT, player_key TEXT, name TEXT, team TEXT, jersey TEXT, pos TEXT,
  color TEXT, alt TEXT, updated_at TEXT, PRIMARY KEY (sport, player_key));
CREATE TABLE IF NOT EXISTS correlations (sport TEXT, relation TEXT, m1 TEXT, m2 TEXT, rho REAL, n INTEGER,
  updated_at TEXT, PRIMARY KEY (sport, relation, m1, m2));
CREATE TABLE IF NOT EXISTS blend_params (sport TEXT PRIMARY KEY, a REAL, b REAL, c REAL, n INTEGER, updated_at TEXT);

CREATE TABLE IF NOT EXISTS alerts_sent (key TEXT PRIMARY KEY, ts TEXT);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE, pw_hash TEXT, created_at TEXT, api_token TEXT UNIQUE,
  bankroll REAL DEFAULT 500, kelly_mult REAL DEFAULT 0.25, max_pct REAL DEFAULT 0.02,
  day_cap_pct REAL DEFAULT 0.08, is_admin INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER, csrf TEXT, created_at TEXT, expires_at TEXT);
CREATE TABLE IF NOT EXISTS user_bets (
  id TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT, app TEXT, entry TEXT, stake REAL,
  legs TEXT, ev REAL, var REAL, status TEXT DEFAULT 'open', ret REAL, profit REAL, settled_at TEXT);
CREATE INDEX IF NOT EXISTS ub_user ON user_bets (user_id, created_at);
"""

_local = threading.local()


def connect(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)   # autocommit; use tx() for batches
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # Columns added after the first release, for databases created before them.
    for table, col in (("boxscores", "home INTEGER"),):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    _rekey_players(conn)
    return conn


PLAYER_KEYS_VERSION = 2


def _rekey_players(conn):
    """When the name normalizer changes (version 2 merged transliterated first names), rewrite stored
    player keys so old and new rows for the same player line up. Runs once per version."""
    if meta_get(conn, "player_keys_version", 1) >= PLAYER_KEYS_VERSION:
        return
    from .sources.http import norm_name
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    conn.execute("BEGIN IMMEDIATE")
    for t in tables:
        if "player_key" not in [c["name"] for c in conn.execute(f"PRAGMA table_info([{t}])")]:
            continue
        for (old,) in conn.execute(f"SELECT DISTINCT player_key FROM [{t}]").fetchall():
            new = norm_name(old or "")
            if old and new != old:
                conn.execute(f"UPDATE OR IGNORE [{t}] SET player_key=? WHERE player_key=?", (new, old))
                conn.execute(f"DELETE FROM [{t}] WHERE player_key=?", (old,))   # rows that were already there under the new key
    meta_set(conn, "player_keys_version", PLAYER_KEYS_VERSION)
    conn.execute("COMMIT")


def get(path):
    """One connection per thread (the web server is threaded)."""
    c = getattr(_local, "conns", None)
    if c is None:
        c = _local.conns = {}
    if path not in c:
        c[path] = connect(path)
    return c[path]


class tx:
    """with tx(conn): ... runs the block in one transaction."""
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, et, ev, tb):
        self.conn.execute("COMMIT" if et is None else "ROLLBACK")


def meta_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except (TypeError, ValueError):
        return r["value"]


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
