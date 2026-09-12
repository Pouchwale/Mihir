"""Session state machine.

  START ──(first message of a 30-min window)──▶ greeting + language buttons ──▶ LANG
  LANG  ──(taps English / हिंदी / ગુજરાતી)──▶ main menu ──▶ MENU
  MENU  ──Order status──▶ AWAIT_SO (SO buttons / list) ──SO with 1 item──▶ result ──▶ DONE
        ──Contact us────▶ contact text (stays in MENU)        │ many items
        ──Change language▶ language buttons ──▶ LANG          ▼
                                                     AWAIT_FG (item buttons / list) ──item──▶ result ──▶ DONE
                                                          │ wrong item → re-ask, max N tries, then apology
  Voice input ──▶ CONFIRM (Yes / No) before any lookup.

Verification (phone in the customer Excel) runs on every message; the byte-exact customer-name
match runs on every lookup. Every button title is plain text the phone sends back, so a tapped
option and a typed answer go through exactly the same parser and checks.

One customer message can produce several bot messages (the greeting followed by the language
question), so `step()` returns a list.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import Session, utcnow
from . import menus, repeat, templates
from .intent import Parsed
from .menus import Options
from .replies import ReplyContext
from .verify import customer_orders, filter_fg, find_customer, find_new_customer, lookup_orders, new_customer_name

LANGS = ("en", "hi", "gu")
LANG_INTENTS = {"lang_en": "en", "lang_hi": "hi", "lang_gu": "gu"}


@dataclass
class Outcome:
    template: str
    ctx: ReplyContext
    code: str  # for stats: welcome | ask_language | menu | contact | ask_so | ask_fg | confirm | status_delivered | not_found | verify_failed | mismatch | bye
    language: str
    options: Options | None = None


def clear_order_state(session: Session) -> None:
    """Forget the order being looked up, keep the language."""
    session.so_no = None
    session.po_no = None
    session.fg_code = None
    session.pending_value = None
    session.pending_kind = None
    session.attempts = 0


def reset(session: Session) -> None:
    """Back to the very beginning: the next message gets the greeting and the language question."""
    clear_order_state(session)
    session.step = "START"
    session.lang_chosen = False


async def get_or_create_session(db: AsyncSession, phone: str) -> Session:
    s = await db.get(Session, phone)
    if s is None:
        s = Session(phone_e164=phone, step="START")
        db.add(s)
        await db.flush()
    else:
        idle = (utcnow() - s.updated_at).total_seconds() if s.updated_at else 0
        if idle > get_settings().session_timeout_min * 60:
            reset(s)  # a new window
    return s


async def step(db: AsyncSession, session: Session, parsed: Parsed, from_audio: bool, voice_blocked: bool = False) -> list[Outcome]:
    settings = get_settings()
    support = settings.support_contact

    # language: an explicit choice from the buttons sticks for the whole window; otherwise follow
    # what the customer wrote in, and keep the session's language for code-only / tapped messages
    if session.lang_chosen and session.language in LANGS:
        lang = session.language
    else:
        lang = parsed.language if parsed.language in LANGS else (session.language if session.language in LANGS else "en")
    session.language = lang

    # 1. verify the phone on every message
    customer = await find_customer(db, session.phone_e164)
    if customer is None:
        reset(session)
        pending = await find_new_customer(db, session.phone_e164)
        if pending is not None:
            # Signed up through a workflow but not in the customer Excel yet: no orders to show - and
            # never another company's, whatever name they typed - but not a stranger either.
            ctx_new = ReplyContext(support=support, customer_name=new_customer_name(pending))
            return [Outcome("new_customer_pending", ctx_new, "new_customer", lang)]
        return [Outcome("verify_failed", ReplyContext(support=support), "verify_failed", lang)]

    def ctx(**kw) -> ReplyContext:
        return ReplyContext(support=support, customer_name=customer.customer_name, **kw)

    # 2. a voice note we cannot turn into text: ask for it in writing, leave the step untouched
    if voice_blocked:
        return [Outcome("voice_off", ctx(), "voice_off", lang, options=menus.buttons_for("voice_off", lang))]

    # 3. a language choice is honoured at any point in the conversation
    if parsed.intent in LANG_INTENTS:
        session.language = lang = LANG_INTENTS[parsed.intent]
        session.lang_chosen = True
        return [_main_menu(session, ctx, lang)]

    # 4. first message of a new window: greeting, then the language question
    if session.step == "START":
        clear_order_state(session)
        session.step = "LANG"
        return [
            Outcome("welcome_first", ctx(), "welcome", lang),
            Outcome("ask_language", ctx(), "ask_language", lang, options=menus.language_buttons(lang)),
        ]
    if session.step == "LANG":  # anything but a language answer -> ask again
        return [Outcome("ask_language", ctx(), "ask_language", lang, options=menus.language_buttons(lang))]

    # 5. resolve what the customer gave us (typed or tapped)
    so_no, po_no, fg_code = parsed.so_no, parsed.po_no, parsed.fg_code
    if parsed.bare_codes and not (so_no or po_no or fg_code):
        if session.step == "AWAIT_FG":
            fg_code = parsed.bare_codes[0]
        else:
            so_no = parsed.bare_codes[0]
    has_code = bool(so_no or po_no or fg_code)

    # 6. navigation words / buttons
    if parsed.intent == "change_language" and not has_code:
        session.step = "LANG"
        return [Outcome("ask_language", ctx(), "ask_language", lang, options=menus.language_buttons(lang))]
    if parsed.intent == "contact_us" and not has_code:
        if session.step in ("MENU", "DONE"):
            session.step = "MENU"
        return [Outcome("contact_us", ctx(), "contact", lang, options=menus.buttons_for("contact_us", lang))]
    if parsed.intent == "bye" and not has_code:
        out = Outcome("bye", ctx(), "bye", lang, options=menus.buttons_for("bye", lang))
        reset(session)
        return [out]
    if parsed.intent == "menu" or (parsed.intent == "greeting" and not has_code):
        return [_main_menu(session, ctx, lang)]

    # 7. admin-defined keyword replies (template editor). Only for a message the bot does not already
    # understand, so a custom trigger can never swallow a menu button, a code or a Yes/No answer.
    if not has_code and parsed.intent == "other":
        custom = templates.registry.match_custom(parsed.raw_text)
        if custom:
            return [Outcome(f"custom:{custom.key}", ctx(), "custom", lang, options=menus.custom_buttons(custom.buttons, lang))]

    # 8. CONFIRM handling (after a voice note)
    if session.step == "CONFIRM":
        if parsed.intent == "confirm_yes" and session.pending_value:
            kind, value = session.pending_kind, session.pending_value
            session.pending_value = session.pending_kind = None
            if kind == "so":
                so_no, po_no, fg_code = value, None, None
            elif kind == "po":
                so_no, po_no, fg_code = None, value, None
            else:
                so_no, po_no, fg_code = None, None, value
            from_audio = False
            session.step = "AWAIT_FG" if kind == "fg" else "AWAIT_SO"
            has_code = True
        elif parsed.intent == "confirm_no" and not has_code:
            kind = session.pending_kind
            session.pending_value = session.pending_kind = None
            if kind == "fg" and session.so_no:
                session.step = "AWAIT_FG"
                return [await _ask_fg_again(db, session, customer, ctx, lang)]
            return [await _so_menu(db, session, customer, ctx, lang)]
        elif not has_code and parsed.intent != "order_status":
            tpl = f"confirm_{session.pending_kind or 'so'}"
            return [Outcome(tpl, ctx(value=session.pending_value), "confirm", lang, options=menus.confirm_buttons(lang))]
        else:
            session.pending_value = session.pending_kind = None
            session.step = "AWAIT_FG" if (fg_code and session.so_no) else "AWAIT_SO"

    # 9. Order status chosen -> show the customer's own orders
    if parsed.intent == "order_status" and not has_code:
        return [await _so_menu(db, session, customer, ctx, lang)]

    # 10. SO / PO given -> (confirm if audio) -> lookup
    if so_no or po_no:
        if from_audio:
            session.step = "CONFIRM"
            session.pending_kind = "so" if so_no else "po"
            session.pending_value = so_no or po_no
            return [Outcome(f"confirm_{session.pending_kind}", ctx(value=session.pending_value), "confirm", lang, options=menus.confirm_buttons(lang))]
        return [await _lookup_so(db, session, customer, so_no, po_no, fg_code, ctx, lang)]

    # 11. FG given while we are waiting for one
    if fg_code and session.step == "AWAIT_FG" and session.so_no:
        if from_audio:
            session.step = "CONFIRM"
            session.pending_kind = "fg"
            session.pending_value = fg_code
            return [Outcome("confirm_fg", ctx(value=fg_code), "confirm", lang, options=menus.confirm_buttons(lang))]
        return [await _lookup_fg(db, session, customer, fg_code, ctx, lang)]

    # 12. nothing usable -> repeat the prompt for the current step
    if session.step == "AWAIT_FG" and session.so_no:
        return [await _ask_fg_again(db, session, customer, ctx, lang)]
    if session.step == "AWAIT_SO":
        return [await _so_menu(db, session, customer, ctx, lang)]
    return [_main_menu(session, ctx, lang)]


# ---------------- prompts ----------------
def _main_menu(session, ctx, lang) -> Outcome:
    clear_order_state(session)
    session.step = "MENU"
    return Outcome("main_menu", ctx(), "menu", lang, options=menus.buttons_for("main_menu", lang))


async def _so_menu(db, session, customer, ctx, lang) -> Outcome:
    """'Order status': the customer's own SOs (byte-exact name match) as buttons or a list."""
    rows = await customer_orders(db, customer)
    clear_order_state(session)
    session.step = "AWAIT_SO"
    if not rows:
        return Outcome("so_none", ctx(), "ask_so", lang, options=menus.buttons_for("so_none", lang))
    opts = menus.so_options(rows, lang, get_settings().so_menu_style)
    return Outcome("ask_so_list", ctx(n_items=len({r.so_no for r in rows})), "ask_so", lang, options=opts)


def _ask_fg_outcome(so_no, rows, ctx, lang) -> Outcome:
    opts = menus.fg_options(rows, lang, get_settings().so_menu_style)
    n = len({r.fg_item_code for r in rows})
    tpl = "ask_fg_list" if opts else "ask_fg"
    return Outcome(tpl, ctx(so_no=so_no, n_items=n), "ask_fg", lang, options=opts or menus.buttons_for(tpl, lang))


async def _ask_fg_again(db, session, customer, ctx, lang) -> Outcome:
    result = await lookup_orders(db, customer, so_no=session.so_no)
    if result.kind != "ok":
        return await _so_menu(db, session, customer, ctx, lang)
    return _ask_fg_outcome(session.so_no, result.rows, ctx, lang)


# ---------------- lookups ----------------
async def _lookup_so(db, session, customer, so_no, po_no, fg_code, ctx, lang) -> Outcome:
    result = await lookup_orders(db, customer, so_no=so_no, po_no=po_no)
    if result.kind == "not_found":
        session.step = "AWAIT_SO"
        return Outcome("not_found", ctx(so_no=so_no or po_no), "not_found", lang, options=menus.buttons_for("not_found", lang))
    if result.kind == "mismatch":
        reset(session)
        return Outcome("verify_failed", ReplyContext(support=ctx().support), "mismatch", lang)

    session.so_no = result.so_no
    session.po_no = po_no
    session.attempts = 0
    rows = result.rows
    if fg_code:
        rows_fg = filter_fg(rows, fg_code)
        if rows_fg:
            rows = rows_fg
    if len(rows) == 1:
        return _deliver(session, rows[0], multi=_multi_item_so(result.rows), ctx=ctx, lang=lang)
    session.step = "AWAIT_FG"
    return _ask_fg_outcome(result.so_no, rows, ctx, lang)


async def _lookup_fg(db, session, customer, fg_code, ctx, lang) -> Outcome:
    settings = get_settings()
    result = await lookup_orders(db, customer, so_no=session.so_no)
    if result.kind != "ok":
        reset(session)
        return Outcome("verify_failed" if result.kind == "mismatch" else "not_found", ctx(), result.kind, lang)
    rows = filter_fg(result.rows, fg_code)
    if rows:
        return _deliver(session, rows[0], multi=True, ctx=ctx, lang=lang)
    session.attempts += 1
    if session.attempts >= settings.fg_max_attempts:
        clear_order_state(session)
        session.step = "AWAIT_SO"
        return Outcome("not_found", ctx(so_no=session.so_no, fg_code=fg_code), "not_found", lang, options=menus.buttons_for("not_found", lang))
    opts = menus.fg_options(result.rows, lang, settings.so_menu_style)
    return Outcome("ask_fg_retry", ctx(so_no=session.so_no, fg_code=fg_code, n_items=len(result.rows)), "ask_fg", lang,
                   options=opts or menus.buttons_for("ask_fg_retry", lang))


def _deliver(session, row, multi: bool, ctx, lang) -> Outcome:
    """The one message that carries order data: only real_status, never connection_status."""
    if repeat.too_many(repeat.note(session, f"{row.so_no}/{row.fg_item_code or ''}")):
        # the same order, again and again: repeating the status is no answer, so say so and stop
        so, fg = row.so_no, row.fg_item_code
        reset(session)
        return Outcome("too_many_repeats", ctx(so_no=so, fg_code=fg), "repeat_stopped", lang)
    session.step = "DONE"
    session.fg_code = row.fg_item_code
    session.attempts = 0
    return Outcome(
        "result",
        ctx(real_status=row.real_status, so_no=row.so_no, po_no=row.po_no, fg_code=row.fg_item_code if multi else None),
        "status_delivered",
        lang,
        options=menus.buttons_for("result", lang),
    )


def _multi_item_so(rows) -> bool:
    return len({r.fg_item_code for r in rows}) > 1
