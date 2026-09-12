"""Your team replying to a customer from Live sessions, once a person has taken the chat."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import menus
from app.services.wati import wati
from tests.flow import MEHTA

H = {"X-Admin-Key": "test-admin"}


async def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def hand_over(c: AsyncClient, assignee: str = "sales@example.com") -> None:
    assert (await c.post(f"/admin/api/handovers/{MEHTA}", json={"assignee": assignee}, headers=H)).status_code == 200


async def hand_back(c: AsyncClient) -> None:
    await c.delete(f"/admin/api/handovers/{MEHTA}", headers=H)


async def test_while_the_bot_has_the_chat_there_is_nothing_to_send_with():
    async with await client() as c:
        await hand_back(c)  # the bot has it
        r = await c.post(f"/admin/api/sessions/{MEHTA}/reply", json={"text": "Hello from sales"}, headers=H)
    assert r.status_code == 409 and "Hand to a person" in r.json()["detail"]
    assert not [o for o in wati.outbox if o["phone"] == MEHTA]  # and nothing reached the customer


async def test_a_person_can_reply_and_the_chat_shows_it_as_theirs():
    async with await client() as c:
        await hand_over(c)
        r = await c.post(f"/admin/api/sessions/{MEHTA}/reply",
                         json={"text": "Your order ships tomorrow morning."}, headers=H)
        assert r.status_code == 200, r.text
        got = (await c.get(f"/admin/api/sessions/{MEHTA}", headers=H)).json()
        await hand_back(c)
    sent = [o for o in wati.outbox if o["phone"] == MEHTA]
    assert [o["text"] for o in sent] == ["Your order ships tomorrow morning."]
    last = got["messages"][-1]
    assert last["direction"] == "out" and last["outcome"] == "agent"
    assert last["text"] == "Your order ships tomorrow morning."


async def test_an_empty_or_over_long_message_is_refused_before_it_is_sent():
    async with await client() as c:
        await hand_over(c)
        blank = await c.post(f"/admin/api/sessions/{MEHTA}/reply", json={"text": "   "}, headers=H)
        huge = await c.post(f"/admin/api/sessions/{MEHTA}/reply", json={"text": "x" * (menus.BODY_MAX + 1)}, headers=H)
        await hand_back(c)
    assert blank.status_code == 400 and "Write something" in blank.json()["detail"]
    assert huge.status_code == 400 and str(menus.BODY_MAX) in huge.json()["detail"]
    assert not [o for o in wati.outbox if o["phone"] == MEHTA]


async def test_handing_the_chat_back_takes_the_reply_box_away_again():
    async with await client() as c:
        await hand_over(c)
        assert (await c.post(f"/admin/api/sessions/{MEHTA}/reply", json={"text": "One moment please."},
                             headers=H)).status_code == 200
        await hand_back(c)
        after = await c.post(f"/admin/api/sessions/{MEHTA}/reply", json={"text": "And another thing"}, headers=H)
    assert after.status_code == 409
    assert [o["text"] for o in wati.outbox if o["phone"] == MEHTA] == ["One moment please."]
