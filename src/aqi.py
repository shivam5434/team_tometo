"""Pure AQI logic for the air advisor. No network, no AWS, no dependencies.

What lives here
  1. compute_index()   - Indian (CPCB) AQI from PM2.5, PM10, NO2, SO2, O3, CO
  2. category_for()    - Good / Satisfactory / Moderate / Poor / Very Poor / Severe
  3. check_reading()   - sanity checks for raw, unvalidated readings

IMPORTANT caveats (also shown in the UI):
  * CPCB defines AQI from 24-hour averages (8-hour for O3 and CO). Where we only
    have the latest model value, what we compute is an *estimate from the latest
    reading*. series.py also computes a 24-hour-average version.
  * AQI is the highest (worst) sub-index among the pollutants available.
  * The breakpoint tables below are the published CPCB bands as I know them.
    VERIFY them against CPCB's official AQI table before you publish. The
    anchors for the top "Severe" band (marked "assumed") are my assumption: they
    only change the exact number above ~400, never the category.
  * Units: PM2.5, PM10, NO2, SO2, O3 in ug/m3; CO in mg/m3 (not ug/m3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# (conc_lo, conc_hi, index_lo, index_hi) - CPCB form, integer concentrations.
PM25_BANDS = [
    (0, 30, 0, 50),
    (31, 60, 51, 100),
    (61, 90, 101, 200),
    (91, 120, 201, 300),
    (121, 250, 301, 400),
    (251, 380, 401, 500),   # 380 = assumed anchor for index 500
]
PM10_BANDS = [
    (0, 50, 0, 50),
    (51, 100, 51, 100),
    (101, 250, 101, 200),
    (251, 350, 201, 300),
    (351, 430, 301, 400),
    (431, 510, 401, 500),   # 510 = assumed anchor
]
NO2_BANDS = [
    (0, 40, 0, 50),
    (41, 80, 51, 100),
    (81, 180, 101, 200),
    (181, 280, 201, 300),
    (281, 400, 301, 400),
    (401, 800, 401, 500),   # 800 = assumed anchor
]
SO2_BANDS = [
    (0, 40, 0, 50),
    (41, 80, 51, 100),
    (81, 380, 101, 200),
    (381, 800, 201, 300),
    (801, 1600, 301, 400),
    (1601, 2400, 401, 500),  # 2400 = assumed anchor
]
O3_BANDS = [
    (0, 50, 0, 50),
    (51, 100, 51, 100),
    (101, 168, 101, 200),
    (169, 208, 201, 300),
    (209, 748, 301, 400),
    (749, 1000, 401, 500),   # 1000 = assumed anchor
]
# CO is given in mg/m3 with one decimal in the CPCB table, so these bands are in
# units of 0.1 mg/m3 (CO_SCALE converts mg/m3 -> those units).
CO_BANDS = [
    (0, 10, 0, 50),
    (11, 20, 51, 100),
    (21, 100, 101, 200),
    (101, 170, 201, 300),
    (171, 340, 301, 400),
    (341, 500, 401, 500),    # 50 mg/m3 = assumed anchor
]

# key -> (bands, scale from display unit to band unit, display unit)
POLLUTANTS = {
    "pm25": (PM25_BANDS, 1, "µg/m³"),
    "pm10": (PM10_BANDS, 1, "µg/m³"),
    "no2": (NO2_BANDS, 1, "µg/m³"),
    "so2": (SO2_BANDS, 1, "µg/m³"),
    "o3": (O3_BANDS, 1, "µg/m³"),
    "co": (CO_BANDS, 10, "mg/m³"),
}

CATEGORIES = [
    (50, "Good"),
    (100, "Satisfactory"),
    (200, "Moderate"),
    (300, "Poor"),
    (400, "Very Poor"),
    (500, "Severe"),
]

# Sanity-check settings (display units; raw Open-Meteo CO is ug/m3, checked as such)
MAX_PLAUSIBLE = {"pm25": 1000.0, "pm10": 2000.0, "no2": 2000.0, "so2": 5000.0,
                 "o3": 1000.0, "co": 100000.0}
MAX_AGE_HOURS = 3.0                                  # older = "stale"
FUTURE_TOLERANCE_MIN = 15.0                          # clock skew allowance
SPIKE_FACTOR = 5.0                                   # jump vs previous value...
SPIKE_MIN_DELTA = {"pm25": 150.0, "pm10": 300.0}     # ...and at least this big


def _round_half_up(x: float) -> int:
    return int(x + 0.5)


def _sub_index(conc: Optional[float], bands, scale: float = 1) -> Optional[int]:
    """CPCB linear interpolation inside one band. None if conc is unusable."""
    if conc is None or conc != conc or conc < 0:  # None, NaN, negative
        return None
    c = _round_half_up(conc * scale)
    if c > bands[-1][1]:
        return 500
    for bp_lo, bp_hi, i_lo, i_hi in bands:
        if bp_lo <= c <= bp_hi:
            return _round_half_up(
                (i_hi - i_lo) / (bp_hi - bp_lo) * (c - bp_lo) + i_lo
            )
    return None  # unreachable with integer c, kept for safety


def category_for(aqi: int) -> str:
    for upper, name in CATEGORIES:
        if aqi <= upper:
            return name
    return "Severe"


def display_bands() -> dict:
    """Upper bounds of the first five bands per pollutant, in display units.

    Used by the chart to draw category bands, so the numbers live in one place.
    """
    out = {}
    for key, (bands, scale, _unit) in POLLUTANTS.items():
        out[key] = [round(b[1] / scale, 2) for b in bands[:-1]]
    return out


def compute_index(pm25: Optional[float] = None, pm10: Optional[float] = None,
                  no2: Optional[float] = None, so2: Optional[float] = None,
                  o3: Optional[float] = None, co: Optional[float] = None):
    """Return {"aqi", "category", "dominant", "sub_indices"} or None.

    AQI is the worst (maximum) of the available sub-indices. co is in mg/m3.
    """
    given = {"pm25": pm25, "pm10": pm10, "no2": no2, "so2": so2, "o3": o3, "co": co}
    subs = {}
    for key, conc in given.items():
        bands, scale, _unit = POLLUTANTS[key]
        s = _sub_index(conc, bands, scale)
        if s is not None:
            subs[key] = s
    if not subs:
        return None
    dominant = max(subs, key=subs.get)
    aqi = subs[dominant]
    return {
        "aqi": aqi,
        "category": category_for(aqi),
        "dominant": dominant,
        "sub_indices": subs,
    }


# ----------------------------------------------------------------------------
# Sanity checks
# ----------------------------------------------------------------------------
@dataclass
class Check:
    ok: bool                      # safe to use for an AQI?
    value: Optional[float]
    flags: list = field(default_factory=list)      # blocking problems
    warnings: list = field(default_factory=list)   # worth showing, not blocking
    age_hours: Optional[float] = None


def parse_utc(s: str) -> datetime:
    """Parse an ISO-8601 timestamp (with Z or an offset) to aware UTC."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def check_reading(
    param: str,
    value,
    measured_at: Optional[datetime],
    now: Optional[datetime] = None,
    previous: Optional[float] = None,
) -> Check:
    """Validate one raw reading. param is a key of POLLUTANTS."""
    now = now or datetime.now(timezone.utc)
    flags, warnings = [], []

    if value is None:
        return Check(False, None, ["missing"])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Check(False, None, ["not_numeric"])
    value = float(value)
    if value != value:
        return Check(False, None, ["not_numeric"])
    if value < 0:
        flags.append("negative")
    if value > MAX_PLAUSIBLE.get(param, 1000.0):
        flags.append("implausible_high")

    age_hours = None
    if measured_at is None:
        flags.append("no_timestamp")
    else:
        age_hours = (now - measured_at).total_seconds() / 3600.0
        if age_hours > MAX_AGE_HOURS:
            flags.append("stale")
        elif age_hours < -FUTURE_TOLERANCE_MIN / 60.0:
            flags.append("future_timestamp")

    if previous is not None and previous >= 0 and value >= 0:
        delta = abs(value - previous)
        big_ratio = value > previous * SPIKE_FACTOR or (
            previous > 0 and value < previous / SPIKE_FACTOR
        )
        if big_ratio and delta >= SPIKE_MIN_DELTA.get(param, 150.0):
            warnings.append("spike")  # may be real (smoke, fireworks)

    return Check(not flags, value, flags, warnings, age_hours)


def cross_check_pm(pm25: Optional[float], pm10: Optional[float]) -> list:
    """PM2.5 is a subset of PM10, so PM2.5 clearly above PM10 is suspicious."""
    if pm25 is None or pm10 is None:
        return []
    if pm25 > pm10 * 1.1 and pm25 - pm10 > 5:
        return ["pm25_exceeds_pm10"]
    return []
