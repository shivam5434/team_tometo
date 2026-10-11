"""Turn a place name typed by a person ("Bombay", "kharagpure", "delhi") into one of
our cities. Pure Python port of the matcher in the web page, so the chat and the
search box agree. Matches against the city list only; anything else the caller
handles (for example the place that is open on the map)."""
from __future__ import annotations

import re
import unicodedata

from cities import CITIES

ALIASES = {
    "bangalore": "Bengaluru", "bombay": "Mumbai", "calcutta": "Kolkata", "madras": "Chennai",
    "benares": "Varanasi", "banaras": "Varanasi", "kashi": "Varanasi", "poona": "Pune",
    "trivandrum": "Thiruvananthapuram", "cochin": "Kochi", "mysore": "Mysuru", "baroda": "Vadodara",
    "gurgaon": "Gurugram", "allahabad": "Prayagraj", "calicut": "Kozhikode", "pondicherry": "Puducherry",
    "vizag": "Visakhapatnam", "waltair": "Visakhapatnam", "new delhi": "Delhi", "mangalore": "Mangaluru",
    "belgaum": "Belagavi", "bellary": "Ballari", "ahmednagar": "Ahilyanagar", "burdwan": "Barddhaman",
    "bahraich": "Bahraigh", "trichur": "Thrissur", "panjim": "Panaji", "simla": "Shimla",
    "faizabad": "Ayodhya", "cawnpore": "Kanpur", "gauhati": "Guwahati", "nasik": "Nashik",
    "hubli": "Hubballi", "bhubaneshwar": "Bhubaneswar",
}


def norm(t) -> str:
    t = unicodedata.normalize("NFD", str(t or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", t)).strip()


def skel(t: str) -> str:
    for a, b in (("aa", "a"), ("ee", "i"), ("oo", "u"), ("ou", "u"), ("ph", "f"),
                 ("w", "v"), ("z", "j"), ("y", "i")):
        t = t.replace(a, b)
    return re.sub(r"([a-z])\1+", r"\1", t)


def dist(a: str, b: str) -> int:
    """Optimal string alignment distance (a swap of neighbours counts as one edit)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + c)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def _allowed(n: int) -> int:
    return 0 if n <= 3 else 1 if n <= 5 else 2


def _score(q: str, sq: str, form: str) -> int:
    if form == q:
        return 100
    if form.startswith(q):
        return 92 - min(10, len(form) - len(q))
    if any(w.startswith(q) for w in form.split(" ")):
        return 80
    if len(q) >= 3 and q in form:
        return 70
    sf = skel(form)
    if sf == sq:
        return 88
    k, best = _allowed(len(sq)), 0
    if k:
        cands = [(sf, 60), (sf[:len(sq) + 1], 52)] + [(w, 58) for w in sf.split(" ")]
        for c, base in cands:
            d = dist(sq, c)
            if d <= k and abs(len(c) - len(sq)) <= k + 1:
                best = max(best, base - d * 8)
    return best


_FORMS = {}
for _c in CITIES:
    _FORMS.setdefault(_c["name"], [norm(_c["name"])])
for _a, _target in ALIASES.items():
    if _target in _FORMS:
        _FORMS[_target].append(_a)


def find_places(query: str, limit: int = 3):
    """Best matching cities first: [{"name", "lat", "lon", "tier", "cell", "score"}]."""
    q = norm(query)
    if len(q) < 2:
        return []
    sq = skel(q)
    out = []
    for c in CITIES:
        best = max(_score(q, sq, f) for f in _FORMS[c["name"]])
        if best >= 40:
            out.append((best, c))
    out.sort(key=lambda x: (-x[0], x[1].get("tier", 3), x[1]["name"]))
    if out and out[0][0] >= 88:
        out = [o for o in out if o[0] >= 60]
    return [{"name": c["name"], "lat": c["lat"], "lon": c["lon"], "tier": c.get("tier", 3),
             "cell": c.get("cell"), "score": s} for s, c in out[:limit]]


def find_places_in_text(text: str, limit: int = 3):
    """Find city names inside a sentence ("compare Delhi and Bombay"). Tries every 1-3 word
    run of the text, keeps confident matches only, and drops runs that overlap a better one."""
    words = norm(text).split(" ")
    hits = []                                   # (score, start, end, city)
    for n in (3, 2, 1):
        for i in range(0, len(words) - n + 1):
            chunk = " ".join(words[i:i + n])
            if n == 1 and (len(chunk) < 4 or chunk in STOPWORDS):
                continue
            r = find_places(chunk, 1)
            # one word: confident, or a plausible typo of a longer word; several words: confident
            if r and r[0]["score"] >= (50 if (n == 1 and len(chunk) >= 5) else 80):
                hits.append((r[0]["score"] + n, i, i + n, r[0]))
    hits.sort(key=lambda h: -h[0])
    used, out = set(), []
    for _s, a, b, city in hits:
        span = set(range(a, b))
        if span & used or any(c["name"] == city["name"] for _a, c in out):
            continue
        used |= span
        out.append((a, city))
    out.sort(key=lambda x: x[0])                # keep the order the person wrote them in
    return [c for _a, c in out][:limit]


STOPWORDS = set("""this that what when where which with from have will would could should about there their then
than they them best time today tomorrow tonight morning evening night week today's please tell give show
air quality aqi pollution good bad safe safely going outside outdoor outdoors walk walking run running jog
jogging cycle cycling exercise email mail alert notify message compare versus between over under below above
drops drop goes rises right now later hours hour next here around near people city town place
monday tuesday wednesday thursday friday saturday sunday january february march april june july august
september october november december noon midnight afternoon weekend threshold level levels category
moderate satisfactory poor severe really maybe thanks thank hello help can't cannot don't""".split())
