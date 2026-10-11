"""Offline tests. Run from phase2/:  python tests/test_all.py"""
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from handler import SNAPSHOT_KEY, _to_dynamo, run_ingest   # noqa: E402
from openmeteo_client import OpenMeteoError                # noqa: E402
from scoring import score                                  # noqa: E402

NOW = datetime(2026, 10, 10, 6, 20, tzinfo=timezone.utc)
PT = {"name": "X", "cell": "10_20", "lat": 1.0, "lon": 2.0}


def rec(pm25, pm10, t="2026-10-10T06:00"):
    return {"time": t, "pm25": pm25, "pm10": pm10, "us_aqi": 77}


# ---- scoring ----
a = score(PT, rec(80, 150), NOW)
assert a["category"] == "Moderate" and a["aqi"] == 166 and a["basis"] == "pm25+pm10" and a["warnings"] == [], a
assert "verdict" not in a and a["dominant"] == "pm25" and a["sub"] == {"pm25": 166, "pm10": 134}, a
d = score(PT, rec(128.5, 667), NOW)               # dust artefact -> PM10 ignored
assert d["basis"] == "pm25" and "possible_dust" in d["warnings"] and d["aqi"] == 307, d
assert d["category"] == "Very Poor"
g = score(PT, rec(53, 117), NOW)                 # ordinary PM10 stays in
assert g["basis"] == "pm25+pm10" and g["category"] == "Moderate", g
clean = score(PT, rec(2, 10), NOW)               # high ratio but tiny values: no dust flag
assert clean["warnings"] == [] and clean["category"] == "Good"
none = score(PT, rec(None, None), NOW)
assert none["category"] is None and none["aqi"] is None and "missing" in none["flags"] and none["sub"] == {}
stale = score(PT, rec(80, 100, "2026-10-09T00:00"), NOW)
assert stale["aqi"] is None and "stale" in stale["flags"]
only10 = score(PT, rec(None, 300), NOW)
assert only10["basis"] == "pm10" and only10["category"] == "Poor", only10

# ---- all six pollutants: worst sub-index wins; CO converted ug/m3 -> mg/m3 ----
full = dict(rec(20, 40), co=1500.0, no2=100.0, so2=10.0, o3=120.0, dust=3.0, aod=0.4, uv=5.5)
f = score(PT, full, NOW)
assert f["co"] == 1.5 and f["no2"] == 100.0 and f["uv"] == 5.5, f
assert f["sub"] == {"pm25": 33, "pm10": 40, "no2": 120, "so2": 13, "o3": 129, "co": 73}, f["sub"]
assert f["aqi"] == 129 and f["dominant"] == "o3" and f["category"] == "Moderate", f
assert f["basis"] == "pm25+pm10+no2+so2+o3+co"
# a bad gas is dropped with a flag; PM still gives an AQI
bad = score(PT, dict(rec(20, 40), no2=99999.0), NOW)
assert "no2_implausible_high" in bad["flags"] and bad["aqi"] == 40 and "no2" not in bad["sub"], bad
# a missing gas is not flagged
assert score(PT, rec(20, 40), NOW)["flags"] == []

# ---- aqi.py: CPCB breakpoints for every pollutant ----
from aqi import compute_index, display_bands   # noqa: E402
chk = [  # (kwargs, expected AQI)
    (dict(pm25=30), 50), (dict(pm25=31), 51), (dict(pm25=60), 100), (dict(pm25=250), 400),
    (dict(pm10=100), 100), (dict(pm10=430), 400),
    (dict(no2=40), 50), (dict(no2=80), 100), (dict(no2=180), 200), (dict(no2=400), 400),
    (dict(so2=40), 50), (dict(so2=380), 200), (dict(so2=1600), 400),
    (dict(o3=50), 50), (dict(o3=168), 200), (dict(o3=748), 400),
    (dict(co=1.0), 50), (dict(co=2.0), 100), (dict(co=10.0), 200), (dict(co=17.0), 300), (dict(co=34.0), 400),
    (dict(pm25=9999), 500),
]
for kw, want in chk:
    got = compute_index(**kw)["aqi"]
    assert got == want, (kw, got, want)
assert compute_index() is None and compute_index(pm25=-5) is None
for aqi_, cat in [(0, "Good"), (50, "Good"), (51, "Satisfactory"), (100, "Satisfactory"), (101, "Moderate"),
                  (200, "Moderate"), (201, "Poor"), (300, "Poor"), (301, "Very Poor"), (400, "Very Poor"),
                  (401, "Severe"), (500, "Severe")]:
    from aqi import category_for
    assert category_for(aqi_) == cat, (aqi_, cat)
bands = display_bands()
assert bands["pm25"] == [30, 60, 90, 120, 250] and bands["co"] == [1.0, 2.0, 10.0, 17.0, 34.0], bands

# ---- dynamo conversion ----
conv = _to_dynamo({"a": 1.5, "b": [0.25], "c": None})
assert conv["a"] == Decimal("1.5") and conv["b"][0] == Decimal("0.25")


# ---- run_ingest with fakes ----
class FakeTable:
    def __init__(self): self.items = []
    def batch_writer(self):
        outer = self
        class BW:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def put_item(s, Item): outer.items.append(Item)
        return BW()


class FakeS3:
    def __init__(self): self.calls = []
    def put_object(self, **kw): self.calls.append(kw)


class FakeClient:
    calls_made = 0
    def __init__(self, fail_on=()): self.fail_on, self.n = set(fail_on), 0
    def fetch_batch(self, pts):
        self.n += 1
        self.calls_made = self.n
        if self.n in self.fail_on:
            raise OpenMeteoError("boom")
        return [rec(60.5 + i, 90.5 + i) for i, _ in enumerate(pts)]


points = [{"name": f"c{i}", "cell": f"{i}_0", "lat": 10.0 + i, "lon": 77.0} for i in range(7)]

t, s3 = FakeTable(), FakeS3()
s = run_ingest(FakeClient(), t, s3, "bkt", now=NOW, points=points, batch_size=3)
assert s["stored"] == 7 and s["failed_batches"] == 0 and s["api_calls"] == 3, s
assert len(t.items) == 7 and t.items[0]["expires_at"] == int(NOW.timestamp()) + 12 * 3600
assert isinstance(t.items[0]["pm25"], Decimal)
snap = json.loads(s3.calls[0]["Body"])
assert s3.calls[0]["Key"] == SNAPSHOT_KEY and s3.calls[0]["Bucket"] == "bkt"
assert snap["count"] == 7 and "Open-Meteo" in snap["attribution"] and snap["stations"][0]["id"] == "0_0"
st0 = snap["stations"][0]
assert st0["category"] == "Moderate" and st0["sub"] and "verdict" not in st0 and "co" in st0, st0
assert s["categories"]["Moderate"] == 7 and s["unknown"] == 0, s

# one failed batch -> the rest still stored
t, s3 = FakeTable(), FakeS3()
s = run_ingest(FakeClient(fail_on=[2]), t, s3, "bkt", now=NOW, points=points, batch_size=3)
assert s["stored"] == 4 and s["failed_batches"] == 1 and s["errors"] == ["boom"], s

# all failed -> raises (Lambda shows an error)
try:
    run_ingest(FakeClient(fail_on=[1, 2, 3]), FakeTable(), FakeS3(), "b", now=NOW,
               points=points, batch_size=3)
    raise SystemExit("expected failure")
except RuntimeError:
    pass

# ---- city list ----
from cities import CITIES, POINTS            # noqa: E402
from grid import in_india_bbox               # noqa: E402
assert 350 <= len(POINTS) <= 700, len(POINTS)
assert len(POINTS) == len(CITIES), "one marker per grid cell"
assert all(in_india_bbox(c["lat"], c["lon"]) for c in CITIES)
assert len({c["name"] for c in CITIES}) == len(CITIES), "duplicate city name"
assert all(c["tier"] in (1, 2, 3) for c in CITIES)
tiers = {t: sum(c["tier"] == t for c in CITIES) for t in (1, 2, 3)}
assert 30 <= tiers[1] <= 80 and tiers[2] >= 30 and tiers[3] >= 150, tiers
assert all(c["name"].isascii() for c in CITIES)
need = {"Delhi", "Mumbai", "Kolkata", "Chennai", "Kharagpur", "Leh", "Port Blair", "Gangtok"}
assert need <= {c["name"] for c in CITIES}, need - {c["name"] for c in CITIES}
assert {c["name"]: c["tier"] for c in CITIES}["Delhi"] == 1

# tier flows through scoring and into the snapshot
tp = dict(PT, tier=2)
assert score(tp, rec(80, 150), NOW)["tier"] == 2 and score(PT, rec(80, 150), NOW)["tier"] == 3
t, s3 = FakeTable(), FakeS3()
run_ingest(FakeClient(), t, s3, "bkt", now=NOW, points=[dict(points[0], tier=1)], batch_size=3)
assert json.loads(s3.calls[0]["Body"])["stations"][0]["t"] == 1

# ---- series: 24-h mean basis, window edges, missing data ----
from datetime import timedelta                       # noqa: E402
from series import build_series                      # noqa: E402
T0 = datetime(2026, 10, 8, tzinfo=timezone.utc)
times = [(T0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(120)]   # now = index 54 (10 Oct 06:00)
pm = [20.0] * 120
for i in range(0, 30):                               # old spike, outside the 24 h window ending at index 54
    pm[i] = 300.0
raw = {"time": times, "pm25": pm, "pm10": [40.0] * 120, "co": [500.0] * 120}
b = build_series(raw, NOW)
assert b["now_index"] == 48 and len(b["time"]) == 97 and b["time"][48] == "2026-10-10T06:00Z"
assert b["time"][0] == "2026-10-08T06:00Z" and b["pm25"][0] == 300.0 and b["co"][0] == 0.5
assert b["aqi_24h"]["sub"]["pm25"] == 33 and b["aqi_24h"]["category"] == "Good", b["aqi_24h"]   # mean 20
assert b["aqi"][0] > 300 and b["aqi"][48] == 40 and "no2" in b and b["co2"][0] is None
# the same data viewed instantly vs as a 24-h mean differs when the last hours spike
pm2 = [20.0] * 120
pm2[54] = 200.0
b2 = build_series({"time": times, "pm25": pm2, "pm10": [40.0] * 120}, NOW)
assert b2["aqi"][48] > 300 and b2["aqi_24h"]["aqi"] < 60, (b2["aqi"][48], b2["aqi_24h"])
# not enough hours of a pollutant -> no mean for it; all missing -> no 24-h AQI
sparse = {"time": times, "pm25": [None] * 120, "pm10": [None] * 120}
assert build_series(sparse, NOW)["aqi_24h"] is None
# gaps handled: None / negative / junk values are skipped
junk = {"time": times, "pm25": [None, -1, "x"] * 40, "pm10": [40.0] * 120}
assert build_series(junk, NOW)["aqi_24h"]["dominant"] == "pm10"
# series that starts after now is an error
try:
    build_series({"time": times[60:], "pm25": pm[60:]}, NOW)
    raise SystemExit("expected ValueError")
except ValueError:
    pass

print("phase2 tests OK")
