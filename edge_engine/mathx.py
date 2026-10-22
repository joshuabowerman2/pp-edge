"""Probability, vig removal, stat distributions, payouts and Kelly sizing."""
import math


def american_to_prob(a):
    a = float(a)
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)


def american_to_decimal(a):
    a = float(a)
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def devig_power(p_over, p_under):
    lo, hi = 0.5, 3.0
    for _ in range(60):
        k = (lo + hi) / 2
        if p_over ** k + p_under ** k > 1:
            lo = k
        else:
            hi = k
    return p_over ** ((lo + hi) / 2)


def devig_mult(p_over, p_under):
    return p_over / (p_over + p_under)


def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-x)) if x > -700 else 0.0


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_ppf(p):
    lo, hi = -8.0, 8.0
    for _ in range(80):
        m = (lo + hi) / 2
        if norm_cdf(m) < p:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def poisson_cdf(k, mu):
    if k < 0:
        return 0.0
    if mu <= 0:
        return 1.0
    term = total = math.exp(-mu)
    for i in range(1, int(k) + 1):
        term *= mu / i
        total += term
    return min(total, 1.0)


def model_over(mu, line, dist, sd=None):
    """P(over line), pushes ignored, for a stat with mean mu. `sd` overrides the cv rule for normals."""
    if dist[0] == "poisson":
        over = 1 - poisson_cdf(math.floor(line), mu)
        under = poisson_cdf(math.ceil(line) - 1, mu)
    else:
        s = max(sd if sd else dist[1] * mu, 0.5)
        if float(line).is_integer():
            over, under = 1 - norm_cdf((line + .5 - mu) / s), norm_cdf((line - .5 - mu) / s)
        else:
            over, under = 1 - norm_cdf((line - mu) / s), norm_cdf((line - mu) / s)
    tot = over + under
    return over / tot if tot > 0 else 0.5


def implied_mean(p_over, line, dist):
    """Mean stat value that makes P(over line) = p_over under the stat model."""
    lo, hi = 0.01, max(3 * line, line + 10)
    for _ in range(70):
        mu = (lo + hi) / 2
        if model_over(mu, line, dist) < p_over:
            lo = mu
        else:
            hi = mu
    return (lo + hi) / 2


def shift_line(p_over, from_line, to_line, dist):
    return model_over(implied_mean(p_over, from_line, dist), to_line, dist)


def max_shift(line, dist):
    return 1.0 if dist[0] == "poisson" else max(1.0, 0.12 * line)


def hit_dist(ps):
    d = [1.0]
    for p in ps:
        n = [0.0] * (len(d) + 1)
        for k, v in enumerate(d):
            n[k] += v * (1 - p)
            n[k + 1] += v * p
        d = n
    return d


def payout_mult(dist, pay):
    return sum(v * pay.get(k, 0) for k, v in enumerate(dist))


def payout_var(dist, pay):
    e = payout_mult(dist, pay)
    return max(sum(v * pay.get(k, 0) ** 2 for k, v in enumerate(dist)) - e * e, 0.0)


def breakeven(n, pay):
    """Per-leg win rate at which an entry of n equal legs breaks even."""
    lo, hi = 0.3, 0.97
    for _ in range(60):
        m = (lo + hi) / 2
        if payout_mult(hit_dist([m] * n), pay) < 1:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def kelly_fraction(dist, pay):
    """Full-Kelly fraction of bankroll for one entry with a hit-count distribution."""
    outs = [(p, pay.get(k, 0) - 1) for k, p in enumerate(dist) if p > 0]
    if sum(p * r for p, r in outs) <= 0:
        return 0.0
    lo, hi = 0.0, 0.999
    for _ in range(60):
        f = (lo + hi) / 2
        if sum(p * r / (1 + f * r) for p, r in outs) > 0:
            lo = f
        else:
            hi = f
    return lo


def ev_sigma(ps, sigmas, pay, dist_fn=hit_dist):
    """Standard error of an entry's expected payout from the uncertainty in each leg's fair %
    (delta method, finite differences)."""
    base = payout_mult(dist_fn(ps), pay)
    var = 0.0
    for i, (p, s) in enumerate(zip(ps, sigmas)):
        h = 0.005
        q = list(ps)
        q[i] = min(p + h, 0.999)
        d = (payout_mult(dist_fn(q), pay) - base) / h
        var += (d * s) ** 2
    return math.sqrt(var)


def uncertainty_shrink(edge, sigma):
    """Bet less when the edge itself is uncertain: edge^2 / (edge^2 + sigma^2)."""
    if edge <= 0:
        return 0.0
    return edge * edge / (edge * edge + sigma * sigma)
