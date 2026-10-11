"""Offline tests for advisor.py, alerts.py, places.py.  Run from phase2/:  python tests/test_advisor.py"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import advisor, alerts                                  # noqa: E402
from data import Deps                                   # noqa: E402
from places import find_places, find_places_in_text     # noqa: E402
from test_api import FakeClient, FakeS3, Err            # noqa: E402

NOW = datetime(2026, 10, 10, 6, 20, tzinfo=timezone.utc)


class Table:
    """In-memory DynamoDB stand-in: get/put/scan/update_item for counters and alert state."""
    def __init__(self):
        self.items = {}
    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it else {}
    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)
    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None,
                    ExpressionAttributeNames=None):
        it = self.items.setdefault(Key["pk"], {"pk": Key["pk"]})
        v = ExpressionAttributeValues
        if ":one" in v:                                     # counter
            if it.get("n", 0) >= v[":max"]:
                raise Err("ConditionalCheckFailedException")
            it["n"] = it.get("n", 0) + 1
            it["expires_at"] = v[":exp"]
        else:                                               # alert state
            it["state"], it["last_sent_at"] = v[":s"], v[":t"]
    def scan(self, FilterExpression=None, ExpressionAttributeValues=None, **kw):
        p = ExpressionAttributeValues[":p"]
        return {"Items": [dict(i) for k, i in self.items.items() if k.startswith(p)]}


class SNS:
    def __init__(self, fail=False): self.subs, self.pubs, self.fail = [], [], fail
    def subscribe(self, **kw):
        if self.fail: raise RuntimeError("boom")
        self.subs.append(kw); return {"SubscriptionArn": "pending confirmation"}
    def publish(self, **kw): self.pubs.append(kw); return {}


from strands.models import Model                         # noqa: E402


class Scripted(Model):
    """A Strands model that plays back scripted turns: ("tool", name, input) or ("text", str) or an exception."""
    def __init__(self, script):
        self.script, self.calls = list(script), []
    def update_config(self, **kw): pass
    def get_config(self): return {}
    async def structured_output(self, *a, **k):
        raise NotImplementedError
        yield
    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        self.calls.append({"messages": json.loads(json.dumps(messages, default=str)), "tools": tool_specs,
                           "system": system_prompt})
        step = self.script.pop(0) if self.script else self.script_last
        if isinstance(step, Exception):
            raise step
        yield {"messageStart": {"role": "assistant"}}
        if step[0] == "tool":
            self.script_last = step
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t{len(self.calls)}", "name": step[1]}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(step[2])}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": step[1]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}


def mk(sns=None, model=None, chat_cap=400, client=None):
    c = client or FakeClient()
    return Deps(table=Table(), s3=FakeS3(b"{}"), bucket="b", client_factory=lambda: c, daily_cap=1000,
                model_factory=(lambda: model) if model else None, model_id="m" if model else "",
                sns=sns, topic_arn="arn:t" if sns else "", chat_cap=chat_cap)


def chat(d, text, ctx=None, prior=None):
    msgs = (prior or []) + [{"role": "user", "content": text}]
    return advisor.handle_chat({"messages": msgs, "context": ctx}, d, NOW)


KGP = {"name": "Kharagpur", "lat": 22.35, "lon": 87.32}

# --- places ---
assert find_places("bombay", 1)[0]["name"] == "Mumbai"
assert [p["name"] for p in find_places_in_text("compare Delhi and Bombay")] == ["Delhi", "Mumbai"]
assert find_places_in_text("how is banglore today")[0]["name"] == "Bengaluru"
assert find_places_in_text("what is the best time to go out") == []

# --- input cleaning ---
m = advisor._clean_messages([{"role": "assistant", "content": "hi"}, {"role": "user", "content": "a"},
                             {"role": "user", "content": "b"}, {"role": "x", "content": "z"},
                             {"role": "user", "content": "q" * 900}])
assert m[0]["role"] == "user" and all(len(x["content"]) <= 500 for x in m)
assert [x["role"] for x in m] == ["user"], m
assert advisor._clean_context({"name": "X", "lat": 10, "lon": 200}) is None
assert advisor._clean_context({"name": "X<b>", "lat": 22.3, "lon": 87.3})["name"] == "Xb"

# --- bad requests / caps ---
d = mk()
assert advisor.handle_chat({"messages": []}, d, NOW)[0] == 400
assert advisor.handle_chat({"messages": [{"role": "assistant", "content": "x"}]}, d, NOW)[0] == 400
d = mk(chat_cap=2)
assert chat(d, "hello")[0] == 200 and chat(d, "hello")[0] == 200 and chat(d, "hello")[0] == 429

# --- rules: current air, compare, best time, help ---
d = mk()
s, r = chat(d, "Is it okay to run now?", KGP)
assert s == 200 and r["engine"] == "rules" and "Kharagpur" in r["reply"] and "AQI" in r["reply"], r
s, r = chat(d, "compare Delhi and Bombay")
assert "Delhi" in r["reply"]["".__len__():] and "Mumbai" in r["reply"], r
s, r = chat(d, "best time to go out tomorrow in Pune")
assert "Pune" in r["reply"] and ("IST" in r["reply"]), r
s, r = chat(d, "air?")
assert "Which place" in r["reply"], r
s, r = chat(d, "tell me a joke")
assert "I can tell you" in r["reply"], r
s, r = chat(d, "what's the air in Atlantis")
assert s == 200 and "Which place" in r["reply"] or "don't know" in r["reply"], r

# --- alerts through rules ---
sns = SNS()
d = mk(sns=sns)
s, r = chat(d, "email me when AQI drops below 100 in Kharagpur")
assert "email address" in r["reply"] and not sns.subs, r
prior = [{"role": "user", "content": "email me when AQI drops below 100 in Kharagpur"},
         {"role": "assistant", "content": r["reply"]}]
s, r = chat(d, "me@example.com", KGP, prior)
assert "confirmation" in r["reply"] and len(sns.subs) == 1, r
sub = sns.subs[0]
assert sub["Endpoint"] == "me@example.com" and json.loads(sub["Attributes"]["FilterPolicy"])["rcpt"] == [alerts.rcpt_hash("me@example.com")]
al = [i for k, i in d.table.items.items() if k.startswith("alert#")]
assert len(al) == 1 and al[0]["kind"] == "below" and al[0]["threshold"] == 100 and al[0]["name"] == "Kharagpur"
# limit of 3 per address
for _ in range(2):
    s, r = chat(d, "alert me when AQI goes above 300 in Delhi, me@example.com")
    assert "confirmation" in r["reply"], r
assert len(sns.subs) == 3
s, r = chat(d, "alert me when AQI goes above 300 in Delhi, me@example.com")
assert "limit" in r["reply"] and len(sns.subs) == 3, r
# no SNS configured / SNS failing
s, r = chat(mk(), "email me when air is good in Pune, x@y.com")
assert "aren't switched on" in r["reply"], r
s, r = chat(mk(sns=SNS(fail=True)), "email me when air is good in Pune, x@y.com")
assert "couldn't set up" in r["reply"], r
assert alerts.create_alert(mk(sns=SNS()), NOW, "not-an-email", KGP, "below", 100)["error"]
assert alerts.create_alert(mk(sns=SNS()), NOW, "a@b.com", KGP, "below", 5)["error"]
assert alerts.create_alert(mk(sns=SNS()), NOW, "a@b.com", {"name": "Paris", "lat": 48, "lon": 2}, "below", 100)["error"]

# --- Strands agent: tool loop ---
m = Scripted([("tool", "get_air_now", {"place": "Kolkata"}), ("text", "Kolkata looks fine.")])
d = mk(model=m)
s, r = chat(d, "How is Calcutta?")
assert r["engine"] == "strands" and r["reply"] == "Kolkata looks fine.", r
names = [t["name"] for t in m.calls[0]["tools"]]
assert names == ["get_air_now", "get_forecast", "find_good_windows", "compare_places", "create_alert"], names
assert "Indian Standard Time" in m.calls[0]["system"] or "IST" in m.calls[0]["system"]
tool_msg = json.dumps(m.calls[1]["messages"])
assert "Kolkata" in tool_msg and "aqi" in tool_msg, tool_msg[:400]
# earlier turns are passed as history, the last one as the prompt
m = Scripted([("text", "ok")]); d = mk(model=m)
chat(d, "and tomorrow?", KGP, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
assert [x["role"] for x in m.calls[0]["messages"]] == ["user", "assistant", "user"], m.calls[0]["messages"]

# the tools themselves
d = mk()
for name, args in (("find_good_windows", {"place": "Pune", "max_aqi": 300}), ("get_forecast", {"place": "Pune", "hours": 12}),
                   ("compare_places", {"places": ["Pune", "Delhi"]})):
    out = advisor.run_tool(d, NOW, None, name, args)
    assert "error" not in out, (name, out)
assert "error" in advisor.run_tool(d, NOW, None, "nope", {})
assert "error" in advisor.run_tool(d, NOW, None, "get_air_now", {"place": "Atlantis"})

# email guard: the model may not invent an address (through the real Strands tool path)
sns = SNS(); d = mk(sns=sns, model=Scripted([("tool", "create_alert", {"email": "evil@x.com", "kind": "below"}), ("text", "done")]))
s, r = chat(d, "alert me please", KGP)
assert r["engine"] == "strands" and not sns.subs and not [k for k in d.table.items if k.startswith("alert#")], r
sns = SNS(); d = mk(sns=sns, model=Scripted([("tool", "create_alert", {"email": "ok@x.com", "kind": "below", "threshold": 120}), ("text", "done")]))
s, r = chat(d, "my mail is ok@x.com, email me when it is below 120", KGP)
assert r["engine"] == "strands" and len(sns.subs) == 1, r
assert advisor.run_tool(mk(sns=SNS()), NOW, KGP, "create_alert", {"email": "ok@x.com", "kind": "below"}, user_text="ok@x.com").get("ok")

# fallback on any model failure
for exc in (Err("AccessDeniedException"), RuntimeError("x")):
    d = mk(model=Scripted([exc]))
    s, r = chat(d, "Is it okay to run now?", KGP)
    assert s == 200 and r["engine"] == "rules" and r.get("fallback_reason"), r
# runaway tool loop falls back (the model never stops asking for tools)
m = Scripted([("tool", "get_air_now", {"place": "Pune"})]); d = mk(model=m)
s, r = chat(d, "Is it okay to run now?", KGP)
assert r["engine"] == "rules" and len(m.calls) <= 7, (r, len(m.calls))
# empty reply falls back
d = mk(model=Scripted([("text", " ")]))
assert chat(d, "air in Pune?")[1]["engine"] == "rules"
# no model configured -> rules, no import of the SDK needed
assert chat(mk(), "air in Pune?")[1]["engine"] == "rules"

# --- alert evaluation ---
sns = SNS(); d = mk(sns=sns)
alerts.create_alert(d, NOW, "me@example.com", KGP, "below", 400)       # model AQI is far below 400
alerts.create_alert(d, NOW, "me@example.com", KGP, "above", 400)       # never true
alerts.create_alert(d, NOW, "me@example.com", KGP, "window", 400)
st = alerts.run_alerts(d, NOW)
assert st["checked"] == 3 and st["errors"] == 0, st
kinds = sorted(p["Subject"].split(":")[0].split(" ")[0] for p in sns.pubs)
assert len(sns.pubs) == 2, [p["Subject"] for p in sns.pubs]
assert all(p["MessageAttributes"]["rcpt"]["StringValue"] == alerts.rcpt_hash("me@example.com") for p in sns.pubs)
assert "unsubscribe" in sns.pubs[0]["Message"]
# edge-triggered: running again straight away sends nothing
st = alerts.run_alerts(d, NOW + timedelta(hours=3))
assert st["sent"] == 0 and len(sns.pubs) == 2, st
# no SNS -> no-op
assert alerts.run_alerts(mk(), NOW)["checked"] == 0

# good_windows helper
S = {"now_index": 0, "time": [(datetime(2026, 10, 10, 0, tzinfo=timezone.utc) + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%MZ") for i in range(24)],
     "aqi": [200] * 6 + [50] * 5 + [200] * 13}
w, low = alerts.good_windows(S, 100)
assert len(w) == 1 and w[0]["hours"] == 5 and w[0]["min_aqi"] == 50 and low["aqi"] == 50, (w, low)
# an HTTP-client style error (response object, not a dict) must not crash the handler
class _Resp:
    status_code = 401
class HttpErr(Exception):
    response = _Resp()
d = mk(model=Scripted([HttpErr("unauthorized")]))
s, r = chat(d, "hello")
assert s == 200 and r["engine"] == "rules" and "HTTP 401" in r["fallback_reason"], r
assert advisor._error_note(Err("AccessDeniedException")) == "AccessDeniedException"

# --- OpenAI-compatible provider (Groq etc.): model is built from config, key comes from SSM ---
import strands_engine
from strands.models.openai import OpenAIModel
class SSM:
    def __init__(self): self.n = 0
    def get_parameter(self, Name, WithDecryption):
        self.n += 1; assert WithDecryption and Name == "/air-advisor/llm-key"
        return {"Parameter": {"Value": "k-123"}}
ssm = SSM()
d = mk(); d.llm_base_url, d.llm_model, d.ssm = "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", ssm
mdl = strands_engine._make_model(d); strands_engine._make_model(d)
assert isinstance(mdl, OpenAIModel) and mdl.get_config()["model_id"] == "llama-3.3-70b-versatile" and ssm.n == 1
d.llm_model = "openai/gpt-oss-120b"
assert strands_engine._make_model(d).get_config()["params"]["reasoning_effort"] == "low"
# a missing key / broken provider falls back to the rules engine
class BadSSM:
    def get_parameter(self, **k): raise Err("ParameterNotFound")
strands_engine._KEY_CACHE.clear()
d = mk(); d.llm_base_url, d.llm_model, d.ssm = "https://x", "m", BadSSM()
s, r = chat(d, "Is it okay to run now?", KGP)
assert r["engine"] == "rules" and r.get("fallback_reason"), r
# --- POST /alert (the page's Alerts dialog) ---
import api                                             # noqa: E402


def post_alert(d, body, raw=None):
    ev = {"rawPath": "/alert", "requestContext": {"http": {"method": "POST"}}, "body": raw if raw is not None else json.dumps(body)}
    r = api.handle(ev, d, NOW)
    return r["statusCode"], json.loads(r["body"])


good = {"email": "me@example.com", "place": {"name": "Kharagpur <b>", "lat": 22.33, "lon": 87.32}, "kind": "above", "threshold": 150}
sns = SNS(); d = mk(sns=sns)
st, r = post_alert(d, good)
assert st == 200 and r["ok"] and r["kind"] == "above" and r["threshold"] == 150, (st, r)
assert len(sns.subs) == 1 and sns.subs[0]["Endpoint"] == "me@example.com"
saved = [i for k, i in d.table.items.items() if k.startswith("alert#")][0]
assert saved["name"] == "Kharagpur b" and "<" not in saved["name"], saved["name"]      # name is cleaned
assert post_alert(mk(sns=SNS()), dict(good, email="nope"))[0] == 400
assert post_alert(mk(sns=SNS()), dict(good, kind="sideways"))[0] == 400
assert post_alert(mk(sns=SNS()), dict(good, threshold=5))[0] == 400
assert post_alert(mk(sns=SNS()), dict(good, place={"name": "Paris", "lat": 48.8, "lon": 2.3}))[0] == 400
assert post_alert(mk(sns=SNS()), dict(good, place={"name": "x", "lat": "abc", "lon": 1}))[0] == 400
assert post_alert(mk(sns=SNS()), {"email": "a@b.com"})[0] == 400                          # no place
assert post_alert(mk(sns=SNS()), None, raw="not json")[0] == 400
assert post_alert(mk(sns=SNS()), None, raw="x" * 7000)[0] == 413
assert post_alert(mk(), good)[0] == 400                                                  # alerts not switched on
d = mk(sns=SNS())
for i in range(3):
    assert post_alert(d, dict(good, threshold=100 + i))[0] == 200
st, r = post_alert(d, good)
assert st == 400 and "limit" in r["error"], r                                            # 3 per address
print("test_advisor: all passed")
