"""Shared page shell and styles for the public results page and the web app."""
import html
import urllib.parse

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#10161F"/>'
            '<polyline points="96,360 196,330 276,348 380,199" fill="none" stroke="#E8B23A" stroke-width="58" stroke-linecap="round" stroke-linejoin="round"/>'
            '<polygon points="416,148 413,244 327,184" fill="#E8B23A" stroke="#E8B23A" stroke-width="28" stroke-linejoin="round"/>'
            '<polyline points="96,360 196,330 276,348 380,199" fill="none" stroke="#34D27B" stroke-width="40" stroke-linecap="round" stroke-linejoin="round"/>'
            '<polygon points="416,148 413,244 327,184" fill="#34D27B" stroke="#34D27B" stroke-width="10" stroke-linejoin="round"/></svg>')
FAVICON = "data:image/svg+xml," + urllib.parse.quote(ICON_SVG)

CSS = """
:root{--bg:#F2F4F1;--panel:#FFFFFF;--panel2:#F6F8F5;--line:#DCE1DA;--ink:#121A16;--muted:#56615A;
--accent:#1F8A50;--on-accent:#FFFFFF;--gold:#8F6300;--good:#1B7A47;--good-bg:#E2F3E8;--warn:#8A5A00;--warn-bg:#FCF0D4;
--bad:#B3261E;--bad-bg:#FBE7E5;--s1:#1F8A50;--s2:#3B6FD1;--grid:#E6EAE4;
--display:"Barlow Condensed","Arial Narrow",system-ui,sans-serif;--body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0E141B;--panel:#151D27;--panel2:#1B2531;--line:#2A3645;--ink:#E8EDF2;--muted:#9AA6B4;
--accent:#34D27B;--on-accent:#0B1A12;--gold:#E8B23A;--good:#5FD197;--good-bg:#14301F;--warn:#E8B45C;--warn-bg:#33270F;
--bad:#F2867B;--bad-bg:#3A1C19;--s1:#25A15E;--s2:#5A8BE6;--grid:#223040;color-scheme:dark}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:15px/1.5 var(--body)}a{color:var(--accent)}:focus-visible{outline:2px solid var(--s2);outline-offset:2px}
.wrap{max-width:1180px;margin:0 auto;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
header.top{background:var(--panel);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:12px;padding-block:12px;flex-wrap:wrap}
.brand img{width:36px;height:36px;border-radius:9px}.brand b{font:700 1.6rem/1 var(--display)}
nav.links{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}
nav.links a{color:var(--muted);text-decoration:none;font-weight:600;padding:10px 12px;border-radius:8px;min-height:44px;display:inline-flex;align-items:center}
nav.links a[aria-current="page"]{color:var(--ink);background:var(--panel2)}
main{padding-block:24px 48px;display:flex;flex-direction:column;gap:20px}
h1{font:700 2.2rem/1.05 var(--display);margin:0;text-wrap:balance}h2{font:700 1.45rem/1.1 var(--display);margin:0 0 8px}
h3{font:700 1.15rem/1.15 var(--display);margin:0 0 6px}
.lead{color:var(--muted);max-width:72ch;margin:6px 0 0}.muted{color:var(--muted)}.small{font-size:.84rem}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}.pos{color:var(--good)}.neg{color:var(--bad)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,170px),1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.kpi{background:var(--panel);padding:12px 14px;display:flex;flex-direction:column;gap:2px}
.kpi .l{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.kpi .v{font:500 1.4rem var(--mono);font-variant-numeric:tabular-nums}.kpi .s{font-size:.78rem;color:var(--muted)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}.scroll table{min-width:600px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.chip{display:inline-block;font:600 .75rem var(--mono);padding:2px 8px;border-radius:99px;white-space:nowrap}
.chip.good{background:var(--good-bg);color:var(--good)}.chip.bad{background:var(--bad-bg);color:var(--bad)}.chip.warn{background:var(--warn-bg);color:var(--warn)}
.side{font-weight:600}.MORE{color:var(--good)}.LESS{color:var(--s2)}
.btn{appearance:none;font:600 .9rem var(--body);border-radius:8px;padding:10px 14px;min-height:44px;cursor:pointer;border:1px solid var(--line);background:var(--panel2);color:var(--ink);text-decoration:none;display:inline-flex;align-items:center;gap:6px}
.btn.primary{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
form.stack{display:flex;flex-direction:column;gap:12px;max-width:420px}
label{display:flex;flex-direction:column;gap:4px;font-size:.85rem;color:var(--muted);font-weight:500}
input,select{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-height:44px}
.alert{border-radius:8px;padding:10px 12px}.alert.warn{background:var(--warn-bg);color:var(--warn)}.alert.bad{background:var(--bad-bg);color:var(--bad)}.alert.good{background:var(--good-bg);color:var(--good)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,330px),1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:10px}
.legs{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}.legs li{font-size:.9rem}
.hash{font:.78rem var(--mono);word-break:break-all;color:var(--muted)}
footer{color:var(--muted);font-size:.8rem;padding-block:24px 40px;border-top:1px solid var(--line)}
svg text{font-family:var(--mono)}
"""


def esc(s):
    return html.escape("" if s is None else str(s))


def pct(x, d=1, sign=False):
    if x is None:
        return "–"
    s = f"{abs(x) * 100:.{d}f}%"
    return ("−" if x < 0 and float(s[:-1]) != 0 else "+" if sign and x > 0 else "") + s


def pts(x, d=2):
    return "–" if x is None else f"{'+' if x > 0 else '−' if x < 0 else ''}{abs(x) * 100:.{d}f}"


def cls(x):
    return "" if x is None else "pos" if x > 1e-9 else "neg" if x < -1e-9 else ""


def page(title, body, nav=None, current=""):
    links = "".join(f'<a href="{esc(h)}"{" aria-current=page" if h == current else ""}>{esc(t)}</a>' for t, h in (nav or []))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{esc(title)}</title><link rel="icon" href="{FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head><body>
<header class="top"><div class="wrap brand"><img src="{FAVICON}" alt=""><b>Edge Engine</b><nav class="links" aria-label="Main">{links}</nav></div></header>
<main class="wrap">{body}</main>
<footer class="wrap">Fair odds are estimates. Results swing widely over small samples. For adults 21+ where legal.
Problem gambling help: <span class="num">1-800-GAMBLER</span>.</footer></body></html>"""


def nice_ticks(lo, hi, count=4):
    import math
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    step0 = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(step0))
    err = step0 / mag
    step = (10 if err >= 7.5 else 5 if err >= 3.5 else 2 if err >= 1.5 else 1) * mag
    t, ticks = math.floor(lo / step) * step, []
    while t <= math.ceil(hi / step) * step + step / 2:
        ticks.append(round(t, 6))
        t += step
    return ticks


def line_chart(points, label_fmt=lambda v: f"{v:g}", title="", series_name="", baseline=None, height=240):
    """Single-series SVG line chart (x = index). Static; the page also offers a table/CSV."""
    if len(points) < 2:
        return '<p class="muted small">The chart appears once there is more than one point.</p>'
    W, H, l, r, t, b = 720, height, 64, 90, 12, 26
    vals = [p for p in points] + ([baseline] if baseline is not None else [])
    ticks = nice_ticks(min(vals), max(vals))
    y0, y1 = ticks[0], ticks[-1]
    X = lambda i: l + (W - l - r) * i / (len(points) - 1)
    Y = lambda v: t + (H - t - b) * (1 - (v - y0) / (y1 - y0))
    path = "".join(f"{'L' if i else 'M'}{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(points))
    grid = "".join(f'<line x1="{l}" x2="{W - r}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--grid)"/>'
                   f'<text x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="var(--muted)">{esc(label_fmt(v))}</text>'
                   for v in ticks)
    base = (f'<line x1="{l}" x2="{W - r}" y1="{Y(baseline):.1f}" y2="{Y(baseline):.1f}" stroke="var(--muted)" stroke-dasharray="4 4"/>'
            if baseline is not None else "")
    end = points[-1]
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}" style="width:100%;height:auto;display:block">{grid}{base}'
            f'<path d="{path}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"/>'
            f'<circle cx="{X(len(points) - 1):.1f}" cy="{Y(end):.1f}" r="4" fill="var(--s1)" stroke="var(--panel)" stroke-width="2"/>'
            f'<text x="{X(len(points) - 1) + 8:.1f}" y="{Y(end) + 4:.1f}" font-size="11" fill="var(--ink)">{esc(series_name)} {esc(label_fmt(end))}</text>'
            f'<text x="{l}" y="{H - 6}" font-size="11" fill="var(--muted)">first</text>'
            f'<text x="{W - r}" y="{H - 6}" font-size="11" fill="var(--muted)" text-anchor="end">latest ({len(points) - 1})</text></svg>')


def calibration_chart(buckets):
    """Expected vs actual hit rate per bucket with 95% intervals. Diagonal = perfectly calibrated."""
    pts_ = [b for b in buckets if b["n"]]
    if not pts_:
        return '<p class="muted small">Calibration appears once picks are graded.</p>'
    W, H, l, r, t, b = 420, 300, 52, 16, 12, 40
    lo, hi = 0.40, 0.80
    X = lambda v: l + (W - l - r) * (v - lo) / (hi - lo)
    Y = lambda v: t + (H - t - b) * (1 - (v - lo) / (hi - lo))
    ticks = [0.4, 0.5, 0.6, 0.7, 0.8]
    g = "".join(f'<line x1="{l}" x2="{W - r}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--grid)"/>'
                f'<text x="{l - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="var(--muted)">{v * 100:.0f}%</text>'
                f'<text x="{X(v):.1f}" y="{H - b + 16}" text-anchor="middle" font-size="11" fill="var(--muted)">{v * 100:.0f}%</text>' for v in ticks)
    dots = ""
    for p in pts_:
        ex, ac = min(max(p["exp"], lo), hi), min(max(p["hit"], lo), hi)
        ci = 1.96 * (max(p["exp"] * (1 - p["exp"]), 1e-6) / p["n"]) ** 0.5
        dots += (f'<line x1="{X(ex):.1f}" x2="{X(ex):.1f}" y1="{Y(min(ac + ci, hi)):.1f}" y2="{Y(max(ac - ci, lo)):.1f}" stroke="var(--s1)" stroke-width="2"/>'
                 f'<circle cx="{X(ex):.1f}" cy="{Y(ac):.1f}" r="{min(4 + p["n"] ** 0.5 / 4, 10):.1f}" fill="var(--s1)" stroke="var(--panel)" stroke-width="2">'
                 f'<title>{esc(p["label"])}: {p["n"]} legs, hit {p["hit"] * 100:.1f}% vs expected {p["exp"] * 100:.1f}%</title></circle>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Calibration: actual hit rate against expected" style="width:100%;max-width:460px;height:auto;display:block">'
            f'{g}<line x1="{X(lo)}" y1="{Y(lo)}" x2="{X(hi)}" y2="{Y(hi)}" stroke="var(--muted)" stroke-dasharray="4 4"/>{dots}'
            f'<text x="{(l + W - r) / 2}" y="{H - 4}" text-anchor="middle" font-size="11" fill="var(--muted)">Expected hit rate</text></svg>')
