"""Auto-translation of workflow text, the customisable language question, date and website answers,
and warnings that say once, clearly, what is missing."""
from __future__ import annotations

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.main import app
from app.services import translate as T
from app.services.workflow import engine, schema, validate

H = {"X-Admin-Key": "test-admin"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    T._cache.clear()
    yield
    T._cache.clear()


def mymemory(text: str) -> Response:
    return Response(200, json={"responseStatus": 200, "responseData": {"translatedText": text}})


# ---------------- translation ----------------
async def test_many_texts_go_to_google_in_one_call_and_keep_placeholders():
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(T.GOOGLE).mock(return_value=Response(200, json=["नमस्ते {sys.customer_name}", "आपका ऑर्डर तैयार है"]))
        got = await T.translate(["Hello {sys.customer_name}", "Your order is ready"], "hi")
    assert got == ["नमस्ते {sys.customer_name}", "आपका ऑर्डर तैयार है"]
    params = route.calls.last.request.url.params
    assert params.get_list("q") == ["Hello {sys.customer_name}", "Your order is ready"] and params["tl"] == "hi"
    assert route.call_count == 1


async def test_common_button_words_come_from_the_glossary_without_a_call():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(T.GOOGLE)
        assert await T.translate(["Yes", "No thanks", "", "Yes please!"], "gu") == ["હા", "ના, આભાર", "", "હા, જરૂર"]
    assert not route.called


async def test_numbers_and_codes_are_not_sent_and_lines_stay_lines():
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(T.GOOGLE).mock(return_value=Response(200, json=["पहली पंक्ति", "दूसरी पंक्ति"]))
        got = await T.translate(["First line\n\nSO 45240\nSecond line"], "hi")
    assert got == ["पहली पंक्ति\n\nSO 45240\nदूसरी पंक्ति"]
    assert route.calls.last.request.url.params.get_list("q") == ["First line", "Second line"]


async def test_when_google_is_blocked_mymemory_takes_over():
    with respx.mock() as mock:
        mock.get(T.GOOGLE).mock(return_value=Response(429, text="Sorry..."))
        mm = mock.get(T.MYMEMORY).mock(return_value=mymemory("કૃપા કરીને પસંદ કરો"))
        assert await T.translate(["Please pick one"], "gu") == ["કૃપા કરીને પસંદ કરો"]
    assert mm.calls.last.request.url.params["langpair"] == "en|gu"


async def test_a_translation_that_drops_a_placeholder_is_redone_around_it():
    def answer(request):
        q = request.url.params["q"]
        return mymemory({"Hello": "नमस्ते", ", welcome": ", स्वागत है"}.get(q, "नमस्ते ग्राहक, स्वागत है"))

    with respx.mock() as mock:
        mock.get(T.GOOGLE).mock(return_value=Response(200, json=["नमस्ते ग्राहक, स्वागत है"]))  # lost it
        mock.get(T.MYMEMORY).mock(side_effect=answer)
        got = await T.translate(["Hello {sys.customer_name}, welcome"], "hi")
    assert got == ["नमस्ते {sys.customer_name}, स्वागत है"]


async def test_when_no_translator_answers_the_editor_is_told_plainly():
    with respx.mock() as mock:
        mock.get(T.GOOGLE).mock(return_value=Response(500))
        mock.get(T.MYMEMORY).mock(return_value=Response(500, json={"responseStatus": 500}))
        with pytest.raises(T.TranslateUnavailable):
            await T.translate(["Please pick one"], "hi")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/admin/api/workflows/translate", json={"texts": ["Please pick one"]}, headers=H)
    assert r.status_code == 503 and "could not be reached" in r.json()["detail"]


async def test_the_translate_endpoint_fills_both_languages():
    def answer(request):
        return Response(200, json={"hi": ["आपकी कंपनी का नाम?"], "gu": ["તમારી કંપનીનું નામ?"]}[request.url.params["tl"]])

    with respx.mock() as mock:
        mock.get(T.GOOGLE).mock(side_effect=answer)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/admin/api/workflows/translate", json={"texts": ["Your company name?", "No"]}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"translations": {"hi": ["आपकी कंपनी का नाम?", "नहीं"], "gu": ["તમારી કંપનીનું નામ?", "ના"]}}


# ---------------- the language question ----------------
def language_question(**spec) -> dict:
    return {"schema": 1, "start": "q", "settings": {},
            "nodes": [{"id": "q", "type": "question", "title": "Language", "x": 0, "y": 0,
                       "text": {"en": "Choose your language", "hi": "अपनी भाषा चुनें", "gu": "તમારી ભાષા પસંદ કરો"},
                       "header": {"en": "Pouchwale", "hi": "Pouchwale", "gu": "Pouchwale"},
                       "input": {"kind": "language", **spec}},
                      {"id": "e", "type": "end", "title": "Bye", "x": 0, "y": 0,
                       "text": {"en": "English it is", "hi": "हिंदी ठीक है", "gu": "ગુજરાતી બરાબર"}}],
            "edges": [{"id": "1", "from": "q", "port": "opt:en", "to": "e"},
                      {"id": "2", "from": "q", "port": "opt:hi", "to": "e"}]}


async def test_a_language_question_can_offer_its_own_buttons_and_title():
    doc = language_question(languages=["en", "hi"], language_labels={"en": "English 🇬🇧", "hi": "हिंदी में"})
    assert [i.message for i in validate.validate_graph(doc) if i.level == "fail"] == []
    assert [p for p, _ in schema.ports_of(doc["nodes"][0])] == ["opt:en", "opt:hi"]  # no Gujarati exit

    graph = schema.parse(doc)
    turn = await engine.start(graph)
    options = turn.messages[0].options
    assert options.titles() == ["English 🇬🇧", "हिंदी में"] and options.header == "Pouchwale"

    tapped = await engine.advance(graph, turn.state, "हिंदी में")
    assert tapped.state.language == "hi" and tapped.messages[0].text == "हिंदी ठीक है"
    typed = await engine.advance(graph, (await engine.start(graph)).state, "Hindi")  # the plain word still works
    assert typed.state.language == "hi"


def test_a_language_question_needs_two_languages_and_short_distinct_buttons():
    def fails(doc):
        return " | ".join(i.message for i in validate.validate_graph(doc) if i.level == "fail")

    assert "fewer than two languages" in fails(language_question(languages=["en"]))
    assert "WhatsApp allows 20" in fails(language_question(language_labels={"en": "English, please choose me"}))
    assert "both say" in fails(language_question(language_labels={"en": "Same", "hi": "same"}))


async def test_without_custom_wording_the_language_buttons_are_the_usual_three():
    graph = schema.parse(language_question())
    turn = await engine.start(graph)
    assert turn.messages[0].options.titles() == ["English", "हिंदी", "ગુજરાતી"]


# ---------------- what an answer may be ----------------
@pytest.mark.parametrize("text,ok", [
    ("25/12/2026", True), ("25-12-2026", True), ("2026-12-25", True), ("5/1/26", True),
    ("31/02/2026", False), ("tomorrow", False), ("", False),
])
def test_a_date_answer(text, ok):
    assert engine._valid(text, {"type": "date"}) is ok


@pytest.mark.parametrize("text,ok", [
    ("www.pouchwale.com", True), ("https://pouchwale.com/sample-kit", True), ("pouchwale.in", True),
    ("pouchwale", False), ("not a site", False),
])
def test_a_website_answer(text, ok):
    assert engine._valid(text, {"type": "url"}) is ok


# ---------------- warnings ----------------
def test_warnings_name_the_step_and_say_once_what_is_missing():
    doc = {"schema": 1, "start": "q", "settings": {},
           "nodes": [{"id": "q", "type": "question", "title": "Pick", "x": 0, "y": 0, "text": {"en": "Pick one"},
                      "input": {"kind": "buttons", "options": [{"value": "y", "label": {"en": "Yes"}},
                                                               {"value": "n", "label": {"en": "No"}}]}},
                     {"id": "e", "type": "end", "title": "E", "x": 0, "y": 0}],
           "edges": [{"id": "1", "from": "q", "port": "opt:y", "to": "e"}, {"id": "2", "from": "q", "port": "opt:n", "to": "e"}]}
    messages = [i.message for i in validate.validate_graph(doc)]
    assert len(messages) == len(set(messages))  # nothing said twice
    assert not any("built-in bot" in m for m in messages)  # "Yes" and "No" are fine inside a workflow
    assert 'Button "Yes" in "Pick" has no Hindi or Gujarati yet, so those customers see the English. Translate fills it in.' in messages
