"""Offline tests for openmeteo_client (mock HTTP server). Run: python tests/test_client.py"""
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from openmeteo_client import (CORE_VARS, CURRENT_VARS, HOURLY_CO2, OpenMeteoClient,  # noqa: E402
                              OpenMeteoError)

STATE = {"reject": set(), "log": []}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        STATE["log"].append(q)
        names = (q.get("current") or q.get("hourly"))[0].split(",")
        if STATE["reject"] & set(names):
            return self._send(400, {"error": True, "reason": "Cannot initialize variable"})
        n = len(q["latitude"][0].split(","))
        if "hourly" in q:
            hourly = {"time": [f"2026-10-10T{h:02d}:00" for h in range(24)]}
            for nm in names:
                hourly[nm] = [1.0] * 24
            return self._send(200, {"latitude": 22.3, "longitude": 87.3, "hourly": hourly})
        items = [{"latitude": 22.3, "longitude": 87.3,
                  "current": {"time": "2026-10-10T06:00", **{nm: 1.5 for nm in names}}} for _ in range(n)]
        self._send(200, items[0] if n == 1 else items)


class Sess:
    def __init__(self): self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(self, url, params=None, timeout=30):
        class R: pass
        r = R()
        try:
            with self.op.open(f"{url}?{urllib.parse.urlencode(params)}", timeout=timeout) as resp:
                r.status_code, r.text = resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            r.status_code, r.text = e.code, e.read().decode()
        r.json = lambda: json.loads(r.text)
        return r


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"


def make(): return OpenMeteoClient(base_url=BASE, session=Sess(), sleep=lambda s: None, min_interval=0)


# full variable list maps to our keys; 10 variables exactly (billing threshold)
assert len(CURRENT_VARS.split(",")) == 10
rows = make().fetch_batch([(22.3, 87.3), (28.6, 77.2)])
assert len(rows) == 2 and rows[0]["pm25"] == 1.5 and rows[0]["co"] == 1.5 and rows[0]["no2"] == 1.5
assert all(k in rows[0] for k in ("pm10", "so2", "o3", "dust", "aod", "uv", "us_aqi", "time"))

# a rejected variable -> retry once with the core list; gases come back None
STATE["reject"], STATE["log"] = {"uv_index"}, []
c = make()
rows = c.fetch_batch([(22.3, 87.3)])
assert c.calls_made == 2 and STATE["log"][1]["current"] == [CORE_VARS], STATE["log"]
assert rows[0]["pm25"] == 1.5 and rows[0]["no2"] is None and rows[0]["uv"] is None

# a 400 on the core list too is a real error
STATE["reject"] = {"pm10"}
try:
    make().fetch_batch([(22.3, 87.3)])
    raise SystemExit("expected error")
except OpenMeteoError as e:
    assert e.status == 400

# hourly series: co2 included, then dropped if rejected
STATE["reject"] = set()
s = make().fetch_series(22.3, 87.3)
assert len(s["time"]) == 24 and s["co2"][0] == 1.0 and s["co"][0] == 1.0 and s["pm25"][0] == 1.0
STATE["reject"], STATE["log"] = {HOURLY_CO2}, []
c = make()
s = c.fetch_series(22.3, 87.3)
assert c.calls_made == 2 and "co2" not in s and s["pm25"][0] == 1.0
assert STATE["log"][0]["past_days"] == ["2"] and STATE["log"][0]["forecast_days"] == ["3"]
srv.shutdown()
print("client tests OK")
