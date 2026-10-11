"""The AI advisor on the Strands Agents SDK (https://strandsagents.com).

Strands runs the model <-> tool loop; this file only supplies the model, the system prompt and the
tools. The tools are the same functions the rules engine uses (advisor.run_tool), so the model can only
quote numbers that come from our data, and create_alert still needs an email address that appears in
the person's own messages.

The model is a Strands model object, so it is one line to swap: BedrockModel here (Bedrock Converse),
but Strands also ships Ollama, OpenAI, Gemini and other providers. deps.model_factory overrides it.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta, timezone
from typing import Optional

from strands import Agent, tool
from strands.hooks import BeforeModelCallEvent

import advisor

IST = timezone(timedelta(hours=5, minutes=30))
MAX_MODEL_CALLS = 5          # model invocations per answer (tool round trips + the final answer)


class TooManySteps(RuntimeError):
    pass


_KEY_CACHE = {}


def _llm_key(deps):
    """The provider API key from SSM Parameter Store (SecureString), cached for the life of the container."""
    if deps.llm_key_param not in _KEY_CACHE:
        r = deps.ssm.get_parameter(Name=deps.llm_key_param, WithDecryption=True)
        _KEY_CACHE[deps.llm_key_param] = r["Parameter"]["Value"]
    return _KEY_CACHE[deps.llm_key_param]


def _make_model(deps):
    if deps.model_factory is not None:
        return deps.model_factory()
    if deps.llm_base_url:                               # Groq / Gemini / OpenRouter / any OpenAI-compatible API
        from strands.models.openai import OpenAIModel
        params = {"temperature": 0.2, "max_tokens": 900}
        if "gpt-oss" in deps.llm_model:                 # reasoning models: keep the thinking short, keep answers fast
            params["reasoning_effort"] = "low"
        return OpenAIModel(client_args={"api_key": _llm_key(deps), "base_url": deps.llm_base_url, "timeout": 12, "max_retries": 0},
                           model_id=deps.llm_model, params=params)
    from strands.models import BedrockModel
    from botocore.config import Config
    return BedrockModel(model_id=deps.model_id, region_name=deps.bedrock_region or None, temperature=0.2, max_tokens=600,
                        boto_client_config=Config(read_timeout=10, connect_timeout=4, retries={"max_attempts": 1}))


def _tools(deps, now, ctx, user_text):
    def call(name, args):
        return json.dumps(advisor.run_tool(deps, now, ctx, name, args, user_text), ensure_ascii=False, default=str)

    @tool
    def get_air_now(place: Optional[str] = None) -> str:
        """Current estimated AQI and pollutant levels for one place in India.

        Args:
            place: City or town name. Omit to use the place open on the map.
        """
        return call("get_air_now", {"place": place})

    @tool
    def get_forecast(place: Optional[str] = None, hours: int = 24) -> str:
        """Hourly estimated AQI for the next hours (IST) plus the 24-hour-average AQI.

        Args:
            place: City or town name. Omit to use the place open on the map.
            hours: How many hours ahead, 1 to 48.
        """
        return call("get_forecast", {"place": place, "hours": hours})

    @tool
    def find_good_windows(place: Optional[str] = None, max_aqi: int = 100, hours: int = 24,
                          waking_hours_only: bool = True) -> str:
        """Stretches of upcoming hours when the estimated AQI stays at or below a limit.
        Use it for 'best time to go out'.

        Args:
            place: City or town name. Omit to use the place open on the map.
            max_aqi: Highest AQI that still counts as good enough (default 100).
            hours: How many hours ahead to look, 1 to 48.
            waking_hours_only: Only consider 05:00-21:59 IST.
        """
        return call("find_good_windows", {"place": place, "max_aqi": max_aqi, "hours": hours,
                                          "waking_hours_only": waking_hours_only})

    @tool
    def compare_places(places: list[str]) -> str:
        """Current estimated AQI for 2 or 3 places side by side.

        Args:
            places: Names of the places to compare (at most 3).
        """
        return call("compare_places", {"places": places})

    @tool
    def create_alert(email: str, kind: str, place: Optional[str] = None, threshold: int = 100) -> str:
        """Create an email alert. Only call this when the person clearly asked for an alert and typed
        their own email address in this conversation.

        Args:
            email: The address the person typed.
            kind: 'below' = tell me when AQI falls to the threshold or lower; 'above' = when it rises to
                the threshold or higher; 'window' = when a good stretch (AQI at or below the threshold)
                is coming up.
            place: City or town name. Omit to use the place open on the map.
            threshold: AQI between 20 and 500.
        """
        return call("create_alert", {"email": email, "kind": kind, "place": place, "threshold": threshold})

    return [get_air_now, get_forecast, find_good_windows, compare_places, create_alert]


def strands_reply(messages, ctx, deps, now):
    """Answer with a Strands agent. Raises on any problem so the caller can fall back to rules."""
    user_text = " ".join(m["content"] for m in messages if m["role"] == "user")
    history = [{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages[:-1]]
    system = advisor.SYSTEM_PROMPT + (f" Current place on the map: {ctx['name']}." if ctx else
                                      " No place is open on the map.")
    system += f" Current time (IST): {now.astimezone(IST).strftime('%a %d %b %I:%M %p')}."

    calls = {"n": 0}
    started = time.monotonic()

    def guard(event):                       # stops a model that keeps asking for tools, or an answer taking too long
        calls["n"] += 1
        if calls["n"] > MAX_MODEL_CALLS:
            raise TooManySteps("too many model calls")
        if time.monotonic() - started > 18:
            raise TooManySteps("answer took too long")

    agent = Agent(model=_make_model(deps), system_prompt=system, tools=_tools(deps, now, ctx, user_text),
                  messages=history, callback_handler=None)
    agent.hooks.add_callback(BeforeModelCallEvent, guard)
    text = str(agent(messages[-1]["content"])).strip()
    if not text:
        raise RuntimeError("empty model reply")
    return text
