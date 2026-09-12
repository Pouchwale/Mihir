"""Walking a drawn workflow.

The engine is deliberately pure: it takes a graph plus the state of one conversation and returns the
messages to send. It touches no database, calls no WATI method and knows nothing about webhooks -
which is what lets the simulator run a workflow with no risk to the live bot. A real conversation
(runtime.py) passes a `Live` instead, which is the only way anything here reaches the outside world.

One customer message can produce several bot messages, because message/condition/set_var steps do
not wait. The run stops at a question or an end, or at the safety caps.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any

from .. import menus
from ..templates import norm_trigger, trigger_matches
from . import actions as act
from .schema import (ACTION_TYPES, AI_ANSWER_MAX, DATA_FIELDS_MAX, DATA_LIST_LINE, DATA_ROWS_MAX,
                     DATA_SETS_MAX, DATA_VALUE_MAX, DELAY_MAX_SEC, LANGS, MAX_MESSAGES_PER_TURN,
                     MAX_VISITS_PER_TURN,
                     FILE_ANSWERS, LANGUAGE_NAMES, WAITING_TYPES, Graph, branch_port, language_label,
                     offered_languages, option_port, text_of,
                     to_person)

# matcher(question, [(choice value, label)], what they typed) -> the choice value they mean, or None
Matcher = Callable[[str, list[tuple[str, str]], str], Awaitable[str | None]]
# answerer(faq, their question, language, tone, max characters) -> {"answer", "confident"}, or None
Answerer = Callable[[str, str, str, str, int], Awaitable[dict | None]]

_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")
_PHONE = re.compile(r"^\+?[\d\s\-()]{8,20}$")
_TIME = re.compile(r"^(([01]?\d|2[0-3])[:.][0-5]\d(\s*[ap]\.?m\.?)?|(1[0-2]|0?[1-9])\s*[ap]\.?m\.?)$", re.I)
_URL = re.compile(r"^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(:\d+)?(/\S*)?$", re.I)
# How people in India write a date, plus the ISO form a phone keyboard may suggest
_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d")


@dataclass
class Message:
    """One thing the bot says. `options` is the WhatsApp menu attached to it, if any.

    header/footer/media/sections carry what WhatsApp draws around the text, so the test chat can show
    a customer's-eye view: a list's section headings, a picture above its caption."""

    text: str
    options: menus.Options | None = None
    node_id: str = ""
    kind: str = "text"  # text | media | product_list
    header: str = ""
    footer: str = ""
    media: dict | None = None  # {"type", "url", "caption"}
    sections: list[dict] | None = None  # list rows grouped under their section titles

    def to_dict(self) -> dict:
        return {"text": self.text, "options": self.options.to_dict() if self.options else None,
                "node_id": self.node_id, "kind": self.kind, "header": self.header,
                "footer": self.footer, "media": self.media, "sections": self.sections}


@dataclass
class RunState:
    """Where a conversation has got to. The simulator keeps one of these per test chat."""

    node: str | None = None
    vars: dict[str, str] = field(default_factory=dict)
    language: str = "en"
    attempts: int = 0
    finished: bool = False
    # What a "Find in your data" step found, by its name: {"orders": {"source", "total", "rows"}}.
    # Kept apart from vars deliberately - vars are carried into another workflow and onto the New
    # customers list, and rows of somebody's orders have no business in either.
    data: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"node": self.node, "vars": dict(self.vars), "language": self.language,
                "attempts": self.attempts, "finished": self.finished, "data": dict(self.data)}

    @classmethod
    def from_dict(cls, d: dict | None) -> RunState:
        """Rebuild from whatever the client sent back.

        This is the one place untrusted JSON becomes engine state, so every field is checked rather
        than trusted: a stale browser tab or a half-written script must get an answer, not a 500."""
        if not isinstance(d, dict):
            d = {}
        raw_vars = d.get("vars")
        variables = ({str(k): "" if v is None else str(v) for k, v in raw_vars.items()}
                     if isinstance(raw_vars, dict) else {})
        node = d.get("node")
        lang = d.get("language")
        try:
            attempts = int(d.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        return cls(node=str(node) if isinstance(node, (str, int)) else None,
                   vars=variables,
                   language=lang if lang in LANGS else "en",
                   attempts=max(0, min(attempts, 100)),
                   finished=bool(d.get("finished")),
                   data=_clean_data(d.get("data")))


@dataclass
class Turn:
    """What one step of a conversation produced: what to say, and what to do."""

    messages: list[Message]
    state: RunState
    stopped: str = ""  # "" | "end" | "cap" | "no_route" | "not_understood" | "jump" | "delay" | "handed_over"
    actions: list[act.Action] = field(default_factory=list)
    jump_to: str = ""  # the workflow key a jump node handed control to
    delay: int = 0  # seconds a live Wait step paused for ("delay"); the run carries on from state.node
    # "" | "matched" (AI understood a typed answer) | "answered" | "unsure" | "unavailable" (an AI reply step)
    ai: str = ""


@dataclass
class Live:
    """What turns a dry run into a real conversation. Each message is sent the moment it is produced,
    so a template or a hand-off lands between the right messages; steps that act on WATI are really
    performed; and a Wait really pauses. `perform` says whether it worked, which is what sends an
    Assign or a template that WATI refused down its "If it fails" exit."""

    emit: Callable[[Message], Awaitable[None]]
    perform: Callable[[act.Action], Awaitable[bool]]


async def start(graph: Graph, *, language: str = "en", system: dict[str, str] | None = None,
                fetch=None, variables: dict[str, str] | None = None, live: Live | None = None,
                data=None) -> Turn:
    """Begin a conversation at the graph's start node. `variables` carries answers over from a
    workflow that handed the conversation here."""
    state = RunState(node=graph.start, language=language)
    for name, value in (variables or {}).items():
        if not str(name).startswith("sys."):
            state.vars[str(name)] = str(value)
    _seed_system(state, system)
    return await _run(graph, state, fetch=fetch, live=live, data=data)


async def resume(graph: Graph, state: RunState, *, system: dict[str, str] | None = None, fetch=None,
                 live: Live | None = None, data=None) -> Turn:
    """Carry on from the step after a Wait."""
    _seed_system(state, system)
    return await _run(graph, state, fetch=fetch, live=live, data=data)


def understands(graph: Graph, state: RunState, text: str, media: dict | None = None) -> bool:
    """Would the step the conversation is waiting on accept this as its answer?

    Lets a live conversation tell "a wrong answer, ask again" apart from "the customer wants
    something else" - a keyword for another workflow, or the order-status menu - without spending
    one of the question's retries to find out."""
    node = graph.node(state.node or "")
    if node is None or state.finished or node.get("type") not in WAITING_TYPES:
        return True
    if node.get("type") == "ai_reply":
        # Any question is its to answer - but only once another workflow's keyword, "menu" or an SO
        # number has had its say, which is what "not understood" gives them.
        return False
    _, _, ok = _answer(node, text, state, media)
    if ok:
        return True
    return bool((text or "").strip()
                and (node.get("input") or {}).get("kind") in ("buttons", "list", "data_list")
                and graph.next_of(str(node["id"]), "default"))


async def advance(graph: Graph, state: RunState, text: str, *, system: dict[str, str] | None = None,
                  fetch=None, live: Live | None = None, lookup=None, media: dict | None = None,
                  matcher: Matcher | None = None, answerer: Answerer | None = None, data=None) -> Turn:
    """Feed the customer's reply to the question the conversation is waiting on.

    `lookup(rule, value)` checks an answer against the business's data (None: not found); `media` is
    a picture, document or location the customer sent instead of typing. `matcher` and `answerer`
    are the AI: without them (no key, a dry run that does not want it) everything still works, the
    AI simply never helps."""
    _seed_system(state, system)
    node = graph.node(state.node or "")
    if node is None or state.finished:
        return Turn([], state, stopped="end")
    if node.get("type") == "ai_reply":
        return await _ai_reply(graph, state, node, text, answerer, fetch, live, data)
    if node.get("type") != "question":
        return await _run(graph, state, fetch=fetch, live=live, data=data)

    kind = (node.get("input") or {}).get("kind")
    port, value, ok = _answer(node, text, state, media)
    typed = (text or "").strip()
    choices_kind = kind in ("buttons", "list", "data_list")
    if not ok and typed and choices_kind and graph.next_of(str(node["id"]), "default"):
        # WATI's "default" route: the customer typed instead of tapping, and the owner said where
        # that should go. Their words are kept, like any other answer.
        port, value, ok = "default", typed, True
    understood_by_ai = False
    # A data list is never shown to the AI: those rows are the customer's own orders.
    if not ok and typed and kind in ("buttons", "list") and matcher is not None and graph.settings.get("ai_understand"):
        # "500 standup chahiye" for the "Stand-up pouch" button. Only when the owner switched it on
        # for this workflow, and only after every exact and synonym match has failed.
        picked = await matcher(render(text_of(node.get("text"), state.language), state), _choices(node, state), typed)
        if picked:
            port, value, ok, understood_by_ai = option_port(picked), picked, True, True

    row: dict[str, str] | None = None
    if ok and port == "next" and kind == "data_list":
        row, value, ok = await _picked(node, state, value, lookup)
        if not ok:
            port = "invalid"

    rule = node.get("validate") or {}
    extra: dict[str, str] = {}
    if row is not None:
        extra = dict(row)
    elif ok and rule.get("type") == "lookup":
        found = await lookup(rule, value) if lookup else None  # no data to check against: not found
        if found is None:
            port, ok = "invalid", False
        else:
            found = dict(found)
            value = str(found.pop("value", value))
            extra = {k: str(v) for k, v in found.items()}
    elif ok and rule.get("type") in ("file", "location") and media:
        extra = {"type": str(media.get("type") or ""), "address": str(media.get("address") or "")}

    store = str(node.get("store") or "")
    if not ok:
        wrong = graph.next_of(str(node["id"]), "invalid") if port == "invalid" else None
        # Tries come first. The "Not valid" exit is where a wrong answer goes when the question does
        # not ask again ("Keep asking" with Not valid connected), or once its tries run out.
        if wrong is None or int(node.get("max_retries") or 0):
            return await _retry(graph, state, node, text, fetch, live, data)
        # The owner said where a wrong answer goes: go there rather than ask again.
        state.attempts = 0
        if store:
            state.vars[store] = value
        state.node = wrong
        return await _run(graph, state, fetch=fetch, live=live, data=data)

    state.attempts = 0
    if store:
        state.vars[store] = value
        for name, v in extra.items():
            state.vars[f"{store}_{name}"] = v
    if (node.get("input") or {}).get("kind") == "language" and value in LANGS:
        state.language = value

    nxt = graph.next_of(str(node["id"]), port)
    if nxt is None:
        return Turn([], state, stopped="no_route", ai="matched" if understood_by_ai else "")
    state.node = nxt
    turn = await _run(graph, state, fetch=fetch, live=live, data=data)
    if understood_by_ai:
        turn.ai = "matched"
    return turn


def _choices(node: dict, state: RunState) -> list[tuple[str, str]]:
    """The buttons as the AI sees them: English, plus the customer's language when it reads differently."""
    out = []
    for o in _options_of(node, state):
        if o.get("value"):
            en, own = text_of(o.get("label"), "en"), text_of(o.get("label"), state.language)
            out.append((str(o["value"]), en if own == en else f"{en} / {own}"))
    return out


async def _ai_reply(graph: Graph, state: RunState, node: dict, text: str, answerer: Answerer | None,
                    fetch=None, live: Live | None = None, data=None) -> Turn:
    """Answer the customer's own question from the owner's FAQ, or take "Not sure".

    A doubtful answer, no key, the limit reached or Groq down all take "Not sure" - the owner connects
    it to a person or the menu, so the AI can never leave anyone in silence or guess at an answer."""
    said = (text or "").strip()
    nid = str(node["id"])
    if node.get("store"):
        state.vars[str(node["store"])] = said
    state.attempts = 0
    try:
        limit = max(100, min(int(node.get("max_chars") or 600), AI_ANSWER_MAX))
    except (TypeError, ValueError):
        limit = 600
    got = None
    if answerer is not None and said:
        got = await answerer(str(node.get("faq") or ""), said, state.language, str(node.get("tone") or "friendly"), limit)
    answer = str((got or {}).get("answer") or "").strip()[:limit]
    if not (got and got.get("confident") and answer):
        state.node = graph.next_of(nid, "unsure")
        if state.node is None:
            state.finished = True
            return Turn([], state, stopped="no_route", ai="unavailable" if got is None else "unsure")
        turn = await _run(graph, state, fetch=fetch, live=live, data=data)
        turn.ai = "unavailable" if got is None else "unsure"
        return turn

    msg = Message(text=answer, node_id=nid)
    if live is not None:
        await live.emit(msg)
    state.node = graph.next_of(nid, "answered")
    if state.node is None:
        state.finished = True
        return Turn([msg], state, stopped="no_route", ai="answered")
    turn = await _run(graph, state, fetch=fetch, live=live, data=data)
    turn.messages.insert(0, msg)
    turn.ai = "answered"
    return turn


# ---------------- the run loop ----------------
async def _run(graph: Graph, state: RunState, *, fetch=None, live: Live | None = None, data=None) -> Turn:
    """Execute nodes until one waits for the customer, or the conversation ends."""
    out: list[Message] = []
    done: list[act.Action] = []
    visits = 0

    async def say(msg: Message) -> bool:
        # Drawn loops can be subtle; stop rather than flood a customer's phone. Checked BEFORE the
        # message is added, because in a live conversation adding it means sending it.
        if len(out) >= MAX_MESSAGES_PER_TURN:
            return False
        out.append(msg)
        if live is not None:
            await live.emit(msg)
        return True

    def stop(why: str) -> Turn:
        state.finished = True
        return Turn(out, state, stopped=why, actions=done)

    while True:
        node = graph.node(state.node or "")
        if node is None:
            return stop("no_route")
        visits += 1
        if visits > MAX_VISITS_PER_TURN:
            return stop("cap")

        kind = node.get("type")
        if kind == "question":
            if (node.get("input") or {}).get("kind") == "data_list" and not _options_of(node, state):
                # Nothing was found, so there is no list to draw: take the owner's "Nothing to show"
                # exit rather than send a question a customer cannot answer.
                nxt = graph.next_of(str(node["id"]), "empty")
                if nxt is None:
                    return stop("no_route")
                state.node = nxt
                continue
            if not await say(_say(node, state, node.get("text"))):
                return stop("cap")
            return Turn(out, state, stopped="", actions=done)
        if kind == "ai_reply":
            # waits for the customer's question; its opening line ("Ask me anything...") is optional
            if text_of(node.get("text"), state.language).strip() and not await say(
                    _say(node, state, node.get("text"), with_options=False)):
                return stop("cap")
            return Turn(out, state, stopped="", actions=done)
        if kind == "end":
            if node.get("text") and not await say(_say(node, state, node.get("text"), with_options=False)):
                return stop("cap")
            state.node = str(node["id"])
            return stop("end")
        port = "next"
        if kind == "message":
            if not await say(_message(node, state)):
                return stop("cap")
        elif kind == "product_list":
            msg = Message(text=render(text_of(node.get("text"), state.language), state),
                          node_id=str(node.get("id") or ""), kind="product_list",
                          header=render(text_of(node.get("header"), state.language), state))
            if not await say(msg):
                return stop("cap")
        elif kind == "set_var":
            values: dict[str, str] = {}
            for name, raw in (node.get("assign") or {}).items():
                state.vars[str(name)] = values[str(name)] = render(str(raw), state)
            if node.get("to_contact") and values:
                # WATI's "Update attribute": the same values also land on the contact in WATI.
                action = act.Action(kind="attributes", detail={"attributes": values}, node_id=str(node.get("id") or ""))
                done.append(action)
                if live is not None:
                    await live.perform(action)
        elif kind == "condition":
            chosen = _branch(node, state)
            port = branch_port(chosen) if chosen else "else"
        elif kind == "jump":
            # Control leaves this workflow entirely; the caller decides what picks it up.
            state.finished = True
            return Turn(out, state, stopped="jump", actions=done,
                        jump_to=str(node.get("workflow") or ""))
        elif kind == "api_request":
            port = await _api_request(node, state, fetch)
        elif kind == "data":
            port = await _data(node, state, data)
        elif kind == "delay" and live is not None:
            # A real conversation really waits: stop here, and the timer carries on from the next
            # step when the time is up. A dry run records the pause below instead.
            done.append(_action(node, state))
            nxt = graph.next_of(str(node["id"]), "next")
            if nxt is None:
                return stop("no_route")
            state.node = nxt
            seconds = _seconds(node)
            if seconds:
                return Turn(out, state, stopped="delay", actions=done, delay=seconds)
            continue
        elif kind in ACTION_TYPES:
            action = _action(node, state)
            done.append(action)
            ok = True if live is None else await live.perform(action)
            if not ok and kind in ("assign", "template"):
                port = "on_error"
            elif to_person(node):
                # A person has the chat now and the bot says nothing more - WATI's rule too. A dry run
                # stops here as well, so the test chat shows exactly what a customer would get.
                return stop("handed_over")
        nxt = graph.next_of(str(node["id"]), port)
        if nxt is None:
            return stop("no_route")
        state.node = nxt


def _seconds(node: dict) -> int:
    try:
        return max(0, min(int(node.get("seconds") or 0), DELAY_MAX_SEC))
    except (TypeError, ValueError):
        return 0


# ---------------- doing things to the outside world ----------------
def _action(node: dict, state: RunState) -> act.Action:
    """Record what a step wants done. Values are rendered first, so a tag or a template can carry an
    answer the customer just gave."""
    kind = str(node.get("type"))
    r = lambda v: render(str(v or ""), state)  # noqa: E731 - a local shorthand, used ten lines below
    if kind == "tags":
        detail = {"tags": [r(t) for t in (node.get("tags") or []) if str(t).strip()],
                  "remove": bool(node.get("remove"))}
    elif kind == "assign":
        detail = {"to": node.get("to") or "operator", "email": r(node.get("email")),
                  "teams": [r(t) for t in (node.get("teams") or []) if str(t).strip()]}
    elif kind == "chat_status":
        detail = {"status": node.get("status") or "open"}
    elif kind == "subscribe":
        detail = {"subscribe": bool(node.get("subscribe", True))}
    elif kind == "template":
        detail = {"name": r(node.get("template_name")),
                  "params": {str(k): r(v) for k, v in (node.get("params") or {}).items()}}
    elif kind == "delay":
        detail = {"seconds": int(node.get("seconds") or 0)}
    else:
        detail = {}
    return act.Action(kind=kind, detail=detail, node_id=str(node.get("id") or ""))


async def _api_request(node: dict, state: RunState, fetch) -> str:
    """Call an outside service and read values out of the answer.

    Returns the port to follow. A failure is never fatal: it takes the "failed" exit, which the
    editor forces the owner to connect, so a customer cannot be stranded by someone else's outage."""
    url = render(str(node.get("url") or ""), state)
    method = str(node.get("method") or "GET").upper()
    headers = {str(k): render(str(v), state) for k, v in (node.get("headers") or {}).items()}
    # Parse the body first, THEN render the values. Rendering the raw text would not work: JSON is
    # full of braces and a placeholder filler cannot tell {"a": 1} from a {placeholder}.
    body = node.get("body")
    if isinstance(body, str) and body.strip():
        import json as _json
        try:
            body = _json.loads(body)
        except ValueError:
            body = render(body, state)
    body = _render_deep(body, state)

    caller = fetch or act.fetch
    result = await caller(method, url, headers, body)
    state.vars["sys.api_status"] = str(result.get("status") or "")
    state.vars["sys.api_error"] = str(result.get("error") or "")

    for name, path in (node.get("save") or {}).items():
        state.vars[str(name)] = act.pick(result.get("data"), str(path))

    if not result.get("ok"):
        return "failed"
    # Response routing: the first rule whose saved value matches wins, else plain success.
    for route in node.get("routes") or []:
        if not isinstance(route, dict):
            continue
        got = act.pick(result.get("data"), str(route.get("path") or ""))
        if _matches(got, str(route.get("op") or "eq"), render(str(route.get("value") or ""), state)):
            return f"route:{route.get('id')}"
    return "success"


def _render_deep(value: Any, state: RunState) -> Any:
    """Fill {placeholders} in every string inside a parsed JSON body."""
    if isinstance(value, str):
        return render(value, state)
    if isinstance(value, dict):
        return {k: _render_deep(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_deep(v, state) for v in value]
    return value


def _matches(left: str, op: str, right: str) -> bool:
    a, b = left.strip().casefold(), right.strip().casefold()
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if op == "contains":
        return b in a
    if op == "is_set":
        return bool(a)
    if op == "is_empty":
        return not a
    return False


def _say(node: dict, state: RunState, value: Any, *, with_options: bool = True) -> Message:
    text = render(text_of(value, state.language), state)
    options = _options(node, state) if with_options else None
    msg = Message(text=text, options=options, node_id=str(node.get("id") or ""))
    if with_options and node.get("type") == "question":
        lang = state.language
        msg.header = render(text_of(node.get("header"), lang), state)
        msg.footer = render(text_of(node.get("footer"), lang), state)
        msg.sections = _sections(node, state)
        media = node.get("header_media")
        if isinstance(media, dict) and media.get("url"):
            msg.media = {"type": media.get("type") or "image", "url": render(str(media["url"]), state),
                         "caption": ""}
    return msg


def _message(node: dict, state: RunState) -> Message:
    """A message step: text, or a picture/video/document/voice note with an optional caption."""
    msg = _say(node, state, node.get("text"), with_options=False)
    media = node.get("media")
    if isinstance(media, dict) and media.get("url"):
        msg.kind = "media"
        msg.media = {"type": media.get("type") or "image",
                     "url": render(str(media["url"]), state),
                     "caption": render(text_of(media.get("caption"), state.language), state)}
    return msg


def _sections(node: dict, state: RunState) -> list[dict] | None:
    """A list's rows grouped under their section titles, in order - exactly as WhatsApp shows them."""
    spec = node.get("input") or {}
    if spec.get("kind") not in ("list", "data_list"):
        return None
    lang = state.language
    fallback = render(text_of(spec.get("section_title"), lang), state)
    groups: list[dict] = []
    for o in _options_of(node, state):
        title = render(text_of(o.get("section"), lang), state) or fallback
        row = {"title": render(text_of(o.get("label"), lang), state),
               "description": render(text_of(o.get("description"), lang), state)}
        if groups and groups[-1]["title"] == title:
            groups[-1]["rows"].append(row)
        else:
            groups.append({"title": title, "rows": [row]})
    return groups


async def _retry(graph: Graph, state: RunState, node: dict, text: str, fetch=None,
                 live: Live | None = None, data=None) -> Turn:
    """The answer did not match. Ask again, or take the give-up exit."""
    state.attempts += 1
    limit = int(node.get("max_retries") or 0)
    if limit and state.attempts > limit:
        state.attempts = 0
        # "Gave up" first; failing that a connected "Not valid" - never silence when a way on was drawn
        nxt = graph.next_of(str(node["id"]), "retry_exhausted") or graph.next_of(str(node["id"]), "invalid")
        if nxt is None:
            state.finished = True
            return Turn([], state, stopped="no_route")
        state.node = nxt
        return await _run(graph, state, fetch=fetch, live=live, data=data)

    invalid = node.get("invalid_text") or node.get("retry_text") or node.get("text")
    msg = _say(node, state, invalid)
    if live is not None:
        await live.emit(msg)
    return Turn([msg], state, stopped="not_understood")


# ---------------- answers ----------------
def _answer(node: dict, text: str, state: RunState, media: dict | None = None) -> tuple[str, str, bool]:
    """(port, stored value, matched?) for what the customer said."""
    spec = node.get("input") or {}
    kind = spec.get("kind")
    said = (text or "").strip()

    if kind == "language":
        n = norm_trigger(said)
        for code in offered_languages(spec):
            # the owner's own wording, and the Bot messages wording in every language, all count:
            # a customer may type "Hindi" instead of tapping "हिंदी में"
            names = ({language_label(spec, code, lg) for lg in LANGS} | {menus.label(f"lang_{code}", lg) for lg in LANGS}
                     | {LANGUAGE_NAMES[code]})
            if n and any(norm_trigger(x) == n for x in names):
                return "opt:" + code, code, True
        return "", "", False

    if kind in ("buttons", "list", "data_list"):
        n = norm_trigger(said)
        if not n:
            return "", "", False
        options = _options_of(node, state)
        # A data list has one exit for every row: which row they picked is an answer, not a path.
        chosen = "next" if kind == "data_list" else ""
        # Exact title first, in every language: a tap sends the title we drew, so it must always win.
        for o in options:
            for lg in LANGS:
                if norm_trigger(text_of(o.get("label"), lg)) == n:
                    return chosen or option_port(str(o.get("value"))), str(o.get("value")), True
        for o in options:  # then looser synonyms, same rule the custom replies use
            for syn in o.get("synonyms") or []:
                if trigger_matches(norm_trigger(str(syn)), n):
                    return chosen or option_port(str(o.get("value"))), str(o.get("value")), True
        return "", "", False

    # a file or a location, sent instead of typed
    rule = node.get("validate") or {}
    vtype = rule.get("type", "any")
    if vtype in ("file", "location"):
        m = media or {}
        wanted = FILE_ANSWERS if vtype == "file" else ("location",)
        if m.get("type") in wanted and m.get("url"):
            return "next", str(m["url"]), True
        return ("invalid", said, False) if (said or m) else ("", "", False)

    # free text
    if not said:
        return "", "", False
    if not _valid(said, rule):
        return "invalid", said, False
    return "next", said, True


def _valid(text: str, rule: dict) -> bool:
    vtype = rule.get("type", "any")
    if vtype == "number":
        t = text.replace(",", "")
        if not _NUMBER.match(t):
            return False
        n = float(t)
        # WATI's min/max. A bound left blank is no bound; an unreadable one is ignored rather than
        # used to turn a customer away.
        for bound, too_far in (("min", lambda b: n < b), ("max", lambda b: n > b)):
            raw = rule.get(bound)
            try:
                if raw not in (None, "") and too_far(float(raw)):
                    return False
            except (TypeError, ValueError):
                continue
        return True
    if vtype == "regex":
        pattern = str(rule.get("pattern") or "")
        try:
            return bool(re.fullmatch(pattern, text)) if pattern else True
        except re.error:
            return True  # the validator refuses a broken pattern; a customer never pays for it
    if vtype == "date":
        for fmt in _DATE_FORMATS:
            try:
                datetime.strptime(text.strip(), fmt)
                return True
            except ValueError:
                continue
        return False
    if vtype == "url":
        return bool(_URL.match(text.strip()))
    if vtype == "time":
        return bool(_TIME.match(text.strip()))
    if vtype == "email":
        return bool(_EMAIL.match(text))
    if vtype == "phone":
        return bool(_PHONE.match(text)) and sum(c.isdigit() for c in text) >= 8
    return True


# ---------------- conditions ----------------
def _branch(node: dict, state: RunState) -> str:
    for b in node.get("branches") or []:
        if isinstance(b, dict) and _test(b.get("when") or {}, state):
            return str(b.get("id") or "")
    return ""


def _test(when: dict, state: RunState) -> bool:
    left = _value(str(when.get("var") or ""), state)
    op = when.get("op")
    right = render(str(when.get("value") or ""), state)
    if op == "is_set":
        return bool(left.strip())
    if op == "is_empty":
        return not left.strip()
    if op == "between":
        return _between(left, right)
    a, b = left.strip().casefold(), right.strip().casefold()
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if op == "contains":
        return b in a
    if op == "starts_with":
        return a.startswith(b)
    if op == "ends_with":
        return a.endswith(b)
    if op in ("gt", "gte", "lt", "lte"):
        try:  # a non-numeric comparison is false, never an exception in front of a customer
            x, y = float(a.replace(",", "")), float(b.replace(",", ""))
        except ValueError:
            return False
        return {"gt": x > y, "gte": x >= y, "lt": x < y, "lte": x <= y}[op]
    return False


def _between(left: str, span: str) -> bool:
    """"10:00-19:00" or "10-19" - numbers, or times of day. A span over midnight ("21:00-06:00")
    means late evening or early morning."""
    lo, sep, hi = span.partition("-")
    if not sep:
        return False

    def read(v: str) -> tuple[float, bool] | None:  # (value, was it a time of day?)
        v = v.strip()
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", v)
        if m:
            return int(m.group(1)) * 60 + int(m.group(2)), True
        try:
            return float(v.replace(",", "")), False
        except ValueError:
            return None

    x, a, b = read(left), read(lo), read(hi)
    if x is None or a is None or b is None:
        return False
    if x[1]:  # a time compared with plain hours: "14:30" between "10-19"
        a = (a[0] * 60 if not a[1] else a[0], True)
        b = (b[0] * 60 if not b[1] else b[0], True)
    return a[0] <= x[0] <= b[0] if a[0] <= b[0] else (x[0] >= a[0] or x[0] <= b[0])


def _value(name: str, state: RunState) -> str:
    if name == "sys.language":
        return state.language
    return str(state.vars.get(name, ""))


# ---------------- rendering ----------------
_PLACEHOLDER = re.compile(r"\{([A-Za-z][A-Za-z0-9_.]{0,40})\}")


def render(text: str, state: RunState) -> str:
    """Fill {name} placeholders from the conversation's answers.

    A regex rather than str.format: format() reads {sys.language} as attribute access and crashed on
    exactly the placeholders the editor suggests, treats {0} as positional, and chokes on a stray
    brace. Anything unknown is left exactly as written - never a crash in front of a customer."""
    if not text or "{" not in text:
        return text or ""

    def fill(m: re.Match) -> str:
        key = m.group(1)
        if key == "sys.language":
            return state.language
        return state.vars.get(key, m.group(0))

    return _PLACEHOLDER.sub(fill, text)


def _options(node: dict, state: RunState) -> menus.Options | None:
    spec = node.get("input") or {}
    kind = spec.get("kind")
    lang = state.language
    if kind == "language":
        return menus.Options(
            kind="buttons",
            items=[menus.Option(language_label(spec, code, lang)[:menus.BUTTON_TEXT_MAX]) for code in offered_languages(spec)],
            header=render(text_of(node.get("header"), lang), state)[:menus.HEADER_MAX],
            footer=render(text_of(node.get("footer"), lang), state)[:menus.FOOTER_MAX])
    if kind == "data_list":
        return _data_options(node, state)
    if kind not in ("buttons", "list"):
        return None
    items = [menus.Option(title=render(text_of(o.get("label"), lang), state),
                          description=render(text_of(o.get("description"), lang), state))
             for o in _options_of(node, state)]
    if not items:
        return None
    header = render(text_of(node.get("header"), lang), state)[:menus.HEADER_MAX]
    footer = render(text_of(node.get("footer"), lang), state)[:menus.FOOTER_MAX]
    if kind == "buttons":
        return menus.Options(kind="buttons", items=[menus.Option(o.title) for o in items],
                             header=header, footer=footer)
    first_section = next((text_of(o.get("section"), lang) for o in spec.get("options") or []
                          if isinstance(o, dict) and text_of(o.get("section"), lang)), "")
    return menus.Options(kind="list", items=items,
                         button_text=text_of(spec.get("button_text"), lang) or "Select",
                         section_title=first_section or text_of(spec.get("section_title"), lang),
                         header=header, footer=footer)


def _seed_system(state: RunState, system: dict[str, str] | None) -> None:
    for name, value in (system or {}).items():
        state.vars[name if name.startswith("sys.") else f"sys.{name}"] = str(value)


# ---------------- the business's own data ----------------
def _options_of(node: dict, state: RunState) -> list[dict]:
    """Every choice a question offers: the ones written on the step, or the rows a data step found.

    The single place that knows the difference. _options, _sections, _answer and _choices all read
    choices through here - were any of them to read the step directly, they would drift, and a
    customer's tap would silently do nothing."""
    spec = node.get("input") or {}
    if spec.get("kind") != "data_list":
        return [o for o in (spec.get("options") or []) if isinstance(o, dict)]
    got = state.data.get(str(spec.get("from") or "")) or {}
    out: list[dict] = []
    for row in got.get("rows") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get(str(spec.get("title_field") or "")) or "").strip()
        if title:
            # The title is what the phone sends back when they tap, so it is also the value. A
            # customer who types instead gets the same row: an order is known by its numbers, and
            # "SO 45240" is how people write one.
            codes = [str(row.get(f) or "") for f in ("so_no", "po_no", "fg_item_code", "customer_code")]
            synonyms = [c for c in codes if c and c != title]
            if row.get("so_no"):
                synonyms.append(f"SO {row['so_no']}")
            out.append({"value": title, "label": title, "row": row, "synonyms": synonyms,
                        "description": str(row.get(str(spec.get("description_field") or "")) or ""),
                        "section": str(row.get(str(spec.get("section_field") or "")) or "")})
    return out


def _data_options(node: dict, state: RunState) -> menus.Options | None:
    """The rows a data step found, as a WhatsApp list. When some were left out the footer says so -
    a customer whose order is missing from the list must know they can still type its number."""
    spec = node.get("input") or {}
    lang = state.language
    rows = _options_of(node, state)
    got = state.data.get(str(spec.get("from") or "")) or {}
    footer = render(text_of(node.get("footer"), lang), state)
    try:
        total = int(got.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    if not footer and total > len(rows):
        footer = menus.label("more_hint", lang)
    return menus.options_from([(o["label"], o["description"]) for o in rows], "list",
                              button_text=render(text_of(spec.get("button_text"), lang), state),
                              section_title=render(text_of(spec.get("section_title"), lang), state),
                              header=render(text_of(node.get("header"), lang), state), footer=footer)


async def _data(node: dict, state: RunState, data) -> str:
    """Find rows in the business's own data and remember what came back.

    Returns the port to follow. Nothing found is not a failure - it is the "Nothing found" exit, which
    the editor makes the owner connect, so a customer is never left waiting on an empty answer."""
    store = str(node.get("store") or "")
    spec = _data_spec(node, state)
    rows = await data(spec, state) if data is not None else None
    found = [r for r in (rows or []) if isinstance(r, dict)]
    kept = _keep_rows(found[:spec["limit"]])
    if store:
        state.data.pop(store, None)
        if kept:
            while len(state.data) >= DATA_SETS_MAX:
                state.data.pop(next(iter(state.data)))
            state.data[store] = {"source": spec["source"], "total": len(found), "rows": kept}
        state.vars[f"{store}_count"] = str(len(found))
        state.vars[f"{store}_list"] = _data_lines(node, kept)
        first = kept[0] if kept else {}
        for name, fld in (node.get("save") or {}).items():
            state.vars[str(name)] = str(first.get(str(fld), ""))
    return "found" if kept else "none"


def _data_spec(node: dict, state: RunState) -> dict:
    """What to look for, with the answers filled in - {typed_so} becomes the number they typed."""
    try:
        limit = max(1, min(int(node.get("limit") or DATA_ROWS_MAX), DATA_ROWS_MAX))
    except (TypeError, ValueError):
        limit = DATA_ROWS_MAX
    return {
        "source": str(node.get("source") or "orders"),
        "find": str(node.get("find") or "all"),
        "match": render(str(node.get("match") or ""), state),
        "group": str(node.get("group") or "so"),
        "sort": str(node.get("sort") or "newest"),
        "limit": limit,
        "filter": [{"field": str(f.get("field") or ""), "op": str(f.get("op") or "eq"),
                    "value": render(str(f.get("value") or ""), state)}
                   for f in (node.get("filter") or []) if isinstance(f, dict)],
    }


def _keep_rows(rows: list[dict]) -> list[dict]:
    """Rows as the run will carry them: capped, and every value a plain short string."""
    out = []
    for r in rows[:DATA_ROWS_MAX]:
        out.append({str(k)[:40]: str("" if v is None else v)[:DATA_VALUE_MAX]
                    for k, v in list(r.items())[:DATA_FIELDS_MAX]})
    return out


def _data_lines(node: dict, rows: list[dict]) -> str:
    """The rows as text for {orders_list}, one line each, the way the owner worded it."""
    line = str(node.get("list_line") or DATA_LIST_LINE)
    out = [_fill_row(line, r) for r in rows]
    return "\n".join(t for t in out if t.strip())[:menus.BODY_MAX]


def _fill_row(template: str, row: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: str(row.get(m.group(1), m.group(0))), template)


async def _picked(node: dict, state: RunState, title: str, lookup) -> tuple[dict | None, str, bool]:
    """The row a customer chose from a data list, checked against the data again before it is used.

    The rows a step remembered are a menu, never the truth: minutes or hours pass before a customer
    taps, so the order is looked up afresh. A status that has moved on is then right, and an order
    that is no longer theirs is refused rather than answered."""
    spec = node.get("input") or {}
    chosen = next((o["row"] for o in _options_of(node, state) if o["value"] == title), None)
    if chosen is None:
        return None, title, False
    row = dict(chosen)
    got = state.data.get(str(spec.get("from") or "")) or {}
    # Check the thing they actually picked: an item row by its item code, an order row by its SO.
    by_field = {"so_no": "so", "po_no": "po", "fg_item_code": "fg"}
    title_field = str(spec.get("title_field") or "")
    source = by_field.get(title_field, "so")
    key = str(row.get(title_field if title_field in by_field else "so_no") or "")
    if str(got.get("source") or "") == "orders" and lookup is not None and key:
        fresh = await lookup({"type": "lookup", "source": source}, key)
        if fresh is None:
            return None, title, False
        fresh = dict(fresh)
        title = str(fresh.pop("value", title))
        row.update({str(k): str(v) for k, v in fresh.items()})
        row["real_status"] = str(fresh.get("status", row.get("real_status", "")))
    return row, title, True


def _clean_data(raw) -> dict[str, dict]:
    """Rows a data step found, as they come back from the database or the test chat.

    Untrusted JSON, like vars: everything is coerced and capped, and anything unusable is dropped
    rather than raised - a stale browser tab must get an answer, not a 500."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for name, got in list(raw.items())[:DATA_SETS_MAX]:
        if not isinstance(got, dict) or not isinstance(got.get("rows"), list):
            continue
        rows = _keep_rows([r for r in got["rows"] if isinstance(r, dict)])
        try:
            total = int(got.get("total") or len(rows))
        except (TypeError, ValueError):
            total = len(rows)
        out[str(name)[:40]] = {"source": str(got.get("source") or "")[:20],
                               "total": max(len(rows), min(total, 100000)), "rows": rows}
    return out
