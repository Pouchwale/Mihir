"""The visual workflow builder: does a drawn conversation actually work, and does the validator
refuse the ones that would break in front of a customer?

The engine itself stays pure: nothing here sends a message or touches the database. Running a
published workflow for real customers is runtime.py's job, tested in test_workflow_runtime.py.
"""
from __future__ import annotations

import copy
import json
import pathlib

import pytest

from app.services import menus
from app.services.workflow import engine, schema, validate

# The example shipped with the app, not a generated fixture: it is what a new install offers as a
# starting point, so the suite proving it works is proving what the owner will actually open.
FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "workflow" / "examples" / "onboarding.json"


def onboarding() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


async def walk(graph, replies: list[str]):
    """Drive a conversation and return every bot message in order."""
    turn = await engine.start(graph)
    out = list(turn.messages)
    for said in replies:
        turn = await engine.advance(graph, turn.state, said)
        out.extend(turn.messages)
    return out, turn


# ---------------- the flow the owner actually wants ----------------
def test_the_onboarding_flow_is_publishable():
    assert validate.validate_graph(onboarding()) == []


@pytest.mark.parametrize("lang,button,confirm", [
    ("en", "English", "That's right"),
    ("hi", "हिंदी", "सही है"),
    ("gu", "ગુજરાતી", "સાચું છે"),
])
@pytest.mark.asyncio
async def test_a_new_customer_can_be_onboarded_in_every_language(lang, button, confirm):
    """The whole point of the first workflow: a number that is NOT in the customer Excel gets a
    real conversation instead of 'could not verify'."""
    g = schema.parse(onboarding())
    msgs, turn = await walk(g, [button, "Shree Industries", "Rajesh Patel", confirm])
    assert turn.stopped == "end"
    assert turn.state.language == lang
    assert turn.state.vars == {"company": "Shree Industries", "person": "Rajesh Patel"}
    assert "Rajesh Patel" in msgs[-1].text  # the goodbye interpolated what they told us


@pytest.mark.asyncio
async def test_saying_no_sends_them_back_to_the_first_question():
    g = schema.parse(onboarding())
    _, turn = await walk(g, ["English", "Wrong Ltd", "Someone", "Let me redo it"])
    assert turn.state.node == "n_company"
    assert not turn.state.finished


@pytest.mark.asyncio
async def test_a_tapped_button_wins_over_a_language_the_session_is_not_in():
    """WhatsApp sends a tap back as the button's own title. A Gujarati customer shown an English
    title (because gu was empty) replies with that English text - the matcher has to accept it."""
    g = schema.parse(onboarding())
    turn = await engine.start(g)
    turn = await engine.advance(g, turn.state, "ગુજરાતી")
    turn = await engine.advance(g, turn.state, "Shree")
    turn = await engine.advance(g, turn.state, "Rajesh")
    turn = await engine.advance(g, turn.state, "That's right")  # English title, Gujarati session
    assert turn.stopped == "end"


@pytest.mark.asyncio
async def test_renaming_every_button_keeps_the_flow_working():
    """Mirrors test_workflow_after_admin_edit for the built-in flow: the owner may rename any label,
    and tapping the CURRENT text must still work. Nothing may hardcode the original wording."""
    doc = onboarding()
    node = next(n for n in doc["nodes"] if n["id"] == "n_confirm")
    for opt, new in zip(node["input"]["options"], ("Perfect", "Start over")):
        opt["label"] = {"en": new, "hi": new, "gu": new}
    assert validate.validate_graph(doc) == []
    g = schema.parse(doc)
    _, turn = await walk(g, ["English", "Acme", "Bob", "Perfect"])
    assert turn.stopped == "end"


# ---------------- the port contract ----------------
def test_every_port_the_engine_can_take_is_one_the_editor_can_draw():
    """The validator decides which exits exist; the engine decides which exit to follow. When they
    disagree the owner sees a connection silently refused - which is exactly what happened with the
    language question during development."""
    doc = onboarding()
    for node in doc["nodes"]:
        declared = {p for p, _ in schema.ports_of(node)}
        if node.get("type") != "question":
            continue
        spec = node.get("input") or {}
        if spec.get("kind") == "language":
            taken = {engine._answer(node, menus.label(f"lang_{c}", "en"), "en")[0] for c in schema.LANGS}
        elif spec.get("kind") in ("buttons", "list"):
            taken = {engine._answer(node, schema.text_of(o["label"], "en"), "en")[0]
                     for o in spec["options"]}
        else:
            taken = {engine._answer(node, "some answer", "en")[0]}
        assert taken <= declared, f"{node['id']} follows exits the editor never drew: {taken - declared}"


# ---------------- what the validator must refuse ----------------
def _q(**over):
    node = {"id": "q", "type": "question", "title": "Pick", "text": {"en": "Pick one", "hi": "", "gu": ""},
            "input": {"kind": "buttons", "options": [
                {"value": "a", "label": {"en": "Apples", "hi": "", "gu": ""}},
                {"value": "b", "label": {"en": "Pears", "hi": "", "gu": ""}}]}}
    node.update(over)
    return {"schema": 1, "start": "q", "nodes": [node, {"id": "e", "type": "end", "title": "Bye"}],
            "edges": [{"id": "1", "from": "q", "port": "opt:a", "to": "e"},
                      {"id": "2", "from": "q", "port": "opt:b", "to": "e"}]}


def fails(doc) -> list[str]:
    return [i.message for i in validate.validate_graph(doc) if i.level == "fail"]


def test_a_button_that_leads_nowhere_is_refused():
    doc = _q()
    doc["edges"] = [e for e in doc["edges"] if e["port"] != "opt:b"]
    assert any("Pears" in m and "not connected" in m for m in fails(doc))


def test_too_many_buttons_is_refused_and_says_what_to_do():
    doc = _q()
    doc["nodes"][0]["input"]["options"] += [
        {"value": "c", "label": {"en": "Plums"}}, {"value": "d", "label": {"en": "Figs"}}]
    doc["edges"] += [{"id": "3", "from": "q", "port": "opt:c", "to": "e"},
                     {"id": "4", "from": "q", "port": "opt:d", "to": "e"}]
    msg = next(m for m in fails(doc) if "WhatsApp allows" in m)
    assert "3" in msg and "Switch it to a list" in msg


def test_a_button_longer_than_whatsapp_allows_is_refused():
    doc = _q()
    doc["nodes"][0]["input"]["options"][0]["label"]["en"] = "x" * 21
    assert any(f"WhatsApp allows {menus.BUTTON_TEXT_MAX}" in m for m in fails(doc))


def test_missing_english_is_refused_but_missing_gujarati_is_only_a_warning():
    doc = _q()
    doc["nodes"][0]["text"]["en"] = ""
    assert any("no English text" in m for m in fails(doc))

    doc = _q()  # hi/gu empty throughout the fixture
    warns = [i.message for i in validate.validate_graph(doc) if i.level == "warn"]
    assert any("no Hindi or Gujarati yet" in m and "Translate" in m for m in warns)
    assert not any("no English" in m for m in fails(doc))


def test_two_buttons_that_read_the_same_are_refused():
    doc = _q()
    doc["nodes"][0]["input"]["options"][1]["label"]["en"] = "Apples"
    assert any("read the same" in m for m in fails(doc))


def test_a_button_that_is_also_a_menu_button_is_fine():
    """Inside a running workflow the workflow reads the tap first, so a label shared with the built-in
    menu ("Yes", "Order status") works. Only a keyword that STARTS a workflow is checked against the
    order-status bot, in runtime.check_routing."""
    doc = _q()
    doc["nodes"][0]["input"]["options"][0]["label"]["en"] = "Order status"
    assert not any("built-in bot" in i.message for i in validate.validate_graph(doc))


def test_a_button_that_reads_as_an_order_code_is_refused():
    doc = _q()
    doc["nodes"][0]["input"]["options"][0]["label"]["en"] = "SO 45231"
    assert any("reads as an order or item code" in m for m in fails(doc))


def test_a_connection_to_a_deleted_step_is_refused():
    doc = _q()
    doc["edges"].append({"id": "9", "from": "q", "port": "opt:a", "to": "gone"})
    assert any("no longer exists" in m for m in fails(doc))


def test_a_loop_that_never_waits_for_the_customer_is_refused():
    doc = {"schema": 1, "start": "m1",
           "nodes": [{"id": "m1", "type": "message", "title": "One", "text": {"en": "one"}},
                     {"id": "m2", "type": "message", "title": "Two", "text": {"en": "two"}}],
           "edges": [{"id": "1", "from": "m1", "port": "next", "to": "m2"},
                     {"id": "2", "from": "m2", "port": "next", "to": "m1"}]}
    assert any("loop forever" in m for m in fails(doc))


def test_an_unusable_answer_name_is_refused():
    doc = _q(store="Company Name")
    assert any("not a usable answer name" in m for m in fails(doc))


def test_an_unreachable_step_warns_but_still_publishes():
    doc = _q()
    doc["nodes"].append({"id": "orphan", "type": "message", "title": "Nobody", "text": {"en": "hi"}})
    issues = validate.validate_graph(doc)
    assert not [i for i in issues if i.level == "fail"]
    assert any("cannot be reached" in i.message for i in issues)


# ---------------- safety ----------------
@pytest.mark.asyncio
async def test_the_run_loop_cannot_flood_a_customer():
    """Even a graph the validator somehow passed must not send a phone hundreds of messages."""
    nodes, edges = [], []
    for i in range(60):
        nodes.append({"id": f"m{i}", "type": "message", "title": f"M{i}", "text": {"en": f"msg {i}"}})
        edges.append({"id": f"e{i}", "from": f"m{i}", "port": "next", "to": f"m{(i + 1) % 60}"})
    g = schema.parse({"schema": 1, "start": "m0", "nodes": nodes, "edges": edges})
    turn = await engine.start(g)
    assert turn.stopped == "cap"
    assert len(turn.messages) <= schema.MAX_MESSAGES_PER_TURN


@pytest.mark.asyncio
async def test_an_unset_placeholder_never_crashes_a_message():
    g = schema.parse({"schema": 1, "start": "m",
                      "nodes": [{"id": "m", "type": "end", "title": "Bye",
                                 "text": {"en": "Thanks {who}, see you at {place}."}}],
                      "edges": []})
    turn = await engine.start(g)
    assert turn.messages[0].text == "Thanks {who}, see you at {place}."


def test_every_menu_a_publishable_workflow_can_send_is_legal_whatsapp():
    """The engine builds Options by hand; menus.validate is what WATI will actually accept."""
    g = schema.parse(onboarding())
    for lang in schema.LANGS:
        state = engine.RunState(node=g.start, language=lang)
        for node in g.nodes.values():
            if node.get("type") != "question":
                continue
            options = engine._options(node, state)
            if options:
                assert menus.validate(options) == [], f"{node['id']} in {lang}"


@pytest.mark.asyncio
async def test_free_text_validation_takes_the_invalid_exit():
    doc = _q(input={"kind": "text"}, store="qty", validate={"type": "number"}, max_retries=1,
             invalid_text={"en": "Numbers only please."})
    doc["edges"] = [{"id": "1", "from": "q", "port": "next", "to": "e"},
                    {"id": "2", "from": "q", "port": "invalid", "to": "e"},
                    {"id": "3", "from": "q", "port": "retry_exhausted", "to": "e"}]
    assert fails(doc) == []
    g = schema.parse(doc)
    turn = await engine.start(g)
    turn = await engine.advance(g, turn.state, "lots")
    assert turn.stopped == "not_understood"
    assert turn.messages[0].text == "Numbers only please."
    turn = await engine.advance(g, turn.state, "250")
    assert turn.state.vars["qty"] == "250"


@pytest.mark.asyncio
async def test_conditions_branch_on_a_saved_answer():
    doc = {"schema": 1, "start": "c",
           "nodes": [{"id": "c", "type": "condition", "title": "Big order?",
                      "branches": [{"id": "big", "label": "Large",
                                    "when": {"var": "qty", "op": "gte", "value": "100"}}]},
                     {"id": "y", "type": "end", "title": "Large", "text": {"en": "Our team will call."}},
                     {"id": "n", "type": "end", "title": "Small", "text": {"en": "Order online."}}],
           "edges": [{"id": "1", "from": "c", "port": "branch:big", "to": "y"},
                     {"id": "2", "from": "c", "port": "else", "to": "n"}]}
    assert fails(doc) == []
    g = schema.parse(doc)
    for qty, expect in (("500", "Our team will call."), ("2", "Order online."), ("many", "Order online.")):
        state = engine.RunState(node=g.start, vars={"qty": qty})
        turn = await engine._run(g, state)
        assert turn.messages[0].text == expect, qty


def test_the_fixture_survives_a_round_trip_through_json():
    """The editor sends this document back verbatim; a lossy parse would corrupt saved work."""
    doc = onboarding()
    assert schema.parse(json.loads(json.dumps(doc))).routes == schema.parse(copy.deepcopy(doc)).routes
