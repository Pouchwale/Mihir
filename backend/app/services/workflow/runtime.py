"""Published workflows answering real customers, next to the order-status bot.

Who answers a customer's message, in this order:
  1. A customer already inside a workflow stays in it. If what they send is not an answer the
     waiting question understands, but starts another workflow or asks for the order-status bot
     ("menu", an SO number), they are let out rather than asked the same question again.
  2. A keyword, or a tap on a workflow's row in the order-status main menu, starts that workflow.
  3. A number that is not in the customer list starts the workflow set up for new numbers.
  4. Everything else goes to the order-status bot, exactly as before.

A published workflow answers only the test numbers in Settings until it is switched on for everyone.
A chat a person has taken (services/handover.py) never gets this far: the bot stays quiet for it.

With no workflow published, steps 1-3 cost one cached lookup and the bot behaves as it always
has. A workflow that fails at runtime hands the message back to the order-status bot rather than
leave the customer in silence.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from sqlalchemy import func, select, update

from ...config import get_settings
from ...db import session_scope
from ...models import MessageLog, NewCustomer, Session, Workflow, WorkflowRun, WorkflowVersion, utcnow
from .. import alerts, handover, menus, replies
from .. import repeat
from ..intent import Parsed, regex_parse
from ..templates import button_phrases, norm_trigger, trigger_matches
from ..ai import live as ai_live
from ..ai.groq_client import configured as ai_configured
from ..verify import find_customer
from ..wati import wati
from . import actions as act
from . import engine, schema
from . import lookup as data_lookup
from .schema import BUILTIN_ORDER_STATUS, LANGS, Issue, text_of

log = structlog.get_logger(__name__)

MAX_JUMPS = 3  # workflow-to-workflow hand-offs in one turn; a drawn loop must not spin for ever
REGISTRY_TTL_SEC = 30.0
_GRAPH_CACHE_MAX = 200
# The order-status main menu becomes a list once workflows add rows to it.
MENU_LIST_BUTTON = {"en": "Choose", "hi": "चुनें", "gu": "પસંદ કરો"}
# What a customer can send to leave a workflow question they do not want to answer.
_LEAVE_INTENTS = ("menu", "order_status", "bye", "change_language", "contact_us")
NEW_NUMBER_TRIGGER = "a number not in the customer list"
SIMILAR = 0.8  # how close a misspelt keyword must be ("samlpe kit" for "sample kit")
# What the order-status bot does with a word, for a warning an owner can act on.
_INTENT_WORDS = {"greeting": "greeting", "menu": "main menu", "order_status": "order lookup",
                 "contact_us": "Contact us reply", "bye": "goodbye", "change_language": "language question",
                 "confirm_yes": "Yes answer", "confirm_no": "No answer", "lang_en": "language choice",
                 "lang_hi": "language choice", "lang_gu": "language choice"}


def test_numbers() -> frozenset[str]:
    """The owner's own phones (Settings): they meet a published workflow before everyone does."""
    raw = get_settings().workflow_test_numbers or ""
    return frozenset(d for d in (schema.digits(x) for x in re.split(r"[,;\n]+", raw)) if len(d) >= 8)


# ---------------- what is live ----------------
@dataclass
class LiveWorkflow:
    key: str
    title: str
    version: int
    priority: int
    graph: schema.Graph
    triggers: dict
    for_everyone: bool  # switched on; until then only the test numbers in Settings reach it
    idle_minutes: int
    jumps_to: frozenset[str]

    def reaches(self, phone: str) -> bool:
        """Published but not switched on: only the test numbers in Settings are answered."""
        return self.for_everyone or schema.digits(phone) in test_numbers()


class Registry:
    """The published workflows, cached. Every write that changes what is live invalidates it, and
    it also expires on its own, so a second server process catches up within REGISTRY_TTL_SEC."""

    def __init__(self) -> None:
        self._live: list[LiveWorkflow] | None = None
        self._at = 0.0
        self._graphs: dict[tuple[str, int], schema.Graph] = {}

    def invalidate(self) -> None:
        self._live = None

    async def live(self, db=None) -> list[LiveWorkflow]:
        if self._live is not None and time.monotonic() - self._at < REGISTRY_TTL_SEC:
            return self._live
        if db is None:
            async with session_scope() as own:
                out = await self._load(own)
        else:
            out = await self._load(db)
        self._live, self._at = out, time.monotonic()
        return out

    async def _load(self, db) -> list[LiveWorkflow]:
        rows = (await db.execute(
            select(Workflow).where(Workflow.published_version.is_not(None))
            .order_by(Workflow.priority, Workflow.id))).scalars().all()
        out = []
        for w in rows:
            doc = await _doc_of(db, w.id, w.published_version)
            if doc is None:
                continue
            graph = schema.parse(doc)
            self._remember(w.key, w.published_version, graph)
            out.append(LiveWorkflow(
                key=w.key, title=w.title, version=w.published_version, priority=w.priority, graph=graph,
                triggers=schema.triggers_of(doc), for_everyone=bool(w.enabled),
                idle_minutes=schema.idle_minutes_of(doc),
                jumps_to=frozenset(str(n.get("workflow") or "") for n in graph.nodes.values()
                                   if n.get("type") == "jump")))
        return out

    async def get(self, key: str, db=None) -> LiveWorkflow | None:
        return next((w for w in await self.live(db) if w.key == key), None)

    async def graph(self, key: str, version: int, db=None) -> schema.Graph | None:
        """The exact version a running conversation started on."""
        got = self._graphs.get((key, version))
        if got is not None:
            return got
        if db is None:
            async with session_scope() as own:
                doc = await _doc_by_key(own, key, version)
        else:
            doc = await _doc_by_key(db, key, version)
        if doc is None:
            return None
        graph = schema.parse(doc)
        self._remember(key, version, graph)
        return graph

    def _remember(self, key: str, version: int, graph: schema.Graph) -> None:
        if len(self._graphs) >= _GRAPH_CACHE_MAX:
            self._graphs.clear()
        self._graphs[(key, version)] = graph


registry = Registry()


async def _doc_of(db, workflow_id: int, version: int | None) -> dict | None:
    if version is None:
        return None
    row = (await db.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))).scalar_one_or_none()
    return _loads(row.graph) or None if row else None


async def _doc_by_key(db, key: str, version: int) -> dict | None:
    w = (await db.execute(select(Workflow).where(Workflow.key == key))).scalar_one_or_none()
    return await _doc_of(db, w.id, version) if w else None


# ---------------- answering a message ----------------
@dataclass
class Handled:
    """What workflows did with one customer message. The messages are already sent and logged."""

    key: str
    messages: list[engine.Message] = field(default_factory=list)
    code: str = "workflow"  # workflow | workflow_wait


class _Conv:
    """One customer message being answered by workflows: where messages go and how steps act."""

    def __init__(self, db, session: Session, phone: str, system: dict[str, str], known: bool = False) -> None:
        self.db, self.session, self.phone, self.system = db, session, phone, system
        self.known = known  # in the customer Excel
        self.sent: list[engine.Message] = []
        self.run: WorkflowRun | None = None
        self.handed_over = False
        self.media: dict | None = None  # a picture, document or location sent instead of text
        self.ai = False  # the AI understood or answered this message: the chat log says so

    async def lookup(self, rule: dict, value: str) -> dict | None:
        """Check an answer against the business's data, for this customer only."""
        return await data_lookup.find(self.db, self.phone, str(rule.get("source") or ""), value)

    async def rows(self, spec: dict, state) -> list[dict] | None:
        """What a Find-in-your-data step asked for - this customer's own orders, nobody else's."""
        return await data_lookup.find_rows(self.db, self.phone, spec)

    async def matcher(self, question: str, choices: list[tuple[str, str]], said: str) -> str | None:
        picked = await ai_live.match_choice(question, choices, said)
        self.ai = self.ai or bool(picked)
        return picked

    async def answerer(self, faq: str, question: str, language: str, tone: str, max_chars: int) -> dict | None:
        got = await ai_live.answer_faq(faq, question, language, tone, max_chars)
        self.ai = self.ai or bool(got and got.get("confident"))
        return got

    def live(self, wf: LiveWorkflow) -> engine.Live:
        async def emit(msg: engine.Message) -> None:
            await self.say(msg, wf.key)

        async def perform(action: act.Action) -> bool:
            ok = await _perform(self.phone, action, wf.key)
            if ok:
                await self._people(action, wf)
            return ok

        return engine.Live(emit=emit, perform=perform)

    async def _people(self, action: act.Action, wf: LiveWorkflow) -> None:
        """An Assign to a person quietens the bot for this customer; handing the chat back to the bot,
        or marking it solved, brings the bot back."""
        d = action.detail
        if action.kind == "assign" and d.get("to") in ("operator", "team"):
            who = d.get("email") if d.get("to") == "operator" else ", ".join(d.get("teams") or [])
            await handover.start(self.db, self.phone, str(who or ""), f"workflow “{wf.title}”")
            self.handed_over = True
        elif (action.kind == "assign" and d.get("to") == "bot") or (
                action.kind == "chat_status" and d.get("status") == "solved"):
            await handover.end(self.db, self.phone, f"workflow “{wf.title}” handed it back")

    async def say(self, msg: engine.Message, key: str) -> None:
        logged = await _send(self.phone, msg)
        if logged is None:
            return
        opts = msg.options
        self.db.add(MessageLog(phone_e164=self.phone, direction="out", msg_type=(opts.kind if opts else msg.kind)[:20],
                               text=logged, outcome="workflow_ai" if self.ai else "workflow", step_after=step_label(key),
                               options=json.dumps(opts.to_dict(), ensure_ascii=False) if opts else None))
        self.sent.append(msg)


async def _stop_repeating(conv: _Conv, run: WorkflowRun, wf: LiveWorkflow, language: str) -> Handled:
    """Say once that the answer is not going to change, and let the customer out of the workflow."""
    _finish(run, "the same answer over and over")
    repeat.clear(conv.session)
    text = replies.build("too_many_repeats", replies.ReplyContext(
        support=get_settings().support_contact, customer_name=conv.system.get("sys.customer_name", "")), language)
    await conv.say(engine.Message(text=text), wf.key)
    if repeat.hand_to_person():
        await handover.start(conv.db, conv.phone, "", f"going round in circles in “{wf.title}”")
        conv.handed_over = True
    log.info("workflow_repeat_stopped", phone=conv.phone, workflow=wf.key)
    return Handled(wf.key, conv.sent)


def step_label(key: str) -> str:
    """What the chat log shows as the step: which workflow answered."""
    return f"WF:{key}"[:20]


async def handle(db, session: Session, phone: str, text: str | None, payload: dict | None = None) -> Handled | None:
    """Let a live workflow answer this message. None means it is the order-status bot's."""
    said = (text or "").strip()
    media = media_of(payload)
    if (not said and not media) or not phone:
        return None
    conv: _Conv | None = None
    run: WorkflowRun | None = None
    try:
        live = await registry.live(db)
        run = await db.get(WorkflowRun, phone)
        if not live:
            if run is not None and run.status != "done":
                _finish(run, "switched off")
            return None
        customer = await find_customer(db, phone)
        conv = _Conv(db, session, phone, _system(customer, payload, phone), known=customer is not None)
        conv.media = media
        return await _route(conv, run, live, said, known=customer is not None)
    except Exception as e:  # noqa: BLE001 - a broken workflow must never leave a customer unanswered
        log.error("workflow_failed", phone=phone, error=str(e), tb=traceback.format_exc())
        await alerts.notify_throttled("workflow_failed", "A workflow failed - the order-status bot answered instead",
                                      f"phone={phone}\n{e}", level="warning")
        stuck = (conv.run if conv else None) or run
        if stuck is not None:
            _finish(stuck, "error")
        return None


async def _route(conv: _Conv, run: WorkflowRun | None, live: list[LiveWorkflow], said: str, *,
                 known: bool) -> Handled | None:
    phone = conv.phone
    if run is not None and run.status in ("active", "waiting", "running"):
        wf = next((w for w in live if w.key == run.workflow_key), None)
        if wf is None or not wf.reaches(phone):
            _finish(run, "switched off")
        elif _idle_minutes(run) > wf.idle_minutes:
            # the conversation went quiet; it ended when the customer stopped answering
            _finish(run, "went quiet", at=run.updated_at)
        elif run.status != "active":
            if run.status == "waiting" and run.due_at is not None and run.due_at <= utcnow():
                await _resume(conv, run, wf)  # the pause is over: carry on now rather than wait for the timer
                return Handled(wf.key, conv.sent)
            # A Wait step is still counting down and its next message is on the way; answering now
            # would talk over it.
            return Handled(wf.key, [], code="workflow_wait")
        else:
            graph = await registry.graph(run.workflow_key, run.version, conv.db) or wf.graph
            state = engine.RunState.from_dict(_loads(run.state))
            # The same answer to the same question, over and over: the workflow is going round in a
            # circle it cannot get out of, and repeating it is no help to anybody.
            if repeat.too_many(repeat.note(conv.session, f"{wf.key}:{state.node}:{said}")):
                return await _stop_repeating(conv, run, wf, state.language)
            if state.finished:
                _finish(run, "already finished")
            elif engine.understands(graph, state, said, conv.media):
                return await _advance(conv, run, wf, graph, state, said)
            else:
                other, how = match_trigger(live, said, phone, exclude=wf.key)
                if other is not None:
                    _finish(run, f"switched to {other.key}")
                    return await _start(conv, run, other, how, conv.session.language)
                if _wants_out(said, conv.session.step):
                    _finish(run, "left for the order-status bot")
                    return None
                return await _advance(conv, run, wf, graph, state, said)  # asks again, or gives up

    wf, how = match_trigger(live, said, phone)
    if wf is None and not known and await conv.db.get(NewCustomer, phone) is None:
        # someone already on the New customers list signed up before: they are not greeted again
        wf = next((w for w in live if w.triggers["unknown_customer"] and w.reaches(phone)
                   and not _just_finished(run, w.key)), None)
        how = NEW_NUMBER_TRIGGER
    if wf is None:
        return None
    return await _start(conv, run, wf, how, conv.session.language)


def match_trigger(live: list[LiveWorkflow], said: str, phone: str, exclude: str = "") -> tuple[LiveWorkflow | None, str]:
    """The live workflow this message starts, highest in the list first, and why."""
    n = norm_trigger(said)
    if not n:
        return None, ""
    coded: bool | None = None
    for wf in live:
        if wf.key == exclude or not wf.reaches(phone):
            continue
        menu = wf.triggers["menu"]
        if menu["enabled"] and any(norm_trigger(text_of(menu["label"], lg)) == n for lg in LANGS):
            return wf, "main-menu button"
        for k in wf.triggers["keywords"]:
            kn = norm_trigger(k["text"])
            if not kn:
                continue
            if kn == n:
                return wf, f'keyword "{k["text"]}"'
            if k["match"] == "contains" and trigger_matches(kn, n):
                if coded is None:
                    coded = _has_code(said)
                if not coded:  # "status of SO 45231" is an order lookup, whatever else it contains
                    return wf, f'keyword "{k["text"]}"'
            if k["match"] == "similar" and len(kn) >= 4 and SequenceMatcher(None, kn, n).ratio() >= SIMILAR:
                return wf, f'keyword "{k["text"]}" (similar spelling)'
    return None, ""


async def _start(conv: _Conv, run: WorkflowRun | None, wf: LiveWorkflow, how: str, language: str,
                 variables: dict[str, str] | None = None, jumps: int = 0) -> Handled:
    if run is None:
        run = WorkflowRun(phone_e164=conv.phone, workflow_key=wf.key, version=wf.version)
        conv.db.add(run)
    run.workflow_key, run.version, run.status, run.due_at = wf.key, wf.version, "active", None
    run.trigger, run.started_at, run.state = how[:80], utcnow(), "{}"
    conv.run = run
    log.info("workflow_started", phone=conv.phone, workflow=wf.key, version=wf.version, trigger=how)
    turn = await engine.start(wf.graph, language=language if language in LANGS else "en", system=conv.system,
                              variables=variables, live=conv.live(wf), data=conv.rows)
    await _after(conv, run, wf, turn, jumps)
    return Handled(wf.key, conv.sent)


async def _advance(conv: _Conv, run: WorkflowRun, wf: LiveWorkflow, graph: schema.Graph,
                   state: engine.RunState, said: str) -> Handled:
    conv.run = run
    turn = await engine.advance(graph, state, said, system=conv.system, live=conv.live(wf), lookup=conv.lookup,
                                media=conv.media, matcher=conv.matcher, answerer=conv.answerer, data=conv.rows)
    if turn.ai:
        log.info("workflow_ai", phone=conv.phone, workflow=wf.key, how=turn.ai)
    await _after(conv, run, wf, turn)
    return Handled(wf.key, conv.sent)


async def _resume(conv: _Conv, run: WorkflowRun, wf: LiveWorkflow) -> None:
    graph = await registry.graph(run.workflow_key, run.version, conv.db) or wf.graph
    state = engine.RunState.from_dict(_loads(run.state))
    run.status, run.due_at = "active", None
    conv.run = run
    turn = await engine.resume(graph, state, system=conv.system, live=conv.live(wf), data=conv.rows)
    await _after(conv, run, wf, turn)


async def _after(conv: _Conv, run: WorkflowRun, wf: LiveWorkflow, turn: engine.Turn, jumps: int = 0) -> None:
    """Record where the conversation got to, and follow a hand-off."""
    run.state = json.dumps(turn.state.to_dict(), ensure_ascii=False)
    _language_back(conv.session, turn.state.language)
    finished = turn.stopped in ("end", "no_route", "jump", "handed_over")
    if finished and run.trigger == NEW_NUMBER_TRIGGER and not conv.known:
        await _remember_new_customer(conv, wf, turn.state)
    if conv.handed_over or turn.stopped == "handed_over":
        # A person has the chat: nothing more from the bot - no hand-off, no Wait step carrying on.
        _finish(run, "handed to a person")
        return
    if turn.stopped == "delay":
        run.status, run.due_at = "waiting", utcnow() + timedelta(seconds=turn.delay)
        return
    if turn.stopped == "jump":
        target = turn.jump_to
        carry = {k: v for k, v in turn.state.vars.items() if not k.startswith("sys.")}
        _finish(run, f"handed over to {target}")
        if target != BUILTIN_ORDER_STATUS:
            nxt = await registry.get(target, conv.db)
            if nxt is not None and nxt.reaches(conv.phone) and jumps < MAX_JUMPS:
                await _start(conv, run, nxt, f"from {wf.title}", turn.state.language, carry, jumps + 1)
                return
            log.warning("workflow_jump_target_not_live", workflow=wf.key, target=target)
        # The order-status bot picks the conversation up - also the safe landing for a hand-off
        # to a workflow that is switched off, so nobody is dropped mid-conversation.
        await _order_status_menu(conv, turn.state.language)
        return
    if turn.stopped in ("end", "no_route", "cap"):
        _finish(run, turn.stopped)
        return
    run.status = "active"


async def _remember_new_customer(conv: _Conv, wf: LiveWorkflow, state: engine.RunState) -> None:
    """A new number finished the new-numbers workflow: keep what they told us on the New customers
    list, so the team can follow up and the bot does not greet them as a stranger again."""
    answers = {k: v for k, v in state.vars.items() if not k.startswith("sys.") and str(v).strip()}
    row = await conv.db.get(NewCustomer, conv.phone)
    if row is None:
        row = NewCustomer(phone_e164=conv.phone)
        conv.db.add(row)
    row.whatsapp_name = str(state.vars.get("sys.customer_name") or "")[:255] or None
    row.details = json.dumps(answers, ensure_ascii=False)
    row.workflow_key = wf.key
    log.info("new_customer_listed", phone=conv.phone, workflow=wf.key)


def _finish(run: WorkflowRun, why: str, at: datetime | None = None) -> None:
    log.info("workflow_finished", phone=run.phone_e164, workflow=run.workflow_key, why=why)
    run.status, run.due_at = "done", None
    run.last_key, run.last_finished_at = run.workflow_key, at or utcnow()


async def _order_status_menu(conv: _Conv, language: str) -> None:
    """Hand the conversation to the order-status bot at its main menu."""
    from ..state_machine import step  # lazy: the order-status bot must never depend on workflows

    s = conv.session
    lang = language if language in LANGS else None
    outcomes = await step(conv.db, s, Parsed(intent="menu", raw_text="menu", language=lang), False)
    for o in outcomes:
        opts = await main_menu(o.options, o.language, conv.phone, conv.db) if o.code == "menu" else o.options
        text = replies.build(o.template, o.ctx, o.language)
        await wati.send_options(conv.phone, text, opts)
        conv.db.add(MessageLog(phone_e164=conv.phone, direction="out", msg_type=(opts.kind if opts else "text"),
                               text=text, outcome=o.code, step_after=s.step,
                               options=json.dumps(opts.to_dict(), ensure_ascii=False) if opts else None))
        conv.sent.append(engine.Message(text=text, options=opts))


async def main_menu(options: menus.Options | None, lang: str, phone: str, db=None) -> menus.Options | None:
    """The order-status main menu plus a row for every live workflow that asked for one.

    Three buttons is WhatsApp's limit, so once a workflow adds a row the menu becomes a list. The
    built-in rows keep their exact titles, so tapping them works exactly as before."""
    if options is None:
        return None
    try:
        live = await registry.live(db)
    except Exception as e:  # noqa: BLE001 - never lose the main menu over a workflow lookup
        log.warning("main_menu_workflows_unavailable", error=str(e))
        return options
    rows = list(options.items)
    seen = {norm_trigger(o.title) for o in rows}
    for wf in live:
        menu = wf.triggers["menu"]
        if not menu["enabled"] or not wf.reaches(phone):
            continue
        title = text_of(menu["label"], lang).strip()
        n = norm_trigger(title)
        if not n or n in seen or len(title) > menus.ROW_TITLE_MAX or len(rows) >= menus.LIST_ROWS_MAX:
            continue
        rows.append(menus.Option(title))
        seen.add(n)
    if len(rows) == len(options.items):
        return options
    lg = lang if lang in LANGS else "en"
    return menus.Options(kind="list", items=rows, button_text=MENU_LIST_BUTTON[lg],
                         section_title=menus.label("menu", lg)[:menus.SECTION_TITLE_MAX],
                         header=options.header, footer=options.footer)


# ---------------- talking to WATI ----------------
async def _send(phone: str, msg: engine.Message) -> str | None:
    """Send one workflow message. Returns what the chat log should show, or None if nothing went."""
    if msg.kind == "media" and msg.media:
        url = str(msg.media.get("url") or "")
        caption = "\n\n".join(dict.fromkeys(x for x in (str(msg.media.get("caption") or ""), msg.text or "") if x))
        await wati.send_file_url(phone, url, caption, str(msg.media.get("type") or "image"))
        return "\n".join(x for x in (caption, url) if x)
    if msg.kind == "product_list":
        # WATI's documented API has no call for catalogue messages, so the list goes as its text.
        text = "\n\n".join(x for x in (msg.header, msg.text) if x)
        if not text:
            return None
        await wati.send_text(phone, text)
        return text
    if not msg.text and not msg.options:
        return None
    opts = msg.options
    media = msg.media if opts is not None and opts.kind == "buttons" else None
    sections = msg.sections if opts is not None and opts.kind == "list" and msg.sections and len(msg.sections) > 1 else None
    await wati.send_options(phone, msg.text, opts, media=media, sections=sections)
    return msg.text


async def _perform(phone: str, action: act.Action, key: str) -> bool:
    """Do what a step asks in WATI. False sends an Assign or a template down its "If it fails" exit."""
    d = action.detail
    try:
        if action.kind == "tags":
            for tag in d.get("tags") or []:
                await (wati.remove_tag if d.get("remove") else wati.add_tag)(phone, tag)
        elif action.kind == "assign":
            to = d.get("to")
            if to == "team":
                await wati.assign_teams(phone, list(d.get("teams") or []))
            elif to == "operator":
                await wati.assign_operator(phone, str(d.get("email") or "") or None)
            else:
                await wati.assign_operator(phone, None)  # back to the bot
        elif action.kind == "chat_status":
            await wati.set_chat_status(phone, str(d.get("status") or "open"))
        elif action.kind == "subscribe":
            # WATI's API cannot change broadcast opt-in, so this is kept on the contact where
            # broadcasts can filter on it.
            await wati.set_contact_attributes(phone, {"subscribed": "true" if d.get("subscribe") else "false"})
        elif action.kind == "attributes":
            await wati.set_contact_attributes(phone, {str(k): str(v) for k, v in (d.get("attributes") or {}).items()})
        elif action.kind == "template":
            await wati.send_template_message(phone, str(d.get("name") or ""), dict(d.get("params") or {}),
                                             broadcast=f"workflow-{key}")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("workflow_action_failed", phone=phone, workflow=key, kind=action.kind, error=str(e))
        await alerts.notify_throttled(f"workflow_action_{action.kind}", f"A workflow step could not be done in WATI ({action.kind})",
                                      f"workflow={key} phone={phone}\n{e}", level="warning")
        return False


# ---------------- the timer ----------------
async def resume_due(limit: int = 20) -> int:
    """Carry on every conversation whose Wait step is over. Run every few seconds by the scheduler."""
    now = utcnow()
    async with session_scope() as db:
        # a resume that crashed half way must not park a customer for ever
        await db.execute(update(WorkflowRun).where(WorkflowRun.status == "running",
                                                   WorkflowRun.updated_at < now - timedelta(minutes=5))
                         .values(status="done", last_finished_at=now))
        phones = (await db.execute(select(WorkflowRun.phone_e164).where(
            WorkflowRun.status == "waiting", WorkflowRun.due_at <= now).order_by(WorkflowRun.due_at).limit(limit))).scalars().all()
    resumed = 0
    for phone in phones:
        async with session_scope() as db:
            # the claim: only one resumer gets each conversation
            got = await db.execute(update(WorkflowRun).where(
                WorkflowRun.phone_e164 == phone, WorkflowRun.status == "waiting", WorkflowRun.due_at <= now)
                .values(status="running"))
            if not got.rowcount:
                continue
        try:
            async with session_scope() as db:
                run = await db.get(WorkflowRun, phone)
                if run is None:
                    continue
                wf = await registry.get(run.workflow_key, db)
                if wf is None or not wf.reaches(phone):
                    _finish(run, "switched off")
                    continue
                if await handover.active(db, phone) is not None:
                    _finish(run, "handed to a person")  # a person has the chat; the pause ends silently
                    continue
                from ..state_machine import get_or_create_session  # lazy, as above

                session = await get_or_create_session(db, phone)
                customer = await find_customer(db, phone)
                system = _system(customer, None, phone)
                if customer is None:
                    system.pop("customer_name")  # keep the WhatsApp name the run started with
                conv = _Conv(db, session, phone, system, known=customer is not None)
                await _resume(conv, run, wf)
                resumed += 1
        except Exception as e:  # noqa: BLE001
            log.error("workflow_resume_failed", phone=phone, error=str(e), tb=traceback.format_exc())
            async with session_scope() as db:
                run = await db.get(WorkflowRun, phone)
                if run is not None:
                    _finish(run, "error")
    return resumed


# ---------------- the dashboard ----------------
async def active_runs(limit: int = 200) -> list[dict]:
    """Customers inside a workflow right now."""
    async with session_scope() as db:
        rows = (await db.execute(select(WorkflowRun).where(WorkflowRun.status != "done")
                                 .order_by(WorkflowRun.updated_at.desc()).limit(limit))).scalars().all()
        titles = {w.key: w.title for w in (await db.execute(select(Workflow))).scalars()}
        out = []
        for r in rows:
            state = _loads(r.state)
            graph = await registry.graph(r.workflow_key, r.version, db)
            node = graph.node(str(state.get("node") or "")) if graph else None
            out.append({"phone": r.phone_e164, "workflow": r.workflow_key,
                        "title": titles.get(r.workflow_key, r.workflow_key), "version": r.version,
                        "status": r.status, "step": str((node or {}).get("title") or ""), "trigger": r.trigger or "",
                        "started_at": _iso(r.started_at), "updated_at": _iso(r.updated_at), "due_at": _iso(r.due_at)})
    return out


async def end_run(phone: str) -> bool:
    """Let a customer out of their workflow; their next message is routed afresh."""
    async with session_scope() as db:
        run = await db.get(WorkflowRun, phone)
        if run is None or run.status == "done":
            return False
        _finish(run, "ended from the dashboard")
    return True


async def overview() -> dict:
    """How incoming messages are shared out, for the Workflows page."""
    live = await registry.live()
    async with session_scope() as db:
        counts = dict((await db.execute(select(WorkflowRun.workflow_key, func.count()).where(
            WorkflowRun.status != "done").group_by(WorkflowRun.workflow_key))).all())
    menu = {}
    for lg in LANGS:
        shown = await main_menu(menus.buttons_for("main_menu", lg), lg, "")
        menu[lg] = shown.titles() if shown else []
    return {
        "workflows": [{
            "key": w.key, "title": w.title, "version": w.version, "priority": w.priority,
            "for_everyone": w.for_everyone, "keywords": w.triggers["keywords"],
            "menu_label": w.triggers["menu"]["label"]["en"] if w.triggers["menu"]["enabled"] else "",
            "unknown_customer": w.triggers["unknown_customer"], "idle_minutes": w.idle_minutes,
            "jumps_to": sorted(j for j in w.jumps_to if j and j != w.key),
            "reached_from": [o.key for o in live if w.key in o.jumps_to and o.key != w.key],
            "active": counts.get(w.key, 0)} for w in live],
        "main_menu": menu,
        "new_numbers": next((w.key for w in live if w.triggers["unknown_customer"] and w.for_everyone), None),
        "test_numbers": sorted(test_numbers()),
        "conflicts": _conflicts(live),
    }


def _conflicts(live: list[LiveWorkflow]) -> list[str]:
    out: list[str] = []
    owner: dict[str, LiveWorkflow] = {}
    for w in live:  # highest in the list first, so the first owner is the one that answers
        words = [k["text"] for k in w.triggers["keywords"]]
        if w.triggers["menu"]["enabled"]:
            words += [v for v in w.triggers["menu"]["label"].values() if v]
        for word in dict.fromkeys(words):
            n = norm_trigger(word)
            first = owner.setdefault(n, w)
            if first is not w:
                out.append(f'"{word}" starts both “{first.title}” and “{w.title}”. “{first.title}” answers, '
                           "because it is higher in the list.")
    greeters = [w for w in live if w.triggers["unknown_customer"] and w.for_everyone]
    if len(greeters) > 1:
        out.append(f"{len(greeters)} live workflows greet new numbers; only “{greeters[0].title}” does, because it "
                   "is highest in the list.")
    return out


# ---------------- publish-time checks across workflows ----------------
async def check_routing(doc: dict, key: str = "", db=None) -> list[Issue]:
    """How this workflow would sit among the live ones and the order-status bot.

    validate_graph checks a workflow on its own; this checks what only makes sense next to the
    others: two workflows claiming one keyword, a menu row that duplicates a built-in button, a
    hand-off to a workflow that does not exist."""
    issues: list[Issue] = []
    t = schema.triggers_of(doc)
    try:
        others = [w for w in await registry.live(db) if w.key != key]
        known = await _workflow_keys(db)
    except Exception as e:  # noqa: BLE001 - these checks are advice; they must never block a save
        log.warning("routing_check_failed", error=str(e))
        others, known = [], {}
    builtin = dict(button_phrases())
    main_titles = {norm_trigger(title) for lg in LANGS
                   for title in (menus.buttons_for("main_menu", lg) or menus.Options(kind="buttons")).titles()}
    where = "settings.triggers"

    for kw in t["keywords"]:
        text, n = kw["text"], norm_trigger(kw["text"])
        if not n:
            continue
        parsed = regex_parse(text)
        if parsed.so_no or parsed.po_no or parsed.fg_code or parsed.bare_codes:
            issues.append(Issue("fail", f'The keyword "{text}" reads like an order or item number. Customers who send '
                                        "that number to check an order would get this workflow instead.", None, where))
            continue
        if n in builtin or parsed.intent != "other":
            what = f'"{text}" button' if n in builtin else _INTENT_WORDS.get(parsed.intent, "reply to it")
            issues.append(Issue("warn", f'Customers who send "{text}" will get this workflow instead of the '
                                        f"order-status bot's {what}. That is right if this workflow should greet "
                                        "them first - it can hand them on with a Go to workflow step to the "
                                        "order-status menu. Otherwise pick another keyword.", None, where))
        for w in others:
            if _claims(w, n):
                issues.append(Issue("warn", f'"{text}" also starts “{w.title}”. Whichever is higher in the Workflows '
                                            "list answers.", None, where))

    menu = t["menu"]
    if menu["enabled"]:
        if not menu["label"]["en"]:
            issues.append(Issue("fail", "The main-menu button has no English label.", None, where))
        for label in dict.fromkeys(v for v in menu["label"].values() if v):
            n = norm_trigger(label)
            if len(label) > menus.ROW_TITLE_MAX:
                issues.append(Issue("fail", f'The main-menu button "{label}" is {len(label)} characters; WhatsApp '
                                            f"allows {menus.ROW_TITLE_MAX} in a menu row.", None, where))
            if n in main_titles:
                issues.append(Issue("fail", f'"{label}" is already a button in the order-status main menu. Pick a '
                                            "different label.", None, where))
                continue
            for w in others:
                other_menu = w.triggers["menu"]
                if other_menu["enabled"] and n in {norm_trigger(v) for v in other_menu["label"].values() if v}:
                    issues.append(Issue("fail", f'"{label}" is already the main-menu button of “{w.title}”. Two rows '
                                                "with one label cannot be told apart.", None, where))
                elif _claims(w, n):
                    issues.append(Issue("warn", f'"{label}" is also a keyword of “{w.title}”. Whichever is higher in '
                                                "the Workflows list answers the tap.", None, where))
        base = len((menus.buttons_for("main_menu", "en") or menus.Options(kind="buttons")).items)
        rows = base + 1 + sum(1 for w in others if w.triggers["menu"]["enabled"])
        if rows > menus.LIST_ROWS_MAX:
            issues.append(Issue("warn", f"The main menu can show {menus.LIST_ROWS_MAX} rows; with this one there "
                                        f"would be {rows}, so the lowest in the Workflows list are left out.", None, where))

    if t["unknown_customer"]:
        for w in others:
            if w.triggers["unknown_customer"]:
                issues.append(Issue("warn", f"“{w.title}” also greets numbers that are not in the customer list. Only "
                                            "the one higher in the Workflows list does.", None, where))

    for node in doc.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "jump":
            continue
        target = str(node.get("workflow") or "").strip()
        if not target or target in (BUILTIN_ORDER_STATUS, key):
            continue
        name = f"“{node.get('title')}”" if node.get("title") else "A Go-to-workflow step"
        if target not in known:
            issues.append(Issue("fail", f'{name} hands over to a workflow that does not exist ("{target}"). Pick one '
                                        "from the list.", str(node.get("id") or "") or None))
        elif not known[target]["live"]:
            issues.append(Issue("warn", f"{name} hands over to “{known[target]['title']}”, which is not live. Until it "
                                        "is, customers who reach this step get the order-status main menu instead.",
                                str(node.get("id") or "") or None))

    reached = bool(key) and any(key in w.jumps_to for w in others)
    if not (t["keywords"] or menu["enabled"] or t["unknown_customer"] or reached):
        issues.append(Issue("warn", "Nothing starts this workflow yet - no keyword, no main-menu button, and no other "
                                    "live workflow hands over to it. Set how it starts in Start settings.", None, where))

    raw = (doc.get("settings") or {}).get("idle_minutes") if isinstance(doc.get("settings"), dict) else None
    if raw not in (None, ""):
        try:
            bad = not 1 <= int(raw) <= schema.IDLE_MINUTES_MAX
        except (TypeError, ValueError):
            bad = True
        if bad:
            issues.append(Issue("fail", f"End after inactivity must be between 1 and {schema.IDLE_MINUTES_MAX} minutes.",
                                None, "settings.idle_minutes"))

    uses_ai = any(isinstance(n, dict) and n.get("type") == "ai_reply" for n in doc.get("nodes") or [])
    if (uses_ai or (doc.get("settings") or {}).get("ai_understand")) and not ai_configured():
        issues.append(Issue("warn", "This workflow uses AI, but no Groq API key is set (Settings, AI assistant). "
                                    "Until one is, Answer-with-AI steps take their Not sure exit and typed answers "
                                    "are only matched word for word.", None, "settings"))

    unique: dict[tuple, Issue] = {}
    for i in issues:
        unique.setdefault((i.level, i.message, i.node_id), i)
    return list(unique.values())


def _claims(wf: LiveWorkflow, n: str) -> bool:
    """Does this live workflow already start on this (normalised) word?"""
    if any(norm_trigger(k["text"]) == n for k in wf.triggers["keywords"]):
        return True
    menu = wf.triggers["menu"]
    return menu["enabled"] and n in {norm_trigger(v) for v in menu["label"].values() if v}


async def _workflow_keys(db=None) -> dict[str, dict]:
    async def load(s) -> dict[str, dict]:
        rows = (await s.execute(select(Workflow))).scalars().all()
        return {w.key: {"title": w.title, "live": bool(w.enabled and w.published_version)} for w in rows}

    if db is not None:
        return await load(db)
    async with session_scope() as own:
        return await load(own)


# ---------------- small helpers ----------------
def _system(customer, payload: dict | None, phone: str) -> dict[str, str]:
    """What a workflow knows about who it is talking to. A number not in the customer list gets
    the name they chose on WhatsApp, which WATI sends with every message."""
    name = customer.customer_name if customer else str((payload or {}).get("senderName") or "").strip()
    return {"customer_name": name, "phone": phone, "verified": "yes" if customer else "no", **schema.clock_vars()}


_MEDIA_KINDS = {"image": "image", "document": "document", "video": "video", "sticker": "image"}


def media_of(payload: dict | None) -> dict | None:
    """A picture, document, video or location the customer sent, as {type, url, address}.

    WATI documents `data` only as "object/null", so both a plain link and an object with a link
    (or coordinates) are read. Voice notes are not here: they become text through speech-to-text."""
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("type") or "").lower()
    data = payload.get("data")
    if kind == "location":
        lat = lng = None
        address = ""
        if isinstance(data, dict):
            lat = data.get("latitude", data.get("lat"))
            lng = data.get("longitude", data.get("lng", data.get("long")))
            address = str(data.get("address") or data.get("name") or "")
        text = data if isinstance(data, str) else str(payload.get("text") or "")
        if lat is None or lng is None:
            m = re.search(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", text)
            if m:
                lat, lng = m.group(1), m.group(2)
        url = f"https://maps.google.com/?q={lat},{lng}" if lat is not None and lng is not None else ""
        return {"type": "location", "url": url, "address": address}
    if kind in _MEDIA_KINDS:
        link = data
        if isinstance(data, dict):
            link = data.get("url") or data.get("link") or data.get("fileName") or data.get("file") or ""
        return {"type": _MEDIA_KINDS[kind], "url": str(link or ""), "address": ""}
    return None


def _language_back(session: Session, language: str) -> None:
    """A language chosen inside a workflow carries on into the order-status bot."""
    if language in LANGS and language != session.language:
        session.language = language
        session.lang_chosen = True


def _has_code(said: str) -> bool:
    p = regex_parse(said)
    return bool(p.so_no or p.po_no or p.fg_code or p.bare_codes)


def _wants_out(said: str, step: str | None) -> bool:
    p = regex_parse(said, step=step)
    return p.intent in _LEAVE_INTENTS or bool(p.so_no or p.po_no or p.fg_code)


def _just_finished(run: WorkflowRun | None, key: str) -> bool:
    """A greeting for new numbers fires once per conversation, not on every message."""
    if run is None or run.last_key != key or run.last_finished_at is None:
        return False
    return utcnow() - run.last_finished_at < timedelta(minutes=get_settings().session_timeout_min)


def _idle_minutes(run: WorkflowRun) -> float:
    return (utcnow() - run.updated_at).total_seconds() / 60 if run.updated_at else 0.0


def _loads(raw: str | None) -> dict:
    try:
        got = json.loads(raw or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
