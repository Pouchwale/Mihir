"""The shape of a drawn workflow, and everything that makes one safe to publish.

The graph is a JSON document, so nothing stops the editor sending nonsense. validate_graph is the
gate: every rule that could reach a customer as a broken conversation is checked here, once, in the
place both the editor and the engine agree on.

Rules live here rather than in TypeScript because the hard ones already exist in Python -
templates.trigger_matches knows that a Devanagari vowel sign breaks a word boundary, and menus knows
what WhatsApp actually accepts. A second implementation in the browser would drift, and the drift
would only show up as a customer's tap doing nothing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from .. import menus

LANGS = ("en", "hi", "gu")
SCHEMA_VERSION = 1

# A turn must not be able to run forever, whatever the owner drew.
MAX_VISITS_PER_TURN = 25
MAX_MESSAGES_PER_TURN = 6

# The node set WATI's own chatbot builder offers, so a flow drawn there can be rebuilt here.
#   say      : message, question
#   route    : condition, jump
#   remember : set_var, tags, subscribe
#   people   : assign, chat_status
#   outside  : api_request, template
#   pacing   : delay
#   catalogue: product_list (a WhatsApp product list from a catalogue connected in WATI)
#   AI       : ai_reply (answers the customer's own question from the owner's FAQ text - not in WATI)
#   data     : data (finds rows in the business's own orders / customer list - not in WATI)
NODE_TYPES = ("message", "question", "condition", "set_var", "end", "delay", "tags", "assign",
              "chat_status", "subscribe", "api_request", "template", "jump", "product_list", "ai_reply",
              "data")
# Steps that stop and wait for the customer to say something.
WAITING_TYPES = ("question", "ai_reply")
AI_FAQ_MAX = 8000
AI_TONES = ("friendly", "formal")
AI_ANSWER_MIN, AI_ANSWER_MAX = 100, 1024

# Nodes that do something to the outside world rather than say something. The engine records them as
# Actions; who actually performs them depends on the caller, which is what lets the simulator show a
# flow end to end without assigning a real chat or tagging a real customer.
# "delay" is here so a dry run SHOWS the pause. A step that happens invisibly is one the
# owner cannot check.
ACTION_TYPES = ("tags", "assign", "chat_status", "subscribe", "template", "delay")

CHAT_STATUSES = ("open", "pending", "solved", "block")
HTTP_METHODS = ("GET", "POST")
DELAY_MAX_SEC = 600  # WATI caps its Time Delay node at 10 minutes
INPUT_KINDS = ("buttons", "list", "text", "language", "data_list")
CONDITION_OPS = ("eq", "ne", "contains", "starts_with", "ends_with", "gt", "gte", "lt", "lte",
                 "is_set", "is_empty", "between")
VALIDATE_TYPES = ("any", "number", "email", "phone", "regex", "date", "url", "time", "file", "location", "lookup")
# What an answer can be checked against in the business's own data (workflow/lookup.py), and the
# values saved beside the answer when it is found - {order_status}, {order_items}, ...
LOOKUP_SOURCES = ("so", "po", "fg", "customer_code")
LOOKUP_FIELDS = {"so": ("status", "so", "po", "items", "count"), "po": ("status", "so", "po", "items", "count"),
                 "fg": ("status", "so", "po", "items", "count"), "customer_code": ("name",)}
FILE_ANSWERS = ("image", "document", "video", "audio")

# ---------------- "Find in your data" ----------------
# What a workflow may read, and what it may show. connection_status is deliberately absent: it is the
# internal status, and the one field that must never reach a customer.
DATA_SOURCES = ("orders", "customer")
DATA_FINDS = ("all", "so", "po", "fg")
DATA_FIELDS: dict[str, tuple[str, ...]] = {
    "orders": ("so_no", "po_no", "fg_item_code", "fg_description", "real_status", "customer_name"),
    "customer": ("customer_code", "customer_name"),
}
# Fields that identify one row. A title taken from anything else can repeat, and two rows a customer
# cannot tell apart is worse than no list at all.
DATA_KEY_FIELDS = ("so_no", "po_no", "fg_item_code", "customer_code")
DATA_OPS = ("eq", "ne", "contains", "is_set", "is_empty")
DATA_SORTS = ("newest", "oldest", "as_is")
# How a data list is drawn: WhatsApp buttons read better for two or three rows, a list beyond that.
DATA_SHOW = ("auto", "list", "buttons")
DATA_GROUPS = ("so", "line")
# What one run may carry. A run is stored as JSON per customer and round-trips through the browser in
# the test chat, so the rows a step remembers are capped at every edge.
DATA_SETS_MAX = 2
DATA_ROWS_MAX = menus.LIST_ROWS_MAX  # the engine can never draw more than WhatsApp shows
DATA_FIELDS_MAX = 8
DATA_VALUE_MAX = 80
DATA_LIST_LINE = "SO {so_no} - {real_status}"
MEDIA_TYPES = ("image", "video", "document", "audio")

_VAR_NAME = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Read-only facts a workflow may test but never assign.
SYSTEM_VARS = ("sys.language", "sys.phone", "sys.customer_name", "sys.verified", "sys.api_status",
               "sys.api_error", "sys.hour", "sys.time", "sys.weekday", "sys.date")
# India does not change its clocks, so a fixed offset is exact - and needs no timezone database.
IST = timezone(timedelta(hours=5, minutes=30))


def clock_vars(now: datetime | None = None) -> dict[str, str]:
    """The time a message arrives, for working-hours branches: {sys.hour}, {sys.time}, {sys.weekday}."""
    now = now or datetime.now(IST)
    return {"hour": str(now.hour), "time": now.strftime("%H:%M"), "weekday": now.strftime("%a"),
            "date": now.strftime("%d/%m/%Y")}


def answer_extras(node: dict) -> list[str]:
    """Values a question saves beside its answer: after an SO check {order_status}, {order_items}...;
    after a photo or a location {photo_type}, {photo_address}."""
    store = str(node.get("store") or "")
    rule = node.get("validate") or {}
    kind = rule.get("type")
    if not store:
        return []
    if kind == "lookup":
        return [f"{store}_{f}" for f in LOOKUP_FIELDS.get(str(rule.get("source") or ""), ())]
    if kind in ("file", "location"):
        return [f"{store}_type", f"{store}_address"]
    return []

Level = Literal["fail", "warn"]


@dataclass
class Issue:
    level: Level
    message: str
    node_id: str | None = None
    field: str = ""

    def to_dict(self) -> dict:
        return {"level": self.level, "message": self.message, "node_id": self.node_id, "field": self.field}


@dataclass
class Graph:
    """A parsed, indexed graph. Built only by parse(), so the engine never sees a raw dict."""

    start: str
    nodes: dict[str, dict]
    order: list[str]  # node ids in document order, for stable UI listing
    edges: list[dict]
    settings: dict = field(default_factory=dict)
    # (node_id, port) -> target node id. Built once so the engine never scans the edge list.
    routes: dict[tuple[str, str], str] = field(default_factory=dict)

    def node(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def next_of(self, node_id: str, port: str) -> str | None:
        return self.routes.get((node_id, port))


def empty_graph() -> dict:
    """A brand-new workflow: one message node, so the canvas is never blank."""
    return {
        "schema": SCHEMA_VERSION,
        "start": "n1",
        "settings": {},
        "nodes": [{"id": "n1", "type": "message", "title": "First message", "x": 80, "y": 80,
                   "text": {"en": "", "hi": "", "gu": ""}}],
        "edges": [],
    }


def parse(doc: dict | str) -> Graph:
    """Index a document for execution. Assumes validate_graph has passed; tolerates a missing edge
    (it simply has no route) so a half-drawn draft can still be previewed."""
    if isinstance(doc, str):
        doc = json.loads(doc)
    nodes = {}
    order = []
    for n in doc.get("nodes") or []:
        if isinstance(n, dict) and n.get("id"):
            nodes[str(n["id"])] = n
            order.append(str(n["id"]))
    edges = [e for e in (doc.get("edges") or []) if isinstance(e, dict)]
    routes: dict[tuple[str, str], str] = {}
    for e in edges:
        src, port, dst = str(e.get("from") or ""), str(e.get("port") or "next"), str(e.get("to") or "")
        if src in nodes and dst in nodes:
            routes.setdefault((src, port), dst)  # first edge wins; validation rejects duplicates
    start = str(doc.get("start") or (order[0] if order else ""))
    return Graph(start=start, nodes=nodes, order=order, edges=edges,
                 settings=dict(doc.get("settings") or {}), routes=routes)


# ---------------- ports ----------------
def option_port(value: str) -> str:
    return f"opt:{value}"


def branch_port(branch_id: str) -> str:
    return f"branch:{branch_id}"


# ---------------- the language question ----------------
LANGUAGE_NAMES = {"en": "English", "hi": "Hindi", "gu": "Gujarati"}


def offered_languages(spec: dict) -> list[str]:
    """Which language buttons a language question shows - all three unless the owner chose fewer."""
    got = [c for c in (spec.get("languages") or LANGS) if c in LANGS]
    return list(dict.fromkeys(got)) or list(LANGS)


def language_label(spec: dict, code: str, lang: str) -> str:
    """A language button's text: the owner's own wording, else the one on the Bot messages page."""
    custom = str((spec.get("language_labels") or {}).get(code) or "").strip()
    return custom or menus.label(f"lang_{code}", lang)


def to_person(node: dict) -> bool:
    """An Assign step that gives the chat to a person rather than back to the bot."""
    return node.get("type") == "assign" and (node.get("to") or "operator") != "bot"


def ports_of(node: dict) -> list[tuple[str, str]]:
    """Every outgoing port of a node as (port, human label), in the order the editor should draw
    them. The single source of truth for handles on the canvas AND for dangling-edge validation."""
    kind = node.get("type")
    if kind in ("message", "set_var", "delay", "tags", "chat_status", "subscribe", "product_list"):
        return [("next", "Next")]
    if kind in ("end", "jump"):
        return []  # a jump leaves this workflow, so nothing follows it here
    if kind == "ai_reply":
        # "Not sure" is also where the conversation goes when the AI cannot be reached
        return [("answered", "Answered"), ("unsure", "Not sure")]
    if kind == "data":
        return [("found", "Found something"), ("none", "Nothing found")]
    if to_person(node):
        # A person takes the chat and the bot goes quiet, so nothing follows - except when WATI
        # refuses the assignment, and then the customer must not be left in silence.
        return [("on_error", "If it fails")]
    if kind in ("assign", "template"):
        # These can be refused by WATI (a plan that does not allow it, a template Meta rejected), and
        # a customer must never be left in silence because of it.
        return [("next", "Done"), ("on_error", "If it fails")]
    if kind == "api_request":
        out = [("success", "Worked"), ("failed", "Failed")]
        for r in node.get("routes") or []:
            if isinstance(r, dict) and r.get("id"):
                out.insert(-1, (f"route:{r['id']}", str(r.get("label") or r["id"])))
        return out
    if kind == "condition":
        out = [(branch_port(str(b.get("id"))), str(b.get("label") or b.get("id") or ""))
               for b in (node.get("branches") or []) if isinstance(b, dict)]
        out.append(("else", "Otherwise"))
        return out
    if kind == "question":
        spec = node.get("input") or {}
        out: list[tuple[str, str]] = []
        if spec.get("kind") == "language":
            # One exit per language offered, named by its button. Listed here, or the editor could
            # not connect them.
            labels = spec.get("language_labels") or {}
            out.extend((option_port(code), str(labels.get(code) or "").strip() or LANGUAGE_NAMES[code])
                       for code in offered_languages(spec))
        elif spec.get("kind") == "data_list":
            # One exit, never one per row: the rows are only known while a customer is talking.
            out.extend([("next", "They chose one"), ("default", "Anything else"),
                        ("empty", "Nothing to show")])
        elif spec.get("kind") in ("buttons", "list"):
            for o in spec.get("options") or []:
                if isinstance(o, dict) and o.get("value"):
                    out.append((option_port(str(o["value"])), text_of(o.get("label"), "en") or str(o["value"])))
            # WATI's "default" route: where to go when the customer types instead of tapping.
            # Optional - left unconnected, the question simply asks again.
            out.append(("default", "Anything else"))
        else:
            out.append(("next", "Answered"))
        if (node.get("validate") or {}).get("type", "any") != "any":
            out.append(("invalid", "Not valid"))
        if int(node.get("max_retries") or 0) > 0:
            out.append(("retry_exhausted", "Gave up"))
        return out
    return []


def text_of(value: Any, lang: str) -> str:
    """One trilingual string resolved for a language, falling back to English then to anything set.

    The engine and the answer matcher MUST use this same function: if a Gujarati customer is shown an
    English button because gu was empty, the text they send back is that English title, and a
    matcher that resolved differently would silently ignore the tap."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    for lg in (lang, "en", *LANGS):
        got = value.get(lg)
        if isinstance(got, str) and got.strip():
            return got
    return ""


def _choice_name(is_list: bool, i: int, option: dict) -> str:
    """How a warning names a button or row: by what it says, which the owner can find at a glance."""
    word = "Row" if is_list else "Button"
    english = text_of(option.get("label"), "en").strip()
    return f'{word} "{english[:30]}"' if english else f"{word} {i + 1}"


def walk_strings(doc: dict) -> list[dict]:
    """Every customer-visible trilingual string in the document.

    One generator feeds validation, the completeness chip and the bulk translation table, so those
    three can never disagree about what still needs translating. `required` marks what the customer
    always sees; headers, footers, captions, descriptions and an End's goodbye may be left blank."""
    out: list[dict] = []

    def add(node_id: str, path: str, label: str, value: Any, max_len: int, required: bool = True) -> None:
        if isinstance(value, dict):
            out.append({"node_id": node_id, "path": path, "label": label, "max": max_len,
                        "required": required, "value": {lg: str(value.get(lg) or "") for lg in LANGS}})

    for n in doc.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        kind = n.get("type")
        if kind in ("message", "question", "end", "product_list"):
            media = n.get("media") if kind == "message" else None
            picture_only = isinstance(media, dict) and bool(media.get("url"))
            add(nid, "text", "Message", n.get("text"), menus.BODY_MAX,
                required=kind in ("message", "question", "product_list") and not picture_only)
        if kind == "ai_reply":
            add(nid, "text", "Opening line", n.get("text"), menus.BODY_MAX, required=False)
        if kind == "message" and isinstance(n.get("media"), dict):
            add(nid, "media.caption", "Caption", n["media"].get("caption"), menus.BODY_MAX, required=False)
        if kind == "product_list":
            add(nid, "header", "Header", n.get("header"), menus.HEADER_MAX, required=False)
        if kind == "question":
            spec = n.get("input") or {}
            is_list = spec.get("kind") in ("list", "data_list")
            if spec.get("kind") in ("buttons", "list", "language", "data_list"):
                add(nid, "header", "Header", n.get("header"), menus.HEADER_MAX, required=False)
                add(nid, "footer", "Footer", n.get("footer"), menus.FOOTER_MAX, required=False)
            if is_list:
                add(nid, "input.button_text", "List button", spec.get("button_text"), menus.LIST_BUTTON_MAX)
                add(nid, "input.section_title", "Section title", spec.get("section_title"),
                    menus.SECTION_TITLE_MAX, required=False)
            cap = menus.ROW_TITLE_MAX if is_list else menus.BUTTON_TEXT_MAX
            for i, o in enumerate(spec.get("options") or []):
                if isinstance(o, dict):
                    name = _choice_name(is_list, i, o)
                    add(nid, f"input.options.{i}.label", name, o.get("label"), cap)
                    if is_list:
                        add(nid, f"input.options.{i}.description", f"The description of {name}",
                            o.get("description"), menus.ROW_DESC_MAX, required=False)
                        add(nid, f"input.options.{i}.section", f"The section of {name}",
                            o.get("section"), menus.SECTION_TITLE_MAX, required=False)
            if n.get("retry_text"):
                add(nid, "retry_text", "Ask again", n.get("retry_text"), menus.BODY_MAX)
            if (n.get("validate") or {}).get("type", "any") != "any":
                add(nid, "invalid_text", "Not valid", n.get("invalid_text"), menus.BODY_MAX)
    return out


# ---------------- going live ----------------
# The built-in order-status bot as a Go-to-workflow target. slugify never produces "@", so this can
# never collide with a workflow the owner happens to call "Order status".
BUILTIN_ORDER_STATUS = "@order-status"
KEYWORD_MATCHES = ("exact", "contains", "similar")
IDLE_MINUTES_DEFAULT = 30
IDLE_MINUTES_MAX = 1440


def _settings(doc: dict) -> dict:
    s = doc.get("settings") if isinstance(doc, dict) else None
    return s if isinstance(s, dict) else {}


def triggers_of(doc: dict) -> dict:
    """How a workflow starts, normalised. Stored in the document's settings, so a change to how it
    starts is drafted, published and rolled back together with the steps it starts."""
    raw = _settings(doc).get("triggers")
    raw = raw if isinstance(raw, dict) else {}
    keywords = []
    for k in raw.get("keywords") or []:
        if isinstance(k, str):
            k = {"text": k}
        if isinstance(k, dict) and str(k.get("text") or "").strip():
            match = k.get("match") if k.get("match") in KEYWORD_MATCHES else "exact"
            keywords.append({"text": str(k["text"]).strip()[:60], "match": match})
    menu = raw.get("menu") if isinstance(raw.get("menu"), dict) else {}
    label = menu.get("label") if isinstance(menu.get("label"), dict) else {}
    return {"keywords": keywords,
            "menu": {"enabled": bool(menu.get("enabled")),
                     "label": {lg: str(label.get(lg) or "").strip() for lg in LANGS}},
            "unknown_customer": bool(raw.get("unknown_customer"))}


def digits(phone: str) -> str:
    return re.sub(r"\D", "", str(phone or ""))


def idle_minutes_of(doc: dict) -> int:
    """After this long without a reply a customer is let out of the workflow."""
    try:
        n = int(_settings(doc).get("idle_minutes") or IDLE_MINUTES_DEFAULT)
    except (TypeError, ValueError):
        return IDLE_MINUTES_DEFAULT
    return max(1, min(n, IDLE_MINUTES_MAX))


def describe_triggers(doc: dict) -> list[str]:
    """How a workflow starts, in words, for the Workflows list."""
    t = triggers_of(doc)
    how = {"contains": " (anywhere in a message)", "similar": " (or a similar spelling)"}
    out = [f'"{k["text"]}"' + how.get(k["match"], "") for k in t["keywords"]]
    if t["menu"]["enabled"] and t["menu"]["label"]["en"]:
        out.append(f'Main-menu button "{t["menu"]["label"]["en"]}"')
    if t["unknown_customer"]:
        out.append("New numbers (not in the customer list)")
    return out


def data_fields(source: str) -> tuple[str, ...]:
    """The fields a workflow may read from a source - never connection_status."""
    return DATA_FIELDS.get(source if source in DATA_SOURCES else "orders", ())


def data_vars(node: dict) -> list[str]:
    """What a "Find in your data" step leaves behind: {orders_count}, {orders_list}, and whatever the
    owner chose to remember from the first row under their own names."""
    store = str(node.get("store") or "")
    out = [f"{store}_count", f"{store}_list"] if store else []
    out += [str(name) for name in (node.get("save") or {}) if _VAR_NAME.match(str(name))]
    return out


def data_answer_extras(node: dict, data_node: dict | None) -> list[str]:
    """Values saved beside the row a customer picked: the row's own fields, and the fresh order
    details from checking it again ({pick_status}, {pick_items}, ...)."""
    store = str(node.get("store") or "")
    if not store:
        return []
    source = str((data_node or {}).get("source") or "orders")
    out = [f"{store}_{f}" for f in data_fields(source)]
    if source == "orders":
        out += [f"{store}_{f}" for f in LOOKUP_FIELDS["so"]]
    return out


def data_node_of(by_id: dict, spec: dict) -> dict | None:
    """The "Find in your data" step a data list shows the rows of."""
    want = str(spec.get("from") or "")
    if not want:
        return None
    return next((n for n in by_id.values() if n.get("type") == "data" and str(n.get("store") or "") == want), None)
