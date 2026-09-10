"""Helpers that drive the conversation through the full processor (mocked WATI).

Everything here resolves button titles through the live template registry at call time, never from a
hardcoded string. That is what lets the very same driver replay a conversation after an admin has
renamed every button - see test_workflow_after_admin_edit.py.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from unittest.mock import patch

from app.db import session_scope
from app.models import NameMismatchLog, Session, utcnow
from app.services import menus
from app.services.processor import ProcessResult, process_payload
from app.services.templates import registry

SHREE = "919167861236"  # SO 45231 (1 FG), 45232 (1 FG)
MEHTA = "919876543210"  # SO 45240 (3 FG)
GUJ = "919898012345"  # SO 45250 (2 FG)
PATEL = "919925001122"  # SO 45260 name mismatch (trailing space)
SUNRISE = "919033445566"  # SO 45270 name mismatch (upper case)
ROYAL = "919712345678"  # SO 45280 via PO-7777
ANAND = "919081726354"  # no orders
OM = "919427000111"  # no orders
UNKNOWN = "910000000000"

CUSTOMER_NAME = {SHREE: "Shree Packaging Pvt Ltd", MEHTA: "Mehta Foods", GUJ: "Gujarat Polymers",
                 PATEL: "Patel Agro Industries", SUNRISE: "Sunrise Pharma", ROYAL: "Royal Textiles",
                 ANAND: "Anand Dairy Products", OM: "Om Snacks"}
# customer -> (SO, how many items, the status of the first item)
ORDERS = {SHREE: ("45231", 1, "In Production"), MEHTA: ("45240", 3, "Ready for Dispatch"), GUJ: ("45250", 2, "Lamination")}

LANG_BUTTONS = ["English", "हिंदी", "ગુજરાતી"]
MENU_BUTTONS = {"en": ["Order status", "Change language", "Contact us"],
                "hi": ["ऑर्डर स्टेटस", "भाषा बदलें", "संपर्क करें"],
                "gu": ["ઓર્ડર સ્ટેટસ", "ભાષા બદલો", "સંપર્ક કરો"]}


async def send(phone: str, text: str | None = None, *, audio: bool = False, tap: bool = False) -> ProcessResult:
    payload: dict = {"id": f"t-{uuid.uuid4().hex}", "waId": phone, "type": "text", "text": text}
    if audio:
        payload.update(type="audio", text=None, data="x.ogg")
    elif tap:
        payload.update(type="interactive", buttonReply={"text": text})
    async with session_scope() as db:
        return await process_payload(db, payload)


async def say(phone: str, text: str | None = None, *, audio: bool = False) -> str:
    return (await send(phone, text, audio=audio)).reply_text or ""


async def tap(phone: str, title: str) -> ProcessResult:
    return await send(phone, title, tap=True)


def titles(r: ProcessResult) -> list[str]:
    return [i["title"] for i in r.options["items"]] if r.options else []


async def step_of(phone: str) -> str:
    async with session_scope() as db:
        s = await db.get(Session, phone)
        return s.step if s else "NONE"


async def open_menu(phone: str, language: str = "English") -> ProcessResult:
    """First contact: greeting + language question, then tap a language -> main menu."""
    r = await send(phone, "hi")
    assert r.outcome == "ask_language", r
    r = await tap(phone, language)
    assert r.outcome == "menu", r
    return r


# ---------------- live label lookups (never hardcode a title) ----------------
def L(key: str, lang: str = "en") -> str:
    """The current text of one button label, as an admin may have renamed it."""
    return menus.label(key, lang)


def BTN(template_key: str, lang: str = "en") -> list[str]:
    """The titles that will appear under one message right now."""
    return [L(k, lang) for k in registry.buttons(template_key)]


def LANGS_FOR(lang: str = "en") -> list[str]:
    return [L(k, lang) for k in ("lang_en", "lang_hi", "lang_gu")]


def options_of(r: ProcessResult) -> menus.Options | None:
    """Rebuild the Options object from a reply, so menus.validate() can check WhatsApp's limits."""
    if not r.options:
        return None
    o = r.options
    return menus.Options(
        kind=o["kind"], items=[menus.Option(i["title"], i.get("description", "")) for i in o["items"]],
        button_text=o.get("button_text", ""), section_title=o.get("section_title", ""),
        header=o.get("header", ""), footer=o.get("footer", ""),
    )


# ---------------- session inspection / manipulation ----------------
async def session_of(phone: str) -> dict:
    async with session_scope() as db:
        s = await db.get(Session, phone)
        if s is None:
            return {}
        return {"step": s.step, "so_no": s.so_no, "po_no": s.po_no, "fg_code": s.fg_code,
                "attempts": s.attempts, "language": s.language, "lang_chosen": bool(s.lang_chosen),
                "pending_kind": s.pending_kind, "pending_value": s.pending_value}


async def age_session(phone: str, minutes: int) -> None:
    """Push the session's last-activity time into the past. Must be the last write before the next
    message, because any other write bumps updated_at again."""
    async with session_scope() as db:
        s = await db.get(Session, phone)
        assert s is not None, f"no session for {phone}"
        s.updated_at = utcnow() - timedelta(minutes=minutes)
    async with session_scope() as db:
        s = await db.get(Session, phone)
        assert (utcnow() - s.updated_at).total_seconds() > minutes * 60 - 90, "the rewind did not persist"


async def mismatch_logs(phone: str) -> list[NameMismatchLog]:
    from sqlalchemy import select

    async with session_scope() as db:
        return list((await db.execute(select(NameMismatchLog).where(NameMismatchLog.phone_e164 == phone))).scalars())


@contextmanager
def voice(transcript: str):
    """Pretend the customer sent a voice note saying `transcript` (the fixture only ever says one
    thing, which cannot cover PO / item / garbage cases)."""
    async def _fake(audio, filename="voice.ogg", language=None):
        return transcript

    with patch("app.services.stt.transcribe", _fake):
        yield


@asynccontextmanager
async def outbox_delta():
    """The WATI outbox entries added inside the block."""
    from app.services.wati import wati

    before = len(wati.outbox)
    collected: list[dict] = []
    yield collected
    collected.extend(list(wati.outbox)[before:])


# ---------------- the conversation driver ----------------
def trace(results: list[ProcessResult]) -> list[tuple[str, str | None]]:
    """The shape of a conversation: what the bot did and where it left the customer. Content edits
    must never change this."""
    return [(r.outcome, r.step_after) for r in results]


async def run_full_conversation(phone: str, lang_key: str = "lang_en", mode: str = "tap") -> list[ProcessResult]:
    """greeting -> language -> menu -> order status -> SO -> (item) -> Real Status.

    `mode` is "tap" (buttons, as most customers answer) or "type" (typed text). Every title is read
    from the registry, so renaming the buttons does not change this code.
    """
    lang = {"lang_en": "en", "lang_hi": "hi", "lang_gu": "gu"}[lang_key]
    so_no, n_items, _ = ORDERS[phone]
    out: list[ProcessResult] = []

    async def answer(tapped: str, typed: str) -> ProcessResult:
        return await (tap(phone, tapped) if mode == "tap" else send(phone, typed))

    # The greeting goes out before any language is chosen, so the buttons are shown in English.
    r = await send(phone, "hi")
    assert r.outcome == "ask_language" and len(r.replies or []) == 2, r
    assert titles(r) == LANGS_FOR("en"), (titles(r), LANGS_FOR("en"))
    out.append(r)

    r = await answer(L(lang_key, "en"), L(lang_key, "en"))
    assert r.outcome == "menu", r
    assert titles(r) == BTN("main_menu", lang), (titles(r), BTN("main_menu", lang))
    out.append(r)

    r = await answer(L("order_status", lang), L("order_status", lang))
    assert r.outcome == "ask_so", r
    assert f"SO {so_no}" in titles(r), titles(r)
    out.append(r)

    r = await answer(f"SO {so_no}", f"SO {so_no}")
    out.append(r)
    if n_items > 1:
        assert r.outcome == "ask_fg", r
        item = titles(r)[0]
        r = await answer(item, item)
        out.append(r)
    assert r.outcome == "status_delivered", r
    assert titles(r) == BTN("result", lang), (titles(r), BTN("result", lang))
    return out
