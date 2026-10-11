"""Turn one raw Open-Meteo record into the item we store. Pure; no I/O.

AQI basis: the worst CPCB sub-index among PM2.5, PM10, NO2, SO2, O3 and CO, from
the latest model values. EXCEPT when modelled PM10 looks like a dust artefact
(PM10 >= 150 and more than 4x PM2.5): then PM10 is ignored and the item carries
a "possible_dust" warning. Both numbers are tunable below. Real station
readings, if ever added, should use every pollutant as-is.

Units in the stored item: ug/m3 for particles and gases, mg/m3 for CO (Open-Meteo
sends CO in ug/m3; we convert once, here).
"""
from __future__ import annotations

from datetime import datetime

from aqi import (POLLUTANTS, check_reading, compute_index, cross_check_pm,
                 parse_utc)

DUST_RATIO = 4.0
DUST_MIN_PM10 = 150.0
SOURCE = "open-meteo"
EXTRAS = ("dust", "aod", "uv", "us_aqi")      # shown for information, not in the AQI


def guard_pm10(pm25, pm10):
    """Return (pm10 to use in the AQI, True if it was dropped as dust)."""
    if pm25 and pm10 and pm10 >= DUST_MIN_PM10 and pm10 / pm25 > DUST_RATIO:
        return None, True
    return pm10, False


def aqi_from_values(vals: dict):
    """vals in display units (CO in mg/m3). Applies the dust guard. -> (result, dust)."""
    pm10_used, dust = guard_pm10(vals.get("pm25"), vals.get("pm10"))
    res = compute_index(pm25=vals.get("pm25"), pm10=pm10_used, no2=vals.get("no2"),
                        so2=vals.get("so2"), o3=vals.get("o3"), co=vals.get("co"))
    return res, dust


def _co_mgm3(v):
    return round(v / 1000.0, 3) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def score(point: dict, rec: dict, now: datetime) -> dict:
    t = None
    if rec.get("time"):
        try:
            t = parse_utc(rec["time"] + "Z")      # Open-Meteo time is GMT
        except ValueError:
            t = None

    vals, flags, warnings = {}, [], []
    for key in POLLUTANTS:
        chk = check_reading(key, rec.get(key), t, now=now)
        if chk.ok:
            vals[key] = chk.value / 1000.0 if key == "co" else chk.value
        # PM2.5 flags are unprefixed; the rest are prefixed. A gas that is simply
        # absent is not worth a flag; a gas that is present but bad is.
        if key == "pm25":
            flags += chk.flags
            warnings += chk.warnings
        elif chk.flags and rec.get(key) is not None:
            flags += [f"{key}_{f}" for f in chk.flags]
            warnings += [f"{key}_{w}" for w in chk.warnings]
        elif key == "pm10":
            flags += [f"pm10_{f}" for f in chk.flags]
    flags += cross_check_pm(vals.get("pm25"), vals.get("pm10"))

    res, dust = aqi_from_values(vals)
    if dust:
        warnings.append("possible_dust")

    item = {
        "pk": point["cell"], "name": point["name"], "tier": point.get("tier", 3),
        "lat": point["lat"], "lon": point["lon"],
        "time": rec.get("time"), "source": SOURCE,
        "pm25": rec.get("pm25"), "pm10": rec.get("pm10"),
        "no2": rec.get("no2"), "so2": rec.get("so2"), "o3": rec.get("o3"),
        "co": _co_mgm3(rec.get("co")),
        "dust": rec.get("dust"), "aod": rec.get("aod"), "uv": rec.get("uv"),
        "us_aqi": rec.get("us_aqi"),
        "flags": flags, "warnings": warnings,
    }
    if res is None:
        item.update(aqi=None, category=None, dominant=None, basis=None, sub={})
        return item
    used = [k for k in POLLUTANTS if k in res["sub_indices"] and not (k == "pm10" and dust)]
    item.update(aqi=res["aqi"], category=res["category"], dominant=res["dominant"],
                basis="+".join(used), sub=res["sub_indices"])
    return item
