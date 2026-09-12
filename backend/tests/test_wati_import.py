"""Importing a chatbot exported from WATI, and exporting ours as a file.

The fixture mirrors the structure of a real WATI export - the same field names, the same edge encoding
("<node>__<button id>", "<node>__<node>-default"), HTML bodies and {{name}} variables - with made-up
content. The webhook credential is a placeholder: a real export can carry a live one, which is exactly
why the import report calls it out.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.workflow import engine, schema
from app.services.workflow.validate import validate_graph
from app.services.workflow.wati_import import NotAWorkflow, html_to_whatsapp, import_any

H = {"X-Admin-Key": "test-admin"}
FAKE_TOKEN = "Bearer placeholder-not-a-real-token"


def wati_export() -> dict:
    def pos(x, y):
        return {"posX": str(x), "posY": str(y)}

    return {
        "id": None, "tenantId": "000000", "name": "Shop bot", "created": None,
        "flowNodes": [
            {"id": "main_message-Start", "flowNodeType": "Message", "isStartNode": True,
             "flowNodePosition": pos(-200.5, 10),
             "flowReplies": [
                 {"flowReplyType": "Text", "caption": "", "mimeType": "",
                  "data": "<p>Hi, {{name}}</p>\n<p><br></p>\n<p><strong>Welcome</strong> to the <em>shop</em>&nbsp;!</p>"},
                 {"flowReplyType": "Image", "data": "https://example.com/banner.jpg",
                  "caption": "<p>Our range</p>", "mimeType": "image/jpeg"}]},
            {"id": "main_buttons-Lang", "flowNodeType": "InteractiveButtons", "isStartNode": False,
             "flowNodePosition": pos(300, 0),
             "interactiveButtonsHeader": {"type": "Text", "text": "Language", "media": None},
             "interactiveButtonsBody": "<p>Please pick a language</p>",
             "interactiveButtonsFooter": "Pick one",
             "interactiveButtonsItems": [
                 {"id": "btnEn", "buttonText": "English", "nodeResultId": "main_list-Menu"},
                 {"id": "btnHi", "buttonText": "Hindi", "nodeResultId": ""}],
             "interactiveButtonsUserInputVariable": "Chosen Language",
             "interactiveButtonsDefaultNodeResultId": "main_message-Help"},
            {"id": "main_list-Menu", "flowNodeType": "InteractiveList", "isStartNode": False,
             "flowNodePosition": pos(700, 0),
             "interactiveListHeader": {"type": "Text", "text": "How can we help?"},
             "interactiveListBody": "<p><strong>Choose below</strong></p>",
             "interactiveListFooter": "", "interactiveListButtonText": "Menu Here",
             "interactiveListSections": [
                 {"id": "s1", "title": "Order", "rows": [
                     {"id": "rowOrder", "title": "I want to order", "description": "", "nodeResultId": "main_question-Qty"}]},
                 {"id": "s2", "title": "Order status", "rows": [
                     {"id": "rowStatus", "title": "Status of my order", "description": "", "nodeResultId": ""}]}],
             "interactiveListUserInputVariable": "", "interactiveListDefaultNodeResultId": ""},
            {"id": "main_question-Qty", "flowNodeType": "Question", "isStartNode": False,
             "flowNodePosition": pos(1100, 0),
             "flowReplies": [{"flowReplyType": "Text", "data": "<p>How many pouches?</p>", "caption": "", "mimeType": ""}],
             "userInputVariable": "Order Qty",
             "answerValidation": {"type": "Number", "minValue": "100", "maxValue": "", "regex": "",
                                  "fallback": "Please send a number of 100 or more.", "failsCount": "2"},
             "isMediaAccepted": False, "expectedAnswers": None},
            {"id": "main_message-Help", "flowNodeType": "Message", "isStartNode": False,
             "flowNodePosition": pos(700, 400),
             "flowReplies": [{"flowReplyType": "Text", "data": "<p>Our team will help you.</p>", "caption": "", "mimeType": ""}]},
            {"id": "main_webhook-Crm", "flowNodeType": "Webhook", "isStartNode": False,
             "flowNodePosition": pos(1500, 0), "methodType": "Post", "url": "https://crm.example.com/lead",
             "headers": [{"headerName": "Authorization", "headerValue": FAKE_TOKEN}],
             "body": '{"qty": "{{Order Qty}}"}', "testVariables": [], "responseVariables": [],
             "expectedStatuses": []},
            {"id": "main_sets-Clear", "flowNodeType": "InteractiveProductList", "isStartNode": False,
             "flowNodePosition": pos(1900, 0), "interactiveProductListHeaderText": "Clear",
             "interactiveProductListBodyText": "Checkout our set here", "catalogId": None, "setId": None,
             "hasError": False},
            {"id": "main_assign-Team", "flowNodeType": "AssignTeam", "isStartNode": False,
             "flowNodePosition": pos(0, 800), "team": "Sales"},
        ],
        "flowEdges": [
            {"id": "e1", "sourceNodeId": "main_message-Start", "targetNodeId": "main_buttons-Lang"},
            {"id": "e2", "sourceNodeId": "main_buttons-Lang__btnEn", "targetNodeId": "main_list-Menu"},
            {"id": "e3", "sourceNodeId": "main_buttons-Lang__main_buttons-Lang-default", "targetNodeId": "main_message-Help"},
            {"id": "e4", "sourceNodeId": "main_list-Menu__rowOrder", "targetNodeId": "main_question-Qty"},
            {"id": "e5", "sourceNodeId": "main_question-Qty", "targetNodeId": "main_webhook-Crm"},
            {"id": "e6", "sourceNodeId": "main_webhook-Crm", "targetNodeId": "main_sets-Clear"},
            {"id": "e7", "sourceNodeId": "main_list-Menu__ghostRow", "targetNodeId": "main_message-Help"},
        ],
        "lastUpdated": "2026-01-01T00:00:00Z", "isDeleted": False,
        "transform": {"posX": "0", "posY": "0", "zoom": "0.5"}, "isPro": True, "flowVersion": 0,
        "fallback": None, "channelTypes": ["WA"],
    }


def by_id(doc: dict) -> dict:
    return {n["id"]: n for n in doc["nodes"]}


def edge(doc: dict, src: str, port: str) -> str | None:
    return next((e["to"] for e in doc["edges"] if e["from"] == src and e["port"] == port), None)


# ---------------- WATI's rich text ----------------
@pytest.mark.parametrize("raw,expected", [
    ("<p>Hi, {{name}}</p>\n<p><br></p>\n<p><strong>Welcome</strong> to the <em>shop</em>&nbsp;!</p>",
     "Hi, {{name}}\n\n*Welcome* to the _shop_ !"),
    ("a<strong> b </strong>c", "a *b* c"),                               # markers hug the words
    ("<strong>one<br>two</strong>", "*one*\n*two*"),                      # bold cannot span a line break
    ('<a href="https://x.example/p">https://x.example/p</a>', "https://x.example/p"),
    ('<a href="https://x.example">our site</a>', "our site (https://x.example)"),
    ("<ul><li>Stand-up</li><li>Centre seal</li></ul>", "• Stand-up\n• Centre seal"),
    ("<p>fish &amp; chips</p>", "fish & chips"),
    ("plain line one\nplain line two", "plain line one\nplain line two"),  # not HTML: its breaks stay
    ("", ""),
])
def test_wati_html_becomes_whatsapp_formatting(raw, expected):
    assert html_to_whatsapp(raw) == expected


# ---------------- the import itself ----------------
def test_a_wati_export_is_imported_step_for_step():
    doc, report = import_any(wati_export())
    nodes = by_id(doc)

    assert doc["start"] == "main_message-Start"
    assert doc["settings"]["multilingual"] is False  # WATI branches by language, one text per step
    assert nodes["main_message-Start"]["text"]["en"] == "Hi, {sys.customer_name}\n\n*Welcome* to the _shop_ !"

    # two replies in one WATI step become two steps, joined in order; the step's own exit leaves the last
    picture = nodes["main_message-Start-2"]
    assert picture["media"] == {"type": "image", "url": "https://example.com/banner.jpg", "caption": {"en": "Our range"}}
    assert edge(doc, "main_message-Start", "next") == "main_message-Start-2"
    assert edge(doc, "main_message-Start-2", "next") == "main_buttons-Lang"

    buttons = nodes["main_buttons-Lang"]
    assert buttons["input"]["kind"] == "buttons"
    assert [o["value"] for o in buttons["input"]["options"]] == ["btnEn", "btnHi"]
    assert buttons["header"] == {"en": "Language"} and buttons["footer"] == {"en": "Pick one"}
    assert buttons["store"] == "chosen_language"
    assert edge(doc, "main_buttons-Lang", "opt:btnEn") == "main_list-Menu"
    assert edge(doc, "main_buttons-Lang", "default") == "main_message-Help"
    assert edge(doc, "main_buttons-Lang", "opt:btnHi") is None  # led nowhere in WATI too

    menu = nodes["main_list-Menu"]
    assert menu["input"]["button_text"] == {"en": "Menu Here"}
    assert [(o["label"]["en"], o["section"]["en"]) for o in menu["input"]["options"]] == [
        ("I want to order", "Order"), ("Status of my order", "Order status")]

    qty = nodes["main_question-Qty"]
    assert qty["validate"] == {"type": "number", "min": "100", "max": ""}
    assert qty["max_retries"] == 2 and qty["store"] == "order_qty"
    assert qty["invalid_text"] == {"en": "Please send a number of 100 or more."}

    hook = nodes["main_webhook-Crm"]
    assert hook["method"] == "POST" and hook["headers"] == {"Authorization": FAKE_TOKEN}
    assert hook["body"] == '{"qty": "{order_qty}"}'  # WATI's {{Order Qty}}, renamed the same way
    assert edge(doc, "main_question-Qty", "next") == "main_webhook-Crm"
    assert edge(doc, "main_webhook-Crm", "success") == "main_sets-Clear"

    assert nodes["main_sets-Clear"]["type"] == "product_list"
    unsupported = nodes["main_assign-Team"]
    assert unsupported["unsupported"] == "AssignTeam" and unsupported["wati_raw"]["team"] == "Sales"

    assert report.found == {"Message": 2, "InteractiveButtons": 1, "InteractiveList": 1, "Question": 1,
                            "Webhook": 1, "InteractiveProductList": 1, "AssignTeam": 1}
    notes = " ".join(report.adapted)
    assert "Chosen Language → chosen_language" in notes and "Order Qty → order_qty" in notes
    assert "sent 2 messages" in notes
    warnings = " ".join(report.warnings)
    assert "Authorization header" in warnings
    assert "no catalogue" in warnings
    assert '"AssignTeam"' in warnings
    assert "1 connection" in warnings  # the edge from a row that is not in the file


def test_the_report_never_repeats_the_credential_itself():
    """Pointing out that a secret is stored is the point; printing it again is not."""
    _, report = import_any(wati_export())
    assert "placeholder-not-a-real-token" not in str(report.to_dict())


def test_an_imported_flow_is_checked_like_any_other():
    doc, _ = import_any(wati_export())
    issues = validate_graph(doc)
    fails = [i.message for i in issues if i.level == "fail"]
    warns = [i.message for i in issues if i.level == "warn"]
    assert any("came from WATI as a kind of step" in m for m in fails)  # the AssignTeam step
    assert any('"Status of my order"' in m and "not connected" in m for m in fails)  # a real dead end
    assert any('"Hindi"' in m and "not connected" in m for m in fails)
    assert not any("no HI text" in m or "no GU text" in m for m in warns)  # one text per step
    assert not any("built-in bot" in m for m in warns)  # sharing a word with a menu button is fine


@pytest.mark.asyncio
async def test_an_imported_flow_walks_like_it_did_in_wati():
    doc, _ = import_any(wati_export())
    g = schema.parse(doc)
    contact = {"customer_name": "Asha"}

    async def fake_crm(method, url, headers, body):
        assert body == {"qty": "250"}
        return {"ok": True, "status": 200, "error": "", "data": {}}

    turn = await engine.start(g, system=contact)
    assert turn.messages[0].text.startswith("Hi, Asha")
    assert turn.messages[1].kind == "media" and turn.messages[1].media["caption"] == "Our range"
    assert turn.messages[2].header == "Language" and turn.messages[2].footer == "Pick one"
    assert turn.messages[2].options.titles() == ["English", "Hindi"]

    turn = await engine.advance(g, turn.state, "English", system=contact)
    menu = turn.messages[0]
    assert [s["title"] for s in menu.sections] == ["Order", "Order status"]

    turn = await engine.advance(g, turn.state, "I want to order", system=contact)
    turn = await engine.advance(g, turn.state, "50", system=contact)  # below WATI's minimum of 100
    assert turn.stopped == "not_understood"
    assert turn.messages[0].text == "Please send a number of 100 or more."

    turn = await engine.advance(g, turn.state, "250", system=contact, fetch=fake_crm)
    assert turn.state.vars["order_qty"] == "250"
    assert turn.messages[-1].kind == "product_list" and turn.messages[-1].header == "Clear"


@pytest.mark.asyncio
async def test_typing_instead_of_tapping_takes_the_default_route():
    """WATI's default route: the customer writes something instead of pressing a button."""
    doc, _ = import_any(wati_export())
    g = schema.parse(doc)
    turn = await engine.start(g)
    turn = await engine.advance(g, turn.state, "can someone call me?")
    assert turn.messages[0].text == "Our team will help you."
    assert turn.state.vars["chosen_language"] == "can someone call me?"


def test_placeholders_the_editor_suggests_never_crash():
    """{sys.language} and {sys.customer_name} used to crash: str.format read them as attributes."""
    state = engine.RunState(vars={"sys.customer_name": "Asha", "company": "Acme"}, language="gu")
    assert engine.render("Hi {sys.customer_name} of {company}, lang {sys.language}", state) == \
        "Hi Asha of Acme, lang gu"
    assert engine.render("unknown {nobody} and {0} and {a!r} stay put", state) == \
        "unknown {nobody} and {0} and {a!r} stay put"


@pytest.mark.parametrize("junk", [[], "text", {"hello": "world"}, {"flowNodes": []}])
def test_something_that_is_not_a_workflow_is_refused_with_a_reason(junk):
    with pytest.raises(NotAWorkflow):
        import_any(junk)


# ---------------- over the API ----------------
async def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_import_export_and_import_again_round_trips():
    async with await client() as c:
        r = await c.post("/admin/api/workflows/import", json={"data": wati_export()}, headers=H)
        assert r.status_code == 200, r.text
        first = r.json()
        assert first["title"] == "Shop bot"
        assert first["report"]["source"] == "wati"
        assert first["doc"]["start"] == "main_message-Start"

        exported = (await c.get(f"/admin/api/workflows/{first['key']}/export", headers=H)).json()
        assert exported["format"] == "order-bot-workflow" and exported["title"] == "Shop bot"

        again = (await c.post("/admin/api/workflows/import", json={"data": exported}, headers=H)).json()
        assert again["report"]["source"] == "native"
        assert again["key"] != first["key"]  # an import never overwrites an existing workflow
        assert again["doc"]["nodes"] == first["doc"]["nodes"] and again["doc"]["edges"] == first["doc"]["edges"]


@pytest.mark.asyncio
async def test_duplicating_a_workflow():
    async with await client() as c:
        first = (await c.post("/admin/api/workflows/import", json={"data": wati_export()}, headers=H)).json()
        copy = (await c.post(f"/admin/api/workflows/{first['key']}/duplicate", headers=H)).json()
        assert copy["title"] == "Copy of Shop bot" and copy["key"] != first["key"]
        assert copy["doc"]["nodes"] == first["doc"]["nodes"]


@pytest.mark.asyncio
async def test_importing_something_else_says_what_was_expected():
    async with await client() as c:
        r = await c.post("/admin/api/workflows/import", json={"data": {"hello": "world"}}, headers=H)
    assert r.status_code == 400
    assert "flowNodes" in r.json()["detail"]


@pytest.mark.asyncio
async def test_the_test_chat_fills_in_the_customers_name():
    async with await client() as c:
        first = (await c.post("/admin/api/workflows/import", json={"data": wati_export()}, headers=H)).json()
        r = (await c.post(f"/admin/api/workflows/{first['key']}/simulate",
                          json={"contact_name": "Asha"}, headers=H)).json()
    assert r["messages"][0]["text"].startswith("Hi, Asha")
