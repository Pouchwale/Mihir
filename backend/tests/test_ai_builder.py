"""The AI assistant that builds, changes and reviews workflows - with Groq stood in for by respx, so
no key is needed and nothing leaves the machine."""
from __future__ import annotations

import asyncio
import json

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.config import apply_overrides, overrides
from app.main import app
from app.services.ai import blueprint as bpm
from app.services.ai import builder
from app.services.ai import groq_client as G
from app.services.workflow.validate import validate_graph

H = {"X-Admin-Key": "test-admin"}
GROQ = "https://api.groq.com/openai/v1/chat/completions"


@pytest.fixture(autouse=True)
def _breaker():
    G.breaker_reset()
    yield
    G.breaker_reset()


@pytest.fixture
def groq_key():
    before = overrides()
    apply_overrides({**before, "groq_api_key": "test-key"})
    yield
    apply_overrides(before)


def ok(obj: dict, tokens: int = 120) -> Response:
    return Response(200, json={"choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}],
                               "usage": {"total_tokens": tokens}})


def by_schema(answers: dict[str, list], seen: list | None = None):
    """Answer each call by the name of the shape it asked for: outline, steps, patch, review."""

    def reply(request):
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        fmt = body["response_format"]
        name = fmt["json_schema"]["name"] if fmt["type"] == "json_schema" else "object"
        return ok(answers[name].pop(0))

    return reply


# ---------------- blueprint helpers ----------------
def T(en: str) -> dict:
    return {"en": en, "hi": "", "gu": ""}


def step(sid: str, kind: str, **kw) -> dict:
    s = {"id": sid, "type": kind, "title": sid.replace("_", " ").title(), "text": None, "next": None,
         "on_fail": None, "answer": None, "branches": [], "else_next": None, "details": None}
    s.update(kw)
    return s


def answer(kind: str, check: str = "any", **kw) -> dict:
    a = {"kind": kind, "check": check, "save_as": None, "choices": [], "list_button": None, "anything_else": None,
         "not_valid": None, "retries": 0, "wrong_answer_text": None}
    a.update(kw)
    return a


def choice(en: str, nxt: str | None) -> dict:
    return {"label": T(en), "description": None, "next": nxt}


def details(**kw) -> dict:
    return bpm._details(**kw)


def small_steps(talk_to: str = "to_team", on_fail: str | None = "thanks") -> list[dict]:
    return [
        step("ask", "question", text=T("Would you like a sample kit?"),
             answer=answer("buttons", choices=[choice("Yes please", "thanks"), choice("Talk to sales", talk_to)])),
        step("to_team", "assign", on_fail=on_fail, details=details(assign_to="team", teams=["Sales"])),
        step("thanks", "end", text=T("Thank you!")),
    ]


def outline(ids: list[tuple[str, str]]) -> dict:
    return {"title": "Sample kit", "summary": "Offers a sample kit.", "three_languages": False,
            "triggers": {"keywords": ["sample"], "menu_label": None, "new_numbers": False}, "start": ids[0][0],
            "sections": [{"name": "Main", "steps": [{"id": i, "type": t, "title": i, "purpose": ""} for i, t in ids]}]}


# ---------------- compiling ----------------
def test_every_step_type_compiles_into_a_publishable_workflow():
    bp = {"three_languages": False, "start": "lang", "triggers": None, "steps": [
        step("lang", "question", text=T("Choose a language"), next="menu", answer=answer("language")),
        step("menu", "question", text=T("How can we help?"),
             answer=answer("list", list_button="Choose", choices=[
                 choice("Track order", "ask_so"), choice("Ask a question", "faq"),
                 choice("Talk to us", "to_team"), choice("Order menu", "back")])),
        step("ask_so", "question", text=T("Your SO number?"), next="check",
             answer=answer("text", "so", save_as="order", retries=2)),
        step("check", "condition", else_next="note",
             branches=[{"label": "Dispatched", "var": "order_status", "op": "eq", "value": "Dispatched", "next": "done"}]),
        step("note", "remember", next="tag", details=details(remember=[{"name": "last_so", "value": "{order}"}],
                                                             save_on_contact=True)),
        step("tag", "tag", next="status", details=details(tags=["tracked"])),
        step("status", "chat_status", next="tmpl", details=details(status="pending")),
        step("tmpl", "template", next="pause", on_fail="done", details=details(template_name="order_update")),
        step("pause", "wait", next="api", details=details(seconds=5)),
        step("api", "call_api", next="done", on_fail="done", details=details(url="https://example.com/hook", method="POST")),
        step("faq", "ai_reply", next="done", on_fail="to_team", details=details(faq="We make stand-up pouches. MOQ 500.")),
        step("to_team", "assign", on_fail="done", details=details(assign_to="team", teams=["Sales"])),
        step("back", "go_to", details=details(workflow="@order-status")),
        step("done", "end", text=T("Thanks!")),
    ]}
    doc, notes = bpm.compile_blueprint(bp)
    assert notes == []
    assert [i.message for i in validate_graph(doc) if i.level == "fail"] == []
    types = {n["id"]: n["type"] for n in doc["nodes"]}
    assert types == {"lang": "question", "menu": "question", "ask_so": "question", "check": "condition",
                     "note": "set_var", "tag": "tags", "status": "chat_status", "tmpl": "template", "pause": "delay",
                     "api": "api_request", "faq": "ai_reply", "to_team": "assign", "back": "jump", "done": "end"}
    by_id = {n["id"]: n for n in doc["nodes"]}
    assert by_id["ask_so"]["validate"] == {"type": "lookup", "source": "so"} and by_id["ask_so"]["store"] == "order"
    assert by_id["note"]["to_contact"] is True
    routes = {(e["from"], e["port"]): e["to"] for e in doc["edges"]}
    assert routes[("menu", "opt:track_order")] == "ask_so" and routes[("check", "else")] == "note"
    assert routes[("faq", "unsure")] == "to_team" and ("to_team", "next") not in routes
    assert {routes[("lang", f"opt:{c}")] for c in ("en", "hi", "gu")} == {"menu"}
    assert doc["settings"]["multilingual"] is False


def test_a_choice_without_its_own_next_follows_the_step_and_unknown_targets_are_reported():
    s = small_steps()
    s[0]["answer"]["choices"][0]["next"] = None
    s[0]["next"] = "thanks"
    s[0]["answer"]["choices"][1]["next"] = "nowhere"
    doc, notes = bpm.compile_blueprint({"start": "ask", "steps": s})
    routes = {(e["from"], e["port"]): e["to"] for e in doc["edges"]}
    assert routes[("ask", "opt:yes_please")] == "thanks"
    assert ("ask", "opt:talk_to_sales") not in routes
    assert notes == ['"Ask" leads to "nowhere", which is not one of the steps.']


def test_a_workflow_goes_to_a_blueprint_and_back_keeping_ids_places_and_what_the_blueprint_cannot_say():
    doc, _ = bpm.compile_blueprint({"start": "ask", "steps": small_steps()})
    ask = next(n for n in doc["nodes"] if n["id"] == "ask")
    ask["x"], ask["y"] = 999, 555
    ask["input"]["options"][0]["synonyms"] = ["haan"]
    ask["header"] = T("Samples")
    again, notes = bpm.compile_blueprint(bpm.doc_to_blueprint(doc), doc)
    assert notes == []
    back = next(n for n in again["nodes"] if n["id"] == "ask")
    assert (back["x"], back["y"]) == (999, 555)
    assert back["input"]["options"][0]["synonyms"] == ["haan"] and back["header"] == T("Samples")
    assert {(e["from"], e["port"], e["to"]) for e in again["edges"]} == {(e["from"], e["port"], e["to"]) for e in doc["edges"]}


def test_a_patch_replaces_adds_and_removes_steps_by_id():
    bp = {"start": "ask", "steps": small_steps()}
    patched = bpm.apply_patch(bp, {"summary": "", "title": None, "start": None, "three_languages": None, "triggers": None,
                                   "upsert": [step("thanks", "end", text=T("Bye!")), step("extra", "end")],
                                   "remove": ["to_team"]})
    assert [s["id"] for s in patched["steps"]] == ["ask", "thanks", "extra"]
    assert patched["steps"][1]["text"] == T("Bye!")
    assert [s["id"] for s in bp["steps"]] == ["ask", "to_team", "thanks"]  # the original is untouched


# ---------------- building with Groq ----------------
async def test_a_new_workflow_is_planned_then_written_then_checked(groq_key):
    stages: list[str] = []

    async def progress(stage, wait=None):
        stages.append(stage)

    answers = {"outline": [outline([("ask", "question"), ("to_team", "assign"), ("thanks", "end")])],
               "steps": [{"steps": small_steps()}]}
    seen: list = []
    with respx.mock() as mock:
        mock.post(GROQ).mock(side_effect=by_schema(answers, seen))
        got = await builder.build("A sample kit offer that can hand over to sales", progress=progress)
    assert got["mode"] == "new" and got["title"] == "Sample kit"
    assert [i for i in got["issues"] if i["level"] == "fail"] == []
    assert {n["id"] for n in got["doc"]["nodes"]} == {"ask", "to_team", "thanks"}
    assert got["doc"]["settings"]["triggers"]["keywords"] == [{"text": "sample", "match": "exact"}]
    assert got["tokens"] == 240
    assert stages == ["Planning the workflow", "Writing the steps (1 of 1)"]
    first = seen[0]
    assert first["model"] == "openai/gpt-oss-120b" and first["response_format"]["json_schema"]["strict"] is True
    assert seen[0]["messages"][0]["role"] == "system" and "WhatsApp limits" in seen[0]["messages"][0]["content"]


async def test_what_the_checks_find_goes_back_to_the_ai_to_fix(groq_key):
    broken = small_steps(talk_to="nowhere", on_fail=None)
    fix = {"summary": "", "title": None, "start": None, "three_languages": None, "triggers": None, "remove": [],
           "upsert": [small_steps()[0], small_steps()[1]]}
    answers = {"outline": [outline([("ask", "question"), ("to_team", "assign"), ("thanks", "end")])],
               "steps": [{"steps": broken}], "patch": [fix]}
    seen: list = []
    with respx.mock() as mock:
        mock.post(GROQ).mock(side_effect=by_schema(answers, seen))
        got = await builder.build("sample kit", progress=_quiet)
    assert [i for i in got["issues"] if i["level"] == "fail"] == []
    repair = seen[-1]["messages"][-1]["content"]
    assert 'leads to "nowhere"' in repair and '"Talk to sales"' in repair


async def test_changing_a_workflow_sends_it_as_it_is_and_keeps_everything_untouched(groq_key):
    base, _ = bpm.compile_blueprint({"start": "ask", "steps": small_steps()})
    for n in base["nodes"]:
        n["x"] = 777
    patch = {"summary": "Friendlier goodbye", "title": None, "start": None, "three_languages": None, "triggers": None,
             "remove": [], "upsert": [step("thanks", "end", text=T("Thank you, see you soon!"))]}
    seen: list = []
    with respx.mock() as mock:
        mock.post(GROQ).mock(side_effect=by_schema({"patch": [patch]}, seen))
        got = await builder.build("make the goodbye friendlier", base=base, history=["add a sample offer"],
                                  progress=_quiet)
    assert got["mode"] == "edit" and got["summary"] == "Friendlier goodbye"
    assert len(seen) == 1
    sent = seen[0]["messages"][-1]["content"]
    assert '"id": "to_team"' in sent and "add a sample offer" in sent
    nodes = {n["id"]: n for n in got["doc"]["nodes"]}
    assert set(nodes) == {"ask", "to_team", "thanks"} and all(n["x"] == 777 for n in nodes.values())
    assert nodes["thanks"]["text"]["en"] == "Thank you, see you soon!"


async def _quiet(stage, wait=None):
    return None


# ---------------- Groq's answers and refusals ----------------
async def test_no_key_is_said_plainly():
    with pytest.raises(G.AiUnavailable, match="Groq API key"):
        await G.chat_json([], {}, name="x")


async def test_a_refused_schema_is_retried_less_strictly(groq_key):
    seen: list = []

    def reply(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return Response(400, json={"error": {"message": "invalid JSON schema for response_format"}})
        return ok({"a": 1})

    with respx.mock() as mock:
        mock.post(GROQ).mock(side_effect=reply)
        got, _ = await G.chat_json([{"role": "user", "content": "hi"}], {"type": "object"}, name="x")
    assert got == {"a": 1}
    assert [b["response_format"]["json_schema"]["strict"] for b in seen] == [True, False]


async def test_the_free_limit_says_when_it_is_ready_again(groq_key):
    with respx.mock() as mock:
        mock.post(GROQ).mock(return_value=Response(429, headers={"retry-after": "7"}, json={"error": {"message": "Rate limit"}}))
        with pytest.raises(G.AiUnavailable) as e:
            await G.chat_json([], {}, name="x")
    assert "ready again in 8 s" in str(e.value) and e.value.retry_after == 7


async def test_a_build_waits_out_the_per_minute_limit_and_says_so(groq_key):
    stages: list = []

    async def progress(stage, wait=None):
        stages.append((stage, wait))

    with respx.mock() as mock:
        mock.post(GROQ).mock(side_effect=[Response(429, headers={"retry-after": "0"}), ok({"a": 1})])
        got, _ = await G.chat_json([], {}, name="x", progress=progress)
    assert got == {"a": 1} and stages == [("Waiting for Groq's per-minute limit", 0.0)]


def test_groq_reset_headers_are_read():
    assert G._retry_after(Response(429, headers={"x-ratelimit-reset-tokens": "1m2.5s"})) == 62.5
    assert G._retry_after(Response(429, headers={"x-ratelimit-reset-tokens": "7.66s"})) == 7.66
    assert G._retry_after(Response(429)) is None


async def test_a_refused_key_is_named(groq_key):
    with respx.mock() as mock:
        mock.post(GROQ).mock(return_value=Response(401, json={"error": {"message": "Invalid API Key"}}))
        with pytest.raises(G.AiUnavailable, match="refused the API key"):
            await G.chat_json([], {}, name="x")


# ---------------- the API ----------------
async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_without_a_key_the_assistant_says_how_to_get_one():
    async with await _client() as c:
        assert (await c.get("/admin/api/workflows/ai/status", headers=H)).json()["configured"] is False
        key = (await c.post("/admin/api/workflows", json={"title": "AI no key"}, headers=H)).json()["key"]
        r = await c.post(f"/admin/api/workflows/{key}/ai/build", json={"instruction": "a sample flow"}, headers=H)
    assert r.status_code == 503 and "Groq API key" in r.json()["detail"]


async def test_building_through_the_api_is_a_job_the_editor_polls(groq_key):
    answers = {"outline": [outline([("ask", "question"), ("to_team", "assign"), ("thanks", "end")])],
               "steps": [{"steps": small_steps()}]}
    async with await _client() as c:
        key = (await c.post("/admin/api/workflows", json={"title": "AI via API"}, headers=H)).json()["key"]
        with respx.mock() as mock:
            mock.post(GROQ).mock(side_effect=by_schema(answers))
            job = (await c.post(f"/admin/api/workflows/{key}/ai/build",
                                json={"instruction": "sample kit offer", "doc": None}, headers=H)).json()
            assert job["status"] == "running"
            for _ in range(100):
                got = (await c.get(f"/admin/api/workflows/ai/jobs/{job['id']}", headers=H)).json()
                if got["status"] != "running":
                    break
                await asyncio.sleep(0.02)
        draft = (await c.get(f"/admin/api/workflows/{key}", headers=H)).json()["doc"]
    assert got["status"] == "done", got
    assert {n["id"] for n in got["result"]["doc"]["nodes"]} == {"ask", "to_team", "thanks"}
    assert len(draft["nodes"]) == 1  # a preview only: nothing saved until the owner applies it


async def test_create_with_ai_saves_a_new_draft(groq_key):
    answers = {"outline": [outline([("ask", "question"), ("to_team", "assign"), ("thanks", "end")])],
               "steps": [{"steps": small_steps()}]}
    async with await _client() as c:
        with respx.mock() as mock:
            mock.post(GROQ).mock(side_effect=by_schema(answers))
            job = (await c.post("/admin/api/workflows/ai/create", json={"instruction": "sample kit offer"}, headers=H)).json()
            for _ in range(100):
                got = (await c.get(f"/admin/api/workflows/ai/jobs/{job['id']}", headers=H)).json()
                if got["status"] != "running":
                    break
                await asyncio.sleep(0.02)
        assert got["status"] == "done", got
        saved = (await c.get(f"/admin/api/workflows/{got['result']['key']}", headers=H)).json()
    assert saved["title"] == "Sample kit" and saved["published_version"] is None
    assert {n["id"] for n in saved["doc"]["nodes"]} == {"ask", "to_team", "thanks"}


async def test_an_unknown_job_is_a_plain_404():
    async with await _client() as c:
        r = await c.get("/admin/api/workflows/ai/jobs/nope", headers=H)
    assert r.status_code == 404


async def test_a_review_keeps_findings_about_real_steps(groq_key):
    doc, _ = bpm.compile_blueprint({"start": "ask", "steps": small_steps()})
    review = {"summary": "Clear and short.", "findings": [
        {"step_id": "ask", "severity": "problem", "problem": "No way to say no.", "suggestion": "Add a No thanks button."},
        {"step_id": "ghost", "severity": "suggestion", "problem": "Greet by name.", "suggestion": ""},
        {"step_id": None, "severity": "problem", "problem": "   ", "suggestion": ""}]}
    async with await _client() as c:
        with respx.mock() as mock:
            mock.post(GROQ).mock(side_effect=by_schema({"review": [review]}))
            r = await c.post("/admin/api/workflows/any/ai/review", json={"doc": doc}, headers=H)
    got = r.json()
    assert r.status_code == 200 and got["summary"] == "Clear and short."
    assert [(f["step_id"], f["severity"]) for f in got["findings"]] == [("ask", "problem"), (None, "suggestion")]
