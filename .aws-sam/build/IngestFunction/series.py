"""Hourly past + forecast series for one place. Pure; no I/O.

Input: the raw hourly dict from OpenMeteoClient.fetch_series (GMT times, CO in
ug/m3, CO2 in ppm). Output: a compact columnar dict for the chart.

Two AQI numbers come out of it:
  * "aqi"       - one per hour, from that hour's values (same method as the map
                  markers), so the chart's "now" point matches the marker.
  * "aqi_24h"   - the CPCB-style number at "now": 24-hour mean for PM2.5, PM10,
                  NO2, SO2 and 8-hour mean for O3 and CO, worst sub-index wins.
                  This is the method the official CPCB AQI uses, so it is the one
                  to compare with CPCB / aqi.in.
Everything is a model estimate; past hours are modelled too, not measured.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aqi import POLLUTANTS, display_bands, parse_utc
from scoring import aqi_from_values

KEYS = ("pm25", "pm10", "no2", "so2", "o3", "co")          # the six AQI pollutants
EXTRA = ("co2", "dust", "uv")                              # shown, not in the AQI
WINDOW = {"pm25": 24, "pm10": 24, "no2": 24, "so2": 24, "o3": 8, "co": 8}
MIN_FRACTION = 0.67                                        # need this share of the window
UNITS = {"pm25": "µg/m³", "pm10": "µg/m³", "no2": "µg/m³", "so2": "µg/m³",
         "o3": "µg/m³", "co": "mg/m³", "co2": "ppm", "dust": "µg/m³", "uv": ""}


def _val(raw, key, i):
    arr = raw.get(key)
    if arr is None or i >= len(arr):
        return None
    v = arr[i]
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
        return None
    return v / 1000.0 if key == "co" else float(v)       # CO: ug/m3 -> mg/m3


def _mean(raw, key, end, n):
    vals = [_val(raw, key, i) for i in range(max(0, end - n + 1), end + 1)]
    ok = [v for v in vals if v is not None]
    if len(ok) < max(1, int(n * MIN_FRACTION)):
        return None
    return sum(ok) / len(ok)


def _r(v, nd=1):
    return None if v is None else round(v, nd)


def build_series(raw: dict, now: datetime, back: int = 48, ahead: int = 48) -> dict:
    times = [parse_utc(t + "Z") for t in raw["time"]]
    now_hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    idx_now = None
    for i, t in enumerate(times):
        if t <= now_hour:
            idx_now = i
        else:
            break
    if idx_now is None:
        raise ValueError("series starts after now")
    lo, hi = max(0, idx_now - back), min(len(times), idx_now + ahead + 1)

    cols = {k: [] for k in KEYS + EXTRA}
    aqi, dom = [], []
    for i in range(lo, hi):
        vals = {k: _val(raw, k, i) for k in KEYS}
        for k in KEYS:
            cols[k].append(_r(vals[k], 3 if k == "co" else 1))
        for k in EXTRA:
            cols[k].append(_r(_val(raw, k, i), 1))
        res, _dust = aqi_from_values({k: v for k, v in vals.items() if v is not None})
        aqi.append(res["aqi"] if res else None)
        dom.append(res["dominant"] if res else None)

    means = {k: _mean(raw, k, idx_now, WINDOW[k]) for k in KEYS}
    res24, dust24 = aqi_from_values({k: v for k, v in means.items() if v is not None})
    aqi_24h = None
    if res24:
        aqi_24h = {"aqi": res24["aqi"], "category": res24["category"],
                   "dominant": res24["dominant"], "sub": res24["sub_indices"],
                   "windows_h": {"pm25": 24, "pm10": 24, "no2": 24, "so2": 24, "o3": 8, "co": 8},
                   "possible_dust": dust24}

    return {
        "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "grid": [raw.get("grid_lat"), raw.get("grid_lon")],
        "time": [t.strftime("%Y-%m-%dT%H:%MZ") for t in times[lo:hi]],
        "now_index": idx_now - lo,
        "aqi": aqi, "dominant": dom,
        **cols,
        "aqi_24h": aqi_24h,
        "units": UNITS,
        "bands": display_bands(),
    }
