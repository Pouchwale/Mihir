"""Template editor: validation, overrides applied live, custom keyword replies, API."""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Template, TemplateHistory
from app.services import menus, replies, templates as T
from app.services.processor import process_payload
from tests.flow import SHREE, open_menu

H = {"X-Admin-Key": "test-admin"}


@pytest.fixture
async def clean_templates():
    async with session_scope() as db:
        await db.execute(delete(Template))
        await db.execute(delete(TemplateHistory))
    await T.load_from_db()
    yield
    async with session_scope() as db:
        await db.execute(delete(Template))
        await db.execute(delete(TemplateHistory))
    await T.load_from_db()


async def _send(phone, text):
    async with session_scope() as db:
        return await process_payload(db, {"id": f"t-{uuid.uuid4().hex}", "waId": phone, "type": "text", "text": text})


# ---------------- validation ----------------
def test_validate_template_rules():
    assert T.validate_template("result", "en", "Status for SO {so_no}: {real_status}") == []
    assert any("missing required" in e for e in T.validate_template("result", "en", "Status: done"))
    assert any("unknown placeholder" in e for e in T.validate_template("main_menu", "en", "Hi {name}"))
    assert any("unbalanced" in e for e in T.validate_template("main_menu", "en", "Hi {support"))
    assert any("empty" in e for e in T.validate_template("main_menu", "hi", "   "))
    assert any("too long" in e for e in T.validate_template("main_menu", "en", "x" * 1025))
    assert T.validate_template("main_menu", "en", "Hello {customer_name}, {support}") == []
    assert T.validate_template("welcome_first", "en", "Hello, welcome!") == []
    assert T.validate_template("result", "en", "Hello {customer_name}, SO {so_no} PO {po_no}: {real_status}") == []
    assert T.validate_template("nope", "en", "x") == ["unknown template 'nope'"]


def test_validate_label_rules():
    assert T.validate_label("yes", "en", "Yes, correct") == []
    assert T.validate_label("yes", "en", "Confirm") == []  # any text works: the parser learns the label
    assert any("already means" in e for e in T.validate_label("done", "en", "yes"))  # conflicts with confirm_yes
    assert any("same text" in e for e in T.validate_label("done", "en", "No"))  # same as the No button
    assert any("look like" in e for e in T.validate_label("another", "en", "SO 45231"))
    assert any("too long" in e for e in T.validate_label("another", "en", "Check another sales order now"))
    assert any("missing placeholder" in e for e in T.validate_label("items_of_so", "en", "Items"))
    assert any("too long" in e for e in T.validate_label("items_of_so", "en", "All the items inside SO {so}"))  # > 24 after render
    assert T.validate_label("done", "hi", "हो गया") == []


def test_validate_custom_rules():
    ok = T.CustomReply(key="office-hours", title="Office hours", triggers=["timing", "समय"], texts={"en": "9-6", "hi": "", "gu": ""})
    assert T.validate_custom(ok) == []
    bad = T.CustomReply(key="result", title="", triggers=[], texts={"en": "", "hi": "", "gu": ""}, buttons=["x", "done", "menu", "another"])
    errs = T.validate_custom(bad)
    assert any("clashes" in e for e in errs) and any("title" in e for e in errs) and any("trigger" in e for e in errs)
    assert any("unknown button" in e for e in errs) and any("at most 3" in e for e in errs) and any("at least one language" in e for e in errs)


# ---------------- overrides applied live ----------------
@pytest.mark.asyncio
async def test_template_override_changes_customer_reply(clean_templates, clean_sessions):
    await open_menu(SHREE)
    assert await T.save_text("template", "result", "en", "Status of SO {so_no}{item} is: {real_status}. Thanks {customer_name}!") == []
    r = await _send(SHREE, "45231")
    assert r.reply_text.startswith("Status of SO 45231 is: In Production. Thanks Shree Packaging Pvt Ltd!")
    # history recorded, reset restores default
    h = await T.history("template", "result")
    assert h and h[0]["action"] == "save" and h[0]["text"] is None
    await T.reset_key("template", "result")
    r = await _send(SHREE, "45231")
    assert r.reply_text.startswith("Hello Shree Packaging Pvt Ltd,\n\nOrder: SO 45231\nReal Status: In Production")


@pytest.mark.asyncio
async def test_greeting_and_language_question_are_editable(clean_templates, clean_sessions):
    assert await T.save_text("template", "welcome_first", "en", "Namaste! Welcome to GPP.") == []
    assert await T.save_text("template", "ask_language", "en", "Language? / भाषा?") == []
    r = await _send(SHREE, "hello")
    assert r.replies == ["Namaste! Welcome to GPP.", "Language? / भाषा?"]
    assert [i["title"] for i in r.options["items"]] == ["English", "हिंदी", "ગુજરાતી"]
    assert await T.save_text("template", "main_menu", "hi", "नमस्ते {customer_name} जी, बताइए?") == []
    r = await _send(SHREE, "हिंदी")
    assert r.reply_text == "नमस्ते Shree Packaging Pvt Ltd जी, बताइए?"


@pytest.mark.asyncio
async def test_saving_default_text_drops_override(clean_templates):
    default = replies.DEFAULTS["main_menu"]["en"]
    await T.save_text("template", "main_menu", "en", "Custom hello {customer_name}")
    assert T.registry.is_overridden("template", "main_menu", "en")
    await T.save_text("template", "main_menu", "en", default)
    assert not T.registry.is_overridden("template", "main_menu", "en")


@pytest.mark.asyncio
async def test_label_override_changes_buttons_and_still_parses(clean_templates, clean_sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), "so_menu_style", "list")
    await open_menu(SHREE)
    assert await T.save_text("label", "another", "en", "Another order") == []
    assert await T.save_text("label", "select_so", "en", "Pick order") == []
    assert menus.label("select_so", "en") == "Pick order"
    r = await _send(SHREE, "45231")
    titles = [i["title"] for i in r.options["items"]]
    assert titles == ["Another order", "Main menu", "Done"]
    r = await _send(SHREE, "Another order")  # tapped (edited) label must still re-open the order list
    assert r.outcome == "ask_so" and r.options["button_text"] == "Pick order"
    r = await _send(SHREE, "another order!")  # typed, different case/punctuation
    assert r.outcome == "ask_so"
    assert await T.save_text("label", "done", "gu", "પૂર્ણ") == []
    r = await _send(SHREE, "પૂર્ણ")
    assert r.outcome == "bye"


@pytest.mark.asyncio
async def test_custom_reply_flow(clean_templates, clean_sessions):
    await open_menu(SHREE)
    c = T.CustomReply(key="office-hours", title="Office hours", triggers=["timing", "office hours", "समय"], texts={"en": "We are open Mon-Sat 9-6. Call {support}.", "hi": "हम सोम-शनि 9-6 खुले हैं।", "gu": ""}, buttons=["my_orders", "done"])
    assert await T.save_custom(c) == []
    r = await _send("919167861236", "What is your timing?")
    assert r.outcome == "custom" and "open Mon-Sat" in r.reply_text and "support@test" in r.reply_text
    assert [i["title"] for i in r.options["items"]] == ["Show my orders", "Done"]
    r = await _send("919167861236", "समय")
    assert "open Mon-Sat" in r.reply_text  # English was chosen, so the reply stays English
    await _send(SHREE, "change language")
    await _send(SHREE, "हिंदी")
    r = await _send("919167861236", "समय")
    assert "सोम-शनि" in r.reply_text
    # Gujarati text empty -> falls back to English
    await _send(SHREE, "भाषा बदलें")
    await _send(SHREE, "ગુજરાતી")
    r = await _send("919167861236", "ઓફિસ office hours")
    assert "open Mon-Sat" in r.reply_text
    # codes always win over custom triggers; session untouched by custom replies
    r = await _send("919167861236", "45231 timing")
    assert r.outcome == "status_delivered"
    # unverified numbers never get custom replies
    r = await _send("910000000000", "timing")
    assert r.outcome == "verify_failed"
    # disable -> normal flow again
    c.enabled = False
    await T.save_custom(c)
    r = await _send("919167861236", "timing")
    assert r.outcome != "custom"
    assert await T.delete_custom("office-hours") is True
    assert "office-hours" not in T.registry.custom


@pytest.mark.asyncio
async def test_short_trigger_matches_only_whole_message(clean_templates):
    await T.save_custom(T.CustomReply(key="gst-test", title="x", triggers=["gst"], texts={"en": "GST reply", "hi": "", "gu": ""}))
    assert T.registry.match_custom("gst") is not None
    assert T.registry.match_custom("GST!") is not None
    assert T.registry.match_custom("suggested") is None  # 'gst' inside a word must not match


@pytest.mark.asyncio
async def test_a_trigger_cannot_steal_a_menu_button(clean_templates):
    """The bot reads a tapped button as its own title, so a trigger that also fires on that title
    would silently disable the button for every customer."""
    errs = T.validate_custom(T.CustomReply(key="orders-x", title="Orders", triggers=["order"], texts={"en": "hi"}))
    assert any("Show my orders" in e or "Order status" in e for e in errs)
    # a word that is not a button title but is still a built-in keyword ("thanks" ends the chat)
    assert any("built-in keyword" in e for e in T.validate_custom(T.CustomReply(key="t", title="T", triggers=["thanks"], texts={"en": "x"})))
    # "menu" is caught as well, by the button check - it is contained in the "Main menu" title
    assert T.validate_custom(T.CustomReply(key="m", title="M", triggers=["menu"], texts={"en": "x"})) != []
    assert any("order or item code" in e for e in T.validate_custom(T.CustomReply(key="c", title="C", triggers=["45240"], texts={"en": "x"})))
    # and a saved reply cannot be shadowed the other way round either
    await T.save_custom(T.CustomReply(key="gst-test", title="GST", triggers=["gst rate"], texts={"en": "18%"}))
    assert any("custom reply" in e for e in T.validate_label("another", "en", "GST rate"))


# ---------------- API ----------------
@pytest.mark.asyncio
async def test_templates_api_roundtrip(clean_templates):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/api/templates")).status_code == 401
        cat = (await c.get("/admin/api/templates", headers=H)).json()
        assert {t["key"] for t in cat["templates"]} == set(T.TEMPLATE_SPECS) and {l["key"] for l in cat["labels"]} == set(T.LABEL_SPECS)
        assert set(cat["templates"][0]["langs"]) == {"en", "hi", "gu"}
        r = await c.post("/admin/api/templates/preview", headers=H, json={"kind": "template", "key": "result", "lang": "en", "text": "SO {so_no}: {real_status}"})
        assert r.json() == {"errors": [], "rendered": "SO 45240: In Production"}
        r = await c.put("/admin/api/templates/template/result", headers=H, json={"texts": {"en": "no placeholders", "hi": "SO {so_no}: {real_status}"}})
        body = r.json()
        assert body["ok"] is False and "en" in body["errors"] and "hi" not in body["errors"]
        assert not T.registry.is_overridden("template", "result", "hi")  # nothing saved when any language fails
        r = await c.put("/admin/api/templates/template/result", headers=H, json={"texts": {"hi": "SO {so_no}: {real_status}"}})
        assert r.json()["ok"] is True and T.registry.is_overridden("template", "result", "hi")
        hist = (await c.get("/admin/api/templates/template/result/history", headers=H)).json()
        assert hist and hist[0]["lang"] == "hi"
        assert (await c.delete("/admin/api/templates/template/result", headers=H)).json()["ok"] is True
        assert not T.registry.is_overridden("template", "result", "hi")
        # "help" is refused: it is already a built-in keyword for the Contact us button
        r = await c.post("/admin/api/templates/custom", headers=H, json={"key": "help", "title": "Help", "triggers": ["help"], "texts": {"en": "Help text"}, "buttons": ["done"]})
        assert r.json()["ok"] is False
        r = await c.post("/admin/api/templates/custom", headers=H, json={"key": "gst", "title": "GST", "triggers": ["gst rate"], "texts": {"en": "18%"}, "buttons": ["done"]})
        assert r.json()["ok"] is True
        r = await c.post("/admin/api/templates/custom/test", headers=H, json={"text": "GST RATE?"})
        assert r.json()["match"]["key"] == "gst"
        assert (await c.delete("/admin/api/templates/custom/gst", headers=H)).json()["ok"] is True
        assert (await c.delete("/admin/api/templates/custom/gst", headers=H)).status_code == 404
        assert (await c.put("/admin/api/templates/template/nope", headers=H, json={"texts": {"en": "x"}})).status_code == 404
