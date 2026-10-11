"""Ingest Lambda: Open-Meteo -> score -> DynamoDB (one item per grid cell) + S3 snapshot.

Triggered by EventBridge every 3 hours (the model only updates 3-hourly).
run_ingest() takes its dependencies as arguments so it can be tested and run
locally without AWS.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

from cities import POINTS
from openmeteo_client import OpenMeteoClient, OpenMeteoError
from scoring import score

SNAPSHOT_KEY = "snapshot/stations.json"
ATTRIBUTION = ("Air-quality data: Open-Meteo.com (CC BY 4.0), based on Copernicus "
               "Atmosphere Monitoring Service (CAMS) model forecasts. Modelled "
               "estimates, not station measurements. AQI is estimated from the "
               "latest model values of PM2.5, PM10, NO2, SO2, O3 and CO using CPCB "
               "breakpoints.")

# Fields copied into the S3 snapshot (what the map and detail card need).
STATION_FIELDS = ("aqi", "category", "dominant", "sub", "pm25", "pm10", "no2", "so2", "o3",
                  "co", "dust", "aod", "uv", "us_aqi", "time", "warnings")


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        return v
    return round(float(v), 3)


def _station(it):
    st = {"id": it["pk"], "name": it["name"], "lat": it["lat"], "lon": it["lon"],
          "t": it["tier"]}
    for k in STATION_FIELDS:
        v = it.get(k)
        st[k] = {a: _num(b) for a, b in v.items()} if isinstance(v, dict) else _num(v)
    return st


def _to_dynamo(item: dict) -> dict:
    """DynamoDB wants Decimal, not float."""
    return json.loads(json.dumps(item, default=lambda o: float(o) if isinstance(o, Decimal) else str(o)),
                      parse_float=Decimal)


def run_ingest(client, table, s3, bucket, now=None, points=POINTS,
               ttl_hours=12, batch_size=50) -> dict:
    now = now or datetime.now(timezone.utc)
    expires_at = int(now.timestamp()) + ttl_hours * 3600
    fetched_at = now.isoformat(timespec="seconds")

    items, failed_batches, errors = [], 0, []
    for i in range(0, len(points), batch_size):
        chunk = points[i:i + batch_size]
        try:
            recs = client.fetch_batch([(p["lat"], p["lon"]) for p in chunk])
        except OpenMeteoError as exc:
            failed_batches += 1
            errors.append(str(exc))
            continue
        for p, rec in zip(chunk, recs):
            it = score(p, rec, now)
            it["fetched_at"] = fetched_at
            it["expires_at"] = expires_at
            items.append(it)

    if not items:
        raise RuntimeError(f"All batches failed: {errors}")

    with table.batch_writer() as bw:
        for it in items:
            bw.put_item(Item=_to_dynamo(it))

    stations = [_station(it) for it in items]
    snapshot = {"generated_at": fetched_at, "source": "open-meteo",
                "attribution": ATTRIBUTION, "count": len(stations), "stations": stations}
    s3.put_object(Bucket=bucket, Key=SNAPSHOT_KEY,
                  Body=json.dumps(snapshot).encode("utf-8"),
                  ContentType="application/json", CacheControl="max-age=300")

    cats = ("Good", "Satisfactory", "Moderate", "Poor", "Very Poor", "Severe")
    summary = {
        "stored": len(items), "unknown": sum(it["aqi"] is None for it in items),
        "failed_batches": failed_batches, "errors": errors,
        "api_calls": getattr(client, "calls_made", None),
        "categories": {c: sum(it["category"] == c for it in items) for c in cats},
    }
    return summary


def lambda_handler(event, context):
    import boto3   # available in the Lambda runtime
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    s3 = boto3.client("s3")
    started = time.time()
    summary = run_ingest(OpenMeteoClient(max_calls=40, min_interval=float(os.environ.get("BATCH_GAP_SECONDS", "8"))), table, s3, os.environ["BUCKET_NAME"],
                         ttl_hours=int(os.environ.get("TTL_HOURS", "12")))
    topic = os.environ.get("ALERT_TOPIC_ARN", "")
    if topic:                       # evaluate email alerts on the fresh data; never fail the ingest
        try:
            from datetime import datetime, timezone
            from data import Deps
            import alerts
            deps = Deps(table=table, s3=s3, bucket=os.environ["BUCKET_NAME"], sns=boto3.client("sns"),
                        topic_arn=topic, client_factory=lambda: OpenMeteoClient(max_calls=3), daily_cap=500)
            summary["alerts"] = alerts.run_alerts(deps, datetime.now(timezone.utc))
        except Exception as exc:
            print(f"alerts failed: {exc!r}")
            summary["alerts"] = {"error": type(exc).__name__}
    summary["seconds"] = round(time.time() - started, 1)
    print(json.dumps(summary))     # shows up in CloudWatch Logs
    return summary
