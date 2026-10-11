"""Open-Meteo air-quality client: batched, paced, budget-guarded, dependency-free.

Uses `requests` if installed, otherwise a tiny urllib adapter, so the Lambda
needs no pip packages (and `sam build` has nothing to download).

Free tier: 600 calls/min, 5,000/hour, 10,000/day, non-commercial, CC BY 4.0
(credit Open-Meteo). Multi-coordinate counting is undocumented, so the guard
assumes one call per request and we stay far below the caps anyway.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

DEFAULT_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# 10 variables: Open-Meteo bills more than one call above 10, so stay at 10.
CURRENT_VARS = ("pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone,"
                "dust,aerosol_optical_depth,uv_index,us_aqi")
CORE_VARS = "pm10,pm2_5,us_aqi"            # fallback if the full list is ever rejected (HTTP 400)
HOURLY_VARS = ("pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone,"
               "dust,uv_index")
HOURLY_CO2 = "carbon_dioxide"              # separate model; dropped if the API rejects it

# Open-Meteo name -> our key
KEYS = {"pm2_5": "pm25", "pm10": "pm10", "carbon_monoxide": "co",
        "nitrogen_dioxide": "no2", "sulphur_dioxide": "so2", "ozone": "o3",
        "dust": "dust", "aerosol_optical_depth": "aod", "uv_index": "uv",
        "us_aqi": "us_aqi", "carbon_dioxide": "co2"}


class OpenMeteoError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class UrllibSession:
    """Minimal stand-in for requests.Session().get(...)."""

    def get(self, url, params=None, timeout=30.0):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "air-advisor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _Resp(r.status, r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return _Resp(e.code, e.read().decode("utf-8", "replace"))


def default_session():
    try:
        import requests
        return requests.Session()
    except ImportError:
        return UrllibSession()


class OpenMeteoClient:
    def __init__(self, base_url: Optional[str] = None, batch_size: int = 50,
                 min_interval: float = 0.5, max_calls: int = 100,
                 timeout: float = 30.0, session=None,
                 sleep: Callable[[float], None] = time.sleep, verbose: bool = False):
        self.base_url = base_url or os.environ.get("OPENMETEO_URL") or DEFAULT_URL
        self.batch_size = batch_size
        self.min_interval = min_interval
        self.max_calls = max_calls
        self.timeout = timeout
        self.session = session or default_session()
        self._sleep = sleep
        self.verbose = verbose
        self.calls_made = 0
        self._last = 0.0

    def _get(self, params: dict):
        for attempt in range(3):
            if self.calls_made >= self.max_calls:
                raise OpenMeteoError(f"Call budget of {self.max_calls} reached; stopping.")
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self.calls_made += 1
            self._last = time.monotonic()
            try:
                r = self.session.get(self.base_url, params=params, timeout=self.timeout)
            except OSError as exc:   # requests and urllib errors both subclass OSError
                if attempt == 2:
                    raise OpenMeteoError(f"Network error: {exc}") from exc
                self._sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                if attempt >= 1:
                    raise OpenMeteoError("Repeated 429 (rate limited). Stop and wait.", 429)
                if self.verbose:
                    print("  [429, waiting 60s once]")
                self._sleep(60)
                continue
            if r.status_code >= 500 and attempt < 2:
                self._sleep(2 ** attempt)
                continue
            reason = ""
            try:
                reason = r.json().get("reason", "")
            except ValueError:
                reason = r.text[:200]
            raise OpenMeteoError(f"HTTP {r.status_code}: {reason}", r.status_code)
        raise OpenMeteoError("Request failed after retries.")

    def _coords(self, points):
        return {"latitude": ",".join(f"{la:.4f}" for la, _ in points),
                "longitude": ",".join(f"{lo:.4f}" for _, lo in points)}

    def fetch_batch(self, points: list, variables: str = CURRENT_VARS) -> list:
        """points: [(lat, lon), ...] -> list of dicts in the same order.

        Each dict has time, grid_lat, grid_lon and our short keys (pm25, pm10, co,
        no2, so2, o3, dust, aod, uv, us_aqi). CO is in ug/m3 here (scoring converts).
        """
        params = {**self._coords(points), "current": variables, "timezone": "GMT"}
        try:
            data = self._get(params)
        except OpenMeteoError as exc:
            if exc.status == 400 and variables != CORE_VARS:   # a variable was rejected
                if self.verbose:
                    print(f"  [400 with full variable list ({exc}); retrying core variables]")
                params["current"] = CORE_VARS
                data = self._get(params)
            else:
                raise
        items = data if isinstance(data, list) else [data]
        if len(items) != len(points):
            raise OpenMeteoError(
                f"Asked for {len(points)} locations, got {len(items)} back.")
        out = []
        for it in items:
            cur = it.get("current") or {}
            row = {"grid_lat": it.get("latitude"), "grid_lon": it.get("longitude"),
                   "time": cur.get("time")}                    # GMT, e.g. 2026-10-10T06:00
            for api_name, key in KEYS.items():
                row[key] = cur.get(api_name)
            out.append(row)
        return out

    def fetch_series(self, lat: float, lon: float, past_days: int = 2,
                     forecast_days: int = 3) -> dict:
        """Hourly series for ONE location: {"time": [...], "pm25": [...], ...}.

        Times are GMT. CO is ug/m3, CO2 is ppm. If the API rejects carbon_dioxide
        (it comes from a separate model) we retry without it.
        """
        params = {**self._coords([(lat, lon)]), "hourly": f"{HOURLY_VARS},{HOURLY_CO2}",
                  "past_days": past_days, "forecast_days": forecast_days, "timezone": "GMT"}
        try:
            data = self._get(params)
        except OpenMeteoError as exc:
            if exc.status != 400:
                raise
            params["hourly"] = HOURLY_VARS
            data = self._get(params)
        if isinstance(data, list):
            data = data[0] if data else {}
        hourly = data.get("hourly") or {}
        if not hourly.get("time"):
            raise OpenMeteoError("No hourly data returned.")
        out = {"time": hourly["time"], "grid_lat": data.get("latitude"),
               "grid_lon": data.get("longitude")}
        for api_name, key in KEYS.items():
            if api_name in hourly:
                out[key] = hourly[api_name]
        return out

    def fetch_current(self, points: list) -> list:
        results: list = []
        for i in range(0, len(points), self.batch_size):
            results.extend(self.fetch_batch(points[i:i + self.batch_size]))
        return results
