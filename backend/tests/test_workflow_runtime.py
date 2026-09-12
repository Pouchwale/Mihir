"""Published workflows answering real customers, next to the order-status bot.

Every conversation here goes through the real processor with WATI in dry-run mode, exactly like a
message from WhatsApp - the only difference from production is that sends land in the outbox.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select, update

from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import (AgentHandover, InboundQueue, MessageLog, NewCustomer, Session, Workflow, WorkflowRun,
                        WorkflowVersion, utcnow)
from app.services import menus
from app.services.processor import process_payload
from app.services.wati import WatiClient, WatiRejected, wati
from app.services.workflow import engine, runtime, store
from app.services.workflow.schema import BUILTIN_ORDER_STATUS
from tests.flow import GUJ, MEHTA, MENU_BUTTONS, SHREE, UNKNOWN, open_menu, send, tap, titles

H = {"X-Admin-Key": "test-admin"}
BASE = "https://live-mt-server.wati.io/tenant123"


async def _wipe() -> None:
    async with session_scope() as db:
        for table in (WorkflowRun, WorkflowVersion, Workflow, Session, MessageLog, InboundQueue, AgentHandover,
                      NewCustomer):
            await db.execute(delete(table))
    runtime.registry.invalidate()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean():
    await _wipe()
    yield
    await _wipe()


# ---------------- drawing workflows ----------------
def en(text: str) -> dict:
    return {"en": text, "hi": "", "gu": ""}


def doc_of(nodes: list[dict], edges: list[tuple[str, str, str]], start: str, **settings) -> dict:
    return {"schema": 1, "start": start, "settings": {"multilingual": False, **settings},
            "nodes": [{"x": 0, "y": 0, "title": n["id"], **n} for n in nodes],
            "edges": [{"id": f"e{i}", "from": a, "port": p, "to": b} for i, (a, p, b) in enumerate(edges)]}


def sample_kit(**settings) -> dict:
    return doc_of(
        [{"id": "hello", "type": "message", "text": en("Hello {sys.customer_name}, here is our sample kit.")},
         {"id": "ask", "type": "question", "text": en("Do you want one?"), "store": "want", "max_retries": 1,
          "input": {"kind": "buttons", "options": [{"value": "yes", "label": en("Yes please")},
                                                   {"value": "no", "label": en("No thanks")}]}},
         {"id": "sent", "type": "end", "text": en("We will send it ({want}).")},
         {"id": "bye", "type": "end", "text": en("No problem.")}],
        [("hello", "next", "ask"), ("ask", "opt:yes", "sent"), ("ask", "opt:no", "bye"), ("ask", "retry_exhausted", "bye")],
        "hello", **settings)


def onboarding(**settings) -> dict:
    return doc_of(
        [{"id": "hi", "type": "message", "text": en("Welcome to Pouchwale, {sys.customer_name}!")},
         {"id": "ask", "type": "question", "text": en("What is your company called?"), "store": "company",
          "input": {"kind": "text"}},
         {"id": "end", "type": "end", "text": en("Thank you, {company}. Our team will be in touch.")}],
        [("hi", "next", "ask"), ("ask", "next", "end")], "hi", **settings)


def doer() -> dict:
    """Every step that acts on the conversation in WATI, one after another."""
    return doc_of(
        [{"id": "tag", "type": "tags", "tags": ["vip"]},
         {"id": "status", "type": "chat_status", "status": "pending"},
         {"id": "sub", "type": "subscribe", "subscribe": False},
         {"id": "tpl", "type": "template", "template_name": "order_ready", "params": {"name": "{sys.customer_name}"}},
         {"id": "ok", "type": "message", "text": en("All done. A person from Sales will take it from here.")},
         {"id": "team", "type": "assign", "to": "team", "teams": ["Sales"]},
         {"id": "bad", "type": "end", "text": en("Something went wrong, a person will help you.")}],
        [("tag", "next", "status"), ("status", "next", "sub"), ("sub", "next", "tpl"), ("tpl", "next", "ok"),
         ("tpl", "on_error", "bad"), ("ok", "next", "team"), ("team", "on_error", "bad")],
        "tag", triggers={"keywords": ["do it"]})


async def publish(title: str, doc: dict, *, for_everyone: bool = True) -> str:
    """Publish, then switch it on for everyone - publishing alone reaches only the test numbers."""
    w = await store.create_from_doc(title, "", doc)
    await store.publish(w["key"])
    if for_everyone:
        await store.set_live(w["key"], True)
    return w["key"]


async def run_of(phone: str) -> WorkflowRun | None:
    async with session_scope() as db:
        return await db.get(WorkflowRun, phone)


async def say_as(phone: str, text: str, name: str):
    """A message as WATI delivers it, with the sender's WhatsApp name."""
    payload = {"id": f"t-{uuid.uuid4().hex}", "waId": phone, "type": "text", "text": text, "senderName": name}
    async with session_scope() as db:
        return await process_payload(db, payload)


# ---------------- who answers ----------------
async def test_with_nothing_live_the_order_status_bot_is_untouched():
    await store.create_from_doc("Draft only", "", sample_kit(triggers={"keywords": ["sample"]}))  # never published
    r = await open_menu(SHREE)
    assert r.options["kind"] == "buttons" and titles(r) == MENU_BUTTONS["en"]
    assert (await send(SHREE, "sample")).outcome != "workflow"


async def test_a_keyword_starts_a_workflow_that_runs_to_its_end():
    key = await publish("Sample kit", sample_kit(triggers={"keywords": ["sample"]}))
    r = await send(SHREE, "Sample")
    assert r.outcome == "workflow" and r.step_after == f"WF:{key}"
    assert r.replies == ["Hello Shree Packaging Pvt Ltd, here is our sample kit.", "Do you want one?"]
    assert titles(r) == ["Yes please", "No thanks"]

    r = await tap(SHREE, "Yes please")
    assert r.replies == ["We will send it (yes)."]
    assert (await run_of(SHREE)).status == "done"
    # finished: the next message is the order-status bot's again
    assert (await send(SHREE, "hi")).outcome == "ask_language"

    async with session_scope() as db:
        logged = (await db.execute(select(MessageLog.text).where(
            MessageLog.phone_e164 == SHREE, MessageLog.outcome == "workflow"))).scalars().all()
    assert "Do you want one?" in logged  # in the chat log, marked as a workflow's message


async def test_a_contains_keyword_never_swallows_an_order_number():
    await publish("Sample kit", sample_kit(triggers={"keywords": [{"text": "sample kit", "match": "contains"}]}))
    assert (await send(SHREE, "can I get a sample kit please")).outcome == "workflow"
    assert (await send(MEHTA, "sample kit for SO 45240")).outcome != "workflow"


async def test_a_main_menu_row_starts_the_workflow_and_the_built_in_rows_still_work():
    await publish("Sample kit", sample_kit(triggers={"menu": {"enabled": True, "label": {"en": "Sample kit", "hi": "सैंपल किट"}}}))
    r = await open_menu(SHREE)
    assert r.options["kind"] == "list"  # four rows no longer fit in three buttons
    assert titles(r) == MENU_BUTTONS["en"] + ["Sample kit"]

    r = await tap(SHREE, "Sample kit")
    assert r.outcome == "workflow" and r.replies[-1] == "Do you want one?"
    assert (await tap(SHREE, "No thanks")).replies == ["No problem."]

    r = await send(SHREE, "menu")
    assert r.outcome == "menu" and titles(r)[-1] == "Sample kit"
    assert (await tap(SHREE, "Order status")).outcome == "ask_so"

    r = await open_menu(MEHTA, "हिंदी")
    assert titles(r) == MENU_BUTTONS["hi"] + ["सैंपल किट"]


@pytest.fixture
def owner_test_numbers(monkeypatch):
    """The owner's own phones, as set in Settings."""
    monkeypatch.setenv("WORKFLOW_TEST_NUMBERS", "+91 91678 61236, 910000000001")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("WORKFLOW_TEST_NUMBERS")
    get_settings.cache_clear()


async def test_publishing_reaches_only_the_test_numbers_until_it_is_switched_on(owner_test_numbers):
    key = await publish("Sample kit", sample_kit(triggers={"keywords": ["sample"]}), for_everyone=False)
    assert (await send(SHREE, "sample")).outcome == "workflow"  # a test number
    assert (await send(MEHTA, "sample")).outcome != "workflow"  # everyone else: not yet
    listed = await store.list_workflows()
    assert listed[0]["published_version"] == 1 and listed[0]["live"] is False

    await store.set_live(key, True)
    assert (await send(MEHTA, "sample")).outcome == "workflow"
    # switched off again: back to the test numbers only, without unpublishing anything
    await store.set_live(key, False)
    await send(GUJ, "hi")
    assert (await send(GUJ, "sample")).outcome != "workflow"
    # republishing a live workflow keeps it live
    await store.set_live(key, True)
    await store.publish(key)
    assert (await store.list_workflows())[0]["live"] is True


async def test_a_new_number_is_greeted_by_onboarding_once_per_conversation():
    await publish("New customer onboarding", onboarding(triggers={"unknown_customer": True}))
    r = await say_as(UNKNOWN, "hello", "Asha")
    assert r.outcome == "workflow"
    assert r.replies == ["Welcome to Pouchwale, Asha!", "What is your company called?"]
    r = await say_as(UNKNOWN, "Acme Foods", "Asha")
    assert r.replies == ["Thank you, Acme Foods. Our team will be in touch."]
    # on the New customers list now, with what they told us, and greeted as someone we know
    async with session_scope() as db:
        listed = await db.get(NewCustomer, UNKNOWN)
    assert listed.whatsapp_name == "Asha" and json.loads(listed.details) == {"company": "Acme Foods"}
    r = await say_as(UNKNOWN, "hello again", "Asha")
    assert r.outcome == "new_customer" and "Welcome back" in r.reply_text
    assert (await send(SHREE, "hi")).outcome != "workflow"  # known customers never see it


# ---------------- hand-offs ----------------
async def test_a_workflow_can_hand_the_customer_to_the_order_status_menu():
    await publish("Help", doc_of(
        [{"id": "m", "type": "message", "text": en("Let me take you to the main menu.")},
         {"id": "j", "type": "jump", "workflow": BUILTIN_ORDER_STATUS}],
        [("m", "next", "j")], "m", triggers={"keywords": ["help me"]}))
    await open_menu(SHREE)
    r = await send(SHREE, "help me")
    assert r.outcome == "workflow"
    assert r.replies[0] == "Let me take you to the main menu."
    assert titles(r) == MENU_BUTTONS["en"]
    assert (await tap(SHREE, "Order status")).outcome == "ask_so"


async def test_one_workflow_can_hand_over_to_another_and_keep_the_answers():
    follow_up = await publish("Follow up", doc_of(
        [{"id": "m", "type": "message", "text": en("Hi {name}, this is the follow-up.")}, {"id": "e", "type": "end"}],
        [("m", "next", "e")], "m"))
    await publish("Intake", doc_of(
        [{"id": "q", "type": "question", "text": en("What is your name?"), "store": "name", "input": {"kind": "text"}},
         {"id": "j", "type": "jump", "workflow": follow_up}],
        [("q", "next", "j")], "q", triggers={"keywords": ["intake"]}))
    await send(SHREE, "intake")
    r = await send(SHREE, "Ravi")
    assert r.replies == ["Hi Ravi, this is the follow-up."]
    run = await run_of(SHREE)
    assert run.status == "done" and run.last_key == follow_up


async def test_a_hand_over_to_a_missing_workflow_cannot_be_published():
    w = await store.create_from_doc("Broken", "", doc_of(
        [{"id": "j", "type": "jump", "workflow": "no-such-workflow"}], [], "j", triggers={"keywords": ["broken"]}))
    with pytest.raises(store.NotPublishable) as e:
        await store.publish(w["key"])
    assert any("does not exist" in i["message"] for i in e.value.issues)


# ---------------- time ----------------
async def test_a_wait_step_really_waits_and_the_timer_carries_on():
    await publish("Slow", doc_of(
        [{"id": "a", "type": "message", "text": en("One moment please.")},
         {"id": "w", "type": "delay", "seconds": 5},
         {"id": "b", "type": "message", "text": en("Here it is.")},
         {"id": "e", "type": "end"}],
        [("a", "next", "w"), ("w", "next", "b"), ("b", "next", "e")], "a", triggers={"keywords": ["slow"]}))
    r = await send(SHREE, "slow")
    assert r.replies == ["One moment please."]
    run = await run_of(SHREE)
    assert run.status == "waiting" and run.due_at > utcnow()

    r = await send(SHREE, "hello?")  # written during the pause: not talked over
    assert r.outcome == "workflow_wait" and r.replies == []
    assert await runtime.resume_due() == 0  # not due yet

    async with session_scope() as db:
        await db.execute(update(WorkflowRun).where(WorkflowRun.phone_e164 == SHREE)
                         .values(due_at=utcnow() - timedelta(seconds=1)))
    assert await runtime.resume_due() == 1
    assert wati.outbox[-1]["text"] == "Here it is."
    assert (await run_of(SHREE)).status == "done"


async def test_a_customer_who_goes_quiet_is_let_out():
    await publish("Sample kit", sample_kit(triggers={"keywords": ["sample"]}, idle_minutes=10))
    await send(SHREE, "sample")
    async with session_scope() as db:
        await db.execute(update(WorkflowRun).where(WorkflowRun.phone_e164 == SHREE)
                         .values(updated_at=utcnow() - timedelta(minutes=11)))
    assert (await tap(SHREE, "Yes please")).outcome != "workflow"
    assert (await run_of(SHREE)).status == "done"


async def test_a_customer_can_leave_a_question_for_another_workflow_or_the_menu():
    await publish("Sample kit", sample_kit(triggers={"keywords": ["sample"]}))
    catalogue = await publish("Catalogue", sample_kit(triggers={"keywords": ["catalogue"]}))
    await open_menu(SHREE)
    await send(SHREE, "sample")

    r = await send(SHREE, "blah blah")  # not an answer and nothing else: asked again
    assert r.outcome == "workflow" and r.replies == ["Do you want one?"]

    r = await send(SHREE, "catalogue")
    assert r.outcome == "workflow" and (await run_of(SHREE)).workflow_key == catalogue

    r = await send(SHREE, "menu")
    assert r.outcome == "menu" and titles(r) == MENU_BUTTONS["en"]
    assert (await run_of(SHREE)).status == "done"


# ---------------- publishing and switching ----------------
async def test_publishing_mid_conversation_keeps_customers_on_their_version():
    key = await publish("Sample kit", sample_kit(triggers={"keywords": ["sample"]}))
    await send(SHREE, "sample")

    v2 = sample_kit(triggers={"keywords": ["sample"]})
    v2["nodes"][2]["text"] = en("Version two: on its way ({want}).")
    await store.save_draft(key, v2)
    await store.publish(key)

    assert (await tap(SHREE, "Yes please")).replies == ["We will send it (yes)."]  # started on v1
    await send(MEHTA, "sample")
    assert (await tap(MEHTA, "Yes please")).replies == ["Version two: on its way (yes)."]


async def test_switching_a_workflow_off_lets_customers_out():
    key = await publish("Sample kit", sample_kit(triggers={"keywords": ["sample"]}))
    await send(SHREE, "sample")
    await store.set_live(key, False)
    assert (await tap(SHREE, "Yes please")).outcome != "workflow"

    await store.set_live(key, True)
    assert (await send(SHREE, "sample")).outcome == "workflow"

    draft = await store.create_from_doc("Never published", "", sample_kit())
    with pytest.raises(store.NotPublishable):
        await store.set_live(draft["key"], True)


# ---------------- doing things in WATI ----------------
async def test_steps_that_act_on_wati_are_really_performed_in_order():
    await publish("Doer", doer())
    r = await send(SHREE, "do it")
    assert r.replies == ["All done. A person from Sales will take it from here."]
    assert [o["kind"] for o in wati.outbox] == ["tag_add", "chat_status", "contact_attributes", "template_send",
                                                "text", "assign_teams"]
    template = next(o for o in wati.outbox if o["kind"] == "template_send")
    assert template["detail"]["params"] == {"name": "Shree Packaging Pvt Ltd"}


async def test_a_step_wati_refuses_takes_its_if_it_fails_exit(monkeypatch):
    async def refuse(phone, teams):
        raise WatiRejected("wati 400: no team called Sales")

    monkeypatch.setattr(wati, "assign_teams", refuse)
    await publish("Doer", doer())
    assert (await send(SHREE, "do it")).replies[-1] == "Something went wrong, a person will help you."
    assert await handed_over(SHREE) is None  # nobody took the chat, so the bot stays in charge


async def test_a_list_with_sections_goes_out_with_its_sections():
    await publish("Pouch", doc_of(
        [{"id": "q", "type": "question", "text": en("Pick a pouch"),
          "input": {"kind": "list", "button_text": en("Pouches"), "options": [
              {"value": "a", "label": en("Stand up"), "section": en("Printed")},
              {"value": "b", "label": en("Flat"), "section": en("Plain")}]}},
         {"id": "e", "type": "end", "text": en("Noted.")}],
        [("q", "opt:a", "e"), ("q", "opt:b", "e")], "q", triggers={"keywords": ["pouch"]}))
    await send(SHREE, "pouch")
    sent = wati.outbox[-1]
    assert sent["kind"] == "list" and [s["title"] for s in sent["sections"]] == ["Printed", "Plain"]
    assert (await tap(SHREE, "Flat")).replies == ["Noted."]


async def test_a_picture_message_goes_out_as_a_file():
    await publish("Photo", doc_of(
        [{"id": "m", "type": "message", "text": en(""),
          "media": {"type": "image", "url": "https://cdn.example.com/kit.jpg", "caption": en("Our sample kit")}},
         {"id": "e", "type": "end"}],
        [("m", "next", "e")], "m", triggers={"keywords": ["photo"]}))
    await send(SHREE, "photo")
    sent = wati.outbox[-1]
    assert sent["kind"] == "file" and sent["url"] == "https://cdn.example.com/kit.jpg" and sent["text"] == "Our sample kit"


async def test_a_broken_workflow_hands_the_message_back_to_the_order_status_bot(monkeypatch):
    await publish("Sample kit", sample_kit(triggers={"keywords": ["sample"]}))

    async def boom(*args, **kwargs):
        raise RuntimeError("drawn wrong")

    monkeypatch.setattr(engine, "start", boom)
    assert (await send(SHREE, "sample")).outcome == "ask_language"  # answered, by the order-status bot
    assert (await run_of(SHREE)).status == "done"


# ---------------- checks across workflows ----------------
async def test_publish_time_checks_across_workflows():
    async def found(doc: dict, key: str = "") -> dict[str, str]:
        issues = await runtime.check_routing(doc, key)
        return {lvl: " | ".join(i.message for i in issues if i.level == lvl) for lvl in ("fail", "warn")}

    assert "reads like an order" in (await found(sample_kit(triggers={"keywords": ["SO 45231"]})))["fail"]
    assert "order-status bot" in (await found(sample_kit(triggers={"keywords": ["hi"]})))["warn"]
    assert "already a button" in (await found(sample_kit(triggers={"menu": {"enabled": True, "label": {"en": "Order status"}}})))["fail"]
    long_label = {"menu": {"enabled": True, "label": {"en": "A label far too long for WhatsApp"}}}
    assert "characters" in (await found(sample_kit(triggers=long_label)))["fail"]
    assert "Nothing starts this workflow" in (await found(sample_kit()))["warn"]

    await publish("Sample kit", sample_kit(triggers={"keywords": ["sample"]}))
    assert "also starts “Sample kit”" in (await found(sample_kit(triggers={"keywords": ["Sample"]}), key="other"))["warn"]


# ---------------- the dashboard ----------------
async def test_the_dashboard_can_see_and_steer_live_workflows():
    key = await publish("Sample kit", sample_kit(triggers={"keywords": ["sample"],
                                                           "menu": {"enabled": True, "label": {"en": "Sample kit"}}}))
    await send(SHREE, "sample")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        routing = (await c.get("/admin/api/workflows/routing", headers=H)).json()
        assert routing["workflows"][0]["key"] == key and routing["workflows"][0]["active"] == 1
        assert routing["main_menu"]["en"] == MENU_BUTTONS["en"] + ["Sample kit"]

        runs = (await c.get("/admin/api/workflows/runs", headers=H)).json()
        assert runs[0]["phone"] == SHREE and runs[0]["step"] == "ask"
        assert (await c.delete(f"/admin/api/workflows/runs/{SHREE}", headers=H)).json() == {"ok": True}
        assert (await c.get("/admin/api/workflows/runs", headers=H)).json() == []

        await send(SHREE, "sample")  # a session reset clears the workflow too
        assert (await c.post(f"/admin/api/sessions/{SHREE}/reset", headers=H)).status_code == 200
        assert await run_of(SHREE) is None

        r = await c.post(f"/admin/api/workflows/{key}/live", json={"live": False}, headers=H)
        assert r.json()["live"] is False
        listed = (await c.get("/admin/api/workflows", headers=H)).json()
        assert listed[0]["live"] is False and 'Main-menu button "Sample kit"' in listed[0]["starts"]


# ---------------- the WATI calls themselves ----------------
@pytest.fixture
def live_wati(monkeypatch):
    monkeypatch.setenv("WATI_BASE_URL", BASE)
    monkeypatch.setenv("WATI_TOKEN", "tok123")
    monkeypatch.setenv("WATI_DRY_RUN", "false")
    monkeypatch.setenv("WATI_CHANNEL_NUMBER", "918888888888")
    get_settings.cache_clear()
    yield WatiClient()
    get_settings.cache_clear()


async def test_wati_calls_use_the_documented_endpoints_and_bodies(live_wati):
    phone = "919876543210"
    ok = Response(200, json={"result": True})
    with respx.mock(assert_all_called=True) as mock:
        tag = mock.post(f"{BASE}/api/ext/v3/conversations/{phone}/tags").mock(return_value=Response(200, json={"added": True}))
        mock.delete(f"{BASE}/api/ext/v3/conversations/{phone}/tags/vip").mock(return_value=Response(200, json={"removed": True}))
        operator = mock.put(f"{BASE}/api/ext/v3/conversations/{phone}/operator").mock(return_value=ok)
        teams = mock.put(f"{BASE}/api/ext/v3/contacts/teams").mock(return_value=ok)
        status = mock.put(f"{BASE}/api/ext/v3/conversations/{phone}/status").mock(return_value=ok)
        attrs = mock.post(f"{BASE}/api/v1/updateContactAttributes/{phone}").mock(return_value=ok)
        template = mock.post(f"{BASE}/api/v1/sendTemplateMessage").mock(return_value=ok)
        await live_wati.add_tag(phone, "vip")
        await live_wati.remove_tag(phone, "vip")
        await live_wati.assign_operator(phone, "ops@pouchwale.test")
        await live_wati.assign_teams(phone, ["Sales"])
        await live_wati.set_chat_status(phone, "pending")
        await live_wati.set_contact_attributes(phone, {"subscribed": "false"})
        await live_wati.send_template_message(phone, "order_ready", {"name": "Mehta Foods"}, broadcast="workflow-x")

    def body(route):
        return json.loads(route.calls.last.request.content)

    assert tag.calls.last.request.headers["Authorization"] == "Bearer tok123"
    assert body(tag) == {"tag_name": "vip"}
    assert body(operator) == {"assignee_email": "ops@pouchwale.test"}
    assert body(teams) == {"target": phone, "teams": ["Sales"]}
    assert body(status) == {"new_status": "pending"}
    assert body(attrs) == {"customParams": [{"name": "subscribed", "value": "false"}]}
    assert template.calls.last.request.url.params["whatsappNumber"] == phone
    assert body(template) == {"template_name": "order_ready", "broadcast_name": "workflow-x",
                              "parameters": [{"name": "name", "value": "Mehta Foods"}],
                              "channel_number": "918888888888"}


async def test_a_step_wati_refuses_is_an_error_not_a_silent_success(live_wati):
    with respx.mock() as mock:
        mock.post(f"{BASE}/api/ext/v3/conversations/919876543210/tags").mock(
            return_value=Response(404, json={"message": "tag not found"}))
        with pytest.raises(WatiRejected):
            await live_wati.add_tag("919876543210", "nope")


async def test_a_picture_is_uploaded_and_a_broken_link_falls_back_to_text(live_wati, monkeypatch):
    from app.services.workflow import actions

    monkeypatch.setattr(actions, "check_url", lambda url: "")  # no DNS in the test run
    phone = "919876543210"
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://cdn.example.com/kit.jpg").mock(
            return_value=Response(200, content=b"\xff\xd8 jpeg bytes", headers={"content-type": "image/jpeg"}))
        upload = mock.post(f"{BASE}/api/v1/sendSessionFile/{phone}").mock(
            return_value=Response(200, json={"ok": True, "result": "success"}))
        await live_wati.send_file_url(phone, "https://cdn.example.com/kit.jpg", "Our sample kit", "image")
    req = upload.calls.last.request
    assert req.url.params["caption"] == "Our sample kit"
    assert req.headers["content-type"].startswith("multipart/form-data")
    content = req.read()
    assert b'filename="kit.jpg"' in content and b"jpeg bytes" in content

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://cdn.example.com/gone.jpg").mock(return_value=Response(404))
        text = mock.post(f"{BASE}/api/v1/sendSessionMessage/{phone}").mock(return_value=Response(200, json={"result": True}))
        await live_wati.send_file_url(phone, "https://cdn.example.com/gone.jpg", "Our sample kit")
    assert text.calls.last.request.url.params["messageText"] == "Our sample kit\nhttps://cdn.example.com/gone.jpg"


def test_a_picture_above_buttons_and_list_sections_use_watis_shapes():
    buttons = menus.Options(kind="buttons", items=[menus.Option("Yes"), menus.Option("No")], header="replaced by the picture")
    p = WatiClient.buttons_payload("Want one?", buttons, {"type": "image", "url": "https://cdn.example.com/kit.jpg"})
    assert p["header"] == {"type": "Image", "media": {"url": "https://cdn.example.com/kit.jpg"}}
    doc = WatiClient.buttons_payload("Here", buttons, {"type": "document", "url": "https://cdn.example.com/files/price%20list.pdf"})
    assert doc["header"]["media"]["fileName"] == "price list.pdf"
    assert WatiClient.buttons_payload("Plain", buttons)["header"] == {"type": "Text", "text": "replaced by the picture"}

    rows = menus.Options(kind="list", items=[menus.Option("A"), menus.Option("B"), menus.Option("C")], button_text="Pick")
    p = WatiClient.list_payload("Choose", rows, [
        {"title": "Pouches", "rows": [{"title": "A", "description": ""}, {"title": "B", "description": "x"}]},
        {"title": "Kits", "rows": [{"title": "C", "description": ""}]}])
    assert set(p) == {"header", "body", "footer", "buttonText", "sections"}
    assert [s["title"] for s in p["sections"]] == ["Pouches", "Kits"]
    assert [len(s["rows"]) for s in p["sections"]] == [2, 1]


# ---------------- a person takes the chat ----------------
def handoff() -> dict:
    return doc_of(
        [{"id": "tell", "type": "message", "text": en("Connecting you to our sales team.")},
         {"id": "team", "type": "assign", "to": "team", "teams": ["Sales"]},
         {"id": "bad", "type": "end", "text": en("Our team is busy, please try later.")}],
        [("tell", "next", "team"), ("team", "on_error", "bad")], "tell", triggers={"keywords": ["talk to sales"]})


async def handed_over(phone: str) -> AgentHandover | None:
    async with session_scope() as db:
        row = await db.get(AgentHandover, phone)
        return row if row is not None and row.active else None


async def test_after_an_assign_step_the_bot_stays_quiet_until_handed_back():
    await publish("Sales", handoff())
    r = await send(SHREE, "talk to sales")
    assert r.replies == ["Connecting you to our sales team."]
    assert (await handed_over(SHREE)).assignee == "Sales"
    assert (await run_of(SHREE)).status == "done"

    sent = len(wati.outbox)
    for text in ("hello?", "menu", "SO 45231", "talk to sales"):
        r = await send(SHREE, text)
        assert r.outcome == "with_agent" and r.reply_text is None, text
    assert len(wati.outbox) == sent  # nothing at all went to the customer

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        people = (await c.get("/admin/api/handovers", headers=H)).json()
        assert people[0]["phone"] == SHREE and people[0]["assignee"] == "Sales" and people[0]["returns_at"]
        assert (await c.delete(f"/admin/api/handovers/{SHREE}", headers=H)).json() == {"ok": True}
    assert (await send(SHREE, "hi")).outcome == "ask_language"  # the bot is back


async def test_a_handed_over_chat_comes_back_after_hours_of_silence():
    await publish("Sales", handoff())
    await send(SHREE, "talk to sales")
    quiet_for = timedelta(hours=get_settings().agent_handover_hours + 1)
    async with session_scope() as db:
        await db.execute(update(AgentHandover).where(AgentHandover.phone_e164 == SHREE)
                         .values(last_message_at=utcnow() - quiet_for))
    assert (await send(SHREE, "hi")).outcome == "ask_language"
    assert await handed_over(SHREE) is None


async def test_the_dashboard_can_hand_a_chat_to_a_person_and_back():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/admin/api/handovers/{MEHTA}", json={"assignee": "ravi@pouchwale.test"}, headers=H)
        assert r.status_code == 200
        assert (await send(MEHTA, "hi")).outcome == "with_agent"
        assert (await c.post(f"/admin/api/sessions/{MEHTA}/reset", headers=H)).status_code == 200  # hands back too
    assert (await send(MEHTA, "hi")).outcome == "ask_language"


async def test_the_test_chat_shows_where_a_person_takes_over():
    turn = await engine.start(runtime.schema.parse(handoff()))
    assert [m.text for m in turn.messages] == ["Connecting you to our sales team."]
    assert turn.stopped == "handed_over"


# ---------------- new customers ----------------
async def test_a_new_customer_is_not_greeted_twice_and_never_sees_orders():
    await publish("New customer onboarding", onboarding(triggers={"unknown_customer": True}))
    await say_as(UNKNOWN, "hello", "Asha")
    await say_as(UNKNOWN, "Mehta Foods", "Asha")  # the name of a real customer, typed by a stranger
    async with session_scope() as db:  # a new conversation, long after the first
        await db.execute(update(WorkflowRun).where(WorkflowRun.phone_e164 == UNKNOWN)
                         .values(last_finished_at=utcnow() - timedelta(hours=5)))
    r = await say_as(UNKNOWN, "hi", "Asha")
    assert r.outcome == "new_customer"  # not onboarded a second time
    r = await say_as(UNKNOWN, "SO 45240", "Asha")
    assert r.outcome == "new_customer" and "Ready for Dispatch" not in (r.reply_text or "")


async def test_a_new_number_who_stops_half_way_is_not_listed():
    await publish("New customer onboarding", onboarding(triggers={"unknown_customer": True}))
    await say_as(UNKNOWN, "hello", "Asha")
    async with session_scope() as db:
        assert await db.get(NewCustomer, UNKNOWN) is None


async def test_the_dashboard_lists_and_removes_new_customers():
    await publish("New customer onboarding", onboarding(triggers={"unknown_customer": True}))
    await say_as(UNKNOWN, "hello", "Asha")
    await say_as(UNKNOWN, "Acme Foods", "Asha")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        rows = (await c.get("/admin/api/new-customers", headers=H)).json()
        assert len(rows) == 1
        assert rows[0]["phone"] == UNKNOWN and rows[0]["name"] == "Acme Foods" and rows[0]["whatsapp_name"] == "Asha"
        assert rows[0]["details"] == {"company": "Acme Foods"} and rows[0]["in_customer_list"] is False
        assert (await c.delete(f"/admin/api/new-customers/{UNKNOWN}", headers=H)).json() == {"ok": True}
        assert (await c.get("/admin/api/new-customers", headers=H)).json() == []
