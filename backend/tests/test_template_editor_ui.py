"""Editable buttons, the conversation flow map, WATI status and test-send."""
import uuid

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete

from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Template, TemplateHistory
from app.services import menus, templates as T
from app.services.processor import process_payload
from app.services.wati import wati
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


# ---------------- flow map ----------------
def test_flow_map_is_consistent_with_the_templates():
    cat = T.catalog()
    nodes = cat["flow"]["nodes"]
    keys = {n["key"] for n in nodes}
    # every bot node is a real, editable template and carries its title
    for n in nodes:
        if n["kind"] == "bot":
            assert n["key"] in T.TEMPLATE_SPECS
            assert n["title"] == T.TEMPLATE_SPECS[n["key"]].title and n["when"]
    # every edge connects nodes that exist
    for e in cat["flow"]["edges"]:
        assert e["from"] in keys and e["to"] in keys, e
        assert e["label"]
    # no two nodes share a cell
    cells = [(n["col"], n["row"]) for n in nodes]
    assert len(set(cells)) == len(cells)
    # templates not on the map are still reachable in the list view
    assert {t["key"] for t in cat["templates"] if t["in_flow"]} == {n["key"] for n in nodes if n["kind"] == "bot"}


# ---------------- editable buttons ----------------
def test_button_validation():
    assert T.validate_buttons("result", ["another", "done"]) == []
    # a message the flow depends on may not be left with no way forward for a tap-only customer
    assert any("at least one button" in e for e in T.validate_buttons("result", []))
    assert any("at least one button" in e for e in T.validate_buttons("main_menu", []))
    assert T.validate_buttons("bye", []) == []  # a dead end on purpose: the window has ended
    assert any("at most 3" in e for e in T.validate_buttons("result", ["another", "done", "my_orders", "menu"]))
    assert any("same button" in e for e in T.validate_buttons("result", ["done", "done"]))
    assert any("unknown button" in e for e in T.validate_buttons("result", ["nope"]))
    assert any("cannot be changed" in e for e in T.validate_buttons("confirm_so", ["yes"]))
    assert any("cannot be changed" in e for e in T.validate_buttons("ask_language", ["lang_en"]))
    assert T.validate_buttons("verify_failed", ["done"]) == ["'verify_failed' cannot have buttons"]


@pytest.mark.asyncio
async def test_changing_buttons_changes_what_the_customer_gets(clean_templates, clean_sessions):
    await open_menu(SHREE)
    r = await _send(SHREE, "45231")
    assert [i["title"] for i in r.options["items"]] == ["Check another SO", "Main menu", "Done"]

    assert await T.save_buttons("result", ["my_orders"]) == []
    r = await _send(SHREE, "45231")
    assert [i["title"] for i in r.options["items"]] == ["Show my orders"]
    # the new button still works when tapped
    r = await _send(SHREE, "Show my orders")
    assert r.outcome == "ask_so"

    # 'result' must keep at least one button; the rejected save changes nothing
    assert await T.save_buttons("result", []) != []
    r = await _send(SHREE, "45231")
    assert [i["title"] for i in r.options["items"]] == ["Show my orders"]

    # a slot that may be empty (bye ends the window) does send a plain message
    assert await T.save_buttons("bye", []) == []
    r = await _send(SHREE, "done")
    assert r.outcome == "bye" and r.options is None

    # back to the default drops the override row
    assert await T.save_buttons("result", ["another", "menu", "done"]) == []
    assert not T.registry.buttons_overridden("result")

    # the main menu buttons are editable too ("done" above ended the window, so start a new one)
    assert await T.save_buttons("main_menu", ["order_status", "contact_us"]) == []
    await open_menu(SHREE)
    r = await _send(SHREE, "menu")
    assert [i["title"] for i in r.options["items"]] == ["Order status", "Contact us"]


@pytest.mark.asyncio
async def test_buttons_follow_renamed_labels(clean_templates, clean_sessions):
    await open_menu(SHREE)
    await T.save_buttons("bye", ["my_orders"])
    await T.save_text("label", "my_orders", "en", "See orders")
    r = await _send(SHREE, "done")
    assert r.outcome == "bye" and [i["title"] for i in r.options["items"]] == ["See orders"]
    # the window ended, so the next message greets again; after the language the renamed button still works
    await open_menu(SHREE)
    r = await _send(SHREE, "See orders")
    assert r.outcome == "ask_so"


@pytest.mark.asyncio
async def test_renamed_language_and_menu_buttons_still_work(clean_templates, clean_sessions):
    await T.save_text("label", "lang_en", "en", "English please")
    await T.save_text("label", "order_status", "en", "My orders")
    r = await _send(SHREE, "hi")
    assert [i["title"] for i in r.options["items"]] == ["English please", "हिंदी", "ગુજરાતી"]
    r = await _send(SHREE, "English please")
    assert r.outcome == "menu" and [i["title"] for i in r.options["items"]][0] == "My orders"
    r = await _send(SHREE, "My orders")
    assert r.outcome == "ask_so"


def test_confirm_buttons_stay_fixed():
    assert T.registry.buttons("confirm_so") == ["yes", "no"]
    assert menus.buttons_for("confirm_so", "en").titles() == ["Yes", "No"]


# ---------------- API ----------------
@pytest.mark.asyncio
async def test_buttons_api(clean_templates):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/admin/api/templates/buttons/result", headers=H, json={"buttons": ["done"]})
        assert r.json() == {"ok": True, "errors": [], "buttons": ["done"]}
        r = await c.put("/admin/api/templates/buttons/result", headers=H, json={"buttons": ["done", "done"]})
        assert r.json()["ok"] is False
        assert T.registry.buttons("result") == ["done"]  # rejected save changed nothing
        r = await c.delete("/admin/api/templates/buttons/result", headers=H)
        assert r.json()["buttons"] == ["another", "menu", "done"]
        assert (await c.delete("/admin/api/templates/buttons/verify_failed", headers=H)).status_code == 404
        # the generic text route is not shadowed by /buttons/{key}
        r = await c.put("/admin/api/templates/template/bye", headers=H, json={"texts": {"en": "Bye now!"}})
        assert r.json()["ok"] is True and T.registry.text("bye", "en") == "Bye now!"


@pytest.mark.asyncio
async def test_wati_status_reports_mock_mode():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/api/templates/wati-status")).status_code == 401
        s = (await c.get("/admin/api/templates/wati-status", headers=H)).json()
    assert s["mocked"] is True and s["connected"] is False and "nothing is sent" in s["detail"]


@pytest.mark.asyncio
async def test_wati_status_live(monkeypatch):
    base = "https://live-mt-server.wati.io/tenant9"
    monkeypatch.setenv("WATI_BASE_URL", base)
    monkeypatch.setenv("WATI_TOKEN", "tok")
    monkeypatch.setenv("WATI_DRY_RUN", "false")
    get_settings.cache_clear()
    try:
        with respx.mock:
            respx.get(f"{base}/api/v1/getContacts").mock(return_value=Response(200, json={"result": "success"}))
            ok = await wati.check()
        assert ok["connected"] is True and ok["mocked"] is False
        # a 401 is only blamed on the token once a second, unrelated endpoint refuses it too
        with respx.mock:
            respx.get(f"{base}/api/v1/getContacts").mock(return_value=Response(401, text="unauthorized"))
            respx.get(f"{base}/api/v2/webhookEndpoints").mock(return_value=Response(401, text="unauthorized"))
            bad = await wati.check()
        assert bad["connected"] is False and "token" in bad["detail"].lower()
        with respx.mock:
            respx.get(f"{base}/api/v1/getContacts").mock(return_value=Response(404, text="nope"))
            missing = await wati.check()
        assert missing["connected"] is False and "WATI_BASE_URL" in missing["detail"]
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_test_send(clean_templates):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        n0 = len(wati.outbox)
        r = (await c.post("/admin/api/templates/test-send", headers=H,
                          json={"kind": "template", "key": "result", "lang": "en", "phone": "9167861236"})).json()
        assert r["ok"] is True and r["mocked"] is True and r["sent_to"] == "919167861236"
        assert r["text"].startswith("Hello Mehta Foods,") and "45240" in r["text"] and "In Production" in r["text"]
        assert [i["title"] for i in r["options"]["items"]] == ["Check another SO", "Main menu", "Done"]
        assert len(wati.outbox) == n0 + 1

        # unsaved draft text is what gets sent
        r = (await c.post("/admin/api/templates/test-send", headers=H,
                          json={"kind": "template", "key": "result", "lang": "en", "phone": "9167861236", "text": "Draft {so_no}: {real_status}"})).json()
        assert r["ok"] is True and r["text"] == "Draft 45240: In Production"
        assert T.registry.text("result", "en") == T.registry.text("result", "en")  # nothing was saved

        # a broken draft is refused before anything is sent
        n1 = len(wati.outbox)
        r = (await c.post("/admin/api/templates/test-send", headers=H,
                          json={"kind": "template", "key": "result", "lang": "en", "phone": "9167861236", "text": "no placeholders"})).json()
        assert r["ok"] is False and "Fix these first" in r["detail"] and len(wati.outbox) == n1

        # bad phone, wrong kind, unknown key
        assert (await c.post("/admin/api/templates/test-send", headers=H,
                             json={"kind": "template", "key": "result", "lang": "en", "phone": "12"})).json()["ok"] is False
        assert (await c.post("/admin/api/templates/test-send", headers=H,
                             json={"kind": "label", "key": "yes", "lang": "en", "phone": "9167861236"})).status_code == 400
        assert (await c.post("/admin/api/templates/test-send", headers=H,
                             json={"kind": "template", "key": "nope", "lang": "en", "phone": "9167861236"})).status_code == 404
        assert (await c.post("/admin/api/templates/test-send", json={"kind": "template", "key": "result", "lang": "en", "phone": "9"})).status_code == 401


@pytest.mark.asyncio
async def test_test_send_uses_the_order_menu_and_edited_labels(clean_templates, monkeypatch):
    await T.save_text("label", "select_so", "en", "Pick order")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = (await c.post("/admin/api/templates/test-send", headers=H,
                          json={"kind": "template", "key": "ask_so_list", "lang": "en", "phone": "9167861236"})).json()
        assert r["options"]["kind"] == "buttons" and [i["title"] for i in r["options"]["items"]] == ["SO 45240", "SO 45231"]
        monkeypatch.setattr(get_settings(), "so_menu_style", "list")
        r = (await c.post("/admin/api/templates/test-send", headers=H,
                          json={"kind": "template", "key": "ask_so_list", "lang": "en", "phone": "9167861236"})).json()
        assert r["options"]["kind"] == "list" and r["options"]["button_text"] == "Pick order"
        assert [i["title"] for i in r["options"]["items"]] == ["SO 45240", "SO 45231"]
        # the language question carries the three language buttons
        r = (await c.post("/admin/api/templates/test-send", headers=H,
                          json={"kind": "template", "key": "ask_language", "lang": "hi", "phone": "9167861236"})).json()
        assert [i["title"] for i in r["options"]["items"]] == ["English", "हिंदी", "गुजराती"]


@pytest.mark.asyncio
async def test_test_send_custom_reply(clean_templates):
    await T.save_custom(T.CustomReply(key="hours", title="Hours", triggers=["timing"], texts={"en": "Open 9-6. Call {support}.", "hi": "", "gu": ""}, buttons=["done"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = (await c.post("/admin/api/templates/test-send", headers=H,
                          json={"kind": "custom", "key": "hours", "lang": "en", "phone": "9167861236"})).json()
    assert r["ok"] is True and "support@test" in r["text"] and [i["title"] for i in r["options"]["items"]] == ["Done"]


@pytest.mark.asyncio
async def test_test_send_reports_a_wati_refusal(clean_templates, monkeypatch):
    base = "https://live-mt-server.wati.io/tenant9"
    monkeypatch.setenv("WATI_BASE_URL", base)
    monkeypatch.setenv("WATI_TOKEN", "tok")
    monkeypatch.setenv("WATI_DRY_RUN", "false")
    get_settings.cache_clear()
    try:
        with respx.mock:
            # interactive send refused, and the plain-text fallback fails too
            respx.post(f"{base}/api/v1/sendInteractiveButtonsMessage").mock(return_value=Response(400, text="outside 24h window"))
            respx.post(f"{base}/api/v1/sendSessionMessage/919167861236").mock(return_value=Response(400, text="outside 24h window"))
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = (await c.post("/admin/api/templates/test-send", headers=H,
                                  json={"kind": "template", "key": "result", "lang": "en", "phone": "9167861236"})).json()
        assert r["ok"] is False and "refused" in r["detail"]
    finally:
        get_settings.cache_clear()
