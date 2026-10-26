"""The public track record: every pick, hash-verified, with calibration, CLV and the model's
paper portfolio. Written as a static page and served by the web app."""
import csv
import io
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import RETIRED_MODELS, backtest, tracking
from .ui import calibration_chart, cls, esc, line_chart, page, pct, pts

def _current_sql():
    """SQL condition for picks from models still in use."""
    if not RETIRED_MODELS:
        return "1=1"
    return "model_version NOT IN (" + ",".join("'" + v.replace("'", "''") + "'" for v in RETIRED_MODELS) + ")"


BUCKETS = [(.50, .54, "50–54%"), (.54, .57, "54–57%"), (.57, .60, "57–60%"), (.60, .65, "60–65%"), (.65, 1.01, "65%+")]


def _summary(rows):
    g = [r for r in rows if r["result"] in ("hit", "miss")]
    c = [r["clv"] for r in rows if r["clv"] is not None and r["result"] != ""]
    n = len(g)
    hits = sum(r["result"] == "hit" for r in g)
    exp = sum(r["fair"] for r in g)
    var = sum(r["fair"] * (1 - r["fair"]) for r in g)
    return {"n": n, "hits": hits, "hit": hits / n if n else None, "exp": exp / n if n else None,
            "z": (hits - exp) / var ** 0.5 if var > 0 else None, "clv_n": len(c),
            "clv": sum(c) / len(c) if c else None, "beat": sum(x > 0 for x in c) / len(c) if c else None}


def data(conn):
    every = [dict(r) for r in conn.execute("SELECT * FROM picks ORDER BY seq")]
    rows = [r for r in every if r["model_version"] not in RETIRED_MODELS]
    models = {}
    for r in every:
        models.setdefault(r["model_version"] or "unknown", []).append(r)
    ok, n, bad = tracking.verify_chain(conn)
    seq, head = tracking.chain_head(conn)
    buckets = []
    graded = [r for r in rows if r["result"] in ("hit", "miss")]
    for lo, hi, label in BUCKETS:
        b = [r for r in graded if lo <= r["fair"] < hi]
        buckets.append({"label": label, "n": len(b), "hit": sum(r["result"] == "hit" for r in b) / len(b) if b else None,
                        "exp": sum(r["fair"] for r in b) / len(b) if b else None})
    groups = {}
    for r in rows:
        for key in (("Sport", r["sport"].upper()), ("App", r["app"]), ("Stat", r["stat"])):
            groups.setdefault(key, []).append(r)
    ents = [dict(e) for e in conn.execute("SELECT * FROM system_entries WHERE status='settled' ORDER BY settled_at")]
    idx, curve, staked, profit = 100.0, [100.0], 0.0, 0.0
    for e in ents:
        idx *= 1 + e["stake_pct"] * (e["ret"] - 1)
        curve.append(idx)
        staked += e["stake_pct"]
        profit += e["stake_pct"] * (e["ret"] - 1)
    exp_profit = sum(e["stake_pct"] * (e["ev"] - 1) for e in ents)
    return {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "chain": {"ok": ok, "count": n, "first_bad": bad, "head_seq": seq, "head_hash": head},
            "all": _summary(rows), "training": _summary([r for r in rows if not r["holdout"]]),
            "holdout": _summary([r for r in rows if r["holdout"]]), "buckets": buckets,
            "models": [{"version": v, "picks": len(rs), "retired": RETIRED_MODELS.get(v, ""), **_summary(rs)}
                       for v, rs in sorted(models.items(), key=lambda kv: min(r["seq"] for r in kv[1]))],
            "current_picks": len(rows),
            "groups": {f"{k[0]}|{k[1]}": _summary(v) for k, v in groups.items()},
            "portfolio": {"entries": len(ents), "index": curve, "staked_pct": staked, "profit_pct": profit,
                          "expected_profit_pct": exp_profit,
                          "open": conn.execute("SELECT COUNT(*) c FROM system_entries WHERE status='open'").fetchone()["c"]},
            "backtest": backtest.evaluate(conn, 1)}


def csv_text(conn):
    buf = io.StringIO()
    cols = ["seq", "id", "created_at", "model_version", "sport", "game_id", "app", "player", "market", "stat", "line",
            "side", "tier", "start", "fair_market", "fair_proj", "fair", "edge", "holdout", "close_fair", "clv", "result",
            "actual", "prev_hash", "hash"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in conn.execute(f"SELECT {', '.join(cols)} FROM picks ORDER BY seq"):
        w.writerow([r[c] for c in cols])
    return buf.getvalue()


def _kpi(l, v, s):
    return f'<div class="kpi"><span class="l">{l}</span><span class="v">{v}</span><span class="s">{s}</span></div>'


def _local(iso, tz, fmt):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    return t.astimezone(tz).strftime(fmt).replace(" 0", " ").lstrip("0")


def _more_link(tab, text):
    return (f'<p style="margin:12px 0 0"><a class="btn xlink" href="picks.html#{tab}">{esc(text)} →</a></p>')


def picks_summary_html(conn):
    """A short panel pointing to picks.html, where every prop, team bet and entry is listed and searchable."""
    from .teams import ensure
    ensure(conn)
    n = conn.execute("SELECT COUNT(*) c, SUM(result='') o FROM picks").fetchone()
    t = conn.execute("SELECT COUNT(*) FROM team_plays").fetchone()[0]
    e = (conn.execute("SELECT COUNT(*) FROM flex_slips").fetchone()[0]
         + conn.execute("SELECT COUNT(*) FROM system_entries").fetchone()[0])
    btn = lambda tab, label, k: f'<a class="btn xlink" href="picks.html#{tab}">{label} <span class="num muted">{k:,}</span></a>'
    return (f'<div class="panel" id="picks"><h2>Every pick</h2><p class="muted small" style="margin:0 0 12px">'
            f'All {n["c"]:,} player props ({n["o"] or 0:,} still open), every team bet and every suggested entry are on their own '
            f'page, searchable by date, team and sport.</p><div style="display:flex;gap:8px;flex-wrap:wrap">'
            f'{btn("props", "Props", n["c"])}{btn("teams", "Team bets", t)}{btn("entries", "Suggested entries", e)}</div></div>')


FLEX_CSS = """
.fsizes{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:10px;margin:0 0 6px}
.fsize{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:2px}
.fsize b{font:700 1.1rem/1.1 var(--display)}.fsize .v{font:500 1.25rem var(--mono)}.fsize .s{font-size:.78rem;color:var(--muted)}
"""


def flex_data(conn):
    """Record for each Flex size, at a flat 1 unit per slip."""
    out = {}
    for e in conn.execute("SELECT * FROM flex_slips ORDER BY seq"):
        s = out.setdefault(e["entry"], {"entry": e["entry"], "n": e["n"], "slips": 0, "open": 0, "settled": 0,
                                        "cashed": 0, "returned": 0.0, "expected": 0.0, "won": 0, "hits": 0})
        s["slips"] += 1
        if e["status"] == "open":
            s["open"] += 1
        elif e["status"] == "settled":
            s["settled"] += 1
            s["returned"] += e["ret"]
            s["expected"] += e["ev"]
            s["cashed"] += e["ret"] >= 1
            s["won"] += e["ret"] > 1
            s["hits"] += e["hits"] or 0
    for s in out.values():
        k = s["settled"]
        s["profit"] = s["returned"] - k
        s["exp_profit"] = s["expected"] - k
        s["roi"] = s["profit"] / k if k else None
        s["cash_rate"] = s["cashed"] / k if k else None
        s["leg_rate"] = s["hits"] / (k * s["n"]) if k else None
    return sorted(out.values(), key=lambda s: s["n"])


def flex_html(conn, tz):
    sizes = flex_data(conn)
    if not sizes:
        return ('<div class="panel"><h2>Flex plays</h2><p class="muted">Each day the engine locks in its best 3, 4, 5 and 6-pick '
                'Flex slip and tracks how each size does. The first ones appear after the next run that finds a positive slip.</p></div>')
    def units(x, settled):
        return f"{x:+.2f}u" if settled else '<span class="muted" style="font-size:.9rem">No results yet</span>'

    def exp_units(s):
        return f"{s['exp_profit']:+.2f}u" if s["settled"] else "–"

    cards = "".join(
        f'<div class="fsize"><b>{esc(s["entry"])}</b>'
        f'<span class="v {cls(s["profit"]) if s["settled"] else ""}">{units(s["profit"], s["settled"])}</span>'
        f'<span class="s">{s["settled"]} settled · {s["open"]} open</span>'
        f'<span class="s">Cashed {pct(s["cash_rate"], 0)} · ROI {pct(s["roi"], 1, True)}</span>'
        f'<span class="s">Expected {exp_units(s)}</span></div>'
        for s in sizes)
    return (f'<style>{FLEX_CSS}</style><div class="panel" id="flex"><h2>Flex plays</h2>'
            f'<p class="muted small" style="margin:0 0 10px">Each day the engine locks in its best Flex slip of each size before '
            f'the games and never changes it. Results count each slip as 1 unit, so you can see which size works best.</p>'
            f'<div class="fsizes">{cards}</div>{_more_link("entries", "See every daily slip")}</div>')


def daily(conn, tz, days=30):
    """Per game day: picks graded, hits vs expected, and average closing line value."""
    out = {}
    for r in conn.execute(f"SELECT start, fair, clv, result FROM picks WHERE {_current_sql()}"):
        day = _local(r["start"], tz, "%Y-%m-%d")
        if not day:
            continue
        g = out.setdefault(day, {"day": day, "n": 0, "hits": 0, "exp": 0.0, "clv": [], "picks": 0})
        g["picks"] += 1
        if r["result"] in ("hit", "miss"):
            g["n"] += 1
            g["hits"] += r["result"] == "hit"
            g["exp"] += r["fair"]
        if r["clv"] is not None and r["result"]:
            g["clv"].append(r["clv"])
    rows = sorted(out.values(), key=lambda g: g["day"], reverse=True)[:days]
    for g in rows:
        g["clv"] = sum(g["clv"]) / len(g["clv"]) if g["clv"] else None
    return rows


def periods(conn, now=None):
    """Accuracy over the last 7 days, 30 days and all time: every graded pick counts, misses included."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    out = []
    for label, days in (("Last 7 days", 7), ("Last 30 days", 30), ("All time", None)):
        q, args = f"SELECT fair, clv, result FROM picks WHERE result IN ('hit','miss') AND {_current_sql()}", []
        if days:
            q += " AND start >= ?"
            args.append((now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        rows = conn.execute(q, args).fetchall()
        n = len(rows)
        hits = sum(r["result"] == "hit" for r in rows)
        brier = sum((r["fair"] - (r["result"] == "hit")) ** 2 for r in rows) / n if n else None
        c = [r["clv"] for r in rows if r["clv"] is not None]
        out.append({"label": label, "n": n, "hit": hits / n if n else None, "exp": sum(r["fair"] for r in rows) / n if n else None,
                    "brier": brier, "clv": sum(c) / len(c) if c else None})
    return out


def _brier(g):
    return "–" if g["brier"] is None else f"{g['brier']:.3f}"


def freshness_html(conn, tz):
    from .export import freshness
    f = freshness(conn)
    when = lambda x: esc(_local(x, tz, "%a %b %d, %I:%M %p")) if x and "T" in str(x) else esc(x or "not yet")
    models = ", ".join(f'{esc(m["version"])} ({m["picks"]:,} picks)' for m in f["models"]) or "none yet"
    return f"""<div class="panel"><h2>Model and data</h2><div class="scroll"><table style="min-width:0"><tbody>
<tr><td>Model versions</td><td>{models}</td></tr>
<tr><td>Sportsbook prices last checked</td><td>{when(f["odds"])}</td></tr>
<tr><td>Injury report last checked</td><td>{when(f["injuries"])}</td></tr>
<tr><td>Rosters last checked</td><td>{when(f["rosters"])}</td></tr>
<tr><td>Box scores loaded through</td><td>{when(f["boxscores"])}</td></tr></tbody></table></div>
<p class="muted small" style="margin:8px 0 0">Every pick is published before its game with the model version that made it. Wins and losses
are both counted; nothing is removed after the fact.</p></div>"""


def clv_curve(conn):
    """Running average CLV across finished picks, in start order."""
    vals, out = [], []
    for r in conn.execute(f"SELECT clv FROM picks WHERE clv IS NOT NULL AND result != '' AND {_current_sql()} ORDER BY start, seq"):
        vals.append(r["clv"] * 100)
        out.append(sum(vals) / len(vals))
    return out


def teams_html(conn, tz):
    from . import teams
    r = teams.record(conn)
    if not (r["won"] + r["lost"] + r["push"]) and not r["open"]:
        return ""
    rec = f'{r["won"]}-{r["lost"]}' + (f'-{r["push"]}' if r["push"] else "")
    units = f'<span class="{cls(r["units"])}">{r["units"]:+.2f}</span>'
    roi = f'<span class="{cls(r["roi"])}">{pct(r["roi"], 1, True)}</span>'
    table = _more_link("teams", "See every team bet")
    return (f'<div class="panel" id="teams"><h2>Team bets</h2><p class="muted small" style="margin:0 0 10px">Moneylines, spreads '
            f"and totals where a sportsbook paid more than the sharp books' fair price. Every one is logged when first found and "
            f'graded from the final score at a flat 1 unit, wins and losses alike.</p>'
            f'<div class="kpis">{_kpi("Record", rec, str(r["open"]) + " open")}'
            f'{_kpi("Units", units, "Expected " + format(r["exp_units"], "+.2f"))}{_kpi("ROI", roi, "per unit staked")}</div>'
            f'{table}</div>')


HIT_RATE_TARGET = 1000   # graded legs before hit rate can tell a ~3-point edge from luck (95% margin about ±3 pts)
CLV_TARGET = 150         # finished legs before average closing line value is a useful early read


def sample_html(conn, d):
    """How close the record is to telling anything: plain progress bars, current model only."""
    from .blend import MIN_PICKS
    a = d["all"]
    tune = [(r["sport"], r["n"]) for r in conn.execute(
        """SELECT sport, COUNT(*) n FROM picks WHERE holdout=0 AND result IN ('hit','miss')
           AND fair_proj IS NOT NULL AND fair_market IS NOT NULL GROUP BY sport ORDER BY n DESC""")]
    best_sport, best_n = tune[0] if tune else ("", 0)

    def bar(label, have, need, note):
        share = min(have / need, 1) if need else 0
        return (f'<div style="display:flex;flex-direction:column;gap:4px"><div style="display:flex;justify-content:space-between;gap:8px">'
                f'<b class="small">{esc(label)}</b><span class="num small">{have:,} of {need:,}</span></div>'
                f'<div role="progressbar" aria-valuemin="0" aria-valuemax="{need}" aria-valuenow="{have}" aria-label="{esc(label)}" '
                f'style="height:8px;border-radius:4px;background:var(--panel2);overflow:hidden">'
                f'<div style="width:{share * 100:.1f}%;height:100%;background:{"var(--accent)" if share >= 1 else "var(--gold)"}"></div></div>'
                f'<span class="muted small">{note}</span></div>')

    rows = [bar("Early read: closing line value", a["clv_n"], CLV_TARGET,
                "Finished legs with a closing price. Steady positive CLV over this many legs is the first real sign of an edge."),
            bar("Model starts tuning itself" + (f" ({best_sport.upper()})" if best_sport else ""), best_n, MIN_PICKS,
                "Graded training legs in the busiest sport. Until then the price is 85% market, 15% projection."),
            bar("Hit rate means something", a["n"], HIT_RATE_TARGET,
                "Graded legs from the current model. Below this, a hot or cold stretch is mostly luck.")]
    return ('<div class="panel" style="display:flex;flex-direction:column;gap:14px"><h2 style="margin:0">How much is proven so far</h2>'
            + "".join(rows) + "</div>")


def html(conn, nav=None, tz=None):
    tz = tz or ZoneInfo("America/Chicago")
    d = data(conn)
    a, h, t, p = d["all"], d["holdout"], d["training"], d["portfolio"]
    ch = d["chain"]
    integrity = (f'<div class="alert good"><b>Record verified.</b> All {ch["count"]:,} picks check out against the hash chain.</div>'
                 if ch["ok"] else f'<div class="alert bad"><b>Record check failed</b> at pick {ch["first_bad"]}. '
                 f'An earlier pick was changed or removed.</div>')
    comp = "".join(f'<tr><td>{label}</td><td class="r num">{s["n"]:,}</td><td class="r num">{pct(s["hit"])}</td>'
                   f'<td class="r num">{pct(s["exp"])}</td><td class="r num {cls((s["hit"] or 0) - (s["exp"] or 0)) if s["n"] else ""}">'
                   f'{pct((s["hit"] - s["exp"]) if s["n"] else None, 1, True)}</td>'
                   f'<td class="r num {cls(s["clv"])}">{pts(s["clv"])}</td><td class="r num">{pct(s["beat"], 0)}</td></tr>'
                   for label, s in (("All picks", a), ("Training picks", t), ("Holdout picks", h)))
    def _mrow(m):
        badge = '<span class="chip warn">Retired</span>' if m["retired"] else '<span class="chip good">Current</span>'
        why = f'<br><span class="muted small">{esc(m["retired"])}</span>' if m["retired"] else ""
        return (f'<tr><td>{esc(m["version"])} {badge}{why}</td><td class="r num">{m["picks"]:,}</td>'
                f'<td class="r num">{m["n"]:,}</td><td class="r num">{pct(m["hit"])}</td><td class="r num">{pct(m["exp"])}</td>'
                f'<td class="r num {cls(m["clv"])}">{pts(m["clv"])}</td></tr>')
    mrows = "".join(_mrow(m) for m in d["models"])
    retired_n = ch["count"] - d["current_picks"]
    retired_note = (f'<div class="alert warn"><b>Headline numbers count the current model only.</b> {retired_n:,} earlier '
                    f'picks came from a retired model version with a pricing bug. They stay in the record and the hash chain, '
                    f'and their results are listed by version below.</div>' if retired_n else "")
    groups = sorted(d["groups"].items(), key=lambda kv: (kv[0].split("|")[0], -kv[1]["n"]))
    grows = "".join(f'<tr><td class="muted small">{esc(k.split("|")[0])}</td><td>{esc(k.split("|")[1])}</td>'
                    f'<td class="r num">{s["n"]:,}</td><td class="r num">{pct(s["hit"])}</td><td class="r num">{pct(s["exp"])}</td>'
                    f'<td class="r num {cls(s["clv"])}">{pts(s["clv"])}</td></tr>' for k, s in groups if s["n"] or s["clv_n"])
    bt = d["backtest"]
    bt_html = ""
    if bt["all"]["market"]:
        rows = "".join(f'<tr><td>{esc(name)}</td><td class="r num">{m["n"]:,}</td><td class="r num">{m["log_loss"]:.4f}</td>'
                       f'<td class="r num">{m["brier"]:.4f}</td></tr>'
                       for name, m in (("Market price", bt["all"]["market"]), ("Projection", bt["with_projection"]["projection"]),
                                       ("Final blended price", bt["all"]["final"])) if m)
        bt_html = (f'<div class="panel"><h2>Holdout test</h2><p class="muted small" style="margin:0 0 8px">One in five picks is set aside '
                   f'by its hash and never used to fit the models. Lower log loss and Brier score mean better probabilities. '
                   f'{esc(bt.get("verdict", "").capitalize())}.</p><div class="scroll"><table><thead><tr><th>Price</th><th class="r">Legs</th>'
                   f'<th class="r">Log loss</th><th class="r">Brier</th></tr></thead><tbody>{rows}</tbody></table></div></div>')
    body = f"""
<div><h1>Track record</h1><p class="muted small" style="margin:4px 0 0">Updated {esc(_local(d["generated_at"], tz, "%a %b %d, %I:%M %p"))} Central time</p><p class="lead">Every pick the model makes is logged the moment it's made, before the game,
and never edited afterward. Each pick's hash includes the one before it, so changing or deleting any old pick breaks
every hash after it. Anyone can download the full log and check it.</p></div>
{integrity}
{retired_note}
<div class="kpis">
{_kpi("Picks logged", f'{ch["count"]:,}', f'{a["n"]:,} graded' + (f' · {d["current_picks"]:,} by the current model' if retired_n else ''))}
{_kpi("Hit rate", pct(a["hit"]), f'Expected {pct(a["exp"])}')}
{_kpi("Closing line value", f'<span class="{cls(a["clv"])}">{pts(a["clv"])} pts</span>', f'Beat the close {pct(a["beat"], 0)}')}
{_kpi("Model portfolio", f'<span class="{cls(p["profit_pct"])}">{p["index"][-1] - 100:+.1f}%</span>', f'{p["entries"]} entries settled, {p["open"]} open')}
</div>
{sample_html(conn, d)}
{flex_html(conn, tz)}
{teams_html(conn, tz)}
{picks_summary_html(conn)}
<div class="panel"><h2>Results by set</h2><div class="scroll"><table><thead><tr><th>Set</th><th class="r">Graded</th>
<th class="r">Hit rate</th><th class="r">Expected</th><th class="r">Difference</th><th class="r">CLV (pts)</th><th class="r">Beat close</th>
</tr></thead><tbody>{comp}</tbody></table></div>
<p class="muted small" style="margin:8px 0 0">Closing line value compares the sharp-market price when the pick was made with the
last price before the game. It's the fastest honest test of an edge: consistently positive CLV means the market moved toward the
picks.</p></div>
<div class="panel"><h2>Results by model version</h2><div class="scroll"><table><thead><tr><th>Model</th><th class="r">Picks</th>
<th class="r">Graded</th><th class="r">Hit rate</th><th class="r">Expected</th><th class="r">CLV (pts)</th></tr></thead><tbody>{mrows}</tbody></table></div></div>
<div class="grid2">
<div class="panel"><h2>Calibration</h2><p class="muted small" style="margin:0 0 8px">Each dot is a group of picks by fair %.
On the dashed line, picks hit exactly as often as predicted. Bars show the 95% range for that sample size.</p>
{calibration_chart(d["buckets"])}</div>
<div class="panel"><h2>Model portfolio</h2><p class="muted small" style="margin:0 0 8px">The entries the model recommended,
at the stakes it recommended, starting from 100. Staked {pct(p["staked_pct"])} of bankroll in total; expected profit
{pct(p["expected_profit_pct"], 1, True)}, actual {pct(p["profit_pct"], 1, True)}.</p>
{line_chart(p["index"], lambda v: f"{v:.0f}", "Model portfolio value, start 100", "Now", baseline=100)}</div>
</div>
{bt_html}
<div class="grid2">
<div class="panel"><h2>Accuracy over time</h2><div class="scroll"><table style="min-width:0"><thead><tr><th>Period</th><th class="r">Graded</th>
<th class="r">Hit rate</th><th class="r">Expected</th><th class="r">Brier</th><th class="r">CLV</th></tr></thead><tbody>
{"".join(f'<tr><td>{g["label"]}</td><td class="r num">{g["n"]:,}</td><td class="r num">{pct(g["hit"])}</td><td class="r num">{pct(g["exp"])}</td>'
         f'<td class="r num">{_brier(g)}</td><td class="r num {cls(g["clv"])}">{pts(g["clv"], 1)}</td></tr>' for g in periods(conn))}
</tbody></table></div><p class="muted small" style="margin:8px 0 0">Brier score: lower is better, and 0.250 is what a coin flip would score.
Hit rate should land near expected if the probabilities are right.</p></div>
{freshness_html(conn, tz)}
</div>
<div class="grid2">
<div class="panel"><h2>Closing line value over time</h2><p class="muted small" style="margin:0 0 8px">Running average, in points, across
every finished pick. Staying above zero means the sharp market keeps moving toward the picks after they're made.</p>
{line_chart(clv_curve(conn), lambda v: f"{v:+.1f}", "Running average closing line value", "Avg", baseline=0)}</div>
<div class="panel"><h2>Day by day</h2><div class="scroll"><table style="min-width:0"><thead><tr><th>Game day</th><th class="r">Graded</th>
<th class="r">Hit rate</th><th class="r">Expected</th><th class="r">CLV</th></tr></thead><tbody>
{"".join(f'<tr><td>{esc(_local(g["day"] + "T12:00:00+00:00", tz, "%a %b %d"))}</td><td class="r num">{g["n"]}<span class="muted small"> of {g["picks"]}</span></td>'
         f'<td class="r num">{pct(g["hits"] / g["n"]) if g["n"] else "–"}</td><td class="r num">{pct(g["exp"] / g["n"]) if g["n"] else "–"}</td>'
         f'<td class="r num {cls(g["clv"])}">{pts(g["clv"], 1)}</td></tr>' for g in daily(conn, tz)) or '<tr><td colspan="5" class="muted">No picks yet.</td></tr>'}
</tbody></table></div></div>
</div>
<div class="panel"><h2>By sport, app and stat</h2><div class="scroll"><table><thead><tr><th>Group</th><th></th><th class="r">Graded</th>
<th class="r">Hit rate</th><th class="r">Expected</th><th class="r">CLV (pts)</th></tr></thead><tbody>{grows or '<tr><td colspan="6" class="muted">No picks yet.</td></tr>'}</tbody></table></div></div>
<div class="panel"><h2>Check it yourself</h2><p class="small" style="margin:0 0 6px">Latest pick #{ch["head_seq"]:,}, chain hash:</p>
<p class="hash">{esc(ch["head_hash"])}</p>
<ol class="small" style="margin:8px 0 0;padding-left:20px;display:flex;flex-direction:column;gap:4px">
<li><a href="picks.csv">Download every pick (CSV)</a>. Each pick was written before its game started, with its fair price at that moment.</li>
<li>Each row's hash is SHA-256 of the previous row's hash plus that pick's fields, so changing or removing any old pick breaks every hash after it.</li>
<li>Run <span class="num">edge verify --csv picks.csv</span> from the open-source engine, or recompute the hashes yourself, and compare the last one with the hash above.</li>
<li>Copies of this hash posted publicly over time pin the record down, so it can't be rebuilt from scratch either.</li></ol></div>"""
    embed = """<style>html.embed header.top,html.embed footer,html.embed .app-link{display:none}html.embed main{padding-top:0}</style>
<script>if(/[?&]embed=1/.test(location.search)){document.documentElement.classList.add("embed");
addEventListener("DOMContentLoaded",function(){document.querySelectorAll("a.xlink").forEach(function(a){a.target="_blank";a.rel="noopener"})})}</script>"""
    return page("Edge Finder · Track record", embed + body, nav, "/results",
                action='<a class="btn primary app-link" href="app.html">Open the app</a>')


def write(conn, cfg, path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html(conn, tz=cfg.tz))
    with open(os.path.join(d or ".", "picks.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(csv_text(conn))
    from . import allpicks
    with open(os.path.join(d or ".", "picks.html"), "w", encoding="utf-8") as f:
        f.write(allpicks.html(conn, tz=cfg.tz))
