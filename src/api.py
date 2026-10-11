"""API Lambda (HTTP API, payload v2).

  GET /          -> the map page (src/web/index.html)
  GET /stations  -> the S3 snapshot written by the ingest Lambda
  GET /series?lat=..&lon=..
                 -> hourly pollutants for the past 48 h and next 48 h (model data),
                    plus a CPCB-style 24-hour-average AQI. Cached in DynamoDB.
  POST /alert    -> create an email alert directly (same rules as the advisor's create_alert)
  POST /chat     -> the AI advisor (see advisor.py). Body: {"messages": [...], "context": {...}}
  GET /lookup?lat=..&lon=..[&name=..]
                 -> air quality for any place in India. Served from DynamoDB if
                    the 0.1-degree cell was fetched in the last FRESH_HOURS,
                    otherwise fetched live from Open-Meteo and stored.

A daily cap on live lookups protects the Open-Meteo free-tier quota
(10,000 calls/day); past it, /lookup returns 429 and the map keeps working.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from data import (DEFAULT_CHAT_CAP, DEFAULT_DAILY_CAP, Deps, cap_ok, check_coords,   # noqa: F401
                  clean_name, current_for, default, num, series_for)
from handler import SNAPSHOT_KEY

SNAPSHOT_CACHE_S = 60
WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 6000                     # bytes accepted by POST /chat


def _json(status, body, max_age=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    headers["Cache-Control"] = f"public, max-age={max_age}" if max_age else "no-store"
    return {"statusCode": status, "headers": headers,
            "body": body if isinstance(body, str) else json.dumps(body, default=default)}


def _html():
    try:
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    except OSError:
        return _json(404, {"error": "page not bundled"})
    return {"statusCode": 200, "body": page,
            "headers": {"Content-Type": "text/html; charset=utf-8",
                        "Cache-Control": "public, max-age=300"}}


def _stations(deps, now):
    c = deps.cache
    ts = c.get("at")
    if ts is None or (now - ts).total_seconds() > SNAPSHOT_CACHE_S:
        try:
            obj = deps.s3.get_object(Bucket=deps.bucket, Key=SNAPSHOT_KEY)
            c["body"] = obj["Body"].read().decode("utf-8")
            c["at"] = now
        except Exception as exc:                 # no snapshot yet / S3 problem
            print(f"snapshot read failed: {exc!r}")
            if "body" not in c:
                return _json(503, {"error": "No air-quality data yet. Try again in a few minutes."})
    return _json(200, c["body"], max_age=60)









def _lookup(params, deps, now):
    lat, lon = num(params, "lat"), num(params, "lon")
    err = check_coords(lat, lon)
    if err:
        return _json(400, {"error": err})
    status, payload = current_for(deps, lat, lon, clean_name(params.get("name")), now)
    return _json(status, payload, max_age=300 if status == 200 and not payload.get("stale") else None)


def _series(params, deps, now):
    lat, lon = num(params, "lat"), num(params, "lon")
    err = check_coords(lat, lon)
    if err:
        return _json(400, {"error": err})
    status, body = series_for(deps, lat, lon, now)
    return _json(status, body, max_age=300 if status == 200 else None)


def _chat(event, deps, now):
    from advisor import handle_chat            # imported lazily: keeps GET routes light
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw.encode("utf-8")) > MAX_BODY:
        return _json(413, {"error": "That message is too long."})
    try:
        body = json.loads(raw)
    except ValueError:
        return _json(400, {"error": "Send JSON like {\"messages\": [...]}."})
    if not isinstance(body, dict):
        return _json(400, {"error": "Send JSON like {\"messages\": [...]}."})
    status, payload = handle_chat(body, deps, now)
    return _json(status, payload)


def _alert(event, deps, now):
    """POST /alert  {"email", "place": {"name","lat","lon"}, "kind", "threshold"} -> alerts.create_alert."""
    import alerts
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw.encode("utf-8")) > MAX_BODY:
        return _json(413, {"error": "That request is too long."})
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    place = body.get("place") if isinstance(body, dict) else None
    if not isinstance(body, dict) or not isinstance(place, dict):
        return _json(400, {"error": "Send JSON like {\"email\", \"place\", \"kind\", \"threshold\"}."})
    try:
        lat, lon = float(place.get("lat")), float(place.get("lon"))
    except (TypeError, ValueError):
        return _json(400, {"error": "I need a place in India for the alert."})
    res = alerts.create_alert(deps, now, body.get("email"),
                              {"name": clean_name(place.get("name")) or "your place", "lat": lat, "lon": lon},
                              body.get("kind"), body.get("threshold"))
    return _json(200 if res.get("ok") else 400, res)


def handle(event, deps, now=None):
    now = now or datetime.now(timezone.utc)
    http = (event.get("requestContext") or {}).get("http") or {}
    method, path = http.get("method", "GET"), event.get("rawPath", "/")
    if method == "POST" and path == "/chat":
        return _chat(event, deps, now)
    if method == "POST" and path == "/alert":
        return _alert(event, deps, now)
    if method != "GET":
        return _json(405, {"error": "method not allowed"})
    if path == "/":
        return _html()
    if path == "/stations":
        return _stations(deps, now)
    if path == "/series":
        return _series(event.get("queryStringParameters") or {}, deps, now)
    if path == "/lookup":
        return _lookup(event.get("queryStringParameters") or {}, deps, now)
    return _json(404, {"error": "not found"})


_DEPS = None


def lambda_handler(event, context):
    global _DEPS
    if _DEPS is None:
        import boto3
        model_id = os.environ.get("BEDROCK_MODEL_ID", "")
        topic = os.environ.get("ALERT_TOPIC_ARN", "")
        _DEPS = Deps(table=boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"]),
                     s3=boto3.client("s3"), bucket=os.environ["BUCKET_NAME"],
                     daily_cap=int(os.environ.get("DAILY_LOOKUP_CAP", DEFAULT_DAILY_CAP)),
                     chat_cap=int(os.environ.get("DAILY_CHAT_CAP", DEFAULT_CHAT_CAP)),
                     model_id=model_id,
                     bedrock_region=os.environ.get("BEDROCK_REGION", ""),
                     llm_base_url=os.environ.get("LLM_BASE_URL", ""), llm_model=os.environ.get("LLM_MODEL", ""),
                     ssm=boto3.client("ssm") if os.environ.get("LLM_BASE_URL") else None,
                     sns=boto3.client("sns") if topic else None, topic_arn=topic)
    return handle(event, _DEPS)
