"""Settings, read from environment variables so the same code runs locally, on a server, or in CI."""
import json
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _env(name, default=""):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _float(name, default):
    try:
        return float(_env(name, default))
    except ValueError:
        return float(default)


def _list(name, default):
    out = []
    for x in _env(name, default).replace(",", " ").split():
        if x.strip() and x.strip() not in out:
            out.append(x.strip())   # keeps order, drops repeats ("nba nfl nhl mlb mlb")
    return out


@dataclass
class Config:
    # Data
    odds_api_key: str = field(default_factory=lambda: _env("ODDS_API_KEY"))
    db_path: str = field(default_factory=lambda: _env("EDGE_DB", "data/edge.db"))
    sports: list = field(default_factory=lambda: _list("SPORTS", "nba nhl"))
    # Ten bookmakers bill as one region. Sharp books and exchanges first, then pick'em apps, then US books.
    books: list = field(default_factory=lambda: _list(
        "BOOKS", "prizepicks underdog pinnacle betonlineag novig prophetx fanduel draftkings betmgm williamhill_us"))
    dfs_apps: list = field(default_factory=lambda: _list("DFS_APPS", "prizepicks underdog"))
    all_markets: bool = field(default_factory=lambda: _env("ALL_MARKETS", "0") == "1")
    alt_lines: bool = field(default_factory=lambda: _env("ALT_LINES", "0") == "1")
    # Polling
    daily_credit_budget: float = field(default_factory=lambda: _float("DAILY_CREDIT_BUDGET", 600))
    min_poll_minutes: float = field(default_factory=lambda: _float("MIN_POLL_MINUTES", 3))
    window_hours: float = field(default_factory=lambda: _float("WINDOW_HOURS", 24))
    # Picks and staking
    min_books: int = field(default_factory=lambda: int(_float("MIN_BOOKS", 2)))
    log_edge: float = field(default_factory=lambda: _float("LOG_EDGE", 0.0))      # log every pick at or above break-even
    alert_edge: float = field(default_factory=lambda: _float("ALERT_EDGE", 0.03))  # alert at 3+ points over break-even
    kelly_mult: float = field(default_factory=lambda: _float("KELLY_MULT", 0.25))
    max_stake_pct: float = field(default_factory=lambda: _float("MAX_STAKE_PCT", 0.02))
    day_cap_pct: float = field(default_factory=lambda: _float("DAY_CAP_PCT", 0.08))
    player_cap_pct: float = field(default_factory=lambda: _float("PLAYER_CAP_PCT", 0.04))
    max_plays: int = field(default_factory=lambda: int(_float("MAX_PLAYS", 6)))
    bankroll: float = field(default_factory=lambda: _float("BANKROLL", 0))
    holdout_share: int = 5   # 1 in 5 picks goes to the holdout set
    # Alerts
    ntfy_topic: str = field(default_factory=lambda: _env("NTFY_TOPIC"))
    discord_webhook: str = field(default_factory=lambda: _env("DISCORD_WEBHOOK"))
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL", _env("REPORT_URL")))
    # Web
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))
    invite_code: str = field(default_factory=lambda: _env("INVITE_CODE"))
    open_signups: bool = field(default_factory=lambda: _env("OPEN_SIGNUPS", "0") == "1")
    secure_cookies: bool = field(default_factory=lambda: _env("SECURE_COOKIES", "1") == "1")
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(_env("REPORT_TZ", "America/Chicago")))
    # Optional JSON file overriding app payout tables (for apps whose payouts change often)
    apps_file: str = field(default_factory=lambda: _env("APPS_FILE"))

    def app_payouts(self):
        from .markets import APPS
        apps = json.loads(json.dumps(APPS))
        if self.apps_file and os.path.exists(self.apps_file):
            with open(self.apps_file, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    apps.setdefault(k, {"label": k, "entries": {}})
                    apps[k].update(v)
        for a in apps.values():   # JSON keys are strings; payouts use int hit counts
            a["entries"] = {name: {"n": int(e["n"]), "pay": {int(h): float(m) for h, m in e["pay"].items()},
                                   "family": e.get("family", "")} for name, e in a.get("entries", {}).items()}
        return apps


def load():
    return Config()
