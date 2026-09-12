"""The AI where it meets real customers - so every function here returns quickly, never raises, and
answers None when the AI cannot help. The conversation then carries on exactly as it would without
AI: the question is asked again, or the "Not sure" exit is taken.

Only what the job needs is ever sent: the question and its choices, the customer's words, the
owner's FAQ text. Never a phone number, a name, or anything from the orders."""
from __future__ import annotations

import json

import structlog

from ...config import get_settings
from ..replies import _COMPANY
from .blueprint import _obj, _str
from .groq_client import AiUnavailable, breaker_open, chat_json, configured

log = structlog.get_logger(__name__)

LANG_NAMES = {"en": "English", "hi": "Hindi", "gu": "Gujarati"}
TRANSLATE_BATCH = 40


def available() -> bool:
    return configured() and not breaker_open()


async def match_choice(question: str, choices: list[tuple[str, str]], said: str) -> str | None:
    """Which button a typed reply means ("500 standup chahiye" -> "stand_up"), or None."""
    if not available() or not choices or not said.strip():
        return None
    values = [v for v, _ in choices]
    schema = _obj({"choice": {"type": "string", "enum": [*values, "none"]}})
    system = ("A WhatsApp customer was offered some buttons but typed a reply instead. Say which button they mean. "
              "The reply may be English, Hindi, Gujarati, romanised Hindi/Gujarati, or misspelt. Answer \"none\" "
              "unless it clearly means exactly one of the buttons.")
    user = json.dumps({"question": question, "buttons": [{"id": v, "label": label} for v, label in choices],
                       "reply": said}, ensure_ascii=False)
    try:
        got, _ = await chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}], schema,
                                 name="choice", model=get_settings().ai_live_model, max_tokens=400, timeout=6, live=True)
    except AiUnavailable as e:
        log.info("ai_match_unavailable", error=str(e))
        return None
    picked = got.get("choice")
    return picked if picked in values else None


async def answer_faq(faq: str, question: str, language: str, tone: str = "friendly", max_chars: int = 600) -> dict | None:
    """{"answer", "confident"} from the owner's FAQ text only, or None when the AI cannot help."""
    if not available() or not faq.strip() or not question.strip():
        return None
    schema = _obj({"answer": _str(), "confident": {"type": "boolean"}})
    system = (f"You answer a customer of {_COMPANY} on WhatsApp, using ONLY the FAQ below. If the FAQ does not clearly "
              "answer the question, set confident to false and leave answer empty. Never invent prices, dates, stock, "
              "order status or promises that are not in the FAQ. Reply in the customer's language "
              f"(they chose {LANG_NAMES.get(language, 'English')}; if they wrote in another, use that), in a "
              f"{'formal' if tone == 'formal' else 'warm, friendly'} tone, as plain text, in at most {max_chars} characters.\n\n"
              f"FAQ:\n{faq.strip()}")
    try:
        got, _ = await chat_json([{"role": "system", "content": system}, {"role": "user", "content": question.strip()}],
                                 schema, name="faq_answer", model=get_settings().ai_live_model, max_tokens=1500,
                                 timeout=15, live=True)
    except AiUnavailable as e:
        log.info("ai_answer_unavailable", error=str(e))
        return None
    answer = str(got.get("answer") or "").strip()
    return {"answer": answer[:max_chars], "confident": bool(got.get("confident")) and bool(answer)}


async def translate_lines(lines: list[str], target: str) -> list[str | None]:
    """English lines into Hindi or Gujarati, one for one; None where the AI gave nothing usable."""
    out: list[str | None] = [None] * len(lines)
    if not configured():
        return out
    schema = _obj({"translations": {"type": "array", "items": _str()}})
    system = (f"Translate WhatsApp chatbot text for {_COMPANY} from English into {LANG_NAMES[target]} "
              f"({'Devanagari' if target == 'hi' else 'Gujarati'} script). Natural, polite and short, as a shop assistant "
              "would say it; keep common English business words people use (SO, PO, sample, pouch) where that reads "
              "naturally. Keep {placeholders}, *bold*, _italic_, numbers and links exactly as they are. Return exactly "
              "one translation per input line, in the same order.")
    for start in range(0, len(lines), TRANSLATE_BATCH):
        chunk = lines[start:start + TRANSLATE_BATCH]
        try:
            got, _ = await chat_json([{"role": "system", "content": system},
                                      {"role": "user", "content": json.dumps({"lines": chunk}, ensure_ascii=False)}],
                                     schema, name="translations", model=get_settings().ai_live_model, max_tokens=4000,
                                     timeout=60)
        except AiUnavailable as e:
            log.info("ai_translate_unavailable", error=str(e))
            return out
        done = got.get("translations") or []
        if len(done) != len(chunk):
            continue  # a count that does not line up cannot be trusted line by line
        for i, t in enumerate(done):
            if isinstance(t, str) and t.strip():
                out[start + i] = t.strip()
    return out
