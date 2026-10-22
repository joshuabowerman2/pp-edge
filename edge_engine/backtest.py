"""Judge the models on holdout picks they were never fit on.

Compares the market price, our projection, and the final blended price on the same graded holdout
legs: log loss and Brier score (lower is better), calibration, and closing line value."""
import math

from . import mathx


def _metrics(pairs):
    n = len(pairs)
    if not n:
        return None
    ll = -sum(math.log(min(max(p, 1e-6), 1 - 1e-6) if y else 1 - min(max(p, 1e-6), 1 - 1e-6)) for p, y in pairs) / n
    br = sum((p - y) ** 2 for p, y in pairs) / n
    hit = sum(y for _, y in pairs) / n
    exp = sum(p for p, _ in pairs) / n
    return {"n": n, "log_loss": ll, "brier": br, "hit_rate": hit, "expected": exp}


def evaluate(conn, holdout=1, sport=None):
    q = """SELECT sport, fair_market, fair_proj, fair, clv, result FROM picks
           WHERE holdout=? AND result IN ('hit','miss') AND fair_market IS NOT NULL"""
    args = [holdout]
    if sport:
        q += " AND sport=?"
        args.append(sport)
    rows = conn.execute(q, args).fetchall()
    both = [r for r in rows if r["fair_proj"] is not None]
    y = lambda r: 1 if r["result"] == "hit" else 0
    out = {
        "set": "holdout" if holdout else "training",
        "all": {"market": _metrics([(r["fair_market"], y(r)) for r in rows]),
                "final": _metrics([(r["fair"], y(r)) for r in rows])},
        "with_projection": {"market": _metrics([(r["fair_market"], y(r)) for r in both]),
                            "projection": _metrics([(r["fair_proj"], y(r)) for r in both]),
                            "final": _metrics([(r["fair"], y(r)) for r in both])},
    }
    clv = [r["clv"] for r in rows if r["clv"] is not None]
    out["clv"] = {"n": len(clv), "mean": sum(clv) / len(clv) if clv else None,
                  "beat": sum(c > 0 for c in clv) / len(clv) if clv else None}
    m, f = out["all"]["market"], out["all"]["final"]
    if m and f:
        d = f["log_loss"] - m["log_loss"]
        out["verdict"] = ("final beats market" if d < -0.001 else "final worse than market" if d > 0.001
                          else "final matches market")
        out["delta_log_loss"] = d
    return out


def report(res):
    lines = [f"Backtest on {res['set']} picks"]
    for group, label in (("all", "All graded legs"), ("with_projection", "Legs with a projection")):
        lines.append(f"\n{label}:")
        for k, m in res[group].items():
            if m:
                lines.append(f"  {k:<11} n={m['n']:<6} log loss {m['log_loss']:.4f}  Brier {m['brier']:.4f}  "
                             f"hit {m['hit_rate'] * 100:.1f}% vs expected {m['expected'] * 100:.1f}%")
    c = res["clv"]
    if c["n"]:
        lines.append(f"\nCLV: {c['mean'] * 100:+.2f} pts average over {c['n']} legs, beat the close {c['beat'] * 100:.0f}%")
    if "verdict" in res:
        lines.append(f"\nVerdict: {res['verdict']} (log loss change {res['delta_log_loss']:+.4f})")
    return "\n".join(lines)
