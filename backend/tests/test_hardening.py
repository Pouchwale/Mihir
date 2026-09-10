"""Production safety rails: the start-up guard, the public webhook, voice notes, the queue, migrations.

These cover the things that only bite once real credentials are in place, so they are the tests that
matter most on the day the bot goes live.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.config import Settings, get_settings
from app.db import session_scope
from app.jobs import queue_worker
from app.main import app
from app.models import InboundQueue
from app.services import alerts, preflight, rate_limit, stt
from tests.flow import SHREE, open_menu, send, titles

H = {"X-Admin-Key": "test-admin"}
HOOK = "/webhook/wati?token=test-hook"


def _prod(**kw) -> Settings:
    """A settings object as it would be in production, safe by default."""
    base = dict(
        app_mode="prod", admin_key="a-long-enough-admin-key", wati_webhook_token="x" * 32,
        wati_token="tok", wati_base_url="https://live-mt-server.wati.io/123456",
        support_contact="+91-2222233333",
    )
    base.update(kw)
    return get_settings().model_copy(update=base)


# ---------------- start-up guard ----------------
def test_production_refuses_the_example_values():
    assert preflight.fatal_problems(_prod()) == []
    assert any("ADMIN_KEY" in p for p in preflight.fatal_problems(_prod(admin_key="change-me-admin-key")))
    assert any("ADMIN_KEY" in p for p in preflight.fatal_problems(_prod(admin_key="short")))
    assert any("WATI_WEBHOOK_TOKEN" in p for p in preflight.fatal_problems(_prod(wati_webhook_token="change-me-webhook-token")))
    assert any("WATI_WEBHOOK_TOKEN" in p for p in preflight.fatal_problems(_prod(wati_webhook_token="tooshort")))
    assert any("SUPPORT_CONTACT" in p for p in preflight.fatal_problems(_prod(support_contact="[phone/email]")))
    assert any("SUPPORT_CONTACT" in p for p in preflight.fatal_problems(_prod(support_contact="+91-XXXXXXXXXX")))


@pytest.mark.parametrize("url", [
    "https://live-mt-server.wati.io/<tenantId>",
    "https://live-mt-server.wati.io/tenant",
    "https://live-mt-server.wati.io/",
    "not-a-url",
])
def test_production_refuses_a_placeholder_tenant_url(url):
    """A token with a placeholder URL is the worst case: live mode, 404 on every send, silence."""
    problems = preflight.fatal_problems(_prod(wati_base_url=url))
    assert any("WATI_BASE_URL" in p for p in problems), (url, problems)
    # ...and until it is fixed the client stays mocked rather than firing into the void
    assert _prod(wati_base_url=url).wati_mocked is True


def test_a_real_tenant_url_goes_live():
    s = _prod(wati_base_url="https://live-mt-server.wati.io/123456")
    assert s.wati_base_url_ok and s.wati_mocked is False and s.wati_config_problem == ""


def test_dev_mode_allows_every_default():
    assert preflight.fatal_problems(get_settings().model_copy(update={"app_mode": "dev", "admin_key": "x"})) == []


@pytest.mark.asyncio
async def test_the_server_refuses_to_boot_with_an_unsafe_production_config(monkeypatch):
    """An open dashboard or an open webhook is worse than being down, so this must fail fast -
    and before the scheduler or the message worker have started anything."""
    from app import main
    from app.jobs import queue_worker as qw, scheduler

    started: list[str] = []
    monkeypatch.setattr(scheduler, "start", lambda: started.append("scheduler"))
    monkeypatch.setattr(qw, "run_forever", lambda: started.append("worker"))
    monkeypatch.setattr(get_settings(), "app_mode", "prod")
    monkeypatch.setattr(get_settings(), "admin_key", "change-me-admin-key")

    with pytest.raises(RuntimeError) as e:
        async with main.lifespan(main.app):
            pass
    assert "ADMIN_KEY" in str(e.value) and "backend/.env" in str(e.value)
    assert started == [], "the server started work before the config was checked"

    # the escape hatch exists so nobody can be locked out of their own server
    monkeypatch.setattr(get_settings(), "allow_insecure_prod", True)
    assert preflight.fatal_problems() != []  # still reported, just not fatal


@pytest.mark.asyncio
async def test_readiness_endpoint_lists_the_checks():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/api/readiness")).status_code == 401
        r = (await c.get("/admin/api/readiness?deep=false", headers=H)).json()
    keys = {c["key"] for c in r["checks"]}
    assert {"admin_key", "webhook_token", "wati_token", "wati_base_url", "customers_imported", "scheduler"} <= keys
    assert set(r["counts"]) == {"pass", "warn", "fail"}
    assert all(c["status"] in ("pass", "warn", "fail") for c in r["checks"])
    # every problem must tell the owner what to do about it
    assert all(c["fix"] for c in r["checks"] if c["status"] != "pass")
    assert r["ready"] is (r["counts"]["fail"] == 0)


# ---------------- "my new token does nothing" ----------------
def test_editing_the_env_file_while_running_is_detected(monkeypatch, tmp_path):
    """The commonest go-live confusion: .env is read once at start-up, and uvicorn --reload only
    watches .py files, so a new WATI token silently has no effect until a restart."""

    from app import config

    env = tmp_path / ".env"
    env.write_text("APP_MODE=dev\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    mtime = env.stat().st_mtime

    # started after the last edit -> the bot has the current file
    monkeypatch.setattr(config, "PROCESS_STARTED", mtime + 1)
    assert config.env_changed_since_start() is False
    # started before it -> the file on disk is newer than what the bot loaded
    monkeypatch.setattr(config, "PROCESS_STARTED", mtime - 1)
    assert config.env_changed_since_start() is True
    # and a missing file must never crash the check
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "gone.env")
    assert config.env_changed_since_start() is False


@pytest.mark.asyncio
async def test_the_readiness_page_says_to_restart_when_env_is_stale(monkeypatch):
    import time

    from app import config

    monkeypatch.setattr(config, "PROCESS_STARTED", 0.0)  # as if started long before any edit
    r = await preflight.run_checks(deep=False, base_url="https://bot.example.com/")
    check = next(c for c in r["checks"] if c["key"] == "env_fresh")
    if config.ENV_FILE.exists():  # only meaningful when there is a .env to compare against
        assert check["status"] == "fail"
        # the fix must name something that still exists - there is no "Apply .env changes" button now
        assert "OLD values" in check["detail"] and "Restart" in check["fix"]


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url,ok", [
    ("https://bot.example.com/", True),
    ("http://127.0.0.1:8000/", False),
    ("http://localhost:8000/", False),
    ("http://192.168.1.20:8000/", False),
    ("http://bot.example.com/", False),  # WATI requires https
])
async def test_the_webhook_address_must_be_reachable_from_the_internet(base_url, ok):
    """A localhost address in the WATI portal can never receive a message."""
    r = await preflight.run_checks(deep=False, base_url=base_url)
    check = next(c for c in r["checks"] if c["key"] == "webhook_url")
    assert (check["status"] == "pass") is ok, (base_url, check)
    if not ok:
        assert "WATI" in check["fix"]


@pytest.mark.asyncio
async def test_apply_env_changes_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/admin/api/settings/reload")).status_code == 401
        r = await c.post("/admin/api/settings/reload", headers=H)
    body = r.json()
    assert body["ok"] is True and "readiness" in body
    # the test environment has no WATI token, so it must still report simulation
    assert body["wati_mocked"] is True


# ---------------- WATI errors in plain English ----------------
@pytest.mark.parametrize("status,body,expect", [
    (401, "", "token"),
    (403, "", "token"),
    (404, "", "tenant id"),
    (429, "", "rate limiting"),
    (503, "", "their end"),
    (None, "Message failed: outside the 24 hour window", "24-hour window"),
    (None, "session expired for this contact", "24-hour window"),
    (None, "interactive messages not supported on your plan", "plain text"),
    (None, "number is not a valid whatsapp number", "not on WhatsApp"),
    (None, "template not approved", "template"),
])
def test_every_wati_failure_says_what_to_do(status, body, expect):
    from app.services.wati import explain

    assert expect in explain(status, body), explain(status, body)


# ---------------- public webhook ----------------
@pytest.mark.asyncio
async def test_an_unusable_body_is_absorbed_not_refused(clean_sessions):
    """WATI disables a webhook that keeps answering non-200, so a payload we cannot use is recorded
    and answered 200. Losing one odd event is far cheaper than losing every future one."""
    from app.models import WebhookLog

    rate_limit.reset_webhook_limit()
    async with session_scope() as db:
        await db.execute(text("DELETE FROM webhook_log"))
    huge = {"id": f"big-{uuid.uuid4().hex}", "waId": SHREE, "type": "text", "text": "x" * 70_000}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post(HOOK, json=huge)).status_code == 200
        assert (await c.post(HOOK, content=b"not json")).status_code == 200
        assert (await c.post(HOOK, json=[1, 2, 3])).status_code == 200

    async with session_scope() as db:
        rows = (await db.execute(select(WebhookLog).order_by(WebhookLog.id))).scalars().all()
        # nothing bogus reached the queue
        assert (await db.scalar(select(InboundQueue.id).where(InboundQueue.phone_e164 == SHREE))) is None
    assert [r.status for r in rows] == [200, 200, 200]
    assert all(r.outcome == "ignored" for r in rows)
    assert "too large" in rows[0].reason and "valid JSON" in rows[1].reason and "JSON object" in rows[2].reason


@pytest.mark.asyncio
async def test_wati_is_never_throttled_but_strangers_are(clean_sessions, monkeypatch):
    """The throttle exists to stop strangers filling the database. Applying it to WATI would make it
    retry, fail, and eventually disable the webhook - so a valid token is never throttled."""
    monkeypatch.setattr(get_settings(), "webhook_max_per_10s", 3)
    rate_limit.reset_webhook_limit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        good = [(await c.post(HOOK, json={"id": uuid.uuid4().hex, "waId": SHREE, "type": "text", "text": "hi"})).status_code
                for _ in range(8)]
        assert good == [200] * 8, good

        rate_limit.reset_webhook_limit()
        bad = [(await c.post("/webhook/wati?token=wrong", json={"waId": SHREE})).status_code for _ in range(6)]
    assert bad[0] == 401 and 429 in bad, bad  # strangers are cut off
    rate_limit.reset_webhook_limit()


@pytest.mark.asyncio
async def test_a_bad_token_is_the_one_thing_still_refused():
    rate_limit.reset_webhook_limit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/webhook/wati?token=nope", json={"waId": "1"})).status_code == 401
        assert (await c.post("/webhook/wati", json={"waId": "1"})).status_code == 401
    rate_limit.reset_webhook_limit()


@pytest.mark.asyncio
async def test_an_internal_fault_asks_for_a_retry_then_protects_the_webhook(clean_sessions, monkeypatch):
    """A database blip must not cost us the registration: a few 503s (WATI re-sends, nothing is lost),
    then 200 with a loud alert (one message lost, webhook kept)."""
    from app.routers import webhook as wh

    rate_limit.reset_webhook_limit()
    wh._reset_failures()

    async def _boom(db, payload):
        raise RuntimeError("database is down")

    monkeypatch.setattr(wh, "enqueue", _boom)
    codes = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(wh.WEBHOOK_RETRY_BUDGET + 2):
            codes.append((await c.post(HOOK, json={"id": uuid.uuid4().hex, "waId": SHREE, "text": "hi"})).status_code)
    assert codes[: wh.WEBHOOK_RETRY_BUDGET] == [503] * wh.WEBHOOK_RETRY_BUDGET, codes
    assert codes[wh.WEBHOOK_RETRY_BUDGET:] == [200, 200], codes
    assert wh.failure_count() > wh.WEBHOOK_RETRY_BUDGET

    # a single success clears the count, matching WATI's own consecutive counter
    monkeypatch.undo()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post(HOOK, json={"id": uuid.uuid4().hex, "waId": SHREE, "type": "text", "text": "hi"})).status_code == 200
    assert wh.failure_count() == 0


# ---------------- voice notes ----------------
@pytest.mark.asyncio
async def test_voice_notes_off_asks_the_customer_to_type(clean_sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), "voice_notes", False)
    await open_menu(SHREE)
    r = await send(SHREE, audio=True)
    assert r.outcome == "voice_off"
    assert "voice" in r.reply_text.lower() and "SO number" in r.reply_text
    assert titles(r) == ["Order status", "Main menu"]
    # the conversation is untouched: typing still works straight afterwards
    r = await send(SHREE, "45231")
    assert r.outcome == "status_delivered"


@pytest.mark.asyncio
async def test_voice_notes_never_guess_a_transcript_in_production(monkeypatch):
    """The old behaviour returned a fixture transcript, so every customer's voice note looked up the
    same order. In production that must be an error instead."""
    monkeypatch.setattr(get_settings(), "voice_notes", True)
    monkeypatch.setattr(get_settings(), "groq_api_key", "")
    monkeypatch.setattr(get_settings(), "app_mode", "prod")
    with pytest.raises(stt.SttUnavailable):
        await stt.transcribe(b"x", filename="v.ogg")
    monkeypatch.setattr(get_settings(), "app_mode", "dev")
    assert await stt.transcribe(b"x", filename="v.ogg") == stt.FIXTURE_TRANSCRIPT


def test_stt_unavailable_is_not_retried():
    """A configuration problem must not cost 3 x 60s of retries per voice note."""
    assert not issubclass(stt.SttUnavailable, stt.SttError)


# ---------------- queue ----------------
@pytest.mark.asyncio
async def test_a_slow_message_cannot_block_the_queue(clean_sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), "queue_item_timeout_sec", 1)

    async def _hang(*a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(queue_worker, "process_payload", _hang)
    async with session_scope() as db:
        item = InboundQueue(message_log_id=None, phone_e164=SHREE, payload=json.dumps({"waId": SHREE, "type": "text", "text": "hi"}))
        db.add(item)
        await db.flush()
        item_id = item.id
    async with session_scope() as db:
        claimed = await db.get(InboundQueue, item_id)
        claimed.status = "processing"
        claimed.attempts = 3  # already retried, so a timeout finishes it as failed

    await asyncio.wait_for(queue_worker._process(await _get(item_id)), timeout=10)
    async with session_scope() as db:
        row = await db.get(InboundQueue, item_id)
    assert row.status == "failed" and "timed out" in (row.error or "")


async def _get(item_id: int) -> InboundQueue:
    async with session_scope() as db:
        return await db.get(InboundQueue, item_id)


@pytest.mark.asyncio
async def test_claim_hands_each_message_to_one_worker_only(clean_sessions):
    async with session_scope() as db:
        for _ in range(3):
            db.add(InboundQueue(message_log_id=None, phone_e164=SHREE, payload="{}"))
    claimed = [await queue_worker._claim() for _ in range(4)]
    ids = [c.id for c in claimed if c]
    assert len(ids) == 3 and len(set(ids)) == 3  # each exactly once, then nothing left
    assert claimed[-1] is None
    async with session_scope() as db:
        for i in ids:
            row = await db.get(InboundQueue, i)
            assert row.status == "processing" and row.attempts == 1
            await db.delete(row)


# ---------------- alerts ----------------
@pytest.mark.asyncio
async def test_an_outage_alerts_once_not_once_per_customer():
    alerts.reset_throttle()
    assert await alerts.notify_throttled("k", "first") is True
    assert await alerts.notify_throttled("k", "second") is False
    assert await alerts.notify_throttled("other", "different key") is True
    alerts.reset_throttle()


# ---------------- migration ----------------
@pytest.mark.asyncio
async def test_a_new_column_is_backfilled_on_an_existing_database():
    """ALTER TABLE cannot carry the Python default, so an upgraded DB would keep NULL and behave
    differently from a fresh one."""
    from app.db import _add_missing_columns, get_engine, is_sqlite

    if not is_sqlite():
        pytest.skip("sqlite-only check")
    async with get_engine().begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS backfill_probe"))
        await conn.execute(text("CREATE TABLE backfill_probe (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO backfill_probe (id) VALUES (1)"))

        from sqlalchemy import Boolean, Column, Integer, MetaData, Table

        md = MetaData()
        Table("backfill_probe", md, Column("id", Integer, primary_key=True), Column("flag", Boolean, default=False))
        from app.db import Base

        original = Base.metadata
        try:
            Base.metadata = md  # type: ignore[misc]
            await conn.run_sync(_add_missing_columns)
        finally:
            Base.metadata = original  # type: ignore[misc]
        value = await conn.scalar(text("SELECT flag FROM backfill_probe WHERE id = 1"))
        await conn.execute(text("DROP TABLE backfill_probe"))
    assert value in (0, False), f"existing row kept NULL instead of the default: {value!r}"


# ---------------- token paste mistakes that look like a WATI fault ----------------
@pytest.mark.parametrize("token,broken", [
    ("eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.sig", False),
    ("Bearer eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.sig", False),  # the client strips the prefix itself
    ("", False),  # "no token" is reported by a different check
    (" eyJa.b.c ", True),
    ("eyJa.b\n.c", True),
    ('"eyJa.b.c"', True),
    ("my-api-key-123", True),
])
def test_a_mistyped_token_is_named_before_wati_is_blamed(token, broken):
    from app.services.preflight import token_shape_problem

    problem = token_shape_problem(token)
    assert bool(problem) is broken, (token[:12], problem)
    if broken:
        assert len(problem) > 30 and token not in problem  # explains, and never echoes the secret


# ---------------- telling "WATI's fault" from "our fault" ----------------
@pytest.mark.asyncio
async def test_a_refused_webhook_call_is_recorded_with_the_reason(clean_sessions):
    """A 401 we return is invisible in WATI's log beyond the number. Record it on our side so the
    Diagnostics page can say WHY - the token in WATI's URL did not match ours."""
    from app.models import WebhookLog

    rate_limit.reset_webhook_limit()
    async with session_scope() as db:
        await db.execute(text("DELETE FROM webhook_log"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/webhook/wati?token=wrong-one", json={"waId": SHREE, "text": "hi"})).status_code == 401
        assert (await c.post("/webhook/wati", json={"waId": SHREE, "text": "hi"})).status_code == 401
        assert (await c.post(HOOK, json={"id": uuid.uuid4().hex, "waId": SHREE, "type": "text", "text": "hi"})).json()["status"] == "queued"

    async with session_scope() as db:
        rows = (await db.execute(select(WebhookLog).order_by(WebhookLog.id))).scalars().all()
    assert len(rows) == 3
    assert rows[0].status == 401 and "does not match" in rows[0].reason
    assert "wrong-one" not in (rows[0].reason or "")  # never echo the token back
    assert rows[1].status == 401 and "without a ?token=" in rows[1].reason
    assert rows[2].status == 200 and rows[2].outcome == "queued" and rows[2].phone_e164 == SHREE


@pytest.mark.asyncio
async def test_diagnostics_says_which_side_the_problem_is_on(clean_sessions):
    from app.models import WebhookLog

    rate_limit.reset_webhook_limit()
    async with session_scope() as db:
        await db.execute(text("DELETE FROM webhook_log"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/api/diagnostics")).status_code == 401
        empty = (await c.get("/admin/api/diagnostics", headers=H)).json()
        assert "never called this server" in empty["verdict"]

        await c.post("/webhook/wati?token=nope", json={"waId": SHREE, "text": "hi"})
        refused = (await c.get("/admin/api/diagnostics", headers=H)).json()
        assert "REFUSED" in refused["verdict"] and "does not match" in refused["verdict"]
        assert refused["inbound"][0]["status"] == 401

        await c.post(HOOK, json={"id": uuid.uuid4().hex, "waId": SHREE, "type": "text", "text": "hi"})
        ok = (await c.get("/admin/api/diagnostics", headers=H)).json()
        assert "reaching this server" in ok["verdict"]
        assert {"inbound", "outbound", "failed_queue", "alerts"} <= set(ok)


# ---------------- knowing our own public address ----------------
@pytest.mark.asyncio
async def test_public_base_url_decides_the_webhook_address(monkeypatch):
    """Behind a proxy or tunnel the request says nothing about how WATI reaches us, so the address
    handed to WATI must come from PUBLIC_BASE_URL when it is set."""
    monkeypatch.setattr(get_settings(), "public_base_url", "https://bot.example.com")
    monkeypatch.setattr(get_settings(), "wati_webhook_token", "y" * 32)
    url = preflight.hook_url_for(get_settings(), "http://127.0.0.1:8000/")
    assert url.startswith("https://bot.example.com/webhook/wati?token=")
    r = await preflight.run_checks(deep=False, base_url="http://127.0.0.1:8000/")
    assert next(c for c in r["checks"] if c["key"] == "webhook_url")["status"] == "pass"


@pytest.mark.asyncio
async def test_a_private_address_with_no_public_base_url_is_called_out(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", "")
    r = await preflight.run_checks(deep=False, base_url="http://127.0.0.1:8000/")
    keys = {c["key"]: c for c in r["checks"]}
    assert keys["webhook_url"]["status"] == "fail"
    assert keys["public_base_url"]["status"] == "fail" and "PUBLIC_BASE_URL" in keys["public_base_url"]["fix"]


@pytest.mark.asyncio
async def test_the_self_test_refuses_to_guess(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", "")
    monkeypatch.setattr(get_settings(), "wati_webhook_token", "change-me-webhook-token")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/admin/api/wati/self-test")).status_code == 401
        r = (await c.post("/admin/api/wati/self-test", headers=H)).json()
    assert r["ok"] is False and "PUBLIC_BASE_URL" in r["detail"]


@pytest.mark.asyncio
async def test_the_self_test_probe_never_creates_a_customer_message(clean_sessions):
    """It must prove the path works without inventing a conversation."""
    from app.routers.webhook import IGNORED_EVENTS

    assert "sessionMessageSent" in IGNORED_EVENTS  # the event the probe uses
    rate_limit.reset_webhook_limit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(HOOK, json={"eventType": "sessionMessageSent", "id": "selftest-1", "text": "self test"})
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    async with session_scope() as db:
        assert (await db.scalar(select(InboundQueue.id))) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,ok,expect", [
    (200, True, "works end to end"),
    (401, False, "does not match WATI_WEBHOOK_TOKEN"),
    (502, False, "answered 502"),
])
async def test_the_self_test_reports_what_the_public_address_did(monkeypatch, status, ok, expect):
    """Calling our own public webhook is what settles 'is it WATI or is it us'."""
    import respx
    from httpx import Response

    monkeypatch.setattr(get_settings(), "public_base_url", "https://bot.example.com")
    monkeypatch.setattr(get_settings(), "wati_webhook_token", "z" * 32)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__startswith="https://bot.example.com/webhook/wati").mock(return_value=Response(status, text="x"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = (await c.post("/admin/api/wati/self-test", headers=H)).json()
    assert r["ok"] is ok
    assert expect in r["detail"], r["detail"]
    assert r["url"].startswith("https://bot.example.com/webhook/wati?token=")


@pytest.mark.asyncio
async def test_the_self_test_explains_an_unreachable_address(monkeypatch):
    import respx
    import httpx as _httpx

    monkeypatch.setattr(get_settings(), "public_base_url", "https://bot.example.com")
    monkeypatch.setattr(get_settings(), "wati_webhook_token", "z" * 32)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__startswith="https://bot.example.com/webhook/wati").mock(side_effect=_httpx.ConnectError("no route"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = (await c.post("/admin/api/wati/self-test", headers=H)).json()
    assert r["ok"] is False
    assert "Could not reach" in r["detail"] and "WATI would fail the same way" in r["detail"]


# ---------------- seeing a webhook heading for disablement ----------------
@pytest.mark.asyncio
async def test_delivery_health_warns_before_wati_gives_up(clean_sessions):
    """WATI disables a webhook after enough consecutive failures, and a disabled webhook is silent -
    so the trend has to be visible while it can still be fixed."""
    from app.models import WebhookLog

    async def _seed(statuses):
        async with session_scope() as db:
            await db.execute(text("DELETE FROM webhook_log"))
            for st in statuses:
                db.add(WebhookLog(status=st, outcome="queued" if st == 200 else "rejected",
                                  reason=None if st == 200 else "token mismatch"))

    await _seed([])
    assert (await preflight.delivery_health())["calls"] == 0

    await _seed([200, 200, 200])
    h = await preflight.delivery_health()
    assert h["bad"] == 0 and h["trailing_bad"] is False and h["last_ok_at"]

    await _seed([200, 200, 401])  # newest last -> one recent failure
    h = await preflight.delivery_health()
    assert h["bad"] == 1 and h["trailing_bad"] is False

    await _seed([401, 401, 401])
    h = await preflight.delivery_health()
    assert h["trailing_bad"] is True and h["last_reason"] == "token mismatch"

    r = await preflight.run_checks(deep=False, base_url="https://bot.example.com/")
    check = next(c for c in r["checks"] if c["key"] == "delivery_health")
    assert check["status"] == "fail" and "goes silent" in check["fix"]
    async with session_scope() as db:
        await db.execute(text("DELETE FROM webhook_log"))


# ---------------- nothing private may be committed ----------------
def test_no_tracked_file_leaks_a_secret_or_personal_data():
    """The repository is shared. Passwords, real phone numbers and machine paths must not be in it."""
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[2]
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.split()
    assert files, "not a git checkout - cannot verify"

    # the sample data is generated, so anything else that looks like a real Indian mobile is suspect
    sample_numbers = {c[2].replace(" ", "").replace("-", "").replace("+", "").lstrip("0")
                      for c in __import__("scripts.make_fixtures", fromlist=["CUSTOMERS"]).CUSTOMERS}
    allowed = {n for n in sample_numbers} | {n[2:] for n in sample_numbers if n.startswith("91")}
    offenders: list[str] = []
    this_file = pathlib.Path(__file__).resolve().relative_to(root).as_posix()
    for rel in files:
        path = root / rel
        # this file necessarily contains the patterns it searches for
        if rel == this_file or path.suffix.lower() in {".png", ".jpg", ".ico", ".xlsx", ".lock"} or not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "Admin Key: " in text or re.search(r"^\s*ADMIN_KEY\s*=\s*(?!change-me|test-|\s*$)\S+", text, re.M):
            offenders.append(f"{rel}: looks like a real admin key")
        if "GP3" in text and rel != "backend/tests/test_hardening.py":
            offenders.append(f"{rel}: contains a developer machine path")
        for num in re.findall(r"\b91[6-9]\d{9}\b", text):
            subscriber = num[2:]
            if num in allowed or subscriber in allowed:
                continue
            if len(set(subscriber)) == 1:  # 9999999999 and friends are obvious test doubles
                continue
            offenders.append(f"{rel}: phone number {num[:4]}... is not sample data")
    assert not offenders, "private data in tracked files:\n  " + "\n  ".join(sorted(set(offenders)))


def test_the_committed_sample_data_is_the_generated_one():
    """Editing backend/fixtures for a live test must never be committed - keep those in backend/local/."""
    import openpyxl

    from scripts.make_fixtures import CUSTOMERS, ORDERS

    root = pathlib.Path(__file__).resolve().parents[1]
    ws = openpyxl.load_workbook(root / "fixtures" / "customers_dummy.xlsx", read_only=True).active
    assert len(list(ws.iter_rows(values_only=True))) - 1 == len(CUSTOMERS)
    csv_rows = (root / "fixtures" / "orders_dummy.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(csv_rows) - 1 == len(ORDERS), "orders_dummy.csv disagrees with the other formats"


# ---------------- running on a host with a throwaway filesystem ----------------
@pytest.mark.parametrize("raw,expected,ssl", [
    ("postgres://u:p@dpg-abc/db", "postgresql+asyncpg://u:p@dpg-abc/db", False),
    ("postgresql://u:p@dpg-abc/db", "postgresql+asyncpg://u:p@dpg-abc/db", False),
    ("postgresql://u:p@dpg-a.oregon-postgres.render.com/db?sslmode=require",
     "postgresql+asyncpg://u:p@dpg-a.oregon-postgres.render.com/db", True),
    ("mysql://u:p@h/db", "mysql+aiomysql://u:p@h/db", False),
    ("sqlite:///./x.db", "sqlite+aiosqlite:///./x.db", False),
    ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db", False),
])
def test_a_hosted_database_url_is_accepted_as_given(raw, expected, ssl):
    """Managed Postgres hands out `postgres://` with `sslmode=`, which this async app cannot use
    verbatim - asyncpg rejects sslmode outright. Pasting the URL from the host must just work."""
    s = Settings(database_url=raw)
    assert s.database_url == expected
    assert s.db_needs_ssl is ssl


@pytest.mark.parametrize("url,dialect_name,expected_sql", [
    ("sqlite+aiosqlite:///x.db", "sqlite", "date("),
    ("mysql+aiomysql://u:p@h/d", "mysql", "date("),
    ("postgresql+asyncpg://u:p@h/d", "postgresql", "CAST("),
])
def test_grouping_by_day_works_on_every_database(url, dialect_name, expected_sql):
    """PostgreSQL has no date() function; SQLite mangles a CAST to DATE. One expression cannot serve
    both, so the dashboard's daily chart needs a dialect-aware one."""
    import importlib

    from sqlalchemy import select

    from app import config as cfg
    from app.db import day_of
    from app.models import MessageLog

    dialect = importlib.import_module(f"sqlalchemy.dialects.{dialect_name}").dialect()
    saved = cfg.overrides()
    cfg.apply_overrides({**saved, "database_url": url})
    try:
        sql = str(select(day_of(MessageLog.created_at)).compile(dialect=dialect))
    finally:
        cfg.apply_overrides(saved)
    assert expected_sql in sql, sql


@pytest.mark.asyncio
async def test_sqlite_on_an_ephemeral_host_is_a_failure_not_a_note(monkeypatch):
    """On Render the SQLite file and the generated .secret_key are wiped by every deploy, taking the
    customers, the chat log and the edited messages with them. Saying "fine for one server" there
    would be wrong advice."""
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr(get_settings(), "secret_key", "")
    assert preflight.ephemeral_host() == "Render"

    r = await preflight.run_checks(deep=False, base_url="https://bot.example.com/")
    checks = {c["key"]: c for c in r["checks"]}
    assert checks["database"]["status"] == "fail"
    assert "deleted every time you deploy" in checks["database"]["fix"]
    assert checks["secret_key"]["status"] == "fail" and "SECRET_KEY" in checks["secret_key"]["fix"]

    monkeypatch.delenv("RENDER")
    assert preflight.ephemeral_host() == ""
    r = await preflight.run_checks(deep=False, base_url="https://bot.example.com/")
    assert next(c for c in r["checks"] if c["key"] == "database")["status"] == "warn"


# ---------------- the webhook secret is a secret, not a URL ----------------
@pytest.mark.parametrize("token,broken,hint", [
    ("wati_7001f457-abcd-efgh-1234-567890abcdef", False, ""),
    ("x" * 40, False, ""),
    ("https://bot.onrender.com/webhook/wati?token=wati_7001f457", True, "whole webhook address"),
    ("bot.example.com/webhook/wati?token=abc", True, "whole webhook address"),
    ("secret with spaces", True, "space or line break"),
    ("abc=def&ghi", True, "? & or ="),
    ("y" * 200, True, "characters long"),
    # the WATI API token in the wrong box - what actually happened, and the reason the owner went
    # looking in the WATI portal for a value WATI never issues
    ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + "a" * 140 + ".sIg" + "n" * 20, True, "API token"),
])
def test_a_webhook_url_pasted_into_the_token_is_caught(token, broken, hint):
    """Pasting the whole address into WATI_WEBHOOK_TOKEN builds
    `...?token=https://.../webhook/wati?token=xxx`, which can never match what WATI sends - and the
    only symptom is a 401 that looks like WATI's fault."""
    from app.services.preflight import webhook_token_problem

    problem = webhook_token_problem(token)
    assert bool(problem) is broken, (token[:40], problem)
    if broken:
        assert hint in problem


@pytest.mark.asyncio
async def test_a_broken_webhook_secret_is_never_pasted_into_the_address(monkeypatch):
    """If the secret is wrong we must not hand out an address built from it - that would just get
    registered in WATI and fail again."""
    monkeypatch.setattr(get_settings(), "public_base_url", "https://bot.example.com")
    monkeypatch.setattr(get_settings(), "wati_webhook_token",
                        "https://bot.example.com/webhook/wati?token=wati_7001f457")
    url = preflight.hook_url_for(get_settings(), "")
    assert url == "https://bot.example.com/webhook/wati?token=<WATI_WEBHOOK_TOKEN>"

    r = await preflight.run_checks(deep=False, base_url="https://bot.example.com/")
    check = next(c for c in r["checks"] if c["key"] == "webhook_token")
    assert check["status"] == "fail" and "whole webhook address" in check["fix"]


def test_the_suggested_secret_passes_the_check_that_offered_it():
    """Offering a value the very next check rejects would be worse than offering nothing."""
    from app.services.preflight import MIN_WEBHOOK_TOKEN, suggest_webhook_token, webhook_token_problem

    value = suggest_webhook_token()
    assert webhook_token_problem(value) == ""
    assert MIN_WEBHOOK_TOKEN <= len(value) <= 128
    assert value != suggest_webhook_token()  # not derived, not cached


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [
    "change-me-webhook-token",                                   # still the example
    "short",                                                     # too short
    "https://bot.example.com/webhook/wati?token=abc",            # the whole address
    "eyJhbGciOiJIUzI1NiJ9." + "a" * 140 + ".sig",                # the WATI API token
])
async def test_every_webhook_secret_problem_says_who_issues_it(monkeypatch, token):
    """The regression that caused a support detour: naming only the mistake ("170 characters long")
    sent the owner into the WATI portal to look for a value WATI never had. Whatever is wrong, the
    fix must say the secret is theirs to invent, and hand them one."""
    monkeypatch.setattr(get_settings(), "public_base_url", "https://bot.example.com")
    monkeypatch.setattr(get_settings(), "wati_webhook_token", token)
    r = await preflight.run_checks(deep=False, base_url="https://bot.example.com/")
    fix = next(c for c in r["checks"] if c["key"] == "webhook_token")["fix"]
    assert "you invent" in fix and "does not issue it" in fix
    assert "wati_hook_" in fix                       # a value to copy, not just advice
    assert "register the webhook again" in fix       # the secret lives inside the registered URL
    assert token not in fix                          # never echo what they pasted


@pytest.mark.asyncio
async def test_the_fix_points_at_the_hosting_dashboard_not_a_file(monkeypatch):
    """On Render backend/.env is not where a setting is changed, and an edit needs a redeploy."""
    monkeypatch.setattr(get_settings(), "wati_webhook_token", "short")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    r = await preflight.run_checks(deep=False, base_url="https://bot.example.com/")
    assert next(c for c in r["checks"] if c["key"] == "webhook_token")["where"] == "backend/.env"

    monkeypatch.setenv("RENDER", "true")
    r = await preflight.run_checks(deep=False, base_url="https://bot.example.com/")
    where = next(c for c in r["checks"] if c["key"] == "webhook_token")["where"]
    assert "Render" in where and "redeploy" in where


# ---------------- the deployment blueprint must match the code ----------------
def _blueprint() -> dict:
    import yaml

    path = pathlib.Path(__file__).resolve().parents[2] / "render.yaml"
    assert path.exists(), "render.yaml is missing"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_render_blueprint_only_sets_real_settings():
    """A renamed setting should fail here, not silently do nothing on the next deploy."""
    service = _blueprint()["services"][0]
    declared = {e["key"] for e in service["envVars"]}
    known = {name.upper() for name in Settings.model_fields}
    supplied_by_render = {"PYTHON_VERSION"}
    unknown = declared - known - supplied_by_render
    assert not unknown, f"render.yaml sets variables the app does not read: {sorted(unknown)}"


def test_the_blueprint_supplies_everything_production_refuses_to_start_without():
    """preflight.fatal_problems() blocks a prod boot on these, so a blueprint deploy must set them."""
    declared = {e["key"] for e in _blueprint()["services"][0]["envVars"]}
    required = {"APP_MODE", "ADMIN_KEY", "WATI_WEBHOOK_TOKEN", "SUPPORT_CONTACT", "WATI_BASE_URL",
                "DATABASE_URL", "SECRET_KEY", "PUBLIC_BASE_URL"}
    assert required <= declared, f"the blueprint would not boot: missing {sorted(required - declared)}"


def test_the_blueprint_binds_to_the_port_render_provides():
    """Copying the local `--port 8000` deploys cleanly and then times out with no useful error."""
    service = _blueprint()["services"][0]
    assert "$PORT" in service["startCommand"], service["startCommand"]
    assert "--host 0.0.0.0" in service["startCommand"]
    # the database must be wired from the database block, not pasted in as a literal
    db_var = next(e for e in service["envVars"] if e["key"] == "DATABASE_URL")
    assert db_var.get("fromDatabase", {}).get("name") == _blueprint()["databases"][0]["name"]
    # SECRET_KEY must be generated and kept, never a literal that someone might change later
    secret = next(e for e in service["envVars"] if e["key"] == "SECRET_KEY")
    assert secret.get("generateValue") is True and "value" not in secret


# ---------------- say WHICH value is wrong, not "set both" ----------------
@pytest.mark.parametrize("base,token,names", [
    ("", "change-me-webhook-token", "PUBLIC_BASE_URL"),
    ("http://127.0.0.1:8000", "w" * 40, "PUBLIC_BASE_URL"),
    ("https://app.onrender.com", "change-me-webhook-token", "WATI_WEBHOOK_TOKEN"),
    ("https://app.onrender.com", "short", "WATI_WEBHOOK_TOKEN"),
    ("https://app.onrender.com", "https://app.onrender.com/webhook/wati?token=abc", "WATI_WEBHOOK_TOKEN"),
])
def test_the_webhook_address_problem_names_one_value(base, token, names):
    """Telling someone to set both sends them checking a setting that was never wrong."""
    s = get_settings().model_copy(update={"public_base_url": base, "wati_webhook_token": token})
    problem = preflight.hook_url_problem(s, "")
    assert problem.startswith(names), problem
    other = "WATI_WEBHOOK_TOKEN" if names == "PUBLIC_BASE_URL" else "PUBLIC_BASE_URL"
    assert other not in problem, f"blames {other} as well: {problem}"


def test_a_correct_pair_produces_the_address_and_no_complaint():
    s = get_settings().model_copy(update={
        "public_base_url": "https://app.onrender.com",
        "wati_webhook_token": "wati_7001f457-abcd-efgh-ijkl-mnopqrstuvwx"})
    assert preflight.hook_url_problem(s, "") == ""
    assert preflight.hook_url_for(s, "") == (
        "https://app.onrender.com/webhook/wati?token=wati_7001f457-abcd-efgh-ijkl-mnopqrstuvwx")


@pytest.mark.asyncio
async def test_the_self_test_repeats_the_specific_problem(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", "https://app.onrender.com")
    monkeypatch.setattr(get_settings(), "wati_webhook_token",
                        "https://app.onrender.com/webhook/wati?token=wati_7001f457")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = (await c.post("/admin/api/wati/self-test", headers=H)).json()
    assert r["ok"] is False
    assert r["detail"].startswith("WATI_WEBHOOK_TOKEN") and "PUBLIC_BASE_URL" not in r["detail"]


@pytest.mark.asyncio
async def test_a_token_wati_accepts_is_never_called_malformed(monkeypatch):
    """A live account showed "Token accepted by WATI" and "does not look like a WATI API token" side
    by side. Not every account is issued a JWT, so the shape rule is a guess about why a connection
    might fail - and once WATI has accepted the token it is settled. Telling someone to replace a
    working credential is the most expensive kind of wrong."""
    odd_but_working = "wati-" + "z" * 160          # no 'eyJ', no dots - and WATI accepts it
    checks = preflight._whatsapp(
        get_settings().model_copy(update={"wati_token": odd_but_working}),
        {"connected": True, "detail": "Token accepted by WATI."})
    fmt = next(c for c in checks if c.key == "wati_token_format")
    assert fmt.status == "pass" and fmt.fix == ""

    # when WATI refused, the same shape is a useful explanation of why
    checks = preflight._whatsapp(
        get_settings().model_copy(update={"wati_token": odd_but_working}),
        {"connected": False, "detail": "401"})
    assert next(c for c in checks if c.key == "wati_token_format").status == "fail"

    # untested (shallow run): a guess, so a nudge rather than a blocker
    checks = preflight._whatsapp(get_settings().model_copy(update={"wati_token": odd_but_working}), None)
    assert next(c for c in checks if c.key == "wati_token_format").status == "warn"


@pytest.mark.asyncio
async def test_the_same_secret_in_both_boxes_is_named(monkeypatch):
    """Two settings whose names differ by one word, one of which WATI issues and one it does not."""
    same = "eyJhbGciOiJIUzI1NiJ9." + "b" * 120 + ".sig"
    monkeypatch.setattr(get_settings(), "wati_token", same)
    monkeypatch.setattr(get_settings(), "wati_webhook_token", same)
    r = await preflight.run_checks(deep=False, base_url="https://bot.example.com/")
    fix = next(c for c in r["checks"] if c["key"] == "webhook_token")["fix"]
    assert "same value as WATI_TOKEN" in fix and same not in fix
