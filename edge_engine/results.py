"""The public track record: every pick, hash-verified, with calibration, CLV and the model's
paper portfolio. Written as a static page and served by the web app."""
import csv
import io
import json
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


PICKS_SHOWN = 300
RESULT_CHIP = {"hit": ("good", "Hit"), "miss": ("bad", "Miss"), "push": ("warn", "Push"), "void": ("warn", "Void"),
               "unmatched": ("warn", "No stat"), "ungraded": ("warn", "Ungraded")}

PICKS_CSS = """
.pfilters{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
.pfilters button{appearance:none;font:600 .85rem var(--body);border:1px solid var(--line);background:var(--panel2);color:var(--muted);
border-radius:99px;padding:8px 14px;min-height:40px;cursor:pointer}
.pfilters button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.pday{font:700 1.05rem/1.2 var(--display);margin:14px 0 4px;color:var(--muted)}
summary.pday{cursor:pointer;color:var(--ink);padding:6px 0;border-top:1px solid var(--line);margin:8px 0 0}
summary.pday::marker{color:var(--accent)}
.plist{list-style:none;margin:0;padding:0}
.pick{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 12px;padding:10px 0;border-bottom:1px solid var(--line)}
.pick .who{font-weight:600;overflow-wrap:anywhere}.pick .what{font-size:.86rem}.pick .meta{font-size:.78rem;color:var(--muted)}
.pick .right{grid-column:2;grid-row:1 / span 3;text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.pick .fair{font:500 .95rem var(--mono)}
.pempty{display:none;padding:12px 0}
"""

PICKS_JS = """<script>
(function(){
  var root=document.getElementById('picks');if(!root)return;
  var btns=root.querySelectorAll('.pfilters button'),empty=root.querySelector('.pempty');
  function apply(f){
    var shown=0;
    root.querySelectorAll('.pick').forEach(function(li){
      var ok=f==='all'||li.dataset.state===f;li.hidden=!ok;if(ok)shown++;});
    root.querySelectorAll('.pgroup').forEach(function(g){
      g.hidden=!g.querySelector('.pick:not([hidden])');});
    empty.style.display=shown?'none':'block';
    btns.forEach(function(b){b.setAttribute('aria-pressed',b.dataset.f===f?'true':'false');});
    try{localStorage.setItem('picksFilter',f);}catch(e){}
  }
  btns.forEach(function(b){b.addEventListener('click',function(){apply(b.dataset.f);});});
  var saved='all';try{saved=localStorage.getItem('picksFilter')||'all';}catch(e){}
  apply(saved);
})();
</script>"""


def _local(iso, tz, fmt):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    return t.astimezone(tz).strftime(fmt).replace(" 0", " ").lstrip("0")


def _p1(x):
    """Points to one decimal, with no sign on values that round to zero."""
    if x is None:
        return "–"
    v = round(x * 100, 1)
    return "0.0" if v == 0 else f"{'+' if v > 0 else '−'}{abs(v):.1f}"


def _c1(x):
    return "" if x is None or round(x * 100, 1) == 0 else cls(x)


def picks_html(conn, tz, limit=PICKS_SHOWN):
    """Every logged pick, newest games first, grouped by day, with open/graded filters."""
    rows = [dict(r) for r in conn.execute(
        "SELECT seq, model_version, sport, app, player, stat, line, side, tier, home, away, start, fair, edge, clv, result, actual "
        "FROM picks ORDER BY start DESC, seq DESC LIMIT ?", (limit,))]
    total = conn.execute("SELECT COUNT(*) c FROM picks").fetchone()["c"]
    if not rows:
        return '<div class="panel"><h2>Picks</h2><p class="muted">No picks logged yet.</p></div>'
    n_open = sum(not r["result"] for r in rows)
    # newest day first, earliest game first within a day
    def _day(r):
        return _local(r["start"], tz, "%Y-%m-%d")
    rows.sort(key=lambda r: (r["start"] or "", r["seq"]))
    rows.sort(key=_day, reverse=True)
    groups, order = {}, []
    for r in rows:
        day = _local(r["start"], tz, "%A, %b %d") or "Date unknown"
        if day not in groups:
            groups[day] = []
            order.append(day)
        state = "open" if not r["result"] else "graded"
        if r["result"] in RESULT_CHIP:
            c, label = RESULT_CHIP[r["result"]]
            res = f'<span class="chip {c}">{label}{" · " + esc(r["actual"]) if r["actual"] not in (None, "") else ""}</span>'
        else:
            res = f'<span class="chip warn">{esc(r["result"])}</span>' if r["result"] else '<span class="muted small">Pending</span>'
        tier = f' · {esc(r["tier"])}' if r["tier"] else ""
        if r["model_version"] in RETIRED_MODELS:
            tier += f' · <span class="warn" title="{esc(RETIRED_MODELS[r["model_version"]])}">retired model {esc(r["model_version"])}</span>'
        game = f'{esc(r["away"])} @ {esc(r["home"])}' if r["home"] or r["away"] else ""
        when = _local(r["start"], tz, "%I:%M %p")
        clv = f' · CLV <span class="{_c1(r["clv"])}">{_p1(r["clv"])}</span>' if r["clv"] is not None else ""
        groups[day].append(
            f'<li class="pick" data-state="{state}">'
            f'<span class="who">{esc(r["player"])}</span>'
            f'<span class="what"><span class="side {esc(r["side"])}">{esc(r["side"])}</span> {r["line"]:g} {esc(r["stat"])}</span>'
            f'<span class="meta">{esc(r["sport"].upper())} · {esc(r["app"])}{tier} · {game}{" · " + when if when else ""}</span>'
            f'<span class="right"><span class="fair">{pct(r["fair"])}</span>'
            f'<span class="small {_c1(r["edge"])}">edge {_p1(r["edge"])}{clv}</span>{res}</span></li>')
    body = "".join(f'<details class="pgroup"{" open" if i == 0 else ""}><summary class="pday">{esc(day)} '
                   f'<span class="muted small">· {len(groups[day])} {"pick" if len(groups[day]) == 1 else "picks"}</span></summary>'
                   f'<ul class="plist">{"".join(groups[day])}</ul></details>'
                   for i, day in enumerate(order))
    more = (f'<p class="muted small" style="margin:10px 0 0">Showing the latest {len(rows):,} of {total:,} picks. '
            f'The full record is in the CSV below.</p>' if total > len(rows) else "")
    return (f'<style>{PICKS_CSS}</style><div class="panel" id="picks"><h2>Picks</h2>'
            f'<p class="muted small" style="margin:0 0 10px">Fair % is the model\'s chance the pick hits. '
            f'Edge is how far that sits above break-even. Times are {"Central" if str(tz) == "America/Chicago" else esc(str(tz))} time.</p>'
            f'<div class="pfilters" role="group" aria-label="Filter picks">'
            f'<button type="button" data-f="all" aria-pressed="true">All</button>'
            f'<button type="button" data-f="open" aria-pressed="false">Open ({n_open})</button>'
            f'<button type="button" data-f="graded" aria-pressed="false">Graded ({len(rows) - n_open})</button></div>'
            f'{body}<p class="pempty muted">No picks in this view.</p>{more}</div>{PICKS_JS}')


LEG_MARK = {"hit": ("pos", "✓"), "miss": ("neg", "✗"), "push": ("muted", "–"), "void": ("muted", "–")}

FLEX_CSS = """
.fsizes{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:10px;margin:0 0 6px}
.fsize{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:2px}
.fsize b{font:700 1.1rem/1.1 var(--display)}.fsize .v{font:500 1.25rem var(--mono)}.fsize .s{font-size:.78rem;color:var(--muted)}
.fslips{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,320px),1fr));gap:12px}
.fslip{border:1px solid var(--line);border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:8px}
.fslip .top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.fslip ol{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:4px;font-size:.88rem}
.fslip .mk{font-weight:700;margin-left:4px}
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


def flex_html(conn, tz, days=14):
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
    slips = [dict(e) for e in conn.execute("SELECT * FROM flex_slips ORDER BY day DESC, n, seq")]
    shown_days = sorted({e["day"] for e in slips}, reverse=True)[:days]
    ids = sorted({i for e in slips if e["day"] in shown_days for i in json.loads(e["pick_ids"])})
    legs = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(f"SELECT id, player, stat, line, side, fair, result, actual FROM picks "
                              f"WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            legs[r["id"]] = dict(r)
    blocks = []
    for day in shown_days:
        cards_d = []
        for e in (x for x in slips if x["day"] == day):
            lis = []
            for pid in json.loads(e["pick_ids"]):
                l = legs.get(pid)
                if not l:
                    continue
                c, m = LEG_MARK.get(l["result"], ("muted", ""))
                act = f' <span class="muted small">({l["actual"]:g})</span>' if l["actual"] is not None else ""
                lis.append(f'<li><span class="side {esc(l["side"])}">{esc(l["side"])}</span> {esc(l["player"])} '
                           f'<span class="muted">{l["line"]:g} {esc(l["stat"])}</span>{act}<span class="mk {c}">{m}</span></li>')
            if e["status"] == "open":
                chip = '<span class="chip warn">Open</span>'
            elif e["status"] == "void":
                chip = '<span class="chip warn">Void · refunded</span>'
            else:
                chip = (f'<span class="chip {"good" if e["ret"] >= 1 else "bad"}">{e["hits"]}/{e["n"]} · '
                        f'{e["ret"]:g}x</span>')
            cards_d.append(f'<div class="fslip"><div class="top"><b>{esc(e["entry"])}</b>{chip}</div><ol>{"".join(lis)}</ol>'
                           f'<span class="muted small">Expected return {pct(e["ev"] - 1, 1, True)} · '
                           f'stake back {pct(e["cash"], 0)} of the time</span></div>')
        label = _local(day + "T12:00:00+00:00", tz, "%A, %b %d")
        blocks.append(f'<h3 class="pday">{esc(label)}</h3><div class="fslips">{"".join(cards_d)}</div>')
    return (f'<style>{FLEX_CSS}</style><div class="panel" id="flex"><h2>Flex plays</h2>'
            f'<p class="muted small" style="margin:0 0 10px">Each day the engine locks in its best Flex slip of each size before '
            f'the games and never changes it. Results count each slip as 1 unit, so you can see which size works best.</p>'
            f'<div class="fsizes">{cards}</div>{"".join(blocks)}</div>')


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


def _bet_label(x):
    from .teams import MARKET_NAME
    pt = x["point"]
    if pt is None or x["market"] == "h2h":
        tail = ""
    elif x["market"] == "spreads":
        tail = f" {pt:+g}"
    else:
        tail = f" {pt:g}"
    return f'{MARKET_NAME.get(x["market"], x["market"])}: <b>{esc(x["outcome"])}{tail}</b>'


def _team_row(x, tz):
    odds = f'{x["odds"]:+d}' if x["odds"] is not None else "–"
    chip = "good" if x["result"] == "win" else "bad" if x["result"] == "loss" else "warn"
    score = f' <span class="muted small">{x["away_score"]:g}-{x["home_score"]:g}</span>' if x["home_score"] is not None else ""
    return (f'<tr><td class="small muted">{esc(_local(x["start"], tz, "%b %d"))}</td><td>{esc(x["away"])} @ {esc(x["home"])}</td>'
            f'<td>{_bet_label(x)}</td><td class="num">{esc(x["book"])} {odds}</td><td class="r num">{pct(x["ev"], 1, True)}</td>'
            f'<td class="r"><span class="chip {chip}">{esc(x["result"])}</span>{score}</td></tr>')


def teams_html(conn, tz):
    from . import teams
    r = teams.record(conn)
    if not (r["won"] + r["lost"] + r["push"]) and not r["open"]:
        return ""
    rec = f'{r["won"]}-{r["lost"]}' + (f'-{r["push"]}' if r["push"] else "")
    units = f'<span class="{cls(r["units"])}">{r["units"]:+.2f}</span>'
    roi = f'<span class="{cls(r["roi"])}">{pct(r["roi"], 1, True)}</span>'
    rows = "".join(_team_row(x, tz) for x in r["recent"][:25])
    table = (f'<div class="scroll" style="margin-top:12px"><table><thead><tr><th>Day</th><th>Game</th><th>Bet</th><th>Price</th>'
             f'<th class="r">EV</th><th class="r">Result</th></tr></thead><tbody>{rows}</tbody></table></div>' if rows
             else '<p class="muted small" style="margin:10px 0 0">Results show here once the first games finish.</p>')
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
{picks_html(conn, tz)}
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
<script>if(/[?&]embed=1/.test(location.search))document.documentElement.classList.add("embed")</script>"""
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
