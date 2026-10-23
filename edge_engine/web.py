"""The web product: public track record, accounts, the daily board sized to each user's bankroll,
bet tracking, and a JSON API. Pure standard library (WSGI), so it runs anywhere Python does.
Run with `edge serve` or any WSGI server (gunicorn "edge_engine.web:make_app()")."""
import hashlib
import hmac
import json
import secrets
import socketserver
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from wsgiref.simple_server import WSGIServer, make_server

from . import config as config_mod, db, results, tracking
from .db import meta_get
from .markets import LABELS, injury_level
from .ui import cls, esc, page, pct

NAV_PUBLIC = [("Track record", "/results"), ("Sign in", "/login")]
NAV_USER = [("Today", "/board"), ("My bets", "/bets"), ("Track record", "/results"), ("Account", "/account")]
MAX_BODY = 64 * 1024


# ------------------------------------------------------------------ passwords and sessions
def hash_pw(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_pw(pw, stored):
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, AttributeError):
        return False


def create_user(conn, email, pw, admin=False):
    conn.execute("INSERT INTO users(email,pw_hash,created_at,api_token,is_admin) VALUES(?,?,?,?,?)",
                 (email.strip().lower(), hash_pw(pw), tracking.iso(datetime.now(timezone.utc)),
                  secrets.token_urlsafe(24), int(admin)))
    return conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()


class Req:
    def __init__(self, environ):
        self.env = environ
        self.method = environ.get("REQUEST_METHOD", "GET")
        self.path = environ.get("PATH_INFO", "/") or "/"
        self.query = {k: v[0] for k, v in urllib.parse.parse_qs(environ.get("QUERY_STRING", "")).items()}
        self.form = {}
        if self.method == "POST":
            try:
                n = min(int(environ.get("CONTENT_LENGTH") or 0), MAX_BODY)
            except ValueError:
                n = 0
            body = environ["wsgi.input"].read(n).decode("utf-8", "replace") if n else ""
            self.form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        cookies = {}
        for part in environ.get("HTTP_COOKIE", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k] = v
        self.cookies = cookies
        self.ip = environ.get("HTTP_X_FORWARDED_FOR", environ.get("REMOTE_ADDR", "")).split(",")[0].strip()


class Resp:
    def __init__(self, body="", status="200 OK", ctype="text/html; charset=utf-8", headers=None):
        self.body, self.status, self.ctype = body, status, ctype
        self.headers = headers or []


def redirect(to, headers=None):
    return Resp("", "303 See Other", headers=[("Location", to)] + (headers or []))


_attempts = {}


def rate_limited(ip, limit=10, window=600):
    now = time.time()
    a = [t for t in _attempts.get(ip, []) if now - t < window]
    _attempts[ip] = a
    return len(a) >= limit


def note_attempt(ip):
    _attempts.setdefault(ip, []).append(time.time())


# ------------------------------------------------------------------ app
class App:
    def __init__(self, cfg=None):
        self.cfg = cfg or config_mod.load()
        self._results_cache = (0, "")

    def conn(self):
        return db.get(self.cfg.db_path)

    def __call__(self, environ, start_response):
        req = Req(environ)
        try:
            resp = self.route(req)
        except Exception:   # never leak internals
            import traceback
            traceback.print_exc()
            resp = Resp(page("Something went wrong", "<h1>Something went wrong</h1><p>Try again in a minute.</p>", NAV_PUBLIC),
                        "500 Internal Server Error")
        body = resp.body.encode() if isinstance(resp.body, str) else resp.body
        headers = [("Content-Type", resp.ctype), ("Content-Length", str(len(body))),
                   ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"),
                   ("X-Frame-Options", "DENY")] + resp.headers
        start_response(resp.status, headers)
        return [body]

    # ---- session helpers
    def user(self, req):
        tok = req.cookies.get("edge_session")
        if not tok:
            return None, None
        c = self.conn()
        s = c.execute("SELECT * FROM sessions WHERE token=? AND expires_at > ?",
                      (tok, tracking.iso(datetime.now(timezone.utc)))).fetchone()
        if not s:
            return None, None
        return c.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone(), s

    def start_session(self, uid):
        tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        now = datetime.now(timezone.utc)
        self.conn().execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                            (tok, uid, csrf, tracking.iso(now), tracking.iso(now + timedelta(days=30))))
        flags = "; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000" + ("; Secure" if self.cfg.secure_cookies else "")
        return ("Set-Cookie", f"edge_session={tok}{flags}")

    def csrf_ok(self, req, sess):
        return sess is not None and hmac.compare_digest(req.form.get("csrf", ""), sess["csrf"])

    # ---- routing
    def route(self, req):
        p = req.path.rstrip("/") or "/"
        public = {"/": self.home, "/results": self.results_page, "/picks.csv": self.picks_csv,
                  "/api/results.json": self.api_results, "/terms": self.terms, "/healthz": lambda r: Resp("ok", ctype="text/plain"),
                  "/login": self.login, "/signup": self.signup, "/logout": self.logout,
                  "/api/latest.json": self.api_latest}
        if p in public:
            return public[p](req)
        user, sess = self.user(req)
        if not user:
            return redirect("/login?next=" + urllib.parse.quote(p))
        private = {"/board": self.board, "/bets": self.bets, "/account": self.account}
        if p in private:
            return private[p](req, user, sess)
        return Resp(page("Not found", "<h1>Page not found</h1>", NAV_USER), "404 Not Found")

    def nav(self, req):
        return NAV_USER if self.user(req)[0] else NAV_PUBLIC

    # ---- public pages
    def home(self, req):
        if self.user(req)[0]:
            return redirect("/board")
        r = results.data(self.conn())
        a, ch = r["all"], r["chain"]
        body = f"""<div style="max-width:760px"><h1>Pick'em entries priced like a trading desk.</h1>
<p class="lead">Edge Engine prices every PrizePicks and Underdog line against sharp sportsbooks and betting exchanges,
adds its own player projections, and sizes each day's entries together with Kelly staking. Every pick is published to a
hash-verified record before the game starts.</p>
<p style="display:flex;gap:10px;flex-wrap:wrap;margin-top:16px"><a class="btn primary" href="/results">See the track record</a>
<a class="btn" href="/login">Sign in</a></p></div>
<div class="kpis">
<div class="kpi"><span class="l">Picks logged</span><span class="v">{ch['count']:,}</span><span class="s">{'verified' if ch['ok'] else 'check failed'}</span></div>
<div class="kpi"><span class="l">Hit rate</span><span class="v">{pct(a['hit'])}</span><span class="s">expected {pct(a['exp'])}</span></div>
<div class="kpi"><span class="l">Beat the closing line</span><span class="v">{pct(a['beat'], 0)}</span><span class="s">{a['clv_n']:,} legs</span></div></div>"""
        return Resp(page("Edge Engine", body, NAV_PUBLIC, "/"))

    def results_page(self, req):
        t, cached = self._results_cache
        if time.time() - t > 300:
            cached = results.html(self.conn(), self.nav(req), self.cfg.tz)
            self._results_cache = (time.time(), cached)
        return Resp(cached)

    def picks_csv(self, req):
        return Resp(results.csv_text(self.conn()), ctype="text/csv; charset=utf-8",
                    headers=[("Content-Disposition", 'attachment; filename="edge-engine-picks.csv"')])

    def api_results(self, req):
        d = results.data(self.conn())
        return Resp(json.dumps(d, default=str), ctype="application/json")

    def api_latest(self, req):
        tok = req.query.get("token", "")
        u = self.conn().execute("SELECT * FROM users WHERE api_token=?", (tok,)).fetchone() if tok else None
        if not u:
            return Resp(json.dumps({"error": "Add ?token= with the API token from your account page."}),
                        "401 Unauthorized", "application/json")
        from .export import app_json
        return Resp(json.dumps(app_json(self.conn(), self.cfg, datetime.now(timezone.utc))), ctype="application/json",
                    headers=[("Access-Control-Allow-Origin", "*")])

    def terms(self, req):
        body = """<h1>Terms and responsible play</h1><div class="panel" style="max-width:72ch">
<p>Edge Engine provides probability estimates for entertainment and research. It is not financial advice and does not
place bets. Estimates come from public market prices and statistical models and can be wrong. Past results do not
guarantee future results.</p><p>You must be 21 or older and located where daily fantasy and sports wagering are legal.
Pick'em apps and sportsbooks may limit or close accounts; follow each app's terms.</p>
<p>Set limits and stick to them. If gambling stops being fun, call or text <b class="num">1-800-GAMBLER</b>.</p></div>"""
        return Resp(page("Terms", body, self.nav(req)))

    def login(self, req):
        err = ""
        if req.method == "POST":
            if rate_limited(req.ip):
                err = "Too many attempts. Wait 10 minutes and try again."
            else:
                u = self.conn().execute("SELECT * FROM users WHERE email=?", (req.form.get("email", "").strip().lower(),)).fetchone()
                if u and check_pw(req.form.get("password", ""), u["pw_hash"]):
                    nxt = req.query.get("next", "/board")
                    if not nxt.startswith("/") or nxt.startswith("//"):
                        nxt = "/board"
                    return redirect(nxt, [self.start_session(u["id"])])
                note_attempt(req.ip)
                err = "That email and password don't match."
        signup = '<p class="small">No account? <a href="/signup">Create one</a>.</p>' if (self.cfg.open_signups or self.cfg.invite_code) else ""
        body = f"""<h1>Sign in</h1>{f'<div class="alert bad">{esc(err)}</div>' if err else ''}
<form class="stack" method="post"><label>Email<input type="email" name="email" id="email" autocomplete="email" required></label>
<label>Password<input type="password" name="password" id="password" autocomplete="current-password" required></label>
<button class="btn primary" type="submit">Sign in</button></form>{signup}"""
        return Resp(page("Sign in", body, NAV_PUBLIC, "/login"))

    def signup(self, req):
        if not (self.cfg.open_signups or self.cfg.invite_code):
            return Resp(page("Sign up", "<h1>Sign-ups are closed</h1><p>Ask the owner for an invite.</p>", NAV_PUBLIC))
        err = ""
        if req.method == "POST":
            email, pw = req.form.get("email", "").strip().lower(), req.form.get("password", "")
            if rate_limited(req.ip):
                err = "Too many attempts. Wait 10 minutes and try again."
            elif not self.cfg.open_signups and not hmac.compare_digest(req.form.get("invite", ""), self.cfg.invite_code):
                note_attempt(req.ip)
                err = "That invite code isn't valid."
            elif "@" not in email or len(email) > 200:
                err = "Enter a valid email address."
            elif len(pw) < 10:
                err = "Use a password of at least 10 characters."
            elif req.form.get("age") != "yes":
                err = "You must confirm you are 21 or older."
            elif self.conn().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                err = "That email already has an account. Sign in instead."
            else:
                u = create_user(self.conn(), email, pw)
                return redirect("/account?welcome=1", [self.start_session(u["id"])])
        invite = "" if self.cfg.open_signups else '<label>Invite code<input name="invite" id="invite" required></label>'
        body = f"""<h1>Create an account</h1>{f'<div class="alert bad">{esc(err)}</div>' if err else ''}
<form class="stack" method="post"><label>Email<input type="email" name="email" id="semail" autocomplete="email" required></label>
<label>Password (10+ characters)<input type="password" name="password" id="spassword" autocomplete="new-password" minlength="10" required></label>
{invite}<label style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" name="age" id="age" value="yes" style="min-height:auto">
I am 21 or older and have read the <a href="/terms">terms</a>.</label><button class="btn primary" type="submit">Create account</button></form>"""
        return Resp(page("Sign up", body, NAV_PUBLIC))

    def logout(self, req):
        tok = req.cookies.get("edge_session")
        if tok and req.method == "POST":
            self.conn().execute("DELETE FROM sessions WHERE token=?", (tok,))
        return redirect("/", [("Set-Cookie", "edge_session=; Max-Age=0; Path=/")])

    # ---- signed-in pages
    def _stake(self, user, stake_pct):
        """Scale the model's stake to this user's Kelly fraction, cap and bankroll."""
        f = stake_pct * (user["kelly_mult"] / max(self.cfg.kelly_mult, 1e-9))
        return round(min(f, user["max_pct"]) * user["bankroll"], 2)

    def board(self, req, user, sess):
        b = meta_get(self.conn(), "board")
        if not b:
            return Resp(page("Today", "<h1>Today</h1><div class='panel'>No board yet. The engine publishes one after its first poll.</div>",
                             NAV_USER, "/board"))
        age_h = (datetime.now(timezone.utc) - tracking.parse_time(b["generated_at"])).total_seconds() / 3600
        stale = f'<div class="alert warn">This board is {age_h:.0f} hours old. The engine may be stopped.</div>' if age_h > 2 else ""
        csrf = sess["csrf"]
        total = sum(self._stake(user, p["stake_pct"]) for p in b["plays"])

        def leg_li(l):
            tag = f' <span class="chip {"bad" if injury_level(l.get("injury")) >= 2 else "warn"}">{esc(l["injury"])}</span>' if l.get("injury") else ""
            proj = f' · proj {l["proj_mu"]:.1f}' if l.get("proj_mu") is not None else ""
            return (f'<li><span class="side {l["side"]}">{l["side"]}</span> {esc(l["player"])} '
                    f'<span class="muted small">{esc(l["stat"])} {l["line"]:g} · fair {pct(l["fair"])}{proj}</span>{tag}</li>')

        cards = "".join(f"""<div class="card"><div style="display:flex;justify-content:space-between;gap:8px">
<b>{esc(p['app_label'])} {esc(p['entry'])}</b><span class="chip {'good' if p['ev'] > 1 else 'bad'}">{(p['ev'] - 1) * 100:+.1f}% EV</span></div>
<ol class="legs">{''.join(leg_li(l) for l in p['legs'])}</ol>
<p class="small muted" style="margin:0">Stake back {pct(p['cash'], 0)} of the time · EV uncertainty ±{p['sigma'] * 100:.1f}%
{' · same-game correlation included' if p['correlated'] else ''}</p>
<form method="post" action="/bets" style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="play" value="{i}">
<input type="hidden" name="generated_at" value="{esc(b['generated_at'])}">
<label>Stake ($)<input class="num" name="stake" id="stake{i}" value="{self._stake(user, p['stake_pct']):.2f}" inputmode="decimal" style="width:120px"></label>
<button class="btn primary" type="submit">Log entry</button></form></div>""" for i, p in enumerate(b["plays"]))
        watch = "".join(f'<div class="alert warn">{esc(w["player"])} {w["side"]} {w["line"]:g} {esc(w["stat"])} ({esc(w["app"])}): '
                        f'{esc(", ".join(w["why"]))}</div>' for w in b.get("watch", []))
        app_f, sport_f = req.query.get("app", ""), req.query.get("sport", "")
        def _edge_chip(e):
            if e is None:
                return '<span class="muted small">no payout table</span>'
            return f'<span class="chip {"good" if e >= 0.01 else "warn" if e >= 0 else "bad"}">{pct(e, 1, True)}</span>'
        legs = [l for l in b["legs"] if (not app_f or l["app"] == app_f) and (not sport_f or l["sport"] == sport_f)][:80]
        legrows = "".join(f"""<tr><td><b>{esc(l['player'])}</b><div class="muted small">{esc(l['sport'].upper())} · {esc(l['stat'])} {l['line']:g} · {esc(l['app'])}</div></td>
<td class="side {l['side']}">{l['side']}</td><td class="r num">{pct(l['fair_market'])}</td><td class="r num">{pct(l['fair_proj'])}</td>
<td class="r num">{pct(l['fair'])}</td><td class="r">{_edge_chip(l['edge'])}</td>
<td class="r num">{l['books']}{'' if l['sharp'] else ' <span class="muted small">no sharp</span>'}</td></tr>""" for l in legs)
        best = "".join(f"""<tr><td><b>{esc(x['player'])}</b><div class="muted small">{esc(x['stat'])}</div></td><td>{esc(x['best_app'])} {x['side']} {x['line']:g}</td>
<td class="r"><span class="chip good">{pct(x['edge'], 1, True)}</span></td><td class="small muted">{esc('; '.join(f"{o['app']} {o['side']} {o['line']:g} ({pct(o['edge'], 1, True)})" for o in x['others']))}</td></tr>"""
                       for x in b.get("best_app", [])[:25])
        apps = sorted({l["app"] for l in b["legs"]})
        sports = sorted({l["sport"] for l in b["legs"]})
        def opts(values, current, label=lambda v: v):
            return "".join(f'<option value="{esc(v)}"{" selected" if v == current else ""}>{esc(label(v))}</option>' for v in values)
        filt = (f'<form method="get" style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">'
                f'<label>App<select name="app" id="fapp"><option value="">All</option>{opts(apps, app_f)}</select></label>'
                f'<label>Sport<select name="sport" id="fsport"><option value="">All</option>{opts(sports, sport_f, str.upper)}</select></label>'
                f'<button class="btn" type="submit">Filter</button></form>')
        body = f"""<div><h1>Today</h1><p class="lead">Updated {esc(tracking.parse_time(b['generated_at']).astimezone(self.cfg.tz).strftime('%a %-I:%M %p'))}.
Stakes use your bankroll of ${user['bankroll']:,.2f} and {user['kelly_mult']:g} Kelly, capped at {pct(user['max_pct'])} per entry.
Recommended total today: <b class="num">${total:,.2f}</b>.</p></div>{stale}{watch}
<h2>Recommended entries</h2>
{f'<div class="cards">{cards}</div>' if cards else '<div class="panel">No entries clear the bar right now. Sitting out is the right call.</div>'}
<p class="muted small" style="margin:0">Recheck each line in the app before entering. If a line moved, this price no longer applies.</p>
<div class="panel"><h2>Every priced leg</h2>{filt}<div class="scroll" style="margin-top:10px"><table><thead><tr><th>Prop</th><th>Pick</th>
<th class="r">Market</th><th class="r">Projection</th><th class="r">Fair</th><th class="r">Edge</th><th class="r">Books</th></tr></thead>
<tbody>{legrows or '<tr><td colspan="7" class="muted">No legs.</td></tr>'}</tbody></table></div></div>
{f'<div class="panel"><h2>Best app for each prop</h2><div class="scroll"><table><thead><tr><th>Prop</th><th>Best</th><th class="r">Edge</th><th>Elsewhere</th></tr></thead><tbody>{best}</tbody></table></div></div>' if best else ''}"""
        return Resp(page("Today", body, NAV_USER, "/board"))

    def bets(self, req, user, sess):
        c = self.conn()
        from .engine import _apps
        apps = _apps(self.cfg)
        msg = ""
        if req.method == "POST":
            if not self.csrf_ok(req, sess):
                return Resp(page("Expired", "<h1>That form expired</h1><p><a href='/board'>Reload the page</a> and try again.</p>", NAV_USER), "403 Forbidden")
            if req.form.get("delete"):
                c.execute("DELETE FROM user_bets WHERE id=? AND user_id=?", (req.form["delete"], user["id"]))
                return redirect("/bets")
            b = meta_get(c, "board") or {}
            try:
                i, stake = int(req.form.get("play", "-1")), float(req.form.get("stake", "0"))
            except ValueError:
                i, stake = -1, 0
            if b.get("generated_at") != req.form.get("generated_at") or not (0 <= i < len(b.get("plays", []))):
                msg = "The board updated before you logged that. Check the new entries and log again."
            elif not (0 < stake <= 1e6):
                msg = "Enter a stake above $0."
            else:
                p = b["plays"][i]
                bid = secrets.token_hex(8)
                legs = [{"player": l["player"], "stat": l["stat"], "line": l["line"], "side": l["side"], "fair": l["fair"],
                         "pick_id": pid} for l, pid in zip(p["legs"], p["pick_ids"])]
                c.execute("""INSERT INTO user_bets(id,user_id,created_at,app,entry,stake,legs,ev,var) VALUES(?,?,?,?,?,?,?,?,?)""",
                          (bid, user["id"], tracking.iso(datetime.now(timezone.utc)), p["app"], p["entry"], stake,
                           json.dumps(legs), p["ev"], 0))
                return redirect("/bets?logged=1")
        # settle from graded picks
        now = datetime.now(timezone.utc)
        for ub in c.execute("SELECT * FROM user_bets WHERE user_id=? AND status='open'", (user["id"],)).fetchall():
            legs = json.loads(ub["legs"])
            res = {r["id"]: r["result"] for r in c.execute(
                f"SELECT id, result FROM picks WHERE id IN ({','.join('?' * len(legs))})", [l["pick_id"] for l in legs])}
            rs = [res.get(l["pick_id"], "") for l in legs]
            if all(r in ("hit", "miss", "push", "void") for r in rs) and ub["app"] in apps:
                ret = tracking.entry_return(apps[ub["app"]], ub["entry"], rs)
                c.execute("UPDATE user_bets SET status='settled', ret=?, profit=?, settled_at=? WHERE id=?",
                          (ret, round(ub["stake"] * (ret - 1), 2), tracking.iso(now), ub["id"]))
        rows = c.execute("SELECT * FROM user_bets WHERE user_id=? ORDER BY created_at DESC LIMIT 300", (user["id"],)).fetchall()
        settled = [r for r in rows if r["status"] == "settled"]
        staked = sum(r["stake"] for r in settled)
        profit = sum(r["profit"] for r in settled)
        exp = sum(r["stake"] * (r["ev"] - 1) for r in settled)
        csrf = sess["csrf"]
        trs = ""
        for r in rows:
            legs = json.loads(r["legs"])
            res = {x["id"]: x["result"] for x in c.execute(
                f"SELECT id, result FROM picks WHERE id IN ({','.join('?' * len(legs))})", [l["pick_id"] for l in legs])}
            legtxt = "; ".join(f"{l['player']} {l['side']} {l['line']:g} ({res.get(l['pick_id']) or 'open'})" for l in legs)
            status = (f'<span class="chip {"good" if r["profit"] > 0 else "bad" if r["profit"] < 0 else "warn"}">${r["profit"]:+,.2f}</span>'
                      if r["status"] == "settled" else '<span class="chip warn">open</span>')
            trs += (f'<tr><td class="small">{esc(r["created_at"][:10])}</td><td><b>{esc(apps.get(r["app"], {}).get("label", r["app"]))} {esc(r["entry"])}</b>'
                    f'<div class="muted small">{esc(legtxt)}</div></td><td class="r num">${r["stake"]:,.2f}</td>'
                    f'<td class="r num">{(r["ev"] - 1) * 100:+.1f}%</td><td>{status}</td><td><form method="post">'
                    f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="delete" value="{esc(r["id"])}">'
                    f'<button class="btn" type="submit" style="min-height:36px;padding:4px 10px">Delete</button></form></td></tr>')
        note = '<div class="alert good">Entry logged.</div>' if req.query.get("logged") else (f'<div class="alert warn">{esc(msg)}</div>' if msg else "")
        body = f"""<h1>My bets</h1>{note}<div class="kpis">
<div class="kpi"><span class="l">Profit</span><span class="v {cls(profit)}">${profit:+,.2f}</span><span class="s">on ${staked:,.2f} staked</span></div>
<div class="kpi"><span class="l">ROI</span><span class="v {cls(profit)}">{pct(profit / staked if staked else None, 1, True)}</span><span class="s">expected {pct(exp / staked if staked else None, 1, True)}</span></div>
<div class="kpi"><span class="l">Luck so far</span><span class="v">${profit - exp:+,.2f}</span><span class="s">actual minus expected</span></div></div>
<div class="panel"><div class="scroll"><table><thead><tr><th>Date</th><th>Entry</th><th class="r">Stake</th><th class="r">EV</th><th>Result</th><th></th></tr></thead>
<tbody>{trs or '<tr><td colspan="6" class="muted">No bets yet. Log entries from Today.</td></tr>'}</tbody></table></div>
<p class="muted small" style="margin:8px 0 0">Bets settle automatically when every leg is graded.</p></div>"""
        return Resp(page("My bets", body, NAV_USER, "/bets"))

    def account(self, req, user, sess):
        c = self.conn()
        msg = ""
        if req.method == "POST":
            if not self.csrf_ok(req, sess):
                return Resp(page("Expired", "<h1>That form expired</h1><p><a href='/account'>Reload</a> and try again.</p>", NAV_USER), "403 Forbidden")
            if req.form.get("new_token"):
                c.execute("UPDATE users SET api_token=? WHERE id=?", (secrets.token_urlsafe(24), user["id"]))
                msg = "New API token created. The old one no longer works."
            else:
                try:
                    bank = float(req.form.get("bankroll", user["bankroll"]))
                    km = float(req.form.get("kelly_mult", user["kelly_mult"]))
                    mx = float(req.form.get("max_pct", user["max_pct"] * 100)) / 100
                    dc = float(req.form.get("day_cap_pct", user["day_cap_pct"] * 100)) / 100
                    assert 0 <= bank <= 1e8 and 0 < km <= 1 and 0 < mx <= 0.25 and 0 < dc <= 1
                    c.execute("UPDATE users SET bankroll=?, kelly_mult=?, max_pct=?, day_cap_pct=? WHERE id=?",
                              (bank, km, mx, dc, user["id"]))
                    msg = "Saved."
                except (ValueError, AssertionError):
                    msg = "Check the numbers: Kelly between 0 and 1, max stake up to 25%."
            user = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        welcome = '<div class="alert good">Account created. Set your bankroll to size your stakes.</div>' if req.query.get("welcome") else ""
        body = f"""<h1>Account</h1>{welcome}{f'<div class="alert good">{esc(msg)}</div>' if msg else ''}
<div class="grid2"><div class="panel"><h2>Bankroll and staking</h2><form class="stack" method="post">
<input type="hidden" name="csrf" value="{esc(sess['csrf'])}">
<label>Bankroll ($)<input class="num" name="bankroll" id="bankroll" value="{user['bankroll']:.2f}" inputmode="decimal"></label>
<label>Kelly fraction<select name="kelly_mult" id="kelly">{''.join(f'<option value="{v}" {"selected" if abs(user["kelly_mult"] - v) < 1e-9 else ""}>{t}</option>' for v, t in ((0.1, "1/10 Kelly"), (0.25, "1/4 Kelly"), (0.5, "1/2 Kelly")))}</select></label>
<label>Max stake per entry (%)<input class="num" name="max_pct" id="maxpct" value="{user['max_pct'] * 100:g}" inputmode="decimal"></label>
<label>Max staked per day (%)<input class="num" name="day_cap_pct" id="daycap" value="{user['day_cap_pct'] * 100:g}" inputmode="decimal"></label>
<button class="btn primary" type="submit">Save</button></form></div>
<div class="panel"><h2>API access</h2><p class="small">Your Edge Finder app or scripts can load the latest board from:</p>
<p class="hash">/api/latest.json?token={esc(user['api_token'])}</p><p class="small muted">Keep this token private.</p>
<form method="post"><input type="hidden" name="csrf" value="{esc(sess['csrf'])}"><input type="hidden" name="new_token" value="1">
<button class="btn" type="submit">Create a new token</button></form>
<form method="post" action="/logout" style="margin-top:16px"><button class="btn" type="submit">Sign out</button></form></div></div>"""
        return Resp(page("Account", body, NAV_USER, "/account"))


def make_app(cfg=None):
    return App(cfg)


class ThreadingServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


def serve(cfg, host="0.0.0.0", port=8080):
    httpd = make_server(host, port, make_app(cfg), server_class=ThreadingServer)
    print(f"Edge Engine web on http://{host}:{port}")
    httpd.serve_forever()
