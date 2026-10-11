"""Tests the page's heatmap maths against the Python AQI code (needs Node). Run from phase2/:  python tests/test_heat.py"""
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).parent.parent
sys.path.insert(0, str(root / "src"))
from scoring import aqi_from_values  # noqa: E402

if not shutil.which("node"):
    print("heat tests SKIPPED (node not installed)")
    sys.exit(0)

html = (root / "src/web/index.html").read_text(encoding="utf-8")
m = re.search(r"// HEAT-START\n(.*?)// HEAT-END", html, re.S)
assert m, "heat block markers missing"

random.seed(7)
ranges = {"pm25": 400, "pm10": 600, "no2": 500, "so2": 1500, "o3": 400, "co": 30}
cases = []
for _ in range(600):
    v = {k: round(random.uniform(0, hi), random.choice([0, 1, 2])) for k, hi in ranges.items() if random.random() > 0.15}
    if random.random() < 0.2 and "pm25" in v:
        v["pm10"] = round(v["pm25"] * random.uniform(4, 9) + 150, 1)      # dust-guard territory
    cases.append(v)
cases += [{}, {"pm25": 0}, {"co": 0.05}, {"pm25": 30.4}, {"pm25": 30.5}, {"pm10": 600}]
expect = []
for v in cases:
    res, _ = aqi_from_values(v)
    expect.append(None if res is None else [res["aqi"], res["dominant"]])

js = m.group(1) + """
var cases = %s, expect = %s, bad = [];
cases.forEach(function (v, i) {
  var r = heatAqi(v), e = expect[i];
  if (e === null) { if (r !== null) bad.push("case " + i + ": wanted null"); return; }
  if (!r || r.aqi !== e[0]) bad.push("case " + i + " " + JSON.stringify(v) + ": js " + (r && r.aqi) + " py " + e[0]);
  else if (r.dominant !== e[1]) bad.push("case " + i + ": dominant " + r.dominant + " vs " + e[1]);
});
// heatClean
if (heatClean("pm25", -1) !== null || heatClean("pm25", NaN) !== null || heatClean("pm25", 5000) !== null || heatClean("pm25", 12) !== 12) bad.push("heatClean");
// heatSample: a constant field returns the constant; full data -> a = 1
var n = HEAT_NLAT * HEAT_NLON, vals = new Float32Array(n).fill(42);
var s = heatSample(vals, 20.3, 77.7);
if (!s || Math.abs(s.v - 42) > 1e-4 || Math.abs(s.a - 1) > 1e-6) bad.push("constant field " + JSON.stringify(s));
// outside the grid
if (heatSample(vals, 5, 77) !== null || heatSample(vals, 20, 98) !== null) bad.push("outside grid");
// edges are inside
if (!heatSample(vals, HEAT_LAT0 + HEAT_NLAT - 1, HEAT_LON0 + HEAT_NLON - 1) || !heatSample(vals, HEAT_LAT0, HEAT_LON0)) bad.push("edge");
// a missing corner lowers a but the value is the mean of the rest
var v2 = new Float32Array(n).fill(NaN);
v2[(20 - HEAT_LAT0) * HEAT_NLON + (77 - HEAT_LON0)] = 10;
v2[(20 - HEAT_LAT0) * HEAT_NLON + (78 - HEAT_LON0)] = 30;
var s2 = heatSample(v2, 20.5, 77.5);
if (!s2 || Math.abs(s2.v - 20) > 1e-4 || Math.abs(s2.a - 0.5) > 1e-6) bad.push("missing corners " + JSON.stringify(s2));
if (heatSample(v2, 30.5, 90.5) !== null) bad.push("all-missing should be null");
// bilinear is exact at a grid point
var s3 = heatSample(v2, 20, 78);
if (!s3 || Math.abs(s3.v - 30) > 1e-4) bad.push("grid point " + JSON.stringify(s3));
// heatVals: layer key and aqi
var cells = new Array(n); cells[0] = { pm25: 100, pm10: 50, no2: 10 }; cells[1] = { pm25: null };
var pv = heatVals(cells, "pm25"), av = heatVals(cells, "aqi");
if (pv[0] !== 100 || pv[1] === pv[1] || pv[5] === pv[5]) bad.push("heatVals pm25");
if (!(av[0] >= 100) || av[1] === av[1]) bad.push("heatVals aqi");
// heatColor ends and monotone blend
var cols = [[0, 100, 0], [255, 255, 0], [255, 0, 0]], ups = [50, 100];
var c0 = heatColor(ups, cols, 0), c9 = heatColor(ups, cols, 9999), cm = heatColor(ups, cols, 50);
if (c0[0] !== 0 || c9[0] !== 255 || c9[1] !== 0) bad.push("color ends");
if (!(cm[0] > 0 && cm[0] < 255 || cm[0] === 255)) bad.push("color mid");
// grid sanity: unique, inside the box, and the indexing used by the page is collision-free
var seen = {};
HEAT_GRID.forEach(function (g) {
  var k = g[0] + "," + g[1];
  if (seen[k]) bad.push("duplicate grid point " + k); seen[k] = 1;
  if (g[0] < HEAT_LAT0 || g[0] > HEAT_LAT0 + HEAT_NLAT - 1 || g[1] < HEAT_LON0 || g[1] > HEAT_LON0 + HEAT_NLON - 1) bad.push("grid point outside " + k);
});
console.log(JSON.stringify(bad.slice(0, 20)));
""" % (json.dumps(cases), json.dumps(expect))
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
    f.write(js)
out = subprocess.run(["node", f.name], capture_output=True, text=True)
assert out.returncode == 0, out.stderr
bad = json.loads(out.stdout.strip().splitlines()[-1])
assert not bad, "\n".join(bad)
print(f"heat tests OK ({len(cases)} AQI cases vs Python)")
