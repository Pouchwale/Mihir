"""The workflow builder's API: create, edit, publish, roll back, and walk a draft in the simulator.

The boundary this feature was designed around is asserted here too: simulating a workflow must not
create a session, write a message log, or reach WATI.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import session_scope
from app.main import app
from app.models import MessageLog, Session
from app.services.wati import wati
from sqlalchemy import func, select

H = {"X-Admin-Key": "test-admin"}


async def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def make(c, title="Onboarding", example="onboarding") -> str:
    r = await c.post("/admin/api/workflows", json={"title": title, "from_example": example}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["key"]


@pytest.mark.asyncio
async def test_the_admin_key_is_required():
    async with await client() as c:
        assert (await c.get("/admin/api/workflows")).status_code == 401


@pytest.mark.asyncio
async def test_a_new_workflow_can_start_from_the_shipped_example():
    async with await client() as c:
        examples = (await c.get("/admin/api/workflows/examples", headers=H)).json()
        assert any(e["key"] == "onboarding" for e in examples)

        key = await make(c)
        got = (await c.get(f"/admin/api/workflows/{key}", headers=H)).json()
        assert got["doc"]["start"] == "n_lang"
        # publishable as it stands; it only warns that nothing starts it until the owner says how
        assert [i for i in got["issues"] if i["level"] == "fail"] == []
        assert got["published_version"] is None  # ...but not live until asked


@pytest.mark.asyncio
async def test_a_blank_workflow_is_not_publishable_until_it_says_something():
    async with await client() as c:
        key = await make(c, "Blank", example="")
        r = await c.post(f"/admin/api/workflows/{key}/publish", json={}, headers=H)
        assert r.status_code == 422
        issues = r.json()["detail"]["issues"]
        assert any("no English text" in i["message"] for i in issues)


@pytest.mark.asyncio
async def test_publish_then_roll_back_to_an_earlier_version():
    async with await client() as c:
        key = await make(c)
        first = (await c.post(f"/admin/api/workflows/{key}/publish", json={"notes": "first"}, headers=H)).json()
        v1 = first["published_version"]

        doc = (await c.get(f"/admin/api/workflows/{key}", headers=H)).json()["doc"]
        doc["nodes"][0]["text"]["en"] = "A completely different greeting."
        saved = (await c.put(f"/admin/api/workflows/{key}/draft", json={"doc": doc}, headers=H)).json()
        assert saved["version"] != v1, "editing a published version must start a new draft"
        v2 = (await c.post(f"/admin/api/workflows/{key}/publish", json={}, headers=H)).json()["published_version"]

        rolled = (await c.post(f"/admin/api/workflows/{key}/publish", json={"version": v1}, headers=H)).json()
        assert rolled["published_version"] == v1

        versions = (await c.get(f"/admin/api/workflows/{key}/versions", headers=H)).json()
        assert {v["version"] for v in versions} == {v1, v2}
        assert next(v for v in versions if v["version"] == v1)["is_live"] is True
        # the older text is intact - publishing never rewrote it
        old = (await c.get(f"/admin/api/workflows/{key}/versions/{v1}", headers=H)).json()
        assert "different greeting" not in old["doc"]["nodes"][0]["text"]["en"]


@pytest.mark.asyncio
async def test_two_tabs_cannot_silently_overwrite_each_other():
    async with await client() as c:
        key = await make(c)
        doc = (await c.get(f"/admin/api/workflows/{key}", headers=H)).json()["doc"]
        r = await c.put(f"/admin/api/workflows/{key}/draft",
                        json={"doc": doc, "base_version": 999}, headers=H)
        assert r.status_code == 409
        assert "while you were editing" in r.json()["detail"]


@pytest.mark.asyncio
async def test_walking_a_draft_sends_nothing_and_leaves_no_trace():
    """The whole safety argument for this phase: testing a workflow cannot reach a customer."""
    async with await client() as c:
        key = await make(c)
        async with session_scope() as db:
            sessions_before = await db.scalar(select(func.count(Session.phone_e164)))
            logs_before = await db.scalar(select(func.count(MessageLog.id)))
        outbox_before = len(wati.outbox)

        r = (await c.post(f"/admin/api/workflows/{key}/simulate", json={}, headers=H)).json()
        assert r["messages"][0]["options"]["kind"] == "buttons"
        state = r["state"]
        for said in ["English", "Shree Industries", "Rajesh Patel", "That's right"]:
            r = (await c.post(f"/admin/api/workflows/{key}/simulate",
                              json={"text": said, "state": state}, headers=H)).json()
            state = r["state"]
        assert r["stopped"] == "end"
        # the customer's answers; sys.* are the stand-in contact details the test chat supplies
        answers = {k: v for k, v in state["vars"].items() if not k.startswith("sys.")}
        assert answers == {"company": "Shree Industries", "person": "Rajesh Patel"}

        async with session_scope() as db:
            assert await db.scalar(select(func.count(Session.phone_e164))) == sessions_before
            assert await db.scalar(select(func.count(MessageLog.id))) == logs_before
        assert len(wati.outbox) == outbox_before, "a dry run must not reach WATI"


@pytest.mark.asyncio
async def test_simulating_the_published_version_ignores_later_edits():
    async with await client() as c:
        key = await make(c)
        await c.post(f"/admin/api/workflows/{key}/publish", json={}, headers=H)
        doc = (await c.get(f"/admin/api/workflows/{key}", headers=H)).json()["doc"]
        doc["nodes"][0]["text"]["en"] = "DRAFT ONLY"
        await c.put(f"/admin/api/workflows/{key}/draft", json={"doc": doc}, headers=H)

        draft = (await c.post(f"/admin/api/workflows/{key}/simulate", json={"use": "draft"}, headers=H)).json()
        live = (await c.post(f"/admin/api/workflows/{key}/simulate", json={"use": "published"}, headers=H)).json()
        assert draft["messages"][0]["text"] == "DRAFT ONLY"
        assert live["messages"][0]["text"] != "DRAFT ONLY"


@pytest.mark.asyncio
async def test_validate_answers_without_saving_anything():
    async with await client() as c:
        bad = {"schema": 1, "start": "q",
               "nodes": [{"id": "q", "type": "question", "title": "Pick",
                          "text": {"en": "Pick"}, "input": {"kind": "buttons", "options": [
                              {"value": "a", "label": {"en": "Order status"}}]}}],
               "edges": []}
        r = (await c.post("/admin/api/workflows/validate", json={"doc": bad}, headers=H)).json()
        assert r["ok"] is False
        assert any("not connected to anything" in i["message"] for i in r["issues"])


@pytest.mark.asyncio
async def test_renaming_and_deleting():
    async with await client() as c:
        key = await make(c, "Temporary")
        await c.patch(f"/admin/api/workflows/{key}", json={"title": "Renamed"}, headers=H)
        assert (await c.get(f"/admin/api/workflows/{key}", headers=H)).json()["title"] == "Renamed"
        assert (await c.delete(f"/admin/api/workflows/{key}", headers=H)).status_code == 200
        assert (await c.get(f"/admin/api/workflows/{key}", headers=H)).status_code == 404


@pytest.mark.asyncio
async def test_two_workflows_with_the_same_name_get_different_keys():
    async with await client() as c:
        a = await make(c, "Support", example="")
        b = await make(c, "Support", example="")
        assert a != b
