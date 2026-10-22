import json
import re
import time
import unicodedata
import urllib.error
import urllib.request

UA = "edge-engine/3.0"


def _fetch(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
        return (json.loads(body) if body.strip() else {}), {k.lower(): v for k, v in r.headers.items()}


fetch = _fetch   # tests replace this


def get_json(url, retries=2):
    """GET a JSON document. Retries transient failures; raises HTTPError for 4xx."""
    for attempt in range(retries + 1):
        try:
            return fetch(url)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(1.5 * (attempt + 1))


def post(url, data, headers=None):
    return fetch(url, data=data, headers=headers, timeout=15)


def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[.\-'’]", " ", s.lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def canon_team(name):
    """Short team key that matches across ESPN, MLB and The Odds API spellings
    ("LA Clippers" and "Los Angeles Clippers" -> "clippers")."""
    t = norm_name(name).split()
    if not t:
        return ""
    return " ".join(t[-2:]) if t[-1] in {"sox"} else t[-1]


def team_match(a, b):
    na, nb = norm_name(a), norm_name(b)
    ta, tb = na.split(), nb.split()
    if not ta or not tb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    if ta[-1] in {"sox"}:              # Red Sox / White Sox
        return ta[-2:] == tb[-2:]
    return ta[-1] == tb[-1]
