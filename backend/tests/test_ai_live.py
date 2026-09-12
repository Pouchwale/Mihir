"""The AI where it meets customers: understanding a typed answer, answering from the owner's FAQ,
and translating - each checked to fall back safely, and to send nothing about the customer."""
from __future__ import annotations

import copy
import json

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.config import apply_overrides, overrides
from app.main import app
from app.services import translate as T
from app.services.ai import groq_client as G
from app.services.ai import live as ai_live
from app.services.workflow import engine, schema
from app.services.workflow.validate import validate_graph

H = {"X-Admin-Key": "test-admin"}
GROQ = "https://api.groq.com/openai/v1/chat/completions"
PHONE = "918888888888"


@pytest.fixture(autouse=True)
def _fresh():
    G.breaker_reset()
    T._cache.clear()
    yield
    G.breaker_reset()
    T._cache.clear()


@pytest.fixture
def groq_key():
    before = overrides()
    apply_overrides({**before, "groq_api_key": "test-key"})
    yield
    apply_overrides(before)


def ok(obj: dict) -> Response:
    return Response(200, json={"choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}],
                               "usage": {"total_tokens": 50}})


SYSTEM = {"phone": PHONE, "customer_name": "Test customer"}


def choices_doc(ai: bool = True) -> dict:
    return {"schema": 1, "start": "ask", "settings": {"ai_understand": ai}, "nodes": [
        {"id": "ask", "type": "question", "title": "Pouch", "text": {"en": "Which pouch?"},
         "input": {"kind": "buttons", "options": [
             {"value": "stand_up", "label": {"en": "Stand-up pouch", "hi": "स्टैंड-अप पाउच"}},
             {"value": "flat", "label": {"en": "Flat pouch"}}]}},
        {"id": "su", "type": "end", "text": {"en": "Stand-up it is"}},
        {"id": "fl", "type": "end", "text": {"en": "Flat it is"}}],
        "edges": [{"id": "e1", "from": "ask", "port": "opt:stand_up", "to": "su"},
                  {"id": "e2", "from": "ask", "port": "opt:flat", "to": "fl"}]}


async def _typed(doc: dict, said: str) -> engine.Turn:
    graph = schema.parse(doc)
    first = await engine.start(graph, system=SYSTEM)
    return await engine.advance(graph, first.state, said, system=SYSTEM, matcher=ai_live.match_choice)


# ---------------- understanding typed answers ----------------
async def test_a_typed_answer_is_understood_as_the_button_it_means(groq_key):
    with respx.mock() as mock:
        route = mock.post(GROQ).mock(return_value=ok({"choice": "stand_up"}))
        turn = await _typed(choices_doc(), "500 standup chahiye")
    assert [m.text for m in turn.messages] == ["Stand-up it is"] and turn.ai == "matched"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "openai/gpt-oss-20b"
    enum = body["response_format"]["json_schema"]["schema"]["properties"]["choice"]["enum"]
    assert enum == ["stand_up", "flat", "none"]
    assert PHONE not in route.calls.last.request.content.decode() and "Test customer" not in route.calls.last.request.content.decode()


async def test_when_the_ai_is_not_sure_the_question_is_asked_again(groq_key):
    with respx.mock() as mock:
        mock.post(GROQ).mock(return_value=ok({"choice": "none"}))
        turn = await _typed(choices_doc(), "what are your prices")
    assert turn.stopped == "not_understood" and turn.ai == ""


async def test_the_ai_is_never_asked_unless_the_workflow_switched_it_on(groq_key):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(GROQ)
        turn = await _typed(choices_doc(ai=False), "standup chahiye")
        exact = await _typed(choices_doc(), "Flat pouch")
    assert not route.called
    assert turn.stopped == "not_understood" and [m.text for m in exact.messages] == ["Flat it is"]


async def test_repeated_failures_pause_the_ai_and_customers_are_simply_asked_again(groq_key):
    with respx.mock() as mock:
        route = mock.post(GROQ).mock(return_value=Response(500))
        for _ in range(4):
            turn = await _typed(choices_doc(), "standup")
            assert turn.stopped == "not_understood"
    assert route.call_count == 3 and G.breaker_open()


# ---------------- answering from the FAQ ----------------
FAQ = "MOQ is 500 pouches. Delivery takes 10 days."


def faq_doc() -> dict:
    return {"schema": 1, "start": "faq", "settings": {}, "nodes": [
        {"id": "faq", "type": "ai_reply", "title": "Questions", "text": {"en": "Ask me anything"}, "faq": FAQ,
         "tone": "friendly", "max_chars": 600, "store": "question"},
        {"id": "more", "type": "end", "text": {"en": "Anything else? Just ask."}},
        {"id": "sorry", "type": "end", "text": {"en": "Let me get a person."}}],
        "edges": [{"id": "e1", "from": "faq", "port": "answered", "to": "more"},
                  {"id": "e2", "from": "faq", "port": "unsure", "to": "sorry"}]}


async def _ask(said: str) -> engine.Turn:
    graph = schema.parse(faq_doc())
    first = await engine.start(graph, system=SYSTEM)
    assert [m.text for m in first.messages] == ["Ask me anything"] and first.stopped == ""
    return await engine.advance(graph, first.state, said, system=SYSTEM, answerer=ai_live.answer_faq)


async def test_a_question_the_faq_answers_is_answered(groq_key):
    with respx.mock() as mock:
        route = mock.post(GROQ).mock(return_value=ok({"answer": "Our minimum order is 500 pouches.", "confident": True}))
        turn = await _ask("what is the MOQ")
    assert [m.text for m in turn.messages] == ["Our minimum order is 500 pouches.", "Anything else? Just ask."]
    assert turn.ai == "answered" and turn.state.vars["question"] == "what is the MOQ"
    sent = route.calls.last.request.content.decode()
    assert FAQ in sent and "what is the MOQ" in sent and PHONE not in sent and "Test customer" not in sent


async def test_a_doubtful_answer_takes_not_sure(groq_key):
    with respx.mock() as mock:
        mock.post(GROQ).mock(return_value=ok({"answer": "Maybe next week?", "confident": False}))
        turn = await _ask("when will my order arrive")
    assert [m.text for m in turn.messages] == ["Let me get a person."] and turn.ai == "unsure"


async def test_with_no_ai_available_not_sure_is_taken_without_a_call():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(GROQ)
        turn = await _ask("what is the MOQ")
    assert not route.called
    assert [m.text for m in turn.messages] == ["Let me get a person."] and turn.ai == "unavailable"


def test_an_ai_reply_step_needs_faq_text_and_somewhere_for_not_sure():
    assert [i.message for i in validate_graph(faq_doc()) if i.level == "fail"] == []
    no_faq = faq_doc()
    no_faq["nodes"][0]["faq"] = "  "
    assert any("no FAQ text" in i.message for i in validate_graph(no_faq) if i.level == "fail")
    loose = faq_doc()
    loose["edges"] = loose["edges"][:1]
    assert any('"Not sure"' in i.message for i in validate_graph(loose) if i.level == "fail")
    long_answers = faq_doc()
    long_answers["nodes"][0]["max_chars"] = 5000
    assert any("characters" in i.message for i in validate_graph(long_answers) if i.level == "fail")


def test_answering_again_and_again_is_not_an_endless_loop():
    doc = faq_doc()
    doc["edges"][0]["to"] = "faq"  # answered -> ask for the next question
    assert not [i for i in validate_graph(doc) if "loop forever" in i.message]
    assert schema.ports_of(doc["nodes"][0]) == [("answered", "Answered"), ("unsure", "Not sure")]


async def test_the_test_chat_really_asks_the_ai(groq_key):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        key = (await c.post("/admin/api/workflows", json={"title": "AI FAQ chat"}, headers=H)).json()["key"]
        assert (await c.put(f"/admin/api/workflows/{key}/draft", json={"doc": faq_doc()}, headers=H)).status_code == 200
        first = (await c.post(f"/admin/api/workflows/{key}/simulate", json={}, headers=H)).json()
        with respx.mock() as mock:
            mock.post(GROQ).mock(return_value=ok({"answer": "10 days.", "confident": True}))
            got = (await c.post(f"/admin/api/workflows/{key}/simulate",
                                json={"text": "delivery time?", "state": first["state"]}, headers=H)).json()
    assert [m["text"] for m in first["messages"]] == ["Ask me anything"]
    assert got["ai"] == "answered" and got["ai_ready"] is True
    assert [m["text"] for m in got["messages"]] == ["10 days.", "Anything else? Just ask."]


# ---------------- translating ----------------
async def test_with_a_key_translation_uses_the_ai_and_keeps_placeholders(groq_key):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(GROQ).mock(return_value=ok({"translations": ["नमस्ते {sys.customer_name}", "आपका ऑर्डर तैयार है"]}))
        google = mock.get(T.GOOGLE)
        got = await T.translate(["Hello {sys.customer_name}", "Your order is ready", "Yes"], "hi")
    assert got == ["नमस्ते {sys.customer_name}", "आपका ऑर्डर तैयार है", "हाँ"]  # "Yes" still from the glossary
    assert not google.called
    assert json.loads(json.loads(route.calls.last.request.content)["messages"][1]["content"])["lines"] == [
        "Hello {sys.customer_name}", "Your order is ready"]


async def test_an_ai_translation_that_loses_a_placeholder_is_redone_by_the_free_translator(groq_key):
    with respx.mock() as mock:
        mock.post(GROQ).mock(return_value=ok({"translations": ["नमस्ते ग्राहक", "आपका ऑर्डर तैयार है"]}))
        google = mock.get(T.GOOGLE).mock(return_value=Response(200, json=["नमस्ते {sys.customer_name}"]))
        got = await T.translate(["Hello {sys.customer_name}", "Your order is ready"], "hi")
    assert got == ["नमस्ते {sys.customer_name}", "आपका ऑर्डर तैयार है"]
    assert google.calls.last.request.url.params.get_list("q") == ["Hello {sys.customer_name}"]


async def test_choosing_the_free_translators_never_calls_the_ai(groq_key):
    before = overrides()
    apply_overrides({**before, "translate_provider": "free"})
    try:
        with respx.mock(assert_all_called=False) as mock:
            groq = mock.post(GROQ)
            mock.get(T.GOOGLE).mock(return_value=Response(200, json=["कृपया एक चुनें"]))
            assert await T.translate(["Please pick one"], "hi") == ["कृपया एक चुनें"]
        assert not groq.called
    finally:
        apply_overrides(before)
