"""Everything that is not the happy path.

The important one is test_garbage_input_at_every_step: whatever a customer sends, at any point, the
bot must answer, must not fall over, and the right answer sent straight afterwards must still work.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from tests.flow import (ANAND, BTN, GUJ, L, LANGS_FOR, MEHTA, OM, PATEL, ROYAL, SHREE, SUNRISE, UNKNOWN,
                        age_session, mismatch_logs, open_menu, say, send, session_of, step_of, tap, titles, voice)

STEPS = ["LANG", "MENU", "AWAIT_SO", "AWAIT_FG", "CONFIRM", "DONE"]
GARBAGE = ["", "   ", "?????", "😀😀😀", "asdkjhasdkjh", "SELECT * FROM orders", "{so_no}", "x" * 3000, "​"]


async def reach(step: str, phone: str = MEHTA) -> None:
    """Put the session in one specific step."""
    if step == "LANG":
        await send(phone, "hi")
        return
    await open_menu(phone)
    if step == "MENU":
        return
    if step == "DONE":
        await send(phone, "SO 45240")
        await send(phone, "FG-2002")
        return
    if step == "CONFIRM":
        with voice("my SO number is 45240"):
            await send(phone, audio=True)
        return
    await tap(phone, L("order_status", "en"))
    if step == "AWAIT_SO":
        return
    await tap(phone, "SO 45240")  # -> AWAIT_FG (three items)


# ---------------- verification ----------------
@pytest.mark.asyncio
async def test_unknown_number_is_refused_in_all_three_languages(clean_sessions):
    r = await send(UNKNOWN, "hi")
    assert r.outcome == "verify_failed" and r.options is None
    for word in ("could not verify", "क्षमा", "માફ"):
        assert word in r.reply_text
    assert "support@test" in r.reply_text
    assert await step_of(UNKNOWN) == "START"
    # it never advances, however many times they write
    assert (await send(UNKNOWN, "45231")).outcome == "verify_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("phone,so", [(PATEL, "45260"), (SUNRISE, "45270")])
async def test_a_name_that_does_not_match_byte_for_byte_is_refused(clean_sessions, phone, so):
    """PPC spells the customer slightly differently (trailing space / capitals). The order must not
    be handed over, and the difference must be logged for someone to fix."""
    await open_menu(phone)
    r = await send(phone, so)
    assert r.outcome == "mismatch"
    assert "could not verify" in r.reply_text
    assert await step_of(phone) == "START"
    logs = await mismatch_logs(phone)
    assert logs and logs[-1].so_no == so and logs[-1].excel_name != logs[-1].api_name


@pytest.mark.asyncio
@pytest.mark.parametrize("phone", [ANAND, OM])
async def test_a_customer_with_no_orders(clean_sessions, phone):
    await open_menu(phone)
    r = await tap(phone, L("order_status", "en"))
    assert r.outcome == "ask_so" and "could not find any orders" in r.reply_text
    assert titles(r) == BTN("so_none", "en")
    r = await tap(phone, L("menu", "en"))
    assert r.outcome == "menu"


@pytest.mark.asyncio
async def test_a_customer_never_sees_another_customers_order(clean_sessions):
    await open_menu(SHREE)
    r = await send(SHREE, "45240")  # belongs to Mehta Foods
    assert r.outcome == "mismatch" and "Printing" not in r.reply_text


# ---------------- not found / retries ----------------
@pytest.mark.asyncio
async def test_an_unknown_order_number_is_recoverable(clean_sessions):
    await open_menu(SHREE)
    r = await send(SHREE, "99999")
    assert r.outcome == "not_found" and titles(r) == BTN("not_found", "en")
    assert await step_of(SHREE) == "AWAIT_SO"
    r = await tap(SHREE, L("my_orders", "en"))
    assert r.outcome == "ask_so" and "SO 45231" in titles(r)
    r = await tap(SHREE, "SO 45231")
    assert r.outcome == "status_delivered"


@pytest.mark.asyncio
async def test_po_number_lookup(clean_sessions):
    await open_menu(ROYAL)
    r = await send(ROYAL, "PO-0000")
    assert r.outcome == "not_found"
    r = await send(ROYAL, "PO PO-7777")
    assert r.outcome == "status_delivered" and "Delivered" in r.reply_text and "45280" in r.reply_text
    assert (await session_of(ROYAL))["so_no"] == "45280"


@pytest.mark.asyncio
async def test_a_wrong_item_is_asked_again_then_gives_up(clean_sessions):
    await open_menu(GUJ)
    await send(GUJ, "45250")
    r = await send(GUJ, "FG-9999")
    assert r.outcome == "ask_fg" and "FG-9999" in r.reply_text
    assert (await session_of(GUJ))["attempts"] == 1
    assert titles(r) == ["FG-3001", "FG-3002"]  # the real items are still offered
    r = await send(GUJ, "FG-3001")  # correct on the second try
    assert r.outcome == "status_delivered" and (await session_of(GUJ))["attempts"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [2, 3])
async def test_the_wrong_item_limit_comes_from_settings(clean_sessions, monkeypatch, limit):
    monkeypatch.setattr(get_settings(), "fg_max_attempts", limit)
    await open_menu(GUJ)
    await send(GUJ, "45250")
    for i in range(limit - 1):
        r = await send(GUJ, f"FG-800{i}")
        assert r.outcome == "ask_fg", i
    r = await send(GUJ, "FG-9999")
    assert r.outcome == "not_found" and await step_of(GUJ) == "AWAIT_SO"
    assert (await session_of(GUJ))["so_no"] is None


# ---------------- voice ----------------
@pytest.mark.asyncio
@pytest.mark.parametrize("transcript,kind,expect", [
    ("my SO number is 45231", "so", "45231"),
    ("PO number PO-7781", "po", "PO-7781"),
])
async def test_a_voice_note_is_confirmed_before_any_lookup(clean_sessions, transcript, kind, expect):
    """Speech-to-text misreads digits, so the number is read back before it is used."""
    await open_menu(SHREE)
    with voice(transcript):
        r = await send(SHREE, audio=True)
    assert r.outcome == "confirm" and expect in r.reply_text
    assert titles(r) == [L("yes", "en"), L("no", "en")]
    assert (await session_of(SHREE))["pending_kind"] == kind
    r = await tap(SHREE, L("yes", "en"))
    assert r.outcome == "status_delivered"


@pytest.mark.asyncio
async def test_saying_no_to_a_voice_note_reopens_the_choices(clean_sessions):
    await open_menu(SHREE)
    with voice("my SO number is 45231"):
        await send(SHREE, audio=True)
    r = await tap(SHREE, L("no", "en"))
    assert r.outcome == "ask_so" and "SO 45231" in titles(r)
    assert await step_of(SHREE) == "AWAIT_SO"


@pytest.mark.asyncio
async def test_a_typed_number_overrides_a_pending_voice_note(clean_sessions):
    await open_menu(SHREE)
    with voice("my SO number is 45231"):
        await send(SHREE, audio=True)
    r = await send(SHREE, "45232")
    assert r.outcome == "status_delivered" and "Dispatched" in r.reply_text


@pytest.mark.asyncio
async def test_an_unclear_answer_at_the_confirmation_asks_again(clean_sessions):
    await open_menu(SHREE)
    with voice("my SO number is 45231"):
        await send(SHREE, audio=True)
    before = await session_of(SHREE)
    r = await send(SHREE, "hmm what")
    assert r.outcome == "confirm" and await step_of(SHREE) == "CONFIRM"
    assert (await session_of(SHREE))["pending_value"] == before["pending_value"]


@pytest.mark.asyncio
async def test_a_voice_note_the_bot_cannot_use_falls_back_to_the_prompt(clean_sessions):
    await open_menu(MEHTA)
    await tap(MEHTA, L("order_status", "en"))
    with voice("hmm er umm"):  # nothing usable in it
        r = await send(MEHTA, audio=True)
    assert r.outcome == "ask_so" and titles(r)  # the order choices again, not an error


# ---------------- navigation ----------------
@pytest.mark.asyncio
@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("label,outcome,after", [
    ("menu", "menu", "MENU"),
    ("contact_us", "contact", None),
    ("change_language", "ask_language", "LANG"),
])
async def test_navigation_works_from_every_step(clean_sessions, step, label, outcome, after):
    await reach(step)
    r = await send(MEHTA, L(label, "en"))
    if step == "LANG":
        # the language question comes first: nothing else is accepted until it is answered
        assert r.outcome == "ask_language"
        return
    assert r.outcome == outcome, (step, label, r.outcome)
    if after:
        assert await step_of(MEHTA) == after
    # and the customer can still get their status afterwards
    await open_menu(MEHTA) if await step_of(MEHTA) == "START" else None
    if await step_of(MEHTA) == "LANG":
        await tap(MEHTA, L("lang_en", "en"))
    r = await send(MEHTA, "SO 45240")
    assert r.outcome in ("ask_fg", "status_delivered")


@pytest.mark.asyncio
@pytest.mark.parametrize("step", STEPS)
async def test_saying_thanks_ends_the_window(clean_sessions, step):
    await reach(step)
    r = await send(MEHTA, "thanks")
    if step == "LANG":
        assert r.outcome == "ask_language"  # language first
        return
    assert r.outcome == "bye" and await step_of(MEHTA) == "START"
    # the next message starts a fresh window with the greeting
    r = await send(MEHTA, "hello")
    assert r.outcome == "ask_language" and len(r.replies) == 2


# ---------------- robustness ----------------
@pytest.mark.asyncio
@pytest.mark.parametrize("step", STEPS)
async def test_garbage_input_at_every_step(clean_sessions, step):
    """Whatever arrives, the bot answers, does not crash, and still works straight afterwards."""
    for junk in GARBAGE:
        await reach(step)
        before = await step_of(MEHTA)
        r = await send(MEHTA, junk)
        assert r.outcome != "service_down", (step, junk, r.reply_text)
        assert r.reply_text, (step, junk)
        # either something to tap, or words telling them what to send
        assert r.options or any(w in r.reply_text.lower() for w in ("type", "send", "choose", "select", "number")), (step, junk, r.reply_text)
        assert await step_of(MEHTA) in (before, "MENU", "LANG", "AWAIT_SO", "AWAIT_FG", "DONE", "START")
        # the real answer still works
        if await step_of(MEHTA) == "START":
            await open_menu(MEHTA)
        if await step_of(MEHTA) == "LANG":
            await tap(MEHTA, L("lang_en", "en"))
        r = await send(MEHTA, "SO 45240")
        assert r.outcome in ("ask_fg", "status_delivered"), (step, junk, r.outcome)
        await send(MEHTA, "thanks")  # reset for the next junk value


@pytest.mark.asyncio
async def test_a_code_beats_a_keyword_in_the_same_message(clean_sessions):
    await open_menu(SHREE)
    assert (await send(SHREE, "45231 please")).outcome == "status_delivered"
    assert (await send(SHREE, "menu 45232")).outcome == "status_delivered"


@pytest.mark.asyncio
async def test_typing_a_digit_does_not_change_the_language(clean_sessions):
    """People number a list themselves. "1" must not be read as "English" outside the language
    question - it used to throw the customer back to the main menu."""
    await open_menu(MEHTA, "हिंदी")
    await tap(MEHTA, L("order_status", "hi"))
    for digit in ("1", "2", "3"):
        r = await send(MEHTA, digit)
        assert r.outcome != "menu", digit
        assert (await session_of(MEHTA))["language"] == "hi", digit
    # ...but at the language question they still work
    await send(MEHTA, L("change_language", "hi"))
    r = await send(MEHTA, "1")
    assert r.outcome == "menu" and (await session_of(MEHTA))["language"] == "en"


# ---------------- limits and timeouts ----------------
@pytest.mark.asyncio
async def test_rate_limit_replies_once_then_goes_quiet(clean_sessions, monkeypatch):
    """One phone must not be able to flood the bot (each inbound message costs a WATI send)."""
    from app.db import session_scope
    from app.models import MessageLog

    monkeypatch.setattr(get_settings(), "rate_limit_msgs", 3)
    phone = SHREE
    # The limiter counts inbound rows, which the webhook writes before the processor runs. One more
    # than the limit means this message is the first over it.
    async with session_scope() as db:
        for _ in range(4):
            db.add(MessageLog(phone_e164=phone, direction="in", msg_type="text", text="hi"))
    r = await send(phone, "hi")
    assert r.outcome == "rate_limited" and r.reply_text
    async with session_scope() as db:
        db.add(MessageLog(phone_e164=phone, direction="in", msg_type="text", text="hi"))
    r = await send(phone, "hi")
    assert r.outcome == "rate_limited" and r.reply_text is None  # silent from here


@pytest.mark.asyncio
@pytest.mark.parametrize("step", STEPS)
async def test_a_long_silence_starts_a_new_window(clean_sessions, step):
    await reach(step)
    await age_session(MEHTA, get_settings().session_timeout_min + 5)
    r = await send(MEHTA, "SO 45240")
    assert r.outcome == "ask_language" and len(r.replies) == 2
    s = await session_of(MEHTA)
    assert s["step"] == "LANG" and s["so_no"] is None and s["lang_chosen"] is False


@pytest.mark.asyncio
async def test_a_short_pause_continues_where_they_left_off(clean_sessions):
    await reach("AWAIT_FG")
    await age_session(MEHTA, get_settings().session_timeout_min - 5)
    r = await send(MEHTA, "FG-2002")
    assert r.outcome == "status_delivered" and "Printing" in r.reply_text
