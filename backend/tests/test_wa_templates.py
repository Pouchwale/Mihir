"""Meta-approved WhatsApp templates: creating them, listing their approval state, deleting them.

The payload shapes here are asserted against what docs.wati.io documents, not against what this
project finds convenient - a field invented here would pass its own tests and be refused by Meta.
"""
from __future__ import annotations

import json

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app import config
from app.config import get_settings
from app.main import app
from app.services.wati import wati

H = {"X-Admin-Key": "test-admin"}
BASE = "https://live-mt-server.wati.io/tenant123"
CREATE = f"{BASE}/api/v1/whatsapp-templates"
LIST = f"{BASE}/api/ext/v3/messagetemplates"


@pytest.fixture
def live_wati():
    saved = config.overrides()
    config.apply_overrides({**saved, "wati_base_url": BASE, "wati_token": "tok123",
                            "wati_dry_run": False, "wati_api_version": "v1"})
    assert get_settings().wati_mocked is False
    yield
    config.apply_overrides(saved)


async def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ---------------- pure payload shape ----------------
def test_the_create_payload_matches_what_wati_documents():
    p = wati.template_payload("order_ready", "en", "Hi {{name}}, order {{so}} is ready.",
                              category="UTILITY", header="Order update", footer="Gujarat Printpack",
                              buttons=[{"type": "quick_reply", "text": "Thanks"}],
                              samples={"name": "Rajesh", "so": "45231"})
    assert p["type"] == "template"
    assert p["category"] == "UTILITY" and p["subCategory"] == "STANDARD"
    assert p["elementName"] == "order_ready" and p["language"] == "en"
    assert p["header"] == {"type": "TEXT", "text": "Order update"}
    assert p["buttonsType"] == "quick_reply"
    assert p["buttons"] == [{"type": "quick_reply", "parameter": {"text": "Thanks"}}]
    assert p["creationMethod"] == 0
    # Meta reviews the template with the examples filled in; an empty one reads as "{{name}}".
    assert p["customParams"] == [{"paramName": "name", "paramValue": "Rajesh"},
                                 {"paramName": "so", "paramValue": "45231"}]


def test_a_template_with_no_buttons_says_so_explicitly():
    assert wati.template_payload("x", "en", "body")["buttonsType"] == "NONE"


def test_link_and_call_buttons_get_the_fields_wati_needs():
    """WATI splits buttons into three kinds with different required fields; a link button with no
    url looks fine and does nothing."""
    p = wati.template_payload("x", "en", "body", buttons=[
        {"type": "url", "text": "Track", "url": "https://gpppl.example/track"},
        {"type": "call", "text": "Call us", "phone": "919876543210"}])
    assert p["buttonsType"] == "call_to_action"
    assert p["buttons"][0] == {"type": "url", "parameter": {
        "text": "Track", "url": "https://gpppl.example/track", "phoneNumber": "", "urlType": "static"}}
    assert p["buttons"][1] == {"type": "call", "parameter": {
        "text": "Call us", "phoneNumber": "919876543210", "url": "", "urlType": "none"}}


def test_mixing_button_kinds_is_declared_as_such():
    p = wati.template_payload("x", "en", "b", buttons=[
        {"type": "quick_reply", "text": "Yes"}, {"type": "url", "text": "Open", "url": "https://x.example"}])
    assert p["buttonsType"] == "quick_reply_and_call_to_action"


def test_a_media_header_carries_its_link():
    p = wati.template_payload("x", "en", "b", header={"type": "IMAGE", "link": "https://x.example/a.jpg"})
    assert p["header"]["type"] == "IMAGE" and p["header"]["link"] == "https://x.example/a.jpg"
    # a media header with no file is dropped rather than sent half-formed
    assert "header" not in wati.template_payload("x", "en", "b", header={"type": "IMAGE"})


def test_meta_variables_use_double_braces():
    """The bot's own messages use {name}; Meta uses {{name}}. Confusing the two is easy and silent."""
    assert wati.template_variables("Hi {{name}}, order {{so}} is {{status}}.") == ["name", "so", "status"]
    assert wati.template_variables("Hi {name}") == []


# ---------------- over the wire ----------------
@pytest.mark.asyncio
async def test_creating_a_template_posts_the_documented_shape(live_wati):
    async with await client() as c:
        with respx.mock() as mock:
            route = mock.post(CREATE).mock(return_value=Response(200, json={
                "ok": True, "result": {"newStatus": "PENDING", "waTemplateId": None, "feedback": ""}}))
            r = await c.post("/admin/api/wa-templates", headers=H, json={
                "name": "order_ready", "language": "en", "body": "Hi {{name}}, order {{so}} is ready.",
                "category": "UTILITY", "samples": {"name": "Rajesh", "so": "45231"}})
    assert r.json()["ok"] is True
    assert "Meta" in r.json()["detail"]  # the owner is told approval is not ours to give
    sent = json.loads(route.calls.last.request.content)
    assert sent["elementName"] == "order_ready" and sent["body"].startswith("Hi {{name}}")


@pytest.mark.asyncio
async def test_listing_shows_the_approval_state(live_wati):
    async with await client() as c:
        with respx.mock() as mock:
            mock.get(url__startswith=LIST).mock(return_value=Response(200, json={"result": [
                {"id": "1", "name": "order_ready", "status": "APPROVED", "quality": "GREEN",
                 "category": "UTILITY", "language_option": {"key": "en_US"}, "body": "Hi {{name}}"},
                {"id": "2", "name": "promo", "status": "REJECTED", "category": "MARKETING",
                 "body": "Buy now", "feedback": "Too promotional"}]}))
            r = (await c.get("/admin/api/wa-templates", headers=H)).json()
    assert r["ok"] is True
    approved, rejected = r["templates"]
    assert approved["status"] == "APPROVED" and approved["quality"] == "GREEN"
    assert approved["language"] == "en_US"
    assert rejected["status"] == "REJECTED" and rejected["feedback"] == "Too promotional"


@pytest.mark.asyncio
async def test_a_wati_failure_is_reported_not_raised(live_wati):
    """The page must still render and say why, rather than showing a 500."""
    async with await client() as c:
        with respx.mock() as mock:
            mock.get(url__startswith=LIST).mock(return_value=Response(403, text="Forbidden"))
            r = (await c.get("/admin/api/wa-templates", headers=H)).json()
    assert r["ok"] is False and r["templates"] == []
    assert "403" in r["detail"]


@pytest.mark.asyncio
async def test_deleting_without_a_waba_id_explains_what_is_missing(live_wati, monkeypatch):
    monkeypatch.setattr(get_settings(), "wati_waba_id", "")
    async with await client() as c:
        r = await c.delete("/admin/api/wa-templates/order_ready", headers=H)
    assert r.status_code == 400
    assert "WABA id" in r.json()["detail"]


@pytest.mark.asyncio
async def test_deleting_uses_the_documented_path(live_wati, monkeypatch):
    monkeypatch.setattr(get_settings(), "wati_waba_id", "waba-99")
    async with await client() as c:
        with respx.mock() as mock:
            route = mock.delete(f"{CREATE}/waba-99/order_ready").mock(return_value=Response(200, json={"ok": True}))
            r = await c.delete("/admin/api/wa-templates/order_ready", headers=H)
    assert r.status_code == 200 and route.call_count == 1


# ---------------- what we refuse before troubling Meta ----------------
@pytest.mark.asyncio
@pytest.mark.parametrize("body,expect", [
    ({"name": "Order Ready", "body": "hi"}, "not a usable template name"),
    ({"name": "ok_name", "body": ""}, "needs some text"),
    ({"name": "ok_name", "body": "hi", "category": "SOMETHING"}, "Category must be"),
    ({"name": "ok_name", "body": "Hi {name}"}, "double braces"),
    ({"name": "ok_name", "body": "hi", "buttons": [{"type": "quick_reply", "text": t} for t in "abcd"]},
     "at most 3"),
    ({"name": "ok_name", "body": "hi", "buttons": [{"type": "url", "text": "Go"}]}, "no address"),
    ({"name": "ok_name", "body": "hi", "buttons": [{"type": "call", "text": "Ring"}]}, "no phone number"),
    ({"name": "ok_name", "body": "hi", "header": {"type": "IMAGE"}}, "needs a public link"),
    ({"name": "ok_name", "body": "Hi {{name}}"}, "Give an example value"),
    ({"name": "ok_name", "body": "hi", "language": "klingon"}, "not a language code"),
])
async def test_obvious_mistakes_are_caught_before_sending(live_wati, body, expect):
    async with await client() as c:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(CREATE).mock(return_value=Response(200, json={"ok": True}))
            r = (await c.post("/admin/api/wa-templates", headers=H,
                              json={"language": "en", **body})).json()
    assert r["ok"] is False and expect in r["detail"]
    assert route.call_count == 0, "a mistake we can see must not be sent to Meta"


@pytest.mark.asyncio
async def test_simulation_mode_says_so_instead_of_pretending():
    async with await client() as c:
        r = (await c.get("/admin/api/wa-templates", headers=H)).json()
    assert r["ok"] is False and "simulation mode" in r["detail"]


@pytest.mark.asyncio
async def test_preview_lists_the_values_a_send_will_need():
    async with await client() as c:
        r = (await c.post("/admin/api/wa-templates/preview", headers=H,
                          json={"name": "x", "body": "Hi {{name}}, your order {{so}} is ready.",
                                "samples": {"name": "Rajesh", "so": "45231"}})).json()
    assert r["variables"] == ["name", "so"]
    # the owner sees the message as Meta will read it, not as a row of placeholders
    assert r["preview"] == "Hi Rajesh, your order 45231 is ready."


@pytest.mark.asyncio
async def test_the_admin_key_is_required():
    async with await client() as c:
        assert (await c.get("/admin/api/wa-templates")).status_code == 401
