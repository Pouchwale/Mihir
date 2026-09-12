"""The WATI chatbot features the builder now matches: answers checked against the business's own
data, picture/document/location/time answers, the Not valid exit, saving to the WATI contact,
similar-spelling keywords, working hours, and the owner's own saved questions."""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db import session_scope
from app.main import app
from app.models import InboundQueue, MessageLog, QuestionPreset, Session, Workflow, WorkflowRun, WorkflowVersion
from app.services.processor import process_payload
from app.services.verify import customer_orders, find_customer
from app.services.wati import wati
from app.services.workflow import engine, lookup, runtime, schema, store, validate
from tests.flow import MEHTA, ROYAL, SHREE, UNKNOWN

H = {"X-Admin-Key": "test-admin"}


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean():
    async def wipe():
        async with session_scope() as db:
            for table in (WorkflowRun, WorkflowVersion, Workflow, Session, MessageLog, InboundQueue, QuestionPreset):
                await db.execute(delete(table))
        runtime.registry.invalidate()

    await wipe()
    yield
    await wipe()


def en(t: str) -> dict:
    return {"en": t, "hi": "", "gu": ""}


def ask(validate_rule: dict, store_as: str = "order", invalid_to: bool = False, then: str = "Found it: {order_status}") -> dict:
    """A one-question workflow: ask, then say what was found."""
    edges = [{"id": "1", "from": "q", "port": "next", "to": "said"}]
    nodes = [{"id": "q", "type": "question", "title": "Ask", "x": 0, "y": 0, "text": en("Please type it."),
              "input": {"kind": "text"}, "store": store_as, "validate": validate_rule, "max_retries": 2,
              "invalid_text": en("That is not one of yours. Try again.")},
             {"id": "said", "type": "end", "title": "Said", "x": 0, "y": 0, "text": en(then)}]
    if invalid_to:
        nodes.append({"id": "nope", "type": "end", "title": "Nope", "x": 0, "y": 0, "text": en("Let me get a person.")})
        edges.append({"id": "2", "from": "q", "port": "invalid", "to": "nope"})
        nodes[0]["max_retries"] = 0  # "Keep asking": a wrong answer goes to Not valid at once
    return {"schema": 1, "start": "q", "settings": {"multilingual": False}, "nodes": nodes, "edges": edges}


async def walk(doc: dict, phone: str, *answers: str, media: dict | None = None):
    """Run a workflow for this customer with real data checks; returns the last turn."""
    graph = schema.parse(doc)
    async with session_scope() as db:
        async def check(rule, value):
            return await lookup.find(db, phone, str(rule.get("source") or ""), value)

        turn = await engine.start(graph)
        for said in answers:
            turn = await engine.advance(graph, turn.state, said, lookup=check, media=media)
    return turn


# ---------------- checking against the business's data ----------------
async def test_an_so_number_is_checked_against_this_customers_own_orders():
    doc = ask({"type": "lookup", "source": "so"})
    assert [i.message for i in validate.validate_graph(doc) if i.level != "warn"] == []  # {order_status} is known
    turn = await walk(doc, SHREE, "SO 45231")
    assert turn.messages[0].text == "Found it: In Production"
    assert turn.state.vars["order"] == "45231" and turn.state.vars["order_count"] == "1"


async def test_another_companys_order_is_never_found():
    turn = await walk(ask({"type": "lookup", "source": "so"}), SHREE, "45240")  # Mehta Foods' order
    assert turn.stopped == "not_understood" and turn.messages[0].text == "That is not one of yours. Try again."
    turn = await walk(ask({"type": "lookup", "source": "so"}), UNKNOWN, "45231")  # a number not in the list
    assert turn.stopped == "not_understood"


async def test_a_po_number_and_an_item_code_can_be_checked_too():
    turn = await walk(ask({"type": "lookup", "source": "po"}, then="SO {order_so}"), ROYAL, "PO-7777")
    assert turn.messages[0].text == "SO 45280"
    async with session_scope() as db:
        item = (await customer_orders(db, await find_customer(db, MEHTA)))[0]
    turn = await walk(ask({"type": "lookup", "source": "fg"}, then="{order_status}"), MEHTA, item.fg_item_code)
    assert turn.messages[0].text == item.real_status


async def test_a_customer_code_proves_who_is_writing():
    async with session_scope() as db:
        code = (await find_customer(db, SHREE)).customer_code
    doc = ask({"type": "lookup", "source": "customer_code"}, then="Welcome back, {order_name}")
    assert (await walk(doc, SHREE, code.lower())).messages[0].text == "Welcome back, Shree Packaging Pvt Ltd"
    assert (await walk(doc, MEHTA, code)).stopped == "not_understood"  # someone else's code


def test_a_data_check_must_say_what_it_checks():
    fails = [i.message for i in validate.validate_graph(ask({"type": "lookup"})) if i.level == "fail"]
    assert any("does not say what to check" in m for m in fails)


async def test_the_not_valid_exit_is_taken_when_it_is_connected():
    turn = await walk(ask({"type": "lookup", "source": "so"}, invalid_to=True), SHREE, "99999")
    assert turn.messages[0].text == "Let me get a person." and turn.state.vars["order"] == "99999"


async def test_the_test_chat_can_check_real_orders_as_a_chosen_customer():
    w = await store.create_from_doc("SO check", "", ask({"type": "lookup", "source": "so"}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        first = (await c.post(f"/admin/api/workflows/{w['key']}/simulate", json={"phone": SHREE}, headers=H)).json()
        r = (await c.post(f"/admin/api/workflows/{w['key']}/simulate",
                          json={"phone": SHREE, "text": "45231", "state": first["state"]}, headers=H)).json()
    assert r["messages"][0]["text"] == "Found it: In Production"
    assert r["state"]["vars"]["sys.customer_name"] == "Shree Packaging Pvt Ltd"


# ---------------- pictures, documents, locations, times ----------------
async def test_a_picture_or_document_can_be_the_answer():
    doc = ask({"type": "file"}, store_as="proof", then="Got your {proof_type}: {proof}")
    turn = await walk(doc, SHREE, "", media={"type": "image", "url": "https://files.example.com/proof.jpg"})
    assert turn.messages[0].text == "Got your image: https://files.example.com/proof.jpg"
    assert (await walk(doc, SHREE, "here it is")).stopped == "not_understood"  # typed, not sent


async def test_a_location_can_be_the_answer():
    doc = ask({"type": "location"}, store_as="site", then="{site_address} {site}")
    media = runtime.media_of({"type": "location", "data": {"latitude": 23.0225, "longitude": 72.5714, "address": "Ahmedabad"}})
    turn = await walk(doc, SHREE, "", media=media)
    assert turn.messages[0].text == "Ahmedabad https://maps.google.com/?q=23.0225,72.5714"


def test_what_wati_sends_is_read_whatever_its_shape():
    assert runtime.media_of({"type": "image", "data": "https://files.example.com/a.jpg"})["url"] == "https://files.example.com/a.jpg"
    assert runtime.media_of({"type": "document", "data": {"url": "https://files.example.com/a.pdf"}})["type"] == "document"
    assert runtime.media_of({"type": "location", "text": "23.02, 72.57"})["url"] == "https://maps.google.com/?q=23.02,72.57"
    assert runtime.media_of({"type": "text", "text": "hi"}) is None


@pytest.mark.parametrize("text,ok", [("14:30", True), ("9.15", True), ("2:30 pm", True), ("10am", True),
                                     ("25:00", False), ("soon", False)])
def test_a_time_answer(text, ok):
    assert engine._valid(text, {"type": "time"}) is ok


async def test_a_photo_sent_on_whatsapp_answers_a_live_workflow():
    doc = ask({"type": "file"}, store_as="proof", then="Thanks, we have your {proof_type}.")
    doc["settings"]["triggers"] = {"keywords": ["send proof"]}
    w = await store.create_from_doc("Proof", "", doc)
    await store.publish(w["key"])
    await store.set_live(w["key"], True)

    async def send(payload):
        async with session_scope() as db:
            return await process_payload(db, {"id": f"t-{uuid.uuid4().hex}", "waId": SHREE, **payload})

    assert (await send({"type": "text", "text": "send proof"})).outcome == "workflow"
    r = await send({"type": "image", "text": None, "data": "https://files.example.com/proof.jpg"})
    assert r.replies == ["Thanks, we have your image."]


# ---------------- saving to the WATI contact ----------------
async def test_remember_can_also_save_on_the_wati_contact():
    doc = {"schema": 1, "start": "r", "settings": {"multilingual": False},
           "nodes": [{"id": "r", "type": "set_var", "title": "Remember", "x": 0, "y": 0, "assign": {"segment": "vip"},
                      "to_contact": True},
                     {"id": "e", "type": "end", "title": "End", "x": 0, "y": 0}],
           "edges": [{"id": "1", "from": "r", "port": "next", "to": "e"}]}
    done: list = []

    async def perform(action):
        done.append(action)
        return True

    turn = await engine.start(schema.parse(doc), live=engine.Live(emit=lambda m: _nothing(), perform=perform))
    assert [a.kind for a in turn.actions] == ["attributes"] and done[0].detail == {"attributes": {"segment": "vip"}}
    assert "Save on the WATI contact: segment = vip" in turn.actions[0].to_dict()["describe"]


async def _nothing():
    return None


# ---------------- keywords with a slip of the finger ----------------
def test_a_similar_spelling_starts_the_workflow_but_nothing_else_does():
    doc = {"settings": {"triggers": {"keywords": [{"text": "sample kit", "match": "similar"}]}}}
    wf = runtime.LiveWorkflow(key="k", title="Sample", version=1, priority=10,
                              graph=schema.parse({"start": "", "nodes": [], "edges": []}),
                              triggers=schema.triggers_of(doc), for_everyone=True, idle_minutes=30, jumps_to=frozenset())
    assert runtime.match_trigger([wf], "samlpe kit", SHREE)[0] is wf
    assert runtime.match_trigger([wf], "sample kits", SHREE)[0] is wf
    assert runtime.match_trigger([wf], "order status", SHREE)[0] is None
    assert '"sample kit" (or a similar spelling)' in schema.describe_triggers(doc)


# ---------------- working hours ----------------
@pytest.mark.parametrize("left,span,ok", [
    ("14:30", "10:00-19:00", True), ("20:15", "10:00-19:00", False), ("23:00", "21:00-06:00", True),
    ("05:30", "21:00-06:00", True), ("12:00", "21:00-06:00", False), ("14", "10-19", True),
    ("14:30", "10-19", True), ("500", "100-1000", True), ("abc", "1-2", False), ("5", "no dash", False),
])
def test_is_between(left, span, ok):
    assert engine._between(left, span) is ok


def test_the_clock_is_indian_time():
    got = schema.clock_vars(datetime(2026, 9, 11, 9, 5, tzinfo=schema.IST))
    assert got == {"hour": "9", "time": "09:05", "weekday": "Fri", "date": "11/09/2026"}


async def test_a_branch_can_test_working_hours():
    doc = {"schema": 1, "start": "c", "settings": {"multilingual": False},
           "nodes": [{"id": "c", "type": "condition", "title": "Open?", "x": 0, "y": 0,
                      "branches": [{"id": "b1", "label": "Open", "when": {"var": "sys.time", "op": "between", "value": "10:00-19:00"}}]},
                     {"id": "open", "type": "end", "title": "Open", "x": 0, "y": 0, "text": en("We are open.")},
                     {"id": "shut", "type": "end", "title": "Shut", "x": 0, "y": 0, "text": en("We are closed.")}],
           "edges": [{"id": "1", "from": "c", "port": "branch:b1", "to": "open"}, {"id": "2", "from": "c", "port": "else", "to": "shut"}]}
    assert [i.message for i in validate.validate_graph(doc) if "nothing in this workflow sets" in i.message] == []
    day = await engine.start(schema.parse(doc), system=schema.clock_vars(datetime(2026, 9, 11, 11, 0, tzinfo=schema.IST)))
    night = await engine.start(schema.parse(doc), system=schema.clock_vars(datetime(2026, 9, 11, 22, 0, tzinfo=schema.IST)))
    assert day.messages[0].text == "We are open." and night.messages[0].text == "We are closed."


# ---------------- the owner's own saved questions ----------------
async def test_a_question_can_be_saved_and_offered_again():
    spec = {"input": {"kind": "text"}, "validate": {"type": "lookup", "source": "so"}, "store": "order",
            "text": en("Your SO number?"), "x": 5, "edges": "ignored"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        saved = (await c.post("/admin/api/workflows/question-presets", json={"label": "Order number", "spec": spec}, headers=H)).json()
        assert saved["label"] == "Order number" and "x" not in saved["spec"] and "edges" not in saved["spec"]
        listed = (await c.get("/admin/api/workflows/question-presets", headers=H)).json()
        assert [p["label"] for p in listed] == ["Order number"]
        bad = await c.post("/admin/api/workflows/question-presets", json={"label": "", "spec": spec}, headers=H)
        assert bad.status_code == 400
        assert (await c.delete(f"/admin/api/workflows/question-presets/{saved['id']}", headers=H)).json() == {"ok": True}
        assert (await c.get("/admin/api/workflows/question-presets", headers=H)).json() == []


async def test_with_tries_left_it_asks_again_then_takes_not_valid():
    doc = ask({"type": "lookup", "source": "so"}, invalid_to=True)
    doc["nodes"][0]["max_retries"] = 1
    assert (await walk(doc, SHREE, "99999")).stopped == "not_understood"  # one more try first
    turn = await walk(doc, SHREE, "99999", "88888")  # out of tries, and no Gave up drawn
    assert turn.messages[0].text == "Let me get a person."


def test_keep_asking_with_not_valid_connected_is_not_asking_forever():
    doc = ask({"type": "lookup", "source": "so"}, invalid_to=True)  # "Keep asking", Not valid connected
    assert not any("never gives up" in i.message for i in validate.validate_graph(doc))
    doc["edges"] = [e for e in doc["edges"] if e["port"] != "invalid"]
    assert any("never gives up" in i.message for i in validate.validate_graph(doc))
