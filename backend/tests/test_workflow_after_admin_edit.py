"""The owner's question: "when I change the workflow, will it still work?"

Every message is rewritten and every button renamed, in all three languages, driven from the specs
themselves so a newly added message is covered automatically. Then the very same conversation driver
replays the whole chat and must reach the same Real Status by the same route.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db import session_scope
from app.models import Template, TemplateHistory
from app.services import intent, menus, replies, templates as T
from tests.flow import (BTN, L, MEHTA, SHREE, open_menu, run_full_conversation, send, step_of, tap,
                        titles, trace, voice)

MARKER = "Zyx"


# ---------------- generating edits from the specs ----------------
def mangled_template_text(spec: T.TemplateSpec, lang: str) -> str:
    """A rewritten message that is still valid: keeps every required placeholder, adds the marker."""
    parts = [f"{MARKER}-{spec.key}-{lang}"]
    parts += ["{" + p + "}" for p in sorted(spec.required)]
    optional = sorted(spec.allowed - spec.required)
    if optional:
        parts.append("{" + optional[0] + "}")
    parts.append(MARKER)
    text = " ".join(parts)
    return text[: spec.max_len]


def mangled_label_text(spec: T.LabelSpec, index: int, lang: str) -> str:
    """A renamed button. Letters only, unique per key, so it cannot look like an order code or
    collide with another button."""
    word = "Qx" + chr(ord("a") + index % 26) * 3 + lang
    if spec.placeholders:
        word += " " + " ".join("{" + p + "}" for p in sorted(spec.placeholders))
    return word


async def rewrite_everything() -> None:
    for spec in T.TEMPLATE_SPECS.values():
        for lang in T.LANGS:
            errs = await T.save_text("template", spec.key, lang, mangled_template_text(spec, lang))
            assert errs == [], f"{spec.key}/{lang}: {errs}"
    for i, spec in enumerate(T.LABEL_SPECS.values()):
        for lang in T.LANGS:
            errs = await T.save_text("label", spec.key, lang, mangled_label_text(spec, i, lang))
            assert errs == [], f"{spec.key}/{lang}: {errs}"


@pytest.fixture
async def clean_templates():
    async def wipe():
        async with session_scope() as db:
            await db.execute(delete(Template))
            await db.execute(delete(TemplateHistory))
        await T.load_from_db()

    await wipe()
    yield
    await wipe()


@pytest.fixture
async def rewritten(clean_templates):
    await rewrite_everything()
    yield MARKER


# ---------------- the edits themselves ----------------
@pytest.mark.asyncio
async def test_every_message_and_button_can_be_rewritten_in_all_three_languages(clean_templates):
    """117 edits today; adding a message or a button adds three more cases here for free."""
    await rewrite_everything()
    for spec in T.TEMPLATE_SPECS.values():
        for lang in T.LANGS:
            assert T.registry.is_overridden("template", spec.key, lang), spec.key
            assert MARKER in T.registry.text(spec.key, lang)
    for spec in T.LABEL_SPECS.values():
        for lang in T.LANGS:
            assert T.registry.is_overridden("label", spec.key, lang), spec.key


# ---------------- the conversation still works ----------------
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tap", "type"])
@pytest.mark.parametrize("lang_key", ["lang_en", "lang_hi", "lang_gu"])
async def test_the_conversation_is_unchanged_by_a_full_rewrite(clean_sessions, clean_templates, lang_key, mode):
    before = trace(await run_full_conversation(MEHTA, lang_key, mode))
    await send(MEHTA, "thanks")  # end the window so the next run starts clean

    await rewrite_everything()
    results = await run_full_conversation(MEHTA, lang_key, mode)

    # same route, same destination
    assert trace(results) == before
    assert "Ready for Dispatch" in results[-1].reply_text
    # and it really was the edited text that went out
    for r in results:
        for text in r.replies or []:
            assert MARKER in text, text


@pytest.mark.asyncio
async def test_every_renamed_button_is_still_understood(rewritten):
    """A tapped button arrives as its title, so the parser must recognise the new name - in any
    language, whatever the customer's current one."""
    for key, expected in T.LABEL_INTENTS.items():
        for lang in T.LANGS:
            title = menus.label(key, lang)
            assert intent.label_intent(title) == expected, (key, lang, title)
            assert intent.regex_parse(title).intent == expected, (key, lang, title)
            # people retype what they see, with different case and punctuation
            assert intent.regex_parse(title.upper()).intent == expected, (key, lang)
            assert intent.regex_parse(f"  {title}! ").intent == expected, (key, lang)


# what each renamed button must actually DO: (step to be in, outcome, step afterwards)
INTENT_BEHAVIOUR = {
    "lang_en": ("LANG", "menu", "MENU"),
    "lang_hi": ("LANG", "menu", "MENU"),
    "lang_gu": ("LANG", "menu", "MENU"),
    "order_status": ("MENU", "ask_so", "AWAIT_SO"),
    "change_language": ("MENU", "ask_language", "LANG"),
    "contact_us": ("MENU", "contact", "MENU"),
    "menu": ("AWAIT_SO", "menu", "MENU"),
    "another": ("DONE", "ask_so", "AWAIT_SO"),
    "my_orders": ("DONE", "ask_so", "AWAIT_SO"),
    "done": ("MENU", "bye", "START"),
    "yes": ("CONFIRM", "status_delivered", "DONE"),
    "no": ("CONFIRM", "ask_so", "AWAIT_SO"),
}


def test_the_behaviour_table_covers_every_button():
    """Fails the moment someone adds a menu button without saying what it should do."""
    assert set(INTENT_BEHAVIOUR) == set(T.LABEL_INTENTS)


async def _reach(step: str) -> None:
    if step == "LANG":
        await send(SHREE, "hi")
        return
    await send(SHREE, "hi")
    await tap(SHREE, menus.label("lang_en", "en"))
    if step == "MENU":
        return
    if step == "CONFIRM":
        with voice("my SO number is 45231"):
            await send(SHREE, audio=True)
        return
    if step == "DONE":
        await send(SHREE, "45231")
        return
    await tap(SHREE, menus.label("order_status", "en"))  # AWAIT_SO


@pytest.mark.asyncio
@pytest.mark.parametrize("key", list(INTENT_BEHAVIOUR))
async def test_every_renamed_button_still_drives_the_conversation(clean_sessions, rewritten, key):
    step, outcome, after = INTENT_BEHAVIOUR[key]
    await _reach(step)
    r = await tap(SHREE, menus.label(key, "en"))
    assert r.outcome == outcome, (key, r.outcome, r.reply_text)
    assert await step_of(SHREE) == after, (key, await step_of(SHREE))


# ---------------- button sets ----------------
SLOT_TRIGGER = {
    "main_menu": "menu", "contact_us": "contact_us", "so_none": "so_none", "result": "result",
    "not_found": "not_found", "bye": "bye", "voice_off": "voice_off", "ask_so": None, "ask_fg": None,
    "ask_fg_retry": None, "welcome_first": None,
}


def test_every_editable_button_slot_is_covered():
    editable = {k for k, s in T.BUTTON_SLOTS.items() if not s.fixed}
    assert editable == set(SLOT_TRIGGER), editable ^ set(SLOT_TRIGGER)


@pytest.mark.asyncio
async def test_button_sets_can_be_changed_and_still_render(clean_sessions, clean_templates):
    """Whatever an admin picks, the buttons that go out are the ones they chose, with their names."""
    choices = list(T.BUTTON_CHOICES)
    for i, (slot_key, slot) in enumerate(T.BUTTON_SLOTS.items()):
        if slot.fixed:
            continue
        wanted = choices[i % len(choices):][:2] or choices[:2]
        if len(wanted) < max(1, slot.min_count):
            wanted = choices[:max(1, slot.min_count)]
        assert await T.save_buttons(slot_key, wanted) == [], slot_key
        assert T.registry.buttons(slot_key) == wanted
        opts = menus.buttons_for(slot_key, "en")
        assert opts and opts.titles() == [menus.label(k, "en") for k in wanted]
        assert menus.validate(opts) == [], (slot_key, opts)


@pytest.mark.asyncio
async def test_the_flow_critical_buttons_cannot_be_emptied(clean_templates):
    for key, slot in T.BUTTON_SLOTS.items():
        if slot.fixed or not slot.min_count:
            continue
        assert T.validate_buttons(key, []) != [], key
        assert await T.save_buttons(key, []) != [], key
        assert T.registry.buttons(key), key  # unchanged


@pytest.mark.asyncio
async def test_fixed_buttons_stay_fixed_even_after_renaming(clean_sessions, rewritten):
    """The language question and the Yes/No confirmation must keep their buttons - the flow cannot
    continue without them - but their names are still the admin's to change."""
    for key, slot in T.BUTTON_SLOTS.items():
        if not slot.fixed:
            continue
        assert T.validate_buttons(key, ["done"]) != []
        assert T.registry.buttons(key) == list(slot.default)
    r = await send(SHREE, "hi")
    assert titles(r) == [menus.label(k, "en") for k in ("lang_en", "lang_hi", "lang_gu")]
    await tap(SHREE, menus.label("lang_en", "en"))
    with voice("my SO number is 45231"):
        r = await send(SHREE, audio=True)
    assert titles(r) == [menus.label("yes", "en"), menus.label("no", "en")]
    r = await tap(SHREE, menus.label("yes", "en"))
    assert r.outcome == "status_delivered"


# ---------------- bad edits ----------------
@pytest.mark.asyncio
async def test_a_broken_edit_is_refused_and_changes_nothing(clean_sessions, clean_templates):
    for spec in T.TEMPLATE_SPECS.values():
        if not spec.required:
            continue
        assert await T.save_text("template", spec.key, "en", "no placeholders here") != []
        assert not T.registry.is_overridden("template", spec.key, "en"), spec.key
    assert await T.save_text("template", "result", "en", "{nope} {real_status} {so_no}") != []
    assert await T.save_text("label", "done", "en", "SO 45240") != []
    # the bot is untouched
    r = await run_full_conversation(MEHTA, "lang_en", "tap")
    assert r[-1].outcome == "status_delivered"


@pytest.mark.asyncio
async def test_restoring_the_defaults_brings_the_original_words_back(clean_sessions, rewritten):
    for key in T.TEMPLATE_SPECS:
        await T.reset_key("template", key)
    for key in T.LABEL_SPECS:
        await T.reset_key("label", key)
    for key in T.TEMPLATE_SPECS:
        for lang in T.LANGS:
            assert T.registry.text(key, lang) == replies.DEFAULTS[key][lang], (key, lang)
    for key in T.LABEL_SPECS:
        for lang in T.LANGS:
            assert T.registry.label(key, lang) == menus.DEFAULT_LABELS[key][lang], (key, lang)
    results = await run_full_conversation(MEHTA, "lang_en", "tap")
    assert MARKER not in results[-1].reply_text
    assert titles(results[-1]) == BTN("result", "en")


@pytest.mark.asyncio
async def test_a_custom_reply_never_takes_over_a_menu_button(clean_sessions, clean_templates):
    """The bug this guards: a reply triggered by "order" used to swallow every Order status tap."""
    assert T.validate_custom(T.CustomReply(key="x", title="X", triggers=["order"], texts={"en": "hi"})) != []
    # a safe trigger works, and does not disturb the flow
    assert await T.save_custom(T.CustomReply(key="gst", title="GST", triggers=["gst rate"], texts={"en": "18 percent"})) == []
    await open_menu(SHREE)
    r = await send(SHREE, "what is the gst rate?")
    assert r.outcome == "custom" and "18 percent" in r.reply_text
    assert await step_of(SHREE) == "MENU"  # a custom reply never moves the customer
    r = await tap(SHREE, L("order_status", "en"))
    assert r.outcome == "ask_so"
