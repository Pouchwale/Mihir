"""Machine translation for workflow text, English into Hindi and Gujarati.

Free services, no key: Google's public translate endpoint (the one the Chrome dictionary uses, which
takes many texts in one call) and MyMemory as the fallback. Placeholders like {sys.customer_name}
and WhatsApp *bold* survive both - checked, not assumed: a translation that loses a placeholder is
redone piece by piece around it. Common button words come from a hand-written glossary, which reads
better than a machine on two-word buttons and costs no quota.

Only text the owner wrote into a workflow is sent - never anything a customer said.
"""
from __future__ import annotations

import re

import httpx
import structlog

log = structlog.get_logger(__name__)

GOOGLE = "https://clients5.google.com/translate_a/t"
MYMEMORY = "https://api.mymemory.translated.net/get"
TARGETS = ("hi", "gu")
TIMEOUT = 12.0
_GOOGLE_BATCH_CHARS = 1500  # the texts travel in the URL
_MYMEMORY_MAX_BYTES = 450  # MyMemory refuses more than 500 bytes per request
_CACHE_MAX = 5000
_PLACEHOLDER = re.compile(r"(\{[A-Za-z][A-Za-z0-9_.]{0,40}\})")
_WORD = re.compile(r"[A-Za-z]{3,}")  # a real word; "SO 45240" or "500" is not

UNAVAILABLE = ("The free translators could not be reached just now. Type the Hindi and Gujarati yourself, "
               "or try again in a minute.")


class TranslateUnavailable(Exception):
    """Neither translator answered. The editor says so; nothing is half-filled."""


# Two-word buttons are where machines go wrong ("Yes please" came back as "जी कहिये"), so the
# common ones are written by hand. Keys are compared lower-case, without a trailing ! or .
GLOSSARY: dict[str, dict[str, str]] = {
    "hi": {"hi": "नमस्ते", "gu": "નમસ્તે"},
    "hello": {"hi": "नमस्ते", "gu": "નમસ્તે"},
    "yes": {"hi": "हाँ", "gu": "હા"},
    "no": {"hi": "नहीं", "gu": "ના"},
    "yes please": {"hi": "हाँ, ज़रूर", "gu": "હા, જરૂર"},
    "no thanks": {"hi": "नहीं, धन्यवाद", "gu": "ના, આભાર"},
    "no, thanks": {"hi": "नहीं, धन्यवाद", "gu": "ના, આભાર"},
    "ok": {"hi": "ठीक है", "gu": "બરાબર"},
    "okay": {"hi": "ठीक है", "gu": "બરાબર"},
    "done": {"hi": "हो गया", "gu": "થઈ ગયું"},
    "thank you": {"hi": "धन्यवाद", "gu": "આભાર"},
    "thanks": {"hi": "धन्यवाद", "gu": "આભાર"},
    "menu": {"hi": "मेनू", "gu": "મેનુ"},
    "main menu": {"hi": "मुख्य मेनू", "gu": "મુખ્ય મેનુ"},
    "back": {"hi": "वापस", "gu": "પાછા"},
    "go back": {"hi": "वापस जाएँ", "gu": "પાછા જાઓ"},
    "next": {"hi": "आगे", "gu": "આગળ"},
    "skip": {"hi": "छोड़ें", "gu": "છોડો"},
    "cancel": {"hi": "रद्द करें", "gu": "રદ કરો"},
    "confirm": {"hi": "पुष्टि करें", "gu": "પુષ્ટિ કરો"},
    "submit": {"hi": "जमा करें", "gu": "સબમિટ કરો"},
    "other": {"hi": "अन्य", "gu": "અન્ય"},
    "others": {"hi": "अन्य", "gu": "અન્ય"},
    "more": {"hi": "और देखें", "gu": "વધુ જુઓ"},
    "select": {"hi": "चुनें", "gu": "પસંદ કરો"},
    "choose": {"hi": "चुनें", "gu": "પસંદ કરો"},
    "contact us": {"hi": "संपर्क करें", "gu": "સંપર્ક કરો"},
    "talk to us": {"hi": "हमसे बात करें", "gu": "અમારી સાથે વાત કરો"},
    "call me": {"hi": "मुझे कॉल करें", "gu": "મને કૉલ કરો"},
    "order status": {"hi": "ऑर्डर स्टेटस", "gu": "ઓર્ડર સ્ટેટસ"},
    "change language": {"hi": "भाषा बदलें", "gu": "ભાષા બદલો"},
    "sample kit": {"hi": "सैंपल किट", "gu": "સેમ્પલ કિટ"},
    "price list": {"hi": "रेट लिस्ट", "gu": "રેટ લિસ્ટ"},
    "catalogue": {"hi": "कैटलॉग", "gu": "કેટલોગ"},
    "catalog": {"hi": "कैटलॉग", "gu": "કેટલોગ"},
}

_cache: dict[tuple[str, str], str] = {}


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold().rstrip("!.")


def _known(text: str, target: str) -> str | None:
    hit = GLOSSARY.get(_key(text))
    if hit:
        return hit[target]
    return _cache.get((text, target))


def _remember(text: str, target: str, translated: str) -> None:
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[(text, target)] = translated


async def translate(texts: list[str], target: str) -> list[str]:
    """English into `target`, one result per text, in order. Empty stays empty; line breaks stay."""
    if target not in TARGETS:
        raise ValueError(f"cannot translate into {target!r}")
    needed: list[str] = []
    for text in texts:
        text = text or ""
        if not text.strip() or _known(text, target) is not None:
            continue
        needed.extend(line for line in text.split("\n") if _worth_sending(line) and _known(line, target) is None)
    if needed:
        unique = list(dict.fromkeys(needed))
        for line, done in zip(unique, await _machine(unique, target)):
            _remember(line, target, done)

    out = []
    for text in texts:
        text = text or ""
        whole = _known(text, target) if text.strip() else None
        if whole is not None:
            out.append(whole)
        else:
            out.append("\n".join((_known(line, target) or line) if _worth_sending(line) else line
                                 for line in text.split("\n")))
    return out


def _worth_sending(line: str) -> bool:
    """Only lines with a real word in them - "500" or "SO 45240" read the same in every language."""
    return bool(line.strip()) and bool(_WORD.search(_PLACEHOLDER.sub("", line)))


def _use_ai() -> bool:
    """AI when chosen in Settings (or on "auto" with a Groq key set); the free translators otherwise."""
    from ..config import get_settings
    from .ai.groq_client import configured

    provider = get_settings().translate_provider
    return provider != "free" and configured()


async def _machine(lines: list[str], target: str) -> list[str]:
    """The AI first when it is chosen - it reads a whole sentence the way a person would say it - and
    the free chain for anything it did not do, or did not do safely."""
    done: list[str | None] = [None] * len(lines)
    if _use_ai():
        from .ai.live import translate_lines

        try:
            got = await translate_lines(lines, target)
        except Exception as e:  # noqa: BLE001 - the free translators are always there to fall back on
            log.info("translate_ai_failed", error=str(e)[:200])
            got = [None] * len(lines)
        for i, (line, t) in enumerate(zip(lines, got)):
            if t and _same_placeholders(line, t):
                done[i] = _keep_edges(line, t)
    rest = [i for i, d in enumerate(done) if d is None]
    if rest:
        for i, t in zip(rest, await _free([lines[i] for i in rest], target)):
            done[i] = t
    return [d or "" for d in done]


async def _free(lines: list[str], target: str) -> list[str]:
    try:
        first = await _google(lines, target)
    except Exception as e:  # noqa: BLE001 - blocked, rate-limited or down: MyMemory takes over
        log.info("translate_google_unavailable", error=str(e)[:200])
        first = [None] * len(lines)
    out = []
    for line, got in zip(lines, first):
        if got is None or not _same_placeholders(line, got):
            got = await _careful(line, target)
        out.append(_keep_edges(line, got))
    return out


async def _google(lines: list[str], target: str) -> list[str | None]:
    """Many lines per call; they travel in the URL, so batches are kept short."""
    out: list[str | None] = []
    batch: list[str] = []
    size = 0
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for line in lines + [None]:  # a sentinel flushes the last batch
            if line is not None and (not batch or size + len(line) <= _GOOGLE_BATCH_CHARS):
                batch.append(line)
                size += len(line)
                continue
            params = [("client", "dict-chrome-ex"), ("sl", "en"), ("tl", target)] + [("q", q.strip()) for q in batch]
            r = await client.get(GOOGLE, params=params)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or len(data) != len(batch):
                raise ValueError("unexpected answer from Google")
            out.extend(_first_text(item) for item in data)
            batch, size = ([line], len(line)) if line is not None else ([], 0)
    return out


def _first_text(item) -> str | None:
    while isinstance(item, list) and item:
        item = item[0]
    return item if isinstance(item, str) else None


async def _careful(line: str, target: str) -> str:
    """MyMemory, one line at a time; around the placeholders if a whole-line try loses one."""
    got = await _mymemory(line, target)
    if _same_placeholders(line, got):
        return got
    parts = _PLACEHOLDER.split(line)
    out = []
    for part in parts:
        if _PLACEHOLDER.fullmatch(part) or not _worth_sending(part):
            out.append(part)
        else:
            out.append(_keep_edges(part, await _mymemory(part, target)))
    return "".join(out)


async def _mymemory(text: str, target: str) -> str:
    if len(text.encode("utf-8")) > _MYMEMORY_MAX_BYTES:  # sentence by sentence
        pieces = re.split(r"(?<=[.!?])\s+", text.strip())
        if len(pieces) > 1:
            return " ".join([await _mymemory(p, target) for p in pieces])
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(MYMEMORY, params={"q": text.strip(), "langpair": f"en|{target}"})
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise TranslateUnavailable(UNAVAILABLE) from e
    if data.get("quotaFinished"):
        raise TranslateUnavailable("The free translator's daily limit is used up. Try again tomorrow, or type "
                                   "the Hindi and Gujarati yourself.")
    translated = str((data.get("responseData") or {}).get("translatedText") or "")
    if str(data.get("responseStatus")) != "200" or not translated or translated.upper().startswith("MYMEMORY WARNING"):
        raise TranslateUnavailable(UNAVAILABLE)
    return translated


def _same_placeholders(source: str, translated: str) -> bool:
    return sorted(_PLACEHOLDER.findall(source)) == sorted(_PLACEHOLDER.findall(translated))


def _keep_edges(source: str, translated: str) -> str:
    """Services trim; the spaces around a piece are part of the sentence it sits in."""
    lead = source[: len(source) - len(source.lstrip())]
    trail = source[len(source.rstrip()):]
    return lead + translated.strip() + trail
