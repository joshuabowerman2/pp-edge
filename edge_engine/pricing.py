"""Fair probability for each pick'em line: market price (sharp books and exchanges, vig removed),
optionally blended with our own projection, plus an uncertainty (sigma) for staking."""
import math

from . import mathx
from .markets import BOOK_WEIGHTS, SHARP, dist_for

CONVERTED_WEIGHT = 0.6
NO_SHARP_SHRINK = 0.75
EST_ONLY_SHRINK = 0.70
MAX_SPREAD = 0.10


def market_fair(sport, market, line, quotes, min_books=2, method="power",
                no_sharp_shrink=None, est_only_shrink=None):
    """Fair P(over line) from book quotes [(book, point, over, under)].
    Returns (p_over or None, info). info carries raw (unshrunk) p, sigma and book detail."""
    nss = NO_SHARP_SHRINK if no_sharp_shrink is None else no_sharp_shrink
    eos = EST_ONLY_SHRINK if est_only_shrink is None else est_only_shrink
    devig = mathx.devig_power if method == "power" else mathx.devig_mult
    dist = dist_for(sport, market)
    best = {}
    for book, point, o, u in quotes or []:
        if book not in best or abs(point - line) < abs(best[book][0] - line):
            best[book] = (point, o, u)
    est, weights, used, exact, conv = [], [], [], 0, 0
    for book, (point, o, u) in best.items():
        p = devig(mathx.american_to_prob(o), mathx.american_to_prob(u))
        w = BOOK_WEIGHTS.get(book, 1)
        if point == line:
            exact += 1
        elif abs(point - line) <= mathx.max_shift(line, dist):
            p = mathx.shift_line(p, point, line, dist)
            w *= CONVERTED_WEIGHT
            conv += 1
        else:
            continue
        est.append(p)
        weights.append(w)
        used.append((book, point, o, u))
    if len(est) < min_books:
        return None, {"reason": "few books"}
    spread = max(est) - min(est)
    if spread > MAX_SPREAD:
        return None, {"reason": "books disagree"}
    W = sum(weights)
    fo = sum(p * w for p, w in zip(est, weights)) / W
    raw = fo
    sharp = any(b in SHARP for b, *_ in used)
    if not sharp:
        fo = 0.5 + (fo - 0.5) * nss
    if exact == 0:
        fo = 0.5 + (fo - 0.5) * eos
    # Uncertainty: disagreement between books over effective sample size, plus a floor by data quality.
    n_eff = W * W / sum(w * w for w in weights)
    sd_books = math.sqrt(sum(w * (p - raw) ** 2 for p, w in zip(est, weights)) / W) if len(est) > 1 else 0.02
    floor = 0.012 if sharp else 0.022
    if exact == 0:
        floor += 0.010
    sigma = math.sqrt(sd_books ** 2 / max(n_eff, 1) + floor ** 2)
    return fo, {"raw": raw, "books": len(est), "exact": exact, "converted": conv, "sharp": sharp,
                "spread": spread, "sigma": sigma,
                "quotes": ", ".join(f"{b} {pt:g} {o:+d}/{u:+d}" for b, pt, o, u in used)}


# Guard rails on the projection. A stat model fed thin or odd data can claim 99.99%, and in a
# logit blend that swamps the sportsbooks. Books are rarely wrong by more than a few points.
PROJ_CLAMP = (0.25, 0.75)   # projection probability is limited to this range before blending
MAX_PROJ_SHIFT = 0.05       # the blend may move the market price by at most 5 points either way


def blend(p_market, p_proj, params):
    """Logistic blend: logit(p) = c + a*logit(p_market) + b*logit(p_proj). Market only when no projection.
    The result stays within MAX_PROJ_SHIFT of the market price."""
    if p_proj is None or params is None:
        return p_market
    a, b, c = params
    p_proj = min(max(p_proj, PROJ_CLAMP[0]), PROJ_CLAMP[1])
    p = mathx.sigmoid(c + a * mathx.logit(p_market) + b * mathx.logit(p_proj))
    return min(max(p, p_market - MAX_PROJ_SHIFT), p_market + MAX_PROJ_SHIFT)


def proj_over(proj, line, sport, market):
    """P(over) from a projection {mu, sd}."""
    if not proj:
        return None
    dist = dist_for(sport, market)
    return mathx.model_over(proj["mu"], line, dist, sd=proj.get("sd"))


def quotes_for(conn, game_id, market, player_key):
    return [(r["book"], r["point"], r["over"], r["under"]) for r in conn.execute(
        "SELECT book, point, over, under FROM quotes WHERE game_id=? AND market=? AND player_key=?",
        (game_id, market, player_key))]
