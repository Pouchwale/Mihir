"""Interactive menus: builders respect WhatsApp limits, WATI payloads are well-formed, tapped options parse."""
import uuid
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.db import session_scope
from app.services import menus
from app.services.intent import regex_parse
from app.services.processor import extract_message, process_payload, selection_title
from app.services.wati import WatiClient, wati
from tests.flow import ANAND, MEHTA, PATEL, SHREE, open_menu


def _rows(so_items: dict[str, list[str]], name="X"):
    out = []
    for so, fgs in so_items.items():
        for fg in fgs:
            out.append(SimpleNamespace(so_no=so, fg_item_code=fg, po_no=f"PO-{so}", customer_name=name, real_status="s", connection_status="CONN-X"))
    return out


def test_so_list_sorted_capped_and_valid():
    rows = _rows({str(45200 + i): ["FG-1"] for i in range(14)})
    o = menus.so_list(rows, "en")
    assert o.kind == "list" and len(o.items) == 10
    assert o.items[0].title == "SO 45213" and o.items[-1].title == "SO 45204"  # newest first
    assert o.footer  # truncated -> "type it" hint
    assert menus.validate(o) == []


def test_so_list_descriptions_in_all_languages():
    rows = _rows({"45240": ["FG-1", "FG-2", "FG-3"], "45231": ["FG-9"]})
    for lang in ("en", "hi", "gu"):
        o = menus.so_list(rows, lang)
        assert menus.validate(o) == []
        assert o.items[0].title == "SO 45240" and "3" in o.items[0].description and "PO-45240" in o.items[0].description
        assert o.items[1].title == "SO 45231" and "1" in o.items[1].description
        # nothing internal leaks into a row
        assert "CONN" not in o.items[0].description


def test_fg_list_and_fallback_when_too_many():
    rows = _rows({"45240": [f"FG-{i}" for i in range(3)]})
    o = menus.fg_list(rows, "hi")
    assert o and [i.title for i in o.items] == ["FG-0", "FG-1", "FG-2"] and menus.validate(o) == []
    assert menus.fg_list(_rows({"1": [f"FG-{i}" for i in range(11)]}), "en") is None


def test_buttons_valid_and_labels_parse_as_intents():
    for lang in ("en", "hi", "gu"):
        for key in ("ask_language", "main_menu", "contact_us", "so_none", "result", "not_found", "confirm_so"):
            o = menus.buttons_for(key, lang)
            assert o and o.kind == "buttons" and menus.validate(o) == [], (lang, key, o)
        yes, no = menus.confirm_buttons(lang).titles()
        assert regex_parse(yes).intent == "confirm_yes", (lang, yes)
        assert regex_parse(no).intent == "confirm_no", (lang, no)
        en, hi, gu = menus.language_buttons(lang).titles()
        assert (regex_parse(en).intent, regex_parse(hi).intent, regex_parse(gu).intent) == ("lang_en", "lang_hi", "lang_gu"), lang
        order, change, contact = menus.buttons_for("main_menu", lang).titles()
        assert regex_parse(order).intent == "order_status", (lang, order)
        assert regex_parse(change).intent == "change_language", (lang, change)
        assert regex_parse(contact).intent == "contact_us", (lang, contact)
        another, menu, done = menus.buttons_for("result", lang).titles()
        assert regex_parse(another).intent == "order_status", (lang, another)
        assert regex_parse(menu).intent == "menu", (lang, menu)
        assert regex_parse(done).intent == "bye", (lang, done)
        my_orders, _ = menus.buttons_for("not_found", lang).titles()
        assert regex_parse(my_orders).intent == "order_status", (lang, my_orders)


def test_row_titles_parse_back_to_codes():
    p = regex_parse("SO 45240")
    assert p.so_no == "45240" and not p.fg_code
    p = regex_parse("FG-2002")
    assert p.fg_code == "FG-2002" and not p.so_no


def test_truncation_marks():
    long = "A" * 40
    o = menus.Options(kind="list", items=[menus.Option(menus._cut(long, 24), menus._cut(long, 72))], button_text="Select")
    assert len(o.items[0].title) == 24 and o.items[0].title.endswith("…") and menus.validate(o) == []
    bad = menus.Options(kind="buttons", items=[menus.Option("x" * 21)])
    assert menus.validate(bad)


def test_wati_payload_shapes():
    body = "Pick one"
    lst = menus.so_list(_rows({"45240": ["FG-1", "FG-2"]}), "en")
    p = WatiClient.list_payload(body, lst)
    assert set(p) == {"header", "body", "footer", "buttonText", "sections"}
    assert p["sections"][0]["rows"][0] == {"title": "SO 45240", "description": "2 items · PO PO-45240"}
    assert p["buttonText"] == "Select SO"
    b = WatiClient.buttons_payload(body, menus.confirm_buttons("en"))
    assert b == {"body": body, "buttons": [{"text": "Yes"}, {"text": "No"}]}
    b = WatiClient.buttons_payload(body, menus.Options(kind="buttons", items=[menus.Option("A")], header="H", footer="F"))
    assert b["header"] == {"type": "Text", "text": "H"} and b["footer"] == "F"


def test_as_text_fallback_lists_options():
    """The fallback must never number the options: a customer answering "1" would be typing something
    the parser has to read as a whole message, and "1" is the English button on the language screen."""
    o = menus.so_list(_rows({"45240": ["FG-1"]}), "en")
    text = o.as_text()
    assert "• SO 45240" in text and "1. SO 45240" not in text
    assert menus.confirm_buttons("en").as_text() == "• Yes\n• No"
    # every line is answerable by typing it back
    assert regex_parse("SO 45240").so_no == "45240"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"type": "interactive", "listReply": {"title": "SO 45240", "description": "3 items"}}, "SO 45240"),
        ({"type": "button", "buttonReply": {"text": "Yes"}}, "Yes"),
        ({"type": "interactive", "interactiveButtonReply": {"title": "No", "id": "1"}}, "No"),
        ({"type": "interactive", "interactive": {"list_reply": {"id": "r1", "title": "FG-2002"}}}, "FG-2002"),
        ({"type": "interactive", "interactive": {"button_reply": {"id": "b1", "title": "Done"}}}, "Done"),
        ({"type": "text", "text": "hello", "listReply": None}, None),
    ],
)
def test_selection_title_variants(payload, expected):
    assert selection_title(payload) == expected


def test_extract_message_prefers_tapped_title():
    phone, t, text, media = extract_message({"waId": "9199", "type": "interactive", "text": "SO 45240", "listReply": {"title": "SO 45240"}})
    assert (phone, t, text, media) == ("9199", "interactive", "SO 45240", None)
    phone, t, text, media = extract_message({"waId": "9199", "type": "text", "text": "hi"})
    assert (t, text) == ("text", "hi")
    phone, t, text, media = extract_message({"waId": "9199", "type": "audio", "data": "a/b.ogg"})
    assert (t, text, media) == ("audio", None, "a/b.ogg")


async def _send(payload: dict):
    payload.setdefault("id", f"m-{uuid.uuid4().hex}")
    async with session_scope() as db:
        return await process_payload(db, payload)


@pytest.mark.asyncio
async def test_list_rows_from_wati_drive_the_conversation(clean_sessions, monkeypatch):
    """WATI reports a tapped list row as listReply; the row title goes through the normal parser."""
    monkeypatch.setattr(get_settings(), "so_menu_style", "list")
    await open_menu(MEHTA)
    r = await _send({"waId": MEHTA, "type": "button", "buttonReply": {"text": "Order status"}})
    assert r.options and r.options["kind"] == "list" and r.options["items"][0]["title"] == "SO 45240"
    assert r.outcome == "ask_so" and r.step_after == "AWAIT_SO"
    r = await _send({"waId": MEHTA, "type": "interactive", "text": "SO 45240", "listReply": {"title": "SO 45240", "description": "3 items"}})
    assert r.outcome == "ask_fg" and r.options["kind"] == "list" and [i["title"] for i in r.options["items"]] == ["FG-2001", "FG-2002", "FG-2003"]
    r = await _send({"waId": MEHTA, "type": "interactive", "listReply": {"title": "FG-2003", "description": ""}})
    assert r.outcome == "status_delivered" and "Awaiting Material" in r.reply_text
    assert r.options["kind"] == "buttons" and [i["title"] for i in r.options["items"]] == ["Check another SO", "Main menu", "Done"]
    r = await _send({"waId": MEHTA, "type": "button", "buttonReply": {"text": "Check another SO"}})
    assert r.outcome == "ask_so" and r.options["kind"] == "list"
    r = await _send({"waId": MEHTA, "type": "button", "buttonReply": {"text": "Done"}})
    assert r.outcome == "bye" and r.step_after == "START" and r.options is None


@pytest.mark.asyncio
async def test_menu_shows_only_own_orders_and_no_orders_case(clean_sessions):
    await open_menu(SHREE)
    r = await _send({"waId": SHREE, "type": "text", "text": "order status"})
    assert [i["title"] for i in r.options["items"]] == ["SO 45232", "SO 45231"]
    await open_menu(PATEL)  # only a mismatching row exists
    r = await _send({"waId": PATEL, "type": "text", "text": "orders"})
    assert "could not find any orders" in r.reply_text and r.options["kind"] == "buttons"
    await open_menu(ANAND)  # no rows
    r = await _send({"waId": ANAND, "type": "button", "buttonReply": {"text": "Order status"}})
    assert r.outcome == "ask_so" and "could not find any orders" in r.reply_text


@pytest.mark.asyncio
async def test_voice_confirm_has_yes_no_buttons_and_not_found_buttons(clean_sessions):
    await open_menu(SHREE)
    r = await _send({"waId": SHREE, "type": "audio", "data": "x.ogg"})
    assert r.outcome == "confirm" and [i["title"] for i in r.options["items"]] == ["Yes", "No"]
    r = await _send({"waId": SHREE, "type": "button", "buttonReply": {"text": "Yes"}})
    assert r.outcome == "status_delivered"
    r = await _send({"waId": SHREE, "type": "text", "text": "99999"})
    assert r.outcome == "not_found" and [i["title"] for i in r.options["items"]] == ["Show my orders", "Main menu"]
    r = await _send({"waId": SHREE, "type": "button", "buttonReply": {"text": "Show my orders"}})
    assert r.outcome == "ask_so" and r.options["kind"] == "buttons"


@pytest.mark.asyncio
async def test_outbox_records_menu(clean_sessions):
    n0 = len(wati.outbox)
    await _send({"waId": MEHTA, "type": "text", "text": "hi"})
    assert len(wati.outbox) == n0 + 2 and wati.outbox[-1]["kind"] == "buttons" and wati.outbox[-1]["options"]["items"]
    assert wati.outbox[-2]["kind"] == "text"


@pytest.mark.asyncio
async def test_hindi_menu_labels(clean_sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), "so_menu_style", "list")
    await open_menu(MEHTA, "हिंदी")
    r = await _send({"waId": MEHTA, "type": "button", "buttonReply": {"text": "ऑर्डर स्टेटस"}})
    assert r.options["button_text"] == "SO चुनें" and "आइटम" in r.options["items"][0]["description"]
    r = await _send({"waId": MEHTA, "type": "interactive", "listReply": {"title": "SO 45240"}})
    assert "SO 45240" in r.options["section_title"] and r.options["button_text"] == "आइटम चुनें"


def test_language_detection_keeps_session_language_for_codes():
    from app.services.intent import detect_language

    assert detect_language("SO 45240") is None
    assert detect_language("45231") is None
    assert detect_language("FG-2002") is None
    assert detect_language("check status of SO 45240") == "en"
    assert detect_language("SO नंबर 45231") == "hi"
    assert detect_language("SO નંબર 45250") == "gu"
    assert detect_language("yes") == "en"


def test_indic_intents_without_word_boundary_bug():
    assert regex_parse("हाँ").intent == "confirm_yes"
    assert regex_parse("नहीं").intent == "confirm_no"
    assert regex_parse("નમસ્તે").intent == "greeting"
    assert regex_parse("नमस्ते!").intent == "greeting"
    p = regex_parse("SO नंबर 45231")
    assert p.so_no == "45231" and p.language == "hi"
    p = regex_parse("SO નંબર 45250 છે")
    assert p.so_no == "45250" and p.language == "gu"
