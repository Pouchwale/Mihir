"""What actually goes over the wire once a real WATI token is in place.

test_wati_http.py checks the payload shapes in isolation; this drives whole conversations through the
processor with WATI mocked at the HTTP layer, which is the path that goes live on day one.
"""
from __future__ import annotations

import json
import uuid

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app import config
from app.config import get_settings
from app.db import session_scope
from app.jobs import queue_worker
from app.main import app
from app.models import MessageLog
from app.services import rate_limit
from app.services.wati import WatiError, WatiRejected, wati
from tests.flow import MEHTA, SHREE, run_full_conversation

BASE = "https://live-mt-server.wati.io/tenant123"
TEXT = f"{BASE}/api/v1/sendSessionMessage"
BUTTONS = f"{BASE}/api/v1/sendInteractiveButtonsMessage"
LIST = f"{BASE}/api/v1/sendInteractiveListMessage"
HOOK = "/webhook/wati?token=test-hook"


@pytest.fixture
def live_wati():
    """Put the shared client into live mode, and put everything back afterwards."""
    saved = config.overrides()
    config.apply_overrides({**saved, "wati_base_url": BASE, "wati_token": "tok123", "wati_dry_run": False, "wati_api_version": "v1"})
    assert get_settings().wati_mocked is False
    yield
    config.apply_overrides(saved)


@pytest.mark.asyncio
async def test_a_whole_conversation_hits_the_documented_endpoints(clean_sessions, live_wati):
    with respx.mock() as mock:
        text = mock.post(url__startswith=TEXT).mock(return_value=Response(200, json={"ok": True}))
        buttons = mock.post(BUTTONS).mock(return_value=Response(200, json={"ok": True}))
        await run_full_conversation(MEHTA, "lang_en", "tap")

    # greeting is plain text; every other step carries buttons
    assert text.call_count == 1
    assert buttons.call_count == 5
    for call in list(text.calls) + list(buttons.calls):
        req = call.request
        assert req.headers["Authorization"] == "Bearer tok123"
        assert req.headers["Content-Type"] == "application/json"
        assert "CONN" not in req.content.decode(), "internal status leaked into a WATI request"
    for call in buttons.calls:
        req = call.request
        assert req.url.params["whatsappNumber"] == MEHTA
        body = json.loads(req.content)
        assert 1 <= len(body["buttons"]) <= 3
        assert all(len(b["text"]) <= 20 for b in body["buttons"])
    assert TEXT in str(text.calls.last.request.url)


@pytest.mark.asyncio
async def test_list_style_uses_the_list_endpoint(clean_sessions, live_wati, monkeypatch):
    monkeypatch.setattr(get_settings(), "so_menu_style", "list")
    with respx.mock() as mock:
        mock.post(url__startswith=TEXT).mock(return_value=Response(200, json={"ok": True}))
        mock.post(BUTTONS).mock(return_value=Response(200, json={"ok": True}))
        lst = mock.post(LIST).mock(return_value=Response(200, json={"ok": True}))
        await run_full_conversation(MEHTA, "lang_en", "tap")
    assert lst.call_count == 2  # the order list and the item list
    body = json.loads(lst.calls[0].request.content)
    assert len(body["sections"][0]["rows"]) <= 10
    assert all(len(r["title"]) <= 24 for r in body["sections"][0]["rows"])


@pytest.mark.asyncio
async def test_the_trilingual_apology_is_one_message(clean_sessions, live_wati):
    from tests.flow import UNKNOWN, send

    with respx.mock() as mock:
        text = mock.post(url__startswith=TEXT).mock(return_value=Response(200, json={"ok": True}))
        await send(UNKNOWN, "hi")
    assert text.call_count == 1
    sent = text.calls.last.request.url.params["messageText"]
    assert "could not verify" in sent and "क्षमा" in sent and "માફ" in sent


@pytest.mark.asyncio
async def test_when_whatsapp_refuses_buttons_the_customer_still_gets_the_options(clean_sessions, live_wati):
    """Some WATI plans reject interactive messages. The customer must still be able to answer."""
    with respx.mock() as mock:
        mock.post(BUTTONS).mock(return_value=Response(400, json={"ok": False, "result": "not allowed"}))
        text = mock.post(url__startswith=TEXT).mock(return_value=Response(200, json={"ok": True}))
        from tests.flow import send, tap

        await send(MEHTA, "hi")
        await tap(MEHTA, "English")
    sent = text.calls.last.request.url.params["messageText"]
    assert "Order status" in sent and "•" in sent
    assert "1." not in sent  # never numbered: "1" means English at the language question


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_calls,error", [
    (500, 3, WatiError), (502, 3, WatiError), (429, 3, WatiError),
    (400, 1, WatiRejected), (401, 1, WatiRejected), (404, 1, WatiRejected),
])
async def test_retries_only_when_retrying_could_help(live_wati, status, expected_calls, error):
    with respx.mock() as mock:
        route = mock.post(url__startswith=TEXT).mock(return_value=Response(status, text="nope"))
        with pytest.raises(error):
            await wati.send_text("919", "hi")
    assert route.call_count == expected_calls


@pytest.mark.asyncio
async def test_ok_false_on_a_200_is_a_refusal_not_a_retry(live_wati):
    with respx.mock() as mock:
        route = mock.post(url__startswith=TEXT).mock(return_value=Response(200, json={"ok": False, "result": "outside 24h window"}))
        with pytest.raises(WatiRejected, match="24h"):
            await wati.send_text("919", "hi")
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_a_failed_send_is_recorded_so_it_is_visible(live_wati):
    before = len(wati.outbox)
    with respx.mock() as mock:
        mock.post(url__startswith=TEXT).mock(return_value=Response(400, text="refused"))
        with pytest.raises(WatiRejected):
            await wati.send_text("919", "hi")
    added = list(wati.outbox)[before:]
    assert added and added[-1]["sent"] is False and "400" in added[-1]["error"]


@pytest.mark.asyncio
async def test_media_download(live_wati):
    with respx.mock() as mock:
        route = mock.get(f"{BASE}/api/v1/getMedia").mock(return_value=Response(200, content=b"OggS.."))
        assert await wati.get_media("voice/1.ogg") == b"OggS.."
    assert route.calls.last.request.url.params["fileName"] == "voice/1.ogg"
    with respx.mock() as mock:
        mock.get(f"{BASE}/api/v1/getMedia").mock(return_value=Response(404))
        with pytest.raises(WatiError):
            await wati.get_media("gone.ogg")


@pytest.mark.asyncio
async def test_a_send_failure_never_breaks_the_conversation(clean_sessions, live_wati):
    from tests.flow import send, step_of

    with respx.mock(assert_all_called=False) as mock:
        # the very first message is plain text, so it never reaches the buttons endpoint
        mock.post(url__startswith=TEXT).mock(return_value=Response(500, text="down"))
        mock.post(BUTTONS).mock(return_value=Response(500, text="down"))
        r = await send(MEHTA, "hi")
    assert r.outcome == "service_down"
    # WATI comes back and the customer carries on. The step had already advanced to LANG before the
    # send failed, so this time they get the language question (buttons only, no second greeting).
    with respx.mock() as mock:
        buttons = mock.post(BUTTONS).mock(return_value=Response(200, json={"ok": True}))
        r = await send(MEHTA, "hi")
    assert r.outcome == "ask_language" and await step_of(MEHTA) == "LANG"
    assert buttons.call_count == 1
    # and the lookup completes normally from there
    with respx.mock() as mock:
        mock.post(BUTTONS).mock(return_value=Response(200, json={"ok": True}))
        from tests.flow import tap

        await tap(MEHTA, "English")
        r = await send(MEHTA, "SO 45240")
    assert r.outcome == "ask_fg"


# ---------------- webhook -> worker -> WATI ----------------
@pytest.mark.asyncio
async def test_the_real_path_from_wati_and_back(clean_sessions, live_wati):
    """Exactly what happens in production: WATI posts, the worker picks it up, the reply goes out."""
    rate_limit.reset_webhook_limit()
    mid = f"wamid-{uuid.uuid4().hex}"
    payload = {"id": mid, "waId": SHREE, "type": "text", "text": "hi", "eventType": "message", "owner": False}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post(HOOK, json=payload)).json()["status"] == "queued"
        assert (await c.post(HOOK, json=payload)).json()["status"] == "duplicate"

    with respx.mock() as mock:
        text = mock.post(url__startswith=TEXT).mock(return_value=Response(200, json={"ok": True}))
        buttons = mock.post(BUTTONS).mock(return_value=Response(200, json={"ok": True}))
        item = await queue_worker._claim()
        assert item is not None
        await queue_worker._process(item)

    assert text.call_count == 1 and buttons.call_count == 1  # greeting + language question
    async with session_scope() as db:
        row = await db.get(MessageLog, item.message_log_id)
    assert row.outcome == "ask_language" and row.step_after == "LANG"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect_text", [
    ({"type": "text", "text": "SO 45231"}, "SO 45231"),
    ({"type": "interactive", "listReply": {"title": "SO 45231", "description": "1 item"}}, "SO 45231"),
    ({"type": "button", "buttonReply": {"text": "Order status"}}, "Order status"),
    ({"type": "interactive", "interactive": {"button_reply": {"id": "b1", "title": "Done"}}}, "Done"),
])
async def test_every_shape_wati_sends_is_understood(clean_sessions, payload, expect_text):
    from app.services.processor import extract_message

    phone, _, text, _ = extract_message({"waId": SHREE, **payload})
    assert phone == SHREE and text == expect_text


@pytest.mark.asyncio
@pytest.mark.parametrize("event", sorted({"sentMessageDELIVERED", "sentMessageREAD", "templateMessageSent"}))
async def test_status_callbacks_are_ignored(clean_sessions, event):
    rate_limit.reset_webhook_limit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(HOOK, json={"id": uuid.uuid4().hex, "waId": SHREE, "type": "text", "text": "hi", "eventType": event})
        assert r.json()["status"] == "ignored"
        r = await c.post(HOOK, json={"id": uuid.uuid4().hex, "waId": SHREE, "type": "text", "text": "hi", "owner": True})
        assert r.json()["status"] == "ignored"


# ---------------- authentication, exactly as WATI documents it ----------------
@pytest.mark.asyncio
async def test_the_authorization_header_matches_watis_spec(live_wati):
    """WATI: `Authorization: Bearer <token>` against https://live-mt-server.wati.io/<tenantId>/api/v1/..."""
    with respx.mock() as mock:
        route = mock.post(url__startswith=TEXT).mock(return_value=Response(200, json={"ok": True}))
        await wati.send_text("919", "hi")
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok123"
    assert str(req.url).startswith(f"{BASE}/api/v1/sendSessionMessage/919")
    assert req.url.params["messageText"] == "hi"  # query parameter, not a body field


@pytest.mark.asyncio
async def test_a_token_already_containing_bearer_is_not_doubled(live_wati):
    from app import config

    saved = config.overrides()
    config.apply_overrides({**saved, "wati_token": "Bearer eyJabc"})
    try:
        with respx.mock() as mock:
            route = mock.post(url__startswith=TEXT).mock(return_value=Response(200, json={"ok": True}))
            await wati.send_text("919", "hi")
        assert route.calls.last.request.headers["Authorization"] == "Bearer eyJabc"
    finally:
        config.apply_overrides(saved)


@pytest.mark.asyncio
async def test_a_401_explains_how_to_get_a_working_token(live_wati):
    """The exact failure the owner is seeing. The message must name the fix, not just the code."""
    with respx.mock() as mock:
        mock.post(url__startswith=TEXT).mock(return_value=Response(401, text="Unauthorized"))
        with pytest.raises(WatiRejected) as e:
            await wati.send_text("919", "hi")
    msg = str(e.value)
    assert "401" in msg
    assert "Create API Token" in msg and "expired" in msg  # regenerate, per WATI's own docs


# ---------------- webhook registration over the API (no WATI dashboard needed) ----------------
@pytest.mark.asyncio
async def test_registering_the_webhook_uses_watis_documented_shape(live_wati):
    hook = f"{BASE}/api/v2/webhookEndpoints"
    with respx.mock() as mock:
        listing = mock.get(hook).mock(return_value=Response(200, json={
            "ok": True, "result": [{"id": "1", "channelPhoneNumber": "919999999999", "url": "https://old.example/x", "status": 1}]}))
        create = mock.post(hook).mock(return_value=Response(200, json={"ok": True, "result": []}))
        await wati.register_webhook("https://bot.example.com/webhook/wati?token=abc")
    assert listing.call_count == 1  # the channel number was reused
    body = json.loads(create.calls.last.request.content)
    assert isinstance(body, list) and len(body) == 1
    assert body[0] == {
        "phoneNumber": "919999999999", "status": 1,
        "url": "https://bot.example.com/webhook/wati?token=abc", "eventTypes": ["message"],
    }


@pytest.mark.asyncio
async def test_registering_says_what_is_missing_when_the_number_is_unknown(live_wati):
    hook = f"{BASE}/api/v2/webhookEndpoints"
    with respx.mock() as mock:
        mock.get(hook).mock(return_value=Response(200, json={"ok": True, "result": []}))
        with pytest.raises(WatiRejected, match="WhatsApp business number"):
            await wati.register_webhook("https://bot.example.com/webhook/wati?token=abc")


@pytest.mark.asyncio
async def test_the_readiness_page_fails_when_no_webhook_is_registered(live_wati):
    """Without this the bot can send but never hears a customer - and nothing else would show it."""
    from app.services import preflight

    hook = f"{BASE}/api/v2/webhookEndpoints"
    base = "https://bot.example.com/"
    expected = preflight.hook_url_for(get_settings(), base)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/v1/getContacts").mock(return_value=Response(200, json={"result": "success"}))
        mock.get(hook).mock(return_value=Response(200, json={"ok": True, "result": []}))
        r = await preflight.run_checks(deep=True, base_url=base)
    check = next(c for c in r["checks"] if c["key"] == "webhook_registered")
    assert check["status"] == "fail" and expected in check["fix"]

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/v1/getContacts").mock(return_value=Response(200, json={"result": "success"}))
        mock.get(hook).mock(return_value=Response(200, json={"ok": True, "result": [{"url": expected, "status": 1}]}))
        r = await preflight.run_checks(deep=True, base_url=base)
    check = next(c for c in r["checks"] if c["key"] == "webhook_registered")
    assert check["status"] == "pass"


# ---------------- a scope-limited token must not look like a broken one ----------------
CONTACTS = f"{BASE}/api/v1/getContacts"
HOOKS = f"{BASE}/api/v2/webhookEndpoints"


@pytest.mark.asyncio
async def test_a_working_token_is_reported_as_connected(live_wati):
    with respx.mock() as mock:
        mock.get(CONTACTS).mock(return_value=Response(200, json={"result": "success"}))
        r = await wati.check()
    assert r["connected"] is True and not r["scope_warning"]


@pytest.mark.asyncio
async def test_a_send_only_token_is_connected_with_a_scope_note(live_wati):
    """WATI's newer tokens are scope-limited. A token that cannot read contacts can still send every
    message this bot needs, so it must not be reported as rejected."""
    with respx.mock() as mock:
        mock.get(CONTACTS).mock(return_value=Response(401, text="Unauthorized"))
        mock.get(HOOKS).mock(return_value=Response(200, json={"ok": True, "result": []}))
        r = await wati.check()
    assert r["connected"] is True
    assert "contacts" in r["scope_warning"] and "unaffected" in r["scope_warning"]


@pytest.mark.asyncio
async def test_a_genuinely_bad_token_is_still_rejected(live_wati):
    with respx.mock() as mock:
        mock.get(CONTACTS).mock(return_value=Response(401, text="Unauthorized"))
        mock.get(HOOKS).mock(return_value=Response(401, text="Unauthorized"))
        r = await wati.check()
    assert r["connected"] is False
    assert "Create API Token" in r["detail"]  # says how to get a working one


@pytest.mark.asyncio
async def test_a_wrong_tenant_url_still_says_so(live_wati):
    with respx.mock() as mock:
        mock.get(CONTACTS).mock(return_value=Response(404, text="not found"))
        r = await wati.check()
    assert r["connected"] is False and "tenant id" in r["detail"]


@pytest.mark.asyncio
async def test_the_readiness_page_warns_rather_than_blocks_on_a_missing_scope(live_wati):
    from app.services import preflight

    with respx.mock(assert_all_called=False) as mock:
        mock.get(CONTACTS).mock(return_value=Response(401, text="Unauthorized"))
        mock.get(HOOKS).mock(return_value=Response(200, json={"ok": True, "result": []}))
        r = await preflight.run_checks(deep=True, base_url="https://bot.example.com/")
    check = next(c for c in r["checks"] if c["key"] == "wati_connection")
    assert check["status"] == "warn" and "contacts" in check["fix"]


# ---------------- WATI documents no way to LIST webhooks ----------------
@pytest.mark.asyncio
async def test_listing_webhooks_degrades_when_wati_does_not_offer_it(live_wati):
    """Only POST /api/v2/webhookEndpoints is documented. A 404 on the GET means "cannot check",
    not "broken" - reporting it as a failure would send the owner chasing nothing."""
    hook = f"{BASE}/api/v2/webhookEndpoints"
    with respx.mock() as mock:
        mock.get(hook).mock(return_value=Response(404, text="not found"))
        assert await wati.list_webhooks() is None
    with respx.mock() as mock:
        mock.get(hook).mock(return_value=Response(405, text="method not allowed"))
        assert await wati.list_webhooks() is None


@pytest.mark.asyncio
async def test_the_readiness_page_warns_when_it_cannot_check_the_webhook(live_wati):
    from app.services import preflight

    hook = f"{BASE}/api/v2/webhookEndpoints"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/api/v1/getContacts").mock(return_value=Response(200, json={"result": "success"}))
        mock.get(hook).mock(return_value=Response(404, text="not found"))
        r = await preflight.run_checks(deep=True, base_url="https://bot.example.com/")
    check = next(c for c in r["checks"] if c["key"] == "webhook_registered")
    assert check["status"] == "warn" and "Register webhook" in check["fix"]


@pytest.mark.asyncio
async def test_registering_asks_for_the_number_when_wati_cannot_be_queried(live_wati):
    hook = f"{BASE}/api/v2/webhookEndpoints"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(hook).mock(return_value=Response(404, text="not found"))
        with pytest.raises(WatiRejected, match="WhatsApp business number"):
            await wati.register_webhook("https://bot.example.com/webhook/wati?token=abc")
    # given the number explicitly, it registers without needing the listing at all
    with respx.mock() as mock:
        create = mock.post(hook).mock(return_value=Response(200, json={"ok": True, "result": []}))
        await wati.register_webhook("https://bot.example.com/webhook/wati?token=abc", "919999999999")
    assert json.loads(create.calls.last.request.content)[0]["phoneNumber"] == "919999999999"


@pytest.mark.asyncio
async def test_a_401_that_cannot_be_confirmed_is_reported_honestly(live_wati):
    """If the second probe does not exist we cannot tell a bad token from a missing scope - say so
    instead of declaring a possibly-working token dead."""
    with respx.mock() as mock:
        mock.get(f"{BASE}/api/v1/getContacts").mock(return_value=Response(401, text="Unauthorized"))
        mock.get(f"{BASE}/api/v2/webhookEndpoints").mock(return_value=Response(404, text="not found"))
        r = await wati.check()
    assert r["connected"] is False
    assert "Either the token is invalid" in r["detail"] and "scope" in r["detail"]
