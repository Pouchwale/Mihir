"""One call to Groq that comes back as JSON in a fixed shape.

Groq's gpt-oss models support strict JSON-schema output (constrained decoding: the answer always fits
the shape). Should Groq refuse a schema, the same call is retried with a looser mode, so a change on
their side degrades quality rather than breaking the feature.

Two kinds of caller, two policies:
  * the owner in the editor (building, reviewing) can wait - a free-tier "limit reached" is waited
    out with a visible countdown, up to a few times;
  * a live customer cannot - any failure returns at once so the conversation carries on without AI,
    and repeated failures pause the AI for five minutes (the same breaker as intent.py).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable

import httpx
import structlog

from ...config import get_settings

log = structlog.get_logger(__name__)

NO_KEY = ("The AI assistant needs a Groq API key. Add it in Settings, under AI assistant - it is the same "
          "key voice notes use, from console.groq.com.")

# (stage text, seconds to wait or None) - how a long job tells the editor what it is doing
Progress = Callable[[str, float | None], Awaitable[None]]


class AiUnavailable(Exception):
    """The AI cannot answer now: no key, a refused key, the limit, or Groq down. Plain words, safe to show."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


_FAIL_LIMIT = 3
_COOLDOWN_SECONDS = 300
_breaker = {"fails": 0, "open_until": 0.0}


def breaker_open() -> bool:
    return time.monotonic() < _breaker["open_until"]


def breaker_reset() -> None:
    _breaker["fails"] = 0
    _breaker["open_until"] = 0.0


def _note_failure(reason: str, immediate: bool = False) -> None:
    _breaker["fails"] += 1
    if immediate or _breaker["fails"] >= _FAIL_LIMIT:
        _breaker["open_until"] = time.monotonic() + _COOLDOWN_SECONDS
        log.warning("ai_paused", reason=reason, minutes=_COOLDOWN_SECONDS // 60)


def configured() -> bool:
    return bool(get_settings().groq_api_key.strip())


_MODES = ("strict", "loose", "object")


async def chat_json(messages: list[dict], schema: dict, *, name: str, model: str | None = None,
                    max_tokens: int = 8000, timeout: float = 120.0, live: bool = False,
                    reasoning: str = "low", progress: Progress | None = None, max_waits: int = 3) -> tuple[dict, int]:
    """(the JSON answer, tokens used). Raises AiUnavailable with a message a person can act on."""
    s = get_settings()
    if not configured():
        raise AiUnavailable(NO_KEY)
    if live and breaker_open():
        raise AiUnavailable("The AI is paused for a few minutes after repeated failures.")
    model = model or s.ai_builder_model
    url = f"{s.groq_base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {s.groq_api_key.strip()}"}
    mode, waits, troubles = 0, 0, 0

    while True:
        body = _body(messages, schema, name, model, max_tokens, _MODES[mode], reasoning)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            troubles += 1
            if not live and troubles <= 1:
                continue
            if live:
                _note_failure(str(e))
            raise AiUnavailable(f"Groq could not be reached ({type(e).__name__}). Try again in a minute.") from e

        if r.status_code == 200:
            try:
                data = r.json()
                parsed = json.loads(data["choices"][0]["message"]["content"])
                if not isinstance(parsed, dict):
                    raise ValueError("not an object")
            except (ValueError, KeyError, IndexError, TypeError) as e:
                if mode < len(_MODES) - 1:
                    mode += 1
                    continue
                raise AiUnavailable("Groq's answer could not be read. Try again.") from e
            if live:
                breaker_reset()
            return parsed, int((data.get("usage") or {}).get("total_tokens") or 0)

        detail = _detail(r)
        if r.status_code == 400 and mode < len(_MODES) - 1 and re.search(r"schema|response_format|json", detail, re.I):
            log.info("ai_schema_refused", mode=_MODES[mode], detail=detail[:200])
            mode += 1
            continue
        if r.status_code in (401, 403):
            _note_failure("key refused", immediate=True)
            raise AiUnavailable("Groq refused the API key. Check it in Settings, under AI assistant.")
        if r.status_code == 404:
            raise AiUnavailable(f"Groq does not offer the model “{model}”. Choose another in Settings, under AI assistant.")
        if r.status_code == 413:
            raise AiUnavailable("That is too much for Groq in one go. Describe a smaller part, or move to a paid Groq plan.")
        if r.status_code == 429:
            wait = _retry_after(r)
            if not live and progress is not None and wait is not None and wait <= 90 and waits < max_waits:
                waits += 1
                await progress("Waiting for Groq's per-minute limit", wait)
                await asyncio.sleep(wait + 0.5)
                continue
            if live:
                _note_failure("rate limited")
            when = f" - ready again in {int(wait) + 1} s" if wait is not None else ""
            raise AiUnavailable(f"Groq's limit is used up{when}. The free plan allows about 8,000 tokens a "
                                "minute; a paid Groq plan lifts it.", retry_after=wait)
        if r.status_code >= 500:
            troubles += 1
            if not live and troubles <= 2:
                await asyncio.sleep(1.0)
                continue
            if live:
                _note_failure(f"groq {r.status_code}")
            raise AiUnavailable(f"Groq is having trouble ({r.status_code}). Try again in a minute.")
        raise AiUnavailable(f"Groq refused the request ({r.status_code}): {detail[:200]}")


def _body(messages: list[dict], schema: dict, name: str, model: str, max_tokens: int, mode: str,
          reasoning: str) -> dict:
    body: dict = {"model": model, "temperature": 0.2, "max_completion_tokens": max_tokens}
    if mode == "object":
        # JSON mode needs the shape spelled out in the conversation itself
        body["messages"] = messages + [{"role": "system", "content": "Answer with one JSON object that follows this "
                                                                     "JSON schema exactly:\n" + json.dumps(schema)}]
        body["response_format"] = {"type": "json_object"}
    else:
        body["messages"] = messages
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": name, "strict": mode == "strict", "schema": schema}}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = reasoning  # less hidden thinking - fewer tokens against the per-minute limit
    return body


def _detail(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return str(err.get("message") or err.get("code") or err) if isinstance(err, dict) else str(err)
    except ValueError:
        return r.text[:300]


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds until Groq accepts another call: `retry-after`, else `x-ratelimit-reset-tokens` ("7.6s", "1m2s")."""
    raw = r.headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    reset = r.headers.get("x-ratelimit-reset-tokens") or r.headers.get("x-ratelimit-reset-requests") or ""
    m = re.fullmatch(r"(?:(\d+)m)?([\d.]+)s", reset.strip())
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return None
