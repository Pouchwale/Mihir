"""The workflow steps that reach outside the conversation.

These mirror WATI's own chatbot builder: time delay, set tags, assign agent/team, update chat
status, subscribe/unsubscribe, webhook (API request) and send template.

The rule the whole design rests on: the engine RECORDS what should happen and hands it back. Pressing
Test in the dashboard must never assign a real chat or tag a real customer.
"""
from __future__ import annotations

import pytest

from app.services.workflow import engine, schema
from app.services.workflow.actions import check_url, pick
from app.services.workflow.validate import validate_graph


def flow(*nodes, edges=()):
    return {"schema": 1, "start": nodes[0]["id"], "nodes": list(nodes), "edges": [dict(e) for e in edges]}


def fails(doc) -> list[str]:
    return [i.message for i in validate_graph(doc) if i.level == "fail"]


@pytest.mark.asyncio
async def test_action_steps_are_recorded_not_performed():
    doc = flow(
        {"id": "t", "type": "tags", "title": "Tag it", "tags": ["lead", "{source}"]},
        {"id": "s", "type": "chat_status", "title": "Mark pending", "status": "pending"},
        {"id": "u", "type": "subscribe", "title": "Opt in", "subscribe": True},
        {"id": "m", "type": "template", "title": "Send receipt", "template_name": "order_ready"},
        {"id": "a", "type": "assign", "title": "To sales", "to": "team", "teams": ["Sales"]},
        {"id": "e", "type": "end", "title": "Bye", "text": {"en": "Done"}},
        edges=[{"id": "1", "from": "t", "port": "next", "to": "s"},
               {"id": "3", "from": "s", "port": "next", "to": "u"},
               {"id": "4", "from": "u", "port": "next", "to": "m"},
               {"id": "5", "from": "m", "port": "next", "to": "a"},
               {"id": "5b", "from": "m", "port": "on_error", "to": "e"},
               {"id": "6", "from": "a", "port": "on_error", "to": "e"}])
    assert fails(doc) == []
    g = schema.parse(doc)
    state = engine.RunState(node=g.start, vars={"source": "website"})
    turn = await engine._run(g, state)
    assert [a.kind for a in turn.actions] == ["tags", "chat_status", "subscribe", "template", "assign"]
    assert turn.actions[0].detail["tags"] == ["lead", "website"]  # an answer interpolates into a tag
    assert "Sales" in turn.actions[-1].to_dict()["describe"]
    # a person has the chat after the Assign, so the bot's part ends there
    assert turn.stopped == "handed_over"


def test_nothing_may_follow_an_assign_to_a_person():
    doc = flow(
        {"id": "a", "type": "assign", "title": "To sales", "to": "team", "teams": ["Sales"]},
        {"id": "m", "type": "message", "title": "After", "text": {"en": "Still here"}},
        {"id": "e", "type": "end", "title": "Bye"},
        edges=[{"id": "1", "from": "a", "port": "next", "to": "m"},
               {"id": "2", "from": "a", "port": "on_error", "to": "e"},
               {"id": "3", "from": "m", "port": "next", "to": "e"}])
    assert any("hands the chat to a person" in f for f in fails(doc))
    # handing the chat back to the bot carries on as before
    back = flow({**doc["nodes"][0], "to": "bot"}, doc["nodes"][1], doc["nodes"][2], edges=doc["edges"])
    assert not any("hands the chat to a person" in f for f in fails(back))


@pytest.mark.asyncio
async def test_an_api_request_saves_values_and_takes_the_success_exit():
    calls = []

    async def fake(method, url, headers, body):
        calls.append((method, url, headers, body))
        return {"ok": True, "status": 200, "error": "",
                "data": {"result": {"quoteId": "Q-77"}, "items": [{"status": "shipped"}]}}

    doc = flow(
        {"id": "q", "type": "api_request", "title": "Get a quote", "method": "POST",
         "url": "https://erp.example.com/quote", "headers": {"X-Key": "{api_key}"},
         "body": '{"company": "{company}"}',
         "save": {"quote_id": "result.quoteId", "ship": "$.items[0].status"}},
        {"id": "e", "type": "end", "title": "Bye", "text": {"en": "Quote {quote_id} is {ship}"}},
        edges=[{"id": "1", "from": "q", "port": "success", "to": "e"},
               {"id": "2", "from": "q", "port": "failed", "to": "e"}])
    assert fails(doc) == []
    g = schema.parse(doc)
    state = engine.RunState(node=g.start, vars={"company": "Shree", "api_key": "secret"})
    turn = await engine._run(g, state, fetch=fake)
    assert calls[0][0] == "POST"
    assert calls[0][2] == {"X-Key": "secret"}          # placeholders resolved before the call
    assert calls[0][3] == {"company": "Shree"}
    assert state.vars["quote_id"] == "Q-77" and state.vars["ship"] == "shipped"
    assert turn.messages[-1].text == "Quote Q-77 is shipped"


@pytest.mark.asyncio
async def test_an_api_failure_takes_the_failed_exit_instead_of_stranding_anyone():
    async def broken(method, url, headers, body):
        return {"ok": False, "status": 503, "data": None, "error": "The service answered 503."}

    doc = flow(
        {"id": "q", "type": "api_request", "title": "Get a quote", "url": "https://erp.example.com/q",
         "method": "GET", "save": {"quote_id": "result.quoteId"}},
        {"id": "ok", "type": "end", "title": "Good", "text": {"en": "Quote {quote_id}"}},
        {"id": "bad", "type": "end", "title": "Sorry", "text": {"en": "We could not reach the system."}},
        edges=[{"id": "1", "from": "q", "port": "success", "to": "ok"},
               {"id": "2", "from": "q", "port": "failed", "to": "bad"}])
    g = schema.parse(doc)
    turn = await engine._run(g, engine.RunState(node=g.start), fetch=broken)
    assert turn.messages[-1].text == "We could not reach the system."
    assert turn.state.vars["sys.api_status"] == "503"


@pytest.mark.asyncio
async def test_response_routing_picks_a_path_from_the_answer():
    async def fake(method, url, headers, body):
        return {"ok": True, "status": 200, "error": "", "data": {"stock": 0}}

    doc = flow(
        {"id": "q", "type": "api_request", "title": "Stock", "url": "https://erp.example.com/s",
         "method": "GET",
         "routes": [{"id": "none", "label": "Out of stock", "path": "stock", "op": "eq", "value": "0"}]},
        {"id": "ok", "type": "end", "title": "In stock", "text": {"en": "In stock"}},
        {"id": "none", "type": "end", "title": "Out", "text": {"en": "Out of stock"}},
        {"id": "bad", "type": "end", "title": "Bad", "text": {"en": "Failed"}},
        edges=[{"id": "1", "from": "q", "port": "success", "to": "ok"},
               {"id": "2", "from": "q", "port": "route:none", "to": "none"},
               {"id": "3", "from": "q", "port": "failed", "to": "bad"}])
    assert fails(doc) == []
    g = schema.parse(doc)
    turn = await engine._run(g, engine.RunState(node=g.start), fetch=fake)
    assert turn.messages[-1].text == "Out of stock"


def test_an_unconnected_failure_exit_is_refused():
    """Someone else's outage must not be able to leave a customer with silence."""
    doc = flow(
        {"id": "q", "type": "api_request", "title": "Stock", "url": "https://e.example.com", "method": "GET"},
        {"id": "e", "type": "end", "title": "Bye", "text": {"en": "ok"}},
        edges=[{"id": "1", "from": "q", "port": "success", "to": "e"}])
    assert any("left with silence" in m for m in fails(doc))


def test_the_api_node_refuses_this_servers_own_network():
    """A URL typed into a workflow must not reach the hosting provider's internals."""
    assert "inside this server's own network" in check_url("http://127.0.0.1:8000/admin")
    assert "inside this server's own network" in check_url("http://169.254.169.254/latest/meta-data/")
    assert check_url("ftp://example.com") == "Use a full address starting with https://"
    assert check_url("https://example.com/api") == ""


def test_reading_values_out_of_a_response():
    """The shapes WATI's own webhook node documents: a key, dot notation, and array indexes."""
    data = {"result": {"quoteId": "Q-1"}, "items": [{"t": {"status": "shipped"}}], "ok": True}
    assert pick(data, "result.quoteId") == "Q-1"
    assert pick(data, "$.items[0].t.status") == "shipped"
    assert pick(data, "ok") == "true"
    assert pick(data, "missing.path") == ""        # never raises in front of a customer
    assert pick(None, "anything") == ""


@pytest.mark.asyncio
async def test_a_jump_hands_control_to_another_workflow():
    doc = flow({"id": "j", "type": "jump", "title": "Hand over", "workflow": "order-status"})
    assert fails(doc) == []
    g = schema.parse(doc)
    turn = await engine._run(g, engine.RunState(node=g.start))
    assert turn.stopped == "jump" and turn.jump_to == "order-status"


def test_a_delay_longer_than_wati_allows_is_refused():
    doc = flow({"id": "d", "type": "delay", "title": "Pause", "seconds": 900},
               {"id": "e", "type": "end", "title": "Bye", "text": {"en": "ok"}},
               edges=[{"id": "1", "from": "d", "port": "next", "to": "e"}])
    assert any("Use between 1 second and 10 minutes" in m for m in fails(doc))


@pytest.mark.parametrize("node,expect", [
    ({"id": "a", "type": "assign", "title": "A", "to": "operator"}, "identifies an operator by their email"),
    ({"id": "a", "type": "assign", "title": "A", "to": "team"}, "does not name a team"),
    ({"id": "a", "type": "tags", "title": "A", "tags": []}, "no tags on it"),
    ({"id": "a", "type": "template", "title": "A"}, "which approved template"),
    ({"id": "a", "type": "jump", "title": "A"}, "which workflow to continue in"),
    ({"id": "a", "type": "chat_status", "title": "A", "status": "nonsense"}, "must set the conversation"),
    ({"id": "a", "type": "api_request", "title": "A", "method": "GET"}, "no address to call"),
    ({"id": "a", "type": "api_request", "title": "A", "url": "https://x.com", "method": "PUT"}, "must use GET or POST"),
    ({"id": "a", "type": "api_request", "title": "A", "url": "https://x.com", "method": "POST",
      "body": "{not json"}, "not valid JSON"),
])
def test_a_half_filled_step_is_refused(node, expect):
    assert any(expect in m for m in fails(flow(node))), node


@pytest.mark.asyncio
async def test_actions_survive_the_round_trip_the_dashboard_makes():
    """The simulator sends state back and forth as JSON; an action must describe itself for the UI."""
    doc = flow({"id": "t", "type": "tags", "title": "Tag", "tags": ["vip"]},
               {"id": "e", "type": "end", "title": "Bye", "text": {"en": "ok"}},
               edges=[{"id": "1", "from": "t", "port": "next", "to": "e"}])
    g = schema.parse(doc)
    turn = await engine._run(g, engine.RunState(node=g.start))
    as_dict = turn.actions[0].to_dict()
    assert as_dict["kind"] == "tags" and as_dict["node_id"] == "t"
    assert as_dict["describe"] == "Add the tag(s) vip"


@pytest.mark.parametrize("bad", [
    None, "oops", {"vars": "oops"}, {"vars": [1, 2]}, {"language": "klingon"},
    {"attempts": "many"}, {"attempts": -5}, {"node": {"a": 1}}, {"finished": "yes"},
])
def test_a_mangled_conversation_state_never_crashes(bad):
    """The simulator sends state back from the browser, so this is untrusted JSON. A stale tab or a
    half-written script must get an answer, not a 500 - this crashed with vars as a string."""
    st = engine.RunState.from_dict(bad)
    assert isinstance(st.vars, dict)
    assert st.language in schema.LANGS
    assert st.attempts >= 0
    assert st.node is None or isinstance(st.node, str)
