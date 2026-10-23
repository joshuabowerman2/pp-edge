"""Command line: `edge <command>`. Run `edge -h` for the list."""
import argparse
import csv
import getpass
import json
import sys
from datetime import datetime, timezone

from . import __version__, backtest, config, db, engine, results, tracking


def main(argv=None):
    ap = argparse.ArgumentParser(prog="edge", description=f"Edge Engine {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the database")
    p = sub.add_parser("run", help="always-on loop: poll, price, alert, grade (for a server)")
    p.add_argument("--export", default="site/index.json", help="Edge Finder app data file")
    p.add_argument("--results", default="site/results.html")
    p = sub.add_parser("once", help="one full pass (for scheduled jobs like GitHub Actions)")
    p.add_argument("--export", default="site/index.json")
    p.add_argument("--results", default="site/results.html")
    sub.add_parser("grade", help="grade finished picks")
    sub.add_parser("fit", help="refit the market/projection blend and correlations")
    p = sub.add_parser("backtest", help="evaluate models on holdout picks")
    p.add_argument("--strict", action="store_true", help="exit 2 if the final price is worse than the market")
    p.add_argument("--training", action="store_true", help="evaluate on training picks instead")
    p = sub.add_parser("backfill", help="load past box scores so models have history")
    p.add_argument("--days", type=int, default=60)
    sub.add_parser("rosters", help="refresh rosters, jersey numbers and team colors from ESPN (free)")
    p = sub.add_parser("results", help="write the public results page and CSV")
    p.add_argument("--out", default="site/results.html")
    p = sub.add_parser("export", help="write the Edge Finder app data file")
    p.add_argument("--out", default="site/index.json")
    p = sub.add_parser("verify", help="check the pick log's hash chain (database, or a downloaded CSV)")
    p.add_argument("--csv", help="verify a picks.csv download instead of the database")
    p = sub.add_parser("import-csv", help="import history/picks.csv from the old script (not hash-chained)")
    p.add_argument("path")
    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p = sub.add_parser("create-user", help="create a web account")
    p.add_argument("email")
    p.add_argument("--admin", action="store_true")
    a = ap.parse_args(argv)

    cfg = config.load()
    conn = db.get(cfg.db_path)
    now = datetime.now(timezone.utc)

    if a.cmd == "init":
        print(f"Database ready at {cfg.db_path}")
    elif a.cmd == "run":
        _need_key(cfg)
        engine.run_forever(conn, cfg, export_path=a.export, results_path=a.results)
    elif a.cmd == "once":
        _need_key(cfg)
        r = engine.run_once(conn, cfg, now, export_path=a.export)
        results.write(conn, cfg, a.results)
        print(json.dumps(r))
    elif a.cmd == "grade":
        apps = engine._apps(cfg)
        print(f"Graded {tracking.grade(conn, now)} picks; settled {tracking.settle_system_entries(conn, apps, now)} model entries.")
    elif a.cmd == "fit":
        print(json.dumps(engine.nightly(conn, cfg), default=str, indent=1))
    elif a.cmd == "backtest":
        res = backtest.evaluate(conn, 0 if a.training else 1)
        print(backtest.report(res))
        if a.strict and res.get("delta_log_loss", 0) > 0.001:
            sys.exit(2)
    elif a.cmd == "backfill":
        for s in cfg.sports:
            print(f"{s}: {tracking.backfill(conn, s, a.days, now)} games stored")
    elif a.cmd == "rosters":
        for s in cfg.sports:
            print(f"{s}: {engine.refresh_rosters(conn, s, now)} players")
    elif a.cmd == "results":
        results.write(conn, cfg, a.out)
        print(f"Wrote {a.out}")
    elif a.cmd == "export":
        from .export import write_app_json
        write_app_json(conn, cfg, a.out, now)
        print(f"Wrote {a.out}")
    elif a.cmd == "verify":
        ok, n, bad = verify_csv(a.csv) if a.csv else tracking.verify_chain(conn)
        print(f"{'OK' if ok else 'FAILED'}: {n} picks checked" + ("" if ok else f", first bad pick #{bad}"))
        if not ok:
            sys.exit(1)
    elif a.cmd == "import-csv":
        print(f"Imported {import_old_csv(conn, a.path)} picks from the old script as a separate legacy set.")
    elif a.cmd == "serve":
        from .web import serve
        serve(cfg, a.host, a.port)
    elif a.cmd == "create-user":
        from .web import create_user
        pw = getpass.getpass("Password (10+ characters): ")
        if len(pw) < 10:
            sys.exit("Password too short.")
        create_user(conn, a.email, pw, a.admin)
        print(f"Created {a.email}")
    # Fold the write-ahead log into the main file so a copied or committed database is complete.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _need_key(cfg):
    if not cfg.odds_api_key:
        sys.exit("Set ODDS_API_KEY (get one at https://the-odds-api.com).")


def verify_csv(path):
    """Recompute the chain from a downloaded CSV, the same way a skeptical reader would."""
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    prev = "genesis"
    floats = {"line", "fair_market", "fair_proj", "fair", "edge"}
    for i, r in enumerate(rows, 1):
        rec = {k: (None if r[k] == "" else float(r[k])) if k in floats else r[k] for k in tracking.CORE}
        if r["prev_hash"] != prev or tracking._hash(prev, rec) != r["hash"]:
            return False, i, r["seq"]
        prev = r["hash"]
    return True, len(rows), None


def import_old_csv(conn, path):
    """Old picks can't join the hash chain (they were never hashed when made), so they're stored as
    a separate, clearly labeled 'legacy' set used only for calibration stats."""
    conn.execute("""CREATE TABLE IF NOT EXISTS legacy_picks (id TEXT PRIMARY KEY, date TEXT, sport TEXT, player TEXT,
                    market TEXT, line REAL, side TEXT, fair REAL, clv REAL, result TEXT)""")
    n = 0
    for r in csv.DictReader(open(path, encoding="utf-8")):
        try:
            conn.execute("INSERT OR IGNORE INTO legacy_picks VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (r["id"], r["date"], r["sport"], r["player"], r["market"], float(r["line"]), r["side"],
                          float(r["pick_fair"]), float(r["clv"]) if r.get("clv") else None, r.get("result", "")))
            n += 1
        except (KeyError, ValueError):
            continue
    return n


if __name__ == "__main__":
    main()
