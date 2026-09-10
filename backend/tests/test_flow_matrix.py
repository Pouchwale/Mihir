"""The happy path, every way a customer can walk it.

3 languages x (tapping buttons / typing) x (button-style / list-style choices) = 12 full conversations,
each ending in a delivered Real Status.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.services import menus
from tests.flow import (BTN, CUSTOMER_NAME, GUJ, L, LANGS_FOR, MEHTA, ORDERS, SHREE, options_of,
                        run_full_conversation, send, session_of, tap, titles, trace)

LANG_KEYS = ["lang_en", "lang_hi", "lang_gu"]
LANG_OF = {"lang_en": "en", "lang_hi": "hi", "lang_gu": "gu"}
MODES = ["tap", "type"]


@pytest.fixture
def menu_style(request, monkeypatch):
    monkeypatch.setattr(get_settings(), "so_menu_style", request.param)
    return request.param


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_style", ["auto", "list"], indirect=True)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("lang_key", LANG_KEYS)
async def test_full_conversation(clean_sessions, lang_key, mode, menu_style):
    """The whole thing, end to end, for one three-item order."""
    lang = LANG_OF[lang_key]
    results = await run_full_conversation(MEHTA, lang_key, mode)
    final = results[-1]

    assert trace(results) == [
        ("ask_language", "LANG"), ("menu", "MENU"), ("ask_so", "AWAIT_SO"), ("ask_fg", "AWAIT_FG"), ("status_delivered", "DONE"),
    ]
    assert "Ready for Dispatch" in final.reply_text
    assert CUSTOMER_NAME[MEHTA] in final.reply_text
    assert "45240" in final.reply_text
    # the item is named because this SO has several
    assert "FG-2001" in final.reply_text
    assert (await session_of(MEHTA))["language"] == lang


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("lang_key", LANG_KEYS)
async def test_single_item_order_skips_the_item_question(clean_sessions, lang_key, mode):
    results = await run_full_conversation(SHREE, lang_key, mode)
    assert [o for o, _ in trace(results)] == ["ask_language", "menu", "ask_so", "status_delivered"]
    text = results[-1].reply_text
    assert "In Production" in text and "45231" in text
    assert "FG-1001" not in text  # only one item, so it is not named


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_two_item_order_names_the_item(clean_sessions, mode):
    results = await run_full_conversation(GUJ, "lang_en", mode)
    assert "Lamination" in results[-1].reply_text and "FG-3001" in results[-1].reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize("lang_key", LANG_KEYS)
async def test_the_reply_is_in_the_language_the_customer_picked(clean_sessions, lang_key):
    lang = LANG_OF[lang_key]
    results = await run_full_conversation(MEHTA, lang_key, "tap")
    menu_text = results[1].reply_text
    marker = {"en": "Hello", "hi": "नमस्ते", "gu": "નમસ્તે"}[lang]
    assert menu_text.startswith(marker), menu_text
    # and the status keeps that language even though the codes are Latin
    assert results[-1].reply_text.startswith(marker)


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [("lang_en", "lang_hi"), ("lang_hi", "lang_gu"), ("lang_gu", "lang_en")])
async def test_language_can_be_changed_mid_conversation(clean_sessions, first, second):
    l1, l2 = LANG_OF[first], LANG_OF[second]
    await run_full_conversation(MEHTA, first, "tap")
    r = await tap(MEHTA, L("change_language", l1))
    assert r.outcome == "ask_language" and titles(r) == LANGS_FOR(l1)
    r = await tap(MEHTA, L(second, l1))
    assert r.outcome == "menu" and titles(r) == BTN("main_menu", l2)
    # the order lookup still completes in the new language
    r = await tap(MEHTA, L("order_status", l2))
    assert r.outcome == "ask_so"
    r = await tap(MEHTA, "SO 45240")
    r = await tap(MEHTA, titles(r)[0])
    assert r.outcome == "status_delivered"
    marker = {"en": "Hello", "hi": "नमस्ते", "gu": "નમસ્તે"}[l2]
    assert r.reply_text.startswith(marker)


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_style", ["auto", "list"], indirect=True)
async def test_every_menu_fits_whatsapp_limits(clean_sessions, menu_style):
    """A payload WhatsApp refuses would drop the customer to plain text, so check every one."""
    for phone in (SHREE, MEHTA, GUJ):
        for r in await run_full_conversation(phone, "lang_en", "tap"):
            o = options_of(r)
            if o:
                assert menus.validate(o) == [], (phone, r.outcome, o)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_the_customer_always_has_a_way_to_answer(clean_sessions, mode):
    """Either the message carries options to tap, or its text says what to send."""
    for r in await run_full_conversation(MEHTA, "lang_en", mode):
        for text in r.replies or []:
            pass
        if not r.options:
            assert "type" in r.reply_text.lower() or "send" in r.reply_text.lower() or "choose" in r.reply_text.lower(), r.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_style", ["auto", "list"], indirect=True)
async def test_internal_status_never_reaches_a_customer(clean_sessions, menu_style):
    """connection_status is plant-internal. Only real_status may ever be sent."""
    for phone in (SHREE, MEHTA, GUJ):
        for r in await run_full_conversation(phone, "lang_en", "tap"):
            for text in (r.replies or []):
                assert "CONN" not in text, (phone, text)
            for item in (r.options or {}).get("items", []):
                assert "CONN" not in item["title"] + item.get("description", "")


@pytest.mark.asyncio
async def test_list_style_uses_real_labels(clean_sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), "so_menu_style", "list")
    results = await run_full_conversation(MEHTA, "lang_en", "tap")
    so_menu = results[2]
    assert so_menu.options["kind"] == "list"
    assert so_menu.options["button_text"] == L("select_so", "en")
    assert so_menu.options["section_title"] == L("your_orders", "en")


@pytest.mark.asyncio
async def test_a_second_order_can_be_checked_without_starting_over(clean_sessions):
    await run_full_conversation(SHREE, "lang_en", "tap")
    r = await tap(SHREE, L("another", "en"))
    assert r.outcome == "ask_so" and "SO 45232" in titles(r)
    r = await tap(SHREE, "SO 45232")
    assert r.outcome == "status_delivered" and "Dispatched" in r.reply_text
