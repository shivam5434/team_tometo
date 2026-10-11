"""Offline tests for api.py. Run from phase2/:  python tests/test_api.py"""
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import api                                         # noqa: E402
from openmeteo_client import OpenMeteoError        # noqa: E402

NOW = datetime(2026, 10, 10, 6, 20, tzinfo=timezone.utc)


class Err(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeTable:
    def __init__(self, cap_hits=0):
        self.items, self.puts, self.caps, self.cap_hits = {}, [], 0, cap_hits
    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it else {}
    def put_item(self, Item):
        self.items[Item["pk"]] = Item; self.puts.append(Item)
    def update_item(self, **kw):
        if self.caps >= kw["ExpressionAttributeValues"][":max"] or self.caps < 0:
            raise Err("ConditionalCheckFailedException")
        self.caps += 1


class FakeBody:
    def __init__(self, b): self.b = b
    def read(self): return self.b


class FakeS3:
    def __init__(self, body=None): self.body, self.reads = body, 0
    def get_object(self, Bucket, Key):
        self.reads += 1
        if self.body is None:
            raise Err("NoSuchKey")
        return {"Body": FakeBody(self.body)}


class FakeClient:
    def __init__(self, fail=False): self.fail, self.calls = fail, 0
    def fetch_batch(self, pts):
        self.calls += 1
        if self.fail:
            raise OpenMeteoError("down")
        return [{"time": "2026-10-10T06:00", "pm25": 80.5, "pm10": 150.5, "us_aqi": 160,
                 "no2": 30.0, "so2": 5.0, "o3": 60.0, "co": 400.0, "dust": 2.0, "aod": 0.3, "uv": 4.0}]
    def fetch_series(self, lat, lon):
        self.calls += 1
        if self.fail:
            raise OpenMeteoError("down")
        times = [(datetime(2026, 10, 8, tzinfo=timezone.utc) + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
                 for i in range(120)]            # 8 Oct 00:00 .. 12 Oct 23:00 GMT
        return {"time": times, "grid_lat": lat, "grid_lon": lon,
                "pm25": [50.0 + i % 7 for i in range(120)], "pm10": [90.0 + i % 5 for i in range(120)],
                "no2": [20.0] * 120, "so2": [4.0] * 120, "o3": [70.0] * 120,
                "co": [500.0] * 120, "co2": [420.0] * 120, "dust": [1.0] * 120, "uv": [3.0] * 120}


def ev(path, **q):
    return {"rawPath": path, "requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": q or None}


def deps(table=None, s3=None, client=None, cap=3):
    client = client or FakeClient()
    return api.Deps(table=table or FakeTable(), s3=s3 or FakeS3(b'{"stations":[]}'),
                    bucket="b", client_factory=lambda: client, daily_cap=cap, cache={})


# --- page, 404, 405 ---
r = api.handle(ev("/"), deps(), NOW)
assert r["statusCode"] == 200 and "text/html" in r["headers"]["Content-Type"] and "Leaflet" in r["body"] or "leaflet" in r["body"]
assert api.handle(ev("/nope"), deps(), NOW)["statusCode"] == 404
post = {"rawPath": "/", "requestContext": {"http": {"method": "POST"}}}
assert api.handle(post, deps(), NOW)["statusCode"] == 405

# --- /stations: passthrough, cache, 503 when no snapshot ---
s3 = FakeS3(b'{"count":1,"stations":[{"id":"x"}]}')
d = deps(s3=s3)
r = api.handle(ev("/stations"), d, NOW)
assert r["statusCode"] == 200 and json.loads(r["body"])["count"] == 1
api.handle(ev("/stations"), d, NOW + timedelta(seconds=30))
assert s3.reads == 1, "should be cached for 60s"
api.handle(ev("/stations"), d, NOW + timedelta(seconds=90))
assert s3.reads == 2
assert api.handle(ev("/stations"), deps(s3=FakeS3(None)), NOW)["statusCode"] == 503

# --- /lookup validation ---
for q in [{}, {"lat": "abc", "lon": "80"}, {"lat": "nan", "lon": "80"}, {"lat": "inf", "lon": "80"}]:
    assert api.handle(ev("/lookup", **q), deps(), NOW)["statusCode"] == 400, q
r = api.handle(ev("/lookup", lat="51.5", lon="-0.1"), deps(), NOW)
assert r["statusCode"] == 400 and "India" in json.loads(r["body"])["error"]

# --- /lookup: miss -> live fetch -> stored; second call served from DynamoDB ---
t, c = FakeTable(), FakeClient()
d = deps(table=t, client=c)
r = api.handle(ev("/lookup", lat="22.3149", lon="87.3105", name="Kharagpur <b>x</b>"), d, NOW)
body = json.loads(r["body"])
assert r["statusCode"] == 200 and body["category"] == "Moderate" and "verdict" not in body and body["sub"]["pm25"] and c.calls == 1
assert body["name"] == "Kharagpur bx" or body["name"].startswith("Kharagpur"), body["name"]
assert "<" not in body["name"] and len(t.puts) == 1
stored = t.puts[0]
assert stored["pk"] == "223_873" and "Kharagpur" not in stored["name"], "user text must not be stored"
assert isinstance(stored["pm25"], Decimal) and "expires_at" in stored
r2 = api.handle(ev("/lookup", lat="22.32", lon="87.32"), d, NOW + timedelta(hours=1))
assert r2["statusCode"] == 200 and c.calls == 1, "same cell, fresh -> no new fetch"

# --- stale cache entry -> refetch ---
r3 = api.handle(ev("/lookup", lat="22.32", lon="87.32"), d, NOW + timedelta(hours=5))
assert c.calls == 2

# --- daily cap -> 429, cached cells still work ---
t = FakeTable(); d = deps(table=t, cap=1)
assert api.handle(ev("/lookup", lat="20.0", lon="80.0"), d, NOW)["statusCode"] == 200
r = api.handle(ev("/lookup", lat="25.0", lon="82.0"), d, NOW)
assert r["statusCode"] == 429
assert api.handle(ev("/lookup", lat="20.0", lon="80.0"), d, NOW)["statusCode"] == 200

# --- upstream failure: 502 with no cache, stale data with cache ---
r = api.handle(ev("/lookup", lat="20.0", lon="80.0"), deps(client=FakeClient(fail=True)), NOW)
assert r["statusCode"] == 502
t = FakeTable(); d = deps(table=t, client=FakeClient())
api.handle(ev("/lookup", lat="20.0", lon="80.0"), d, NOW)
d.client_factory = lambda: FakeClient(fail=True)
r = api.handle(ev("/lookup", lat="20.0", lon="80.0"), d, NOW + timedelta(hours=6))
assert r["statusCode"] == 200 and json.loads(r["body"]).get("stale") is True

# --- refreshing an existing city cell keeps its name/tier ---
t = FakeTable()
t.items["286_772"] = {"pk": "286_772", "name": "Delhi", "lat": Decimal("28.61"), "lon": Decimal("77.21"),
                     "tier": 1, "fetched_at": (NOW - timedelta(hours=6)).isoformat(timespec="seconds")}
d = deps(table=t)
r = api.handle(ev("/lookup", lat="28.61", lon="77.21"), d, NOW)
assert r["statusCode"] == 200 and json.loads(r["body"])["name"] == "Delhi"
assert t.items["286_772"]["name"] == "Delhi" and t.items["286_772"]["tier"] == 1

# --- /series ---
for q in [{}, {"lat": "x", "lon": "80"}, {"lat": "51.5", "lon": "-0.1"}]:
    assert api.handle(ev("/series", **q), deps(), NOW)["statusCode"] == 400, q
t, c = FakeTable(), FakeClient()
d = deps(table=t, client=c)
r = api.handle(ev("/series", lat="22.3149", lon="87.3105"), d, NOW)
sb = json.loads(r["body"])
assert r["statusCode"] == 200 and c.calls == 1 and len(t.puts) == 1 and t.puts[0]["pk"] == "series#223_873"
assert sb["time"][sb["now_index"]] == "2026-10-10T06:00Z", sb["time"][sb["now_index"]]
assert sb["now_index"] == 48 and len(sb["time"]) == 97 and len(sb["aqi"]) == 97 and len(sb["co"]) == 97
assert sb["co"][0] == 0.5 and sb["co2"][0] == 420.0 and sb["units"]["co"] == "mg/m³"
assert sb["aqi_24h"]["category"] in ("Satisfactory", "Moderate") and sb["aqi_24h"]["windows_h"]["o3"] == 8
assert sb["bands"]["pm25"] == [30, 60, 90, 120, 250]
r2 = api.handle(ev("/series", lat="22.32", lon="87.32"), d, NOW + timedelta(hours=1))
assert r2["statusCode"] == 200 and c.calls == 1, "same cell, fresh -> served from DynamoDB"
api.handle(ev("/series", lat="22.32", lon="87.32"), d, NOW + timedelta(hours=4))
assert c.calls == 2, "older than 3 h -> refetch"
# cap -> 429 without cache, old data with cache
t = FakeTable(); d = deps(table=t, cap=1)
assert api.handle(ev("/series", lat="20.0", lon="80.0"), d, NOW)["statusCode"] == 200
assert api.handle(ev("/series", lat="25.0", lon="82.0"), d, NOW)["statusCode"] == 429
assert api.handle(ev("/series", lat="20.0", lon="80.0"), d, NOW + timedelta(hours=5))["statusCode"] == 200
# upstream failure: 502 with no cache, old data with cache
assert api.handle(ev("/series", lat="20.0", lon="80.0"), deps(client=FakeClient(fail=True)), NOW)["statusCode"] == 502
t = FakeTable(); d = deps(table=t)
api.handle(ev("/series", lat="20.0", lon="80.0"), d, NOW)
d.client_factory = lambda: FakeClient(fail=True)
assert api.handle(ev("/series", lat="20.0", lon="80.0"), d, NOW + timedelta(hours=5))["statusCode"] == 200

print("api tests OK")
