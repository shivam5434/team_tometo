"""Shared data access for the API, the AI advisor and the alerts.

One place knows how to get "the air right now" and "the hourly series" for a
0.1-degree cell: DynamoDB first (fresh), Open-Meteo second (live, capped), old
data if the source is down. The HTTP routes and the AI tools both call these, so
the chat can never show a number the map would not.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from aqi import parse_utc
from grid import cell_center, cell_id, in_india_bbox
from handler import _to_dynamo
from openmeteo_client import OpenMeteoClient, OpenMeteoError
from scoring import score
from series import build_series

FRESH_HOURS = 4
LOOKUP_TTL_HOURS = 12
SERIES_FRESH_HOURS = 3
SERIES_TTL_HOURS = 8
DEFAULT_DAILY_CAP = 3000          # live Open-Meteo calls per day (lookups + series)
DEFAULT_CHAT_CAP = 400            # chat messages per day

PUBLIC_FIELDS = ("name", "lat", "lon", "aqi", "category", "dominant", "basis", "sub",
                 "pm25", "pm10", "no2", "so2", "o3", "co", "dust", "aod", "uv", "us_aqi",
                 "time", "fetched_at", "warnings", "flags", "source")


@dataclass
class Deps:
    table: object
    s3: object
    bucket: str
    client_factory: object = lambda: OpenMeteoClient(max_calls=3)
    daily_cap: int = DEFAULT_DAILY_CAP
    cache: dict = field(default_factory=dict)
    # AI advisor + alerts (all optional: without them the rule-based advisor still works)
    model_id: str = ""                # Bedrock model id; empty = rules only
    bedrock_region: str = ""
    model_factory: object = None      # optional: callable returning a Strands model (tests, other providers)
    # Any OpenAI-compatible provider (Groq, Gemini, OpenRouter...). The API key is read from SSM Parameter Store.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_key_param: str = "/air-advisor/llm-key"
    ssm: object = None
    sns: object = None
    topic_arn: str = ""
    chat_cap: int = DEFAULT_CHAT_CAP


def default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    return str(o)


def num(params, key):
    try:
        v = float(params.get(key))
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def clean_name(raw):
    if not raw:
        return None
    name = re.sub(r"[^\w\s,.'()-]", "", str(raw), flags=re.UNICODE).strip()[:60]
    return name or None


def is_fresh(item, now, hours=FRESH_HOURS):
    try:
        t = parse_utc(str(item["fetched_at"]))
    except (KeyError, ValueError):
        return False
    return now - t < timedelta(hours=hours)


def cap_ok(deps, now, kind="cap", limit=None, key=None, ttl_days=3):
    """Atomically count uses of `kind` (per day, or under `key`); False once the limit is reached."""
    day = now.strftime("%Y-%m-%d")
    try:
        deps.table.update_item(
            Key={"pk": key or f"{kind}#{day}"},
            UpdateExpression="ADD n :one SET expires_at = :exp",
            ConditionExpression="attribute_not_exists(n) OR n < :max",
            ExpressionAttributeValues={":one": 1, ":max": limit if limit is not None else deps.daily_cap,
                                       ":exp": int(now.timestamp()) + ttl_days * 86400},
        )
        return True
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return False
        raise


def public(item, name=None):
    out = {k: item.get(k) for k in PUBLIC_FIELDS}
    out["cell"] = item.get("pk")
    if name:
        out["name"] = name
    return out


def check_coords(lat, lon):
    """-> error message or None."""
    if lat is None or lon is None:
        return "lat and lon are required numbers"
    if not in_india_bbox(lat, lon):
        return "Only places in India are supported right now."
    return None


def current_for(deps, lat, lon, name, now):
    """The air right now for the cell containing (lat, lon). -> (http status, dict)."""
    cid = cell_id(lat, lon)
    item = deps.table.get_item(Key={"pk": cid}).get("Item")
    if item and is_fresh(item, now):
        return 200, public(item, name)

    if not cap_ok(deps, now):
        return 429, {"error": "Lots of people are checking right now. "
                              "Pick a marked city on the map or try again later."}
    clat, clon = cell_center(lat, lon)
    point = {"name": (item or {}).get("name") or f"{clat:.1f}, {clon:.1f}", "cell": cid,
             "lat": float((item or {}).get("lat", clat)), "lon": float((item or {}).get("lon", clon)),
             "tier": int((item or {}).get("tier", 3))}
    try:
        rec = deps.client_factory().fetch_batch([(clat, clon)])[0]
    except OpenMeteoError as exc:
        print(f"open-meteo failed: {exc}")
        if item:                                   # serve slightly old data rather than nothing
            return 200, {**public(item, name), "stale": True}
        return 502, {"error": "The air-quality source is unavailable. Try again shortly."}

    new = score(point, rec, now)
    new["fetched_at"] = now.isoformat(timespec="seconds")
    new["expires_at"] = int(now.timestamp()) + LOOKUP_TTL_HOURS * 3600
    deps.table.put_item(Item=_to_dynamo(new))
    return 200, public(new, name)


def series_for(deps, lat, lon, now):
    """Past 48 h + next 48 h for the cell. -> (http status, JSON string or error dict)."""
    pk = f"series#{cell_id(lat, lon)}"
    item = deps.table.get_item(Key={"pk": pk}).get("Item")
    if item and is_fresh(item, now, SERIES_FRESH_HOURS):
        return 200, str(item["body"])
    if not cap_ok(deps, now):
        if item:                                    # over the cap: old data beats none
            return 200, str(item["body"])
        return 429, {"error": "Lots of people are checking right now. Try again later."}
    clat, clon = cell_center(lat, lon)
    try:
        raw = deps.client_factory().fetch_series(clat, clon)
        body = json.dumps(build_series(raw, now), default=default)
    except (OpenMeteoError, ValueError, KeyError) as exc:
        print(f"series failed: {exc!r}")
        if item:
            return 200, str(item["body"])
        return 502, {"error": "The forecast source is unavailable. Try again shortly."}
    deps.table.put_item(Item={"pk": pk, "body": body,
                              "fetched_at": now.isoformat(timespec="seconds"),
                              "expires_at": int(now.timestamp()) + SERIES_TTL_HOURS * 3600})
    return 200, body
