"""Email alerts: create (from the AI chat) and evaluate (after every ingest).

Delivery is Amazon SNS email. One SNS topic, one subscription per email address,
each with a filter policy on a hash of the address, so a message published for
one person is delivered only to that person. SNS sends the recipient a
confirmation email first and puts an unsubscribe link in every message, so nobody
can be signed up without clicking a link. Abuse limits: 3 alerts per address,
25 new alert requests per day for the whole app.

Alert kinds
  below   - tell me when AQI falls to <= threshold   (edge-triggered)
  above   - tell me when AQI rises to >= threshold   (edge-triggered)
  window  - tell me when a stretch of waking hours with AQI <= threshold is coming up soon
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from data import cap_ok, current_for, series_for
from grid import cell_id, in_india_bbox

IST = timezone(timedelta(hours=5, minutes=30))
KINDS = ("below", "above", "window")
MAX_PER_EMAIL = 3
MAX_NEW_PER_DAY = 25
ALERT_DAYS = 30
EDGE_COOLDOWN_H = 6          # below/above: at most one email per 6 h per alert
WINDOW_COOLDOWN_H = 20       # window: at most about one email a day
WINDOW_LOOKAHEAD_H = 12      # only announce windows that start within this many hours
WAKING = (5, 21)             # IST hours when going outside makes sense
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")


def rcpt_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:20]


def _ist(iso_z):
    return datetime.strptime(iso_z, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc).astimezone(IST)


def label(dt):
    return dt.strftime("%a ") + f"{dt.hour % 12 or 12} " + dt.strftime("%p")


def good_windows(S, max_aqi, horizon=48, waking_only=True, min_hours=2):
    """Runs of hours (from 'now' on) with AQI <= max_aqi. -> (windows, lowest_hour or None)."""
    n0, times, aqi = S["now_index"], S["time"], S["aqi"]
    runs, cur, lowest = [], None, None
    for i in range(n0, min(len(aqi), n0 + horizon + 1)):
        t = _ist(times[i])
        awake = (not waking_only) or (WAKING[0] <= t.hour <= WAKING[1])
        v = aqi[i]
        if awake and v is not None and (lowest is None or v < lowest["aqi"]):
            lowest = {"time": label(t), "aqi": v, "_i": i}
        if awake and v is not None and v <= max_aqi:
            cur = [i, i] if cur is None else [cur[0], i]
        elif cur:
            runs.append(cur); cur = None
    if cur:
        runs.append(cur)
    out = []
    for a, b in runs:
        if b - a + 1 < min_hours:
            continue
        vals = aqi[a:b + 1]
        end = _ist(times[b]) + timedelta(hours=1)
        out.append({"start": label(_ist(times[a])), "end": label(end), "hours": b - a + 1,
                    "avg_aqi": round(sum(vals) / len(vals)), "min_aqi": min(vals),
                    "starts_in_hours": a - n0, "_i": a})
    return out, lowest


def create_alert(deps, now, email, place, kind, threshold=None):
    """-> dict for the AI to relay. place = {"name", "lat", "lon"}."""
    email = str(email or "").strip()
    if not EMAIL_RE.match(email) or len(email) > 120:
        return {"error": "That doesn't look like a valid email address."}
    if kind not in KINDS:
        return {"error": f"Alert kind must be one of {', '.join(KINDS)}."}
    try:
        thr = int(threshold) if threshold is not None else 100
    except (TypeError, ValueError):
        return {"error": "The AQI threshold must be a number."}
    if not 20 <= thr <= 500:
        return {"error": "The AQI threshold must be between 20 and 500."}
    lat, lon = place.get("lat"), place.get("lon")
    if lat is None or lon is None or not in_india_bbox(float(lat), float(lon)):
        return {"error": "I need a place in India for the alert."}
    if deps.sns is None or not deps.topic_arn:
        return {"error": "Email alerts aren't switched on for this deployment yet."}

    rcpt = rcpt_hash(email)
    if not cap_ok(deps, now, "newalert", limit=MAX_NEW_PER_DAY):
        return {"error": "Lots of alerts were requested today. Please try again tomorrow."}
    if not cap_ok(deps, now, key=f"rcpt#{rcpt}", limit=MAX_PER_EMAIL, ttl_days=ALERT_DAYS + 1):
        return {"error": f"You already have {MAX_PER_EMAIL} alerts on that address, which is the limit."}
    try:
        deps.sns.subscribe(TopicArn=deps.topic_arn, Protocol="email", Endpoint=email,
                           Attributes={"FilterPolicy": json.dumps({"rcpt": [rcpt]})},
                           ReturnSubscriptionArn=True)
    except Exception as exc:                    # not verified yet, throttled, bad endpoint...
        print(f"sns subscribe failed: {exc!r}")
        return {"error": "I couldn't set up the email subscription right now. Please try again later."}

    aid = uuid.uuid4().hex[:12]
    deps.table.put_item(Item={
        "pk": f"alert#{aid}", "email": email, "rcpt": rcpt, "name": str(place.get("name") or "your place")[:60],
        "lat": str(round(float(lat), 4)), "lon": str(round(float(lon), 4)), "kind": kind, "threshold": thr,
        "state": False, "last_sent_at": 0, "created_at": now.isoformat(timespec="seconds"),
        "expires_at": int(now.timestamp()) + ALERT_DAYS * 86400})
    return {"ok": True, "alert_id": aid, "place": place.get("name"), "kind": kind, "threshold": thr,
            "needs_confirmation": True, "lasts_days": ALERT_DAYS}


def _scan_alerts(table):
    kw = {"FilterExpression": "begins_with(pk, :p)", "ExpressionAttributeValues": {":p": "alert#"}}
    while True:
        r = table.scan(**kw)
        for it in r.get("Items", []):
            yield it
        if not r.get("LastEvaluatedKey"):
            return
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _publish(deps, alert, subject, body):
    deps.sns.publish(TopicArn=deps.topic_arn, Subject=subject[:100],
                     Message=body + "\n\nThese are model estimates (CAMS via Open-Meteo), not readings from a sensor "
                                    "near you. General information, not medical advice.\n"
                                    "You asked India Air Advisor for this alert. Use the unsubscribe link below to "
                                    "stop all alerts to this address.",
                     MessageAttributes={"rcpt": {"DataType": "String", "StringValue": alert["rcpt"]}})


def run_alerts(deps, now):
    """Evaluate every stored alert against fresh data. Never raises for one bad alert."""
    stats = {"checked": 0, "sent": 0, "errors": 0}
    if deps.sns is None or not deps.topic_arn:
        return stats
    for al in _scan_alerts(deps.table):
        stats["checked"] += 1
        try:
            if _eval_one(deps, now, al):
                stats["sent"] += 1
        except Exception as exc:                # keep going; one alert must not block the rest
            stats["errors"] += 1
            print(f"alert {al.get('pk')} failed: {exc!r}")
    return stats


def _eval_one(deps, now, al):
    lat, lon = float(al["lat"]), float(al["lon"])
    thr, kind, name = int(al["threshold"]), al["kind"], al["name"]
    since = now.timestamp() - float(al.get("last_sent_at", 0))
    state, sent = bool(al.get("state")), False

    if kind in ("below", "above"):
        status, cur = current_for(deps, lat, lon, name, now)
        aqi = cur.get("aqi") if status == 200 else None
        if aqi is None:
            return False
        cond = aqi <= thr if kind == "below" else aqi >= thr
        if cond and not state and since >= EDGE_COOLDOWN_H * 3600:
            word = "dropped to" if kind == "below" else "risen to"
            _publish(deps, al, f"Air alert: {name} AQI {aqi}",
                     f"{name}: the estimated AQI is now {aqi} ({cur.get('category')}), which has {word} your "
                     f"threshold of {thr}.")
            sent = True
        new_state = cond if (sent or state or not cond) else False   # blocked by cooldown: retry next run
    else:
        status, body = series_for(deps, lat, lon, now)
        if status != 200:
            return False
        S = json.loads(body)
        wins, _low = good_windows(S, thr)
        soon = [w for w in wins if w["starts_in_hours"] <= WINDOW_LOOKAHEAD_H]
        cond = bool(soon)
        if cond and since >= WINDOW_COOLDOWN_H * 3600 and not state:
            w = soon[0]
            when = "from now" if w["starts_in_hours"] == 0 else f"starting {w['start']}"
            _publish(deps, al, f"Good air window in {name}",
                     f"{name}: the forecast shows an estimated AQI of {w['avg_aqi']} on average "
                     f"(at most {thr}) {when} until {w['end']} IST ({w['hours']} hours).")
            sent = True
        new_state = cond if (sent or state or not cond) else False
    if new_state != state or sent:
        deps.table.update_item(Key={"pk": al["pk"]}, UpdateExpression="SET #s = :s, last_sent_at = :t",
                               ExpressionAttributeNames={"#s": "state"},
                               ExpressionAttributeValues={":s": new_state,
                                                          ":t": int(now.timestamp()) if sent else int(float(al.get("last_sent_at", 0)))})
    return sent
