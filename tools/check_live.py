"""Live check of the new Open-Meteo variables (run from phase2/):  python tools/check_live.py

Calls the real API for Kharagpur and Delhi and prints, for each variable, whether
a value came back. Paste the output back if anything shows MISSING.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from openmeteo_client import OpenMeteoClient          # noqa: E402
from scoring import score                             # noqa: E402
from series import build_series                      # noqa: E402


def main(client=None):
    now = datetime.now(timezone.utc)
    c = client or OpenMeteoClient(max_calls=6, verbose=True)
    pts = [(22.3, 87.3), (28.6, 77.2)]
    rows = c.fetch_batch(pts)
    for (la, lo), r in zip(pts, rows):
        print(f"\n== current @ {la},{lo}  time={r['time']} (GMT)")
        for k in ("pm25", "pm10", "no2", "so2", "o3", "co", "dust", "aod", "uv", "us_aqi"):
            print(f"  {k:7} {'MISSING' if r.get(k) is None else r[k]}")
        it = score({"name": "x", "cell": "0_0", "lat": la, "lon": lo}, r, now)
        print("  -> AQI", it["aqi"], it["category"], "dominant", it["dominant"], "sub", it["sub"], "flags", it["flags"])
    s = c.fetch_series(*pts[0])
    print(f"\n== hourly series: {len(s['time'])} hours, {s['time'][0]} .. {s['time'][-1]}")
    for k in ("pm25", "pm10", "no2", "so2", "o3", "co", "co2", "dust", "uv"):
        vals = s.get(k)
        nn = sum(v is not None for v in vals) if vals else 0
        print(f"  {k:5} {'MISSING (not returned)' if vals is None else f'{nn}/{len(vals)} values, e.g. {vals[60]}'}")
    b = build_series(s, now)
    print("\n24-hour-average AQI:", b["aqi_24h"] and (b["aqi_24h"]["aqi"], b["aqi_24h"]["category"], b["aqi_24h"]["dominant"]))
    print("hourly AQI now:", b["aqi"][b["now_index"]], "| next 12 h:", b["aqi"][b["now_index"] + 1:b["now_index"] + 13])


if __name__ == "__main__":
    main()
