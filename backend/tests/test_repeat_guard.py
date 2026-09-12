"""A conversation going round in circles: the same order asked about again and again.

The status will not change because it was asked a third time, so the bot says so once and stops -
either ending the conversation or handing it to a person, whichever Settings says.
"""
from __future__ import annotations

import pytest

from app.config import apply_overrides, get_settings, overrides
from app.db import session_scope
from app.models import AgentHandover, Session
from app.services import handover
from tests.flow import GUJ, MEHTA, SHREE, open_menu, send, tap


@pytest.fixture(autouse=True)
async def _fresh():
    """Every test starts with the bot in charge and nothing counted."""
    before = overrides()
    yield
    apply_overrides(before)
    async with session_scope() as db:
        for phone in (SHREE, MEHTA):
            s = await db.get(Session, phone)
            if s is not None:
                await db.delete(s)
            row = await db.get(AgentHandover, phone)
            if row is not None:
                await db.delete(row)


def setting(**values):
    apply_overrides({**overrides(), **values})


async def ask_status(phone: str, so: str):
    """Ask for one order's status the way a customer does, from the main menu."""
    await open_menu(phone)
    await tap(phone, "Order status")
    return await send(phone, so)


async def test_the_third_time_the_same_order_is_asked_about_the_bot_stops():
    setting(repeat_limit=3, repeat_action="end")
    first = await ask_status(SHREE, "45231")
    assert first.outcome == "status_delivered" and "In Production" in first.reply_text

    second = await send(SHREE, "45231")
    assert second.outcome == "status_delivered"

    third = await send(SHREE, "45231")
    assert third.outcome == "repeat_stopped"
    assert "asked about this a few times" in third.reply_text
    assert "In Production" not in third.reply_text  # the status is not repeated a third time

    # the conversation is over: the next message is greeted as a fresh one
    again = await send(SHREE, "hi")
    assert again.outcome == "ask_language"


async def test_a_different_order_starts_the_count_again():
    """Someone checking two orders in turn is working, not going in circles."""
    setting(repeat_limit=3, repeat_action="end")
    await ask_status(SHREE, "45231")
    for so in ("45232", "45231", "45232", "45231", "45232"):
        got = await send(SHREE, so)
        assert got.outcome == "status_delivered", (so, got.outcome)


async def test_the_limit_can_be_switched_off():
    setting(repeat_limit=0)
    await ask_status(SHREE, "45231")
    for _ in range(4):
        again = await send(SHREE, "45231")
    assert again.outcome == "status_delivered"


async def test_it_can_hand_the_chat_to_a_person_instead_of_ending_it():
    setting(repeat_limit=2, repeat_action="person")
    await ask_status(SHREE, "45231")
    stopped = await send(SHREE, "45231")
    assert stopped.outcome == "repeat_stopped"

    async with session_scope() as db:
        assert await handover.active(db, SHREE) is not None  # a person has it now

    quiet = await send(SHREE, "45231")
    assert quiet.outcome == "with_agent" and not quiet.reply_text  # and the bot says nothing more


async def test_a_workflow_going_round_in_circles_stops_too():
    """A question whose "anything else" comes back to itself: three identical answers and the bot
    stops rather than asking a fourth time."""
    from app.services.workflow import store
    from tests.test_workflow_runtime import doc_of, en, publish, run_of

    setting(repeat_limit=3, repeat_action="end")
    loop = doc_of(
        [{"id": "ask", "type": "question", "text": en("Which pouch size?"), "store": "size",
          "input": {"kind": "buttons", "options": [{"value": "small", "label": en("Small")}]}},
         {"id": "done", "type": "end", "text": en("Thank you.")}],
        [("ask", "opt:small", "done"), ("ask", "default", "ask")], "ask",
        triggers={"keywords": [{"text": "sizes", "match": "exact"}]})
    key = await publish("Pouch sizes", loop)
    try:
        assert (await send(GUJ, "sizes")).outcome == "workflow"
        first = await send(GUJ, "something else")
        second = await send(GUJ, "something else")
        assert first.outcome == second.outcome == "workflow"
        third = await send(GUJ, "something else")
        assert "asked about this a few times" in (third.reply_text or "")
        run = await run_of(GUJ)
        assert run is None or run.status == "done"  # and they are let out of the workflow
    finally:
        await store.remove(key)


async def test_the_setting_is_one_the_owner_can_change():
    from app.services import settings_store

    assert "repeat_limit" in settings_store.FIELDS and "repeat_action" in settings_store.FIELDS
    errors = settings_store.validate({"repeat_limit": 25})[1]
    assert "repeat_limit" in errors  # 20 in a row is already absurd
    assert settings_store.validate({"repeat_action": "sideways"})[1]
    assert get_settings().repeat_limit == 3  # the default: three identical requests
