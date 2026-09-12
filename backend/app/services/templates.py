"""Editable customer-facing text: conversation templates, menu labels, custom keyword replies.

Defaults live in code (replies.DEFAULTS, menus.DEFAULT_LABELS). Admin overrides live in the
`templates` table and are cached in memory (`registry`). The cache is reloaded after every save
and every 60 s by the scheduler, so a second server process also picks changes up.

Every save is validated (placeholders, required fields, length limits) so an edit can never make
the bot crash or send a WATI payload that WhatsApp rejects.
"""
from __future__ import annotations

import json
import re
import string
from dataclasses import asdict, dataclass, field

import structlog
from sqlalchemy import delete, select

from ..db import session_scope
from ..models import Template, TemplateHistory, utcnow

log = structlog.get_logger(__name__)

LANGS = ("en", "hi", "gu")
LANG_NAMES = {"en": "English", "hi": "Hindi", "gu": "Gujarati"}

# Sample values used for validation and for the live preview in the editor
SAMPLE = {"so_no": "45240", "po_no": "PO-8801", "fg_code": "FG-2002", "n_items": 3, "value": "45240", "customer_name": "Mehta Foods",
          "support": "+91-98765 43210", "item": ", item FG-2002", "real_status": "In Production"}

# ---------------- specs ----------------
@dataclass(frozen=True)
class TemplateSpec:
    key: str
    title: str
    when: str
    allowed: frozenset
    required: frozenset = frozenset()
    max_len: int = 1024
    trilingual: bool = False  # sent stacked in all three languages
    neutral: bool = False  # sent before the language is known: one text for everyone
    menu: str = ""  # which menu is attached (for the preview)


_S = frozenset({"support", "customer_name"})
_ORDER = frozenset({"so_no", "po_no", "fg_code", "n_items", "support", "customer_name"})
TEMPLATE_SPECS: dict[str, TemplateSpec] = {
    s.key: s
    for s in [
        TemplateSpec("welcome_first", "Greeting (first message)", "The first message of a new conversation window (after 30 minutes of silence). Sent before the language is known, so it is one text for everyone.", frozenset({"support"}), neutral=True),
        TemplateSpec("ask_language", "Ask for the language", "Sent right after the greeting, with the English / Hindi / Gujarati buttons.", frozenset({"support"}), neutral=True, menu="language"),
        TemplateSpec("main_menu", "Main menu", "Shown once the language is chosen, and whenever the customer says 'menu' or 'hi' during the conversation.", _S, menu="buttons"),
        TemplateSpec("contact_us", "Contact us", "Customer taps 'Contact us'. {support} is your support number / email from Settings.", _S, menu="buttons"),
        TemplateSpec("ask_so_list", "Choose an order", "Customer taps 'Order status' and has orders in the PPC table. Sent with their SO numbers as buttons (up to 3) or a list.", _S, menu="so_options"),
        TemplateSpec("so_none", "No orders found", "Customer taps 'Order status' but no rows in the PPC table match their name.", _S, menu="buttons"),
        TemplateSpec("ask_so", "Ask SO (plain)", "Bot needs an SO number and cannot show choices.", _S),
        TemplateSpec("ask_fg_list", "Choose an item", "The SO has several FG items; sent with the item codes as buttons (up to 3) or a list.", _ORDER, frozenset({"so_no"}), menu="fg_options"),
        TemplateSpec("ask_fg", "Ask item (plain)", "The SO has more than 10 items, so no choices can be shown.", _ORDER, frozenset({"so_no"})),
        TemplateSpec("ask_fg_retry", "Item not in SO, ask again", "Customer sent an item code that is not in the chosen SO (before the retry limit).", _ORDER, frozenset({"so_no"}), menu="fg_options"),
        TemplateSpec("confirm_so", "Confirm SO (after voice note)", "A voice note was transcribed; bot echoes the SO number with Yes / No buttons.", frozenset({"value", "support", "customer_name"}), frozenset({"value"}), menu="confirm"),
        TemplateSpec("confirm_po", "Confirm PO (after voice note)", "Same as above for a PO number.", frozenset({"value", "support", "customer_name"}), frozenset({"value"}), menu="confirm"),
        TemplateSpec("confirm_fg", "Confirm item (after voice note)", "Same as above for an FG item code.", frozenset({"value", "support", "customer_name"}), frozenset({"value"}), menu="confirm"),
        TemplateSpec("result", "Real Status result", "The answer. {real_status} is the only status field ever sent. {item} expands to ', item FG-…' when the SO has several items, else empty.", frozenset({"so_no", "po_no", "fg_code", "item", "real_status", "support", "customer_name"}), frozenset({"real_status", "so_no"}), menu="buttons"),
        TemplateSpec("not_found", "SO / item not found", "The SO or PO does not exist, or the item was wrong too many times.", frozenset({"so_no", "fg_code", "support", "customer_name"}), menu="buttons"),
        TemplateSpec("bye", "Goodbye", "Customer taps Done or says thanks / bye. The next message starts a new window with the greeting.", _S, menu="buttons"),
        TemplateSpec("voice_off", "Voice note received", "Customer sent a voice message while voice notes are switched off (Settings -> Conversation). Speech-to-text can misread digits, so the bot asks for the number in writing rather than guessing.", _S, menu="buttons"),
        TemplateSpec("verify_failed", "Verification failed", "Number not in the customer Excel, or the PPC customer name does not match byte-for-byte. Sent in all three languages together.", frozenset({"support"}), trilingual=True),
        TemplateSpec("new_customer_pending", "New customer, not set up yet", "A number on Data -> New customers (it signed up through a workflow) writes again before it is in the customer Excel. It has no orders to show yet. {customer_name} is the name they gave.", _S),
        TemplateSpec("too_many_repeats", "Asked the same thing too often", "The same order has been asked about "
                     "several times in a row (Settings -> Conversation). The bot says so once and stops, rather than "
                     "sending the same status again; the next message starts a fresh conversation.",
                     frozenset({"so_no", "fg_code", "support", "customer_name"})),
        TemplateSpec("service_down", "Service unavailable", "Unexpected error while processing. Sent in all three languages together.", frozenset({"support"}), trilingual=True),
        TemplateSpec("rate_limited", "Too many messages", "Customer exceeded the per-phone rate limit.", frozenset({"support"})),
    ]
}


@dataclass(frozen=True)
class LabelSpec:
    key: str
    title: str
    when: str
    max_len: int
    placeholders: frozenset = frozenset()


LABEL_SPECS: dict[str, LabelSpec] = {
    s.key: s
    for s in [
        LabelSpec("lang_en", "Language button: English", "One of the three language buttons under the greeting.", 20),
        LabelSpec("lang_hi", "Language button: Hindi", "One of the three language buttons under the greeting.", 20),
        LabelSpec("lang_gu", "Language button: Gujarati", "One of the three language buttons under the greeting.", 20),
        LabelSpec("order_status", "Menu button: Order status", "Opens the customer's order choices.", 20),
        LabelSpec("change_language", "Menu button: Change language", "Shows the language buttons again.", 20),
        LabelSpec("contact_us", "Menu button: Contact us", "Sends your support contact.", 20),
        LabelSpec("yes", "Button: Yes", "Voice-note confirmation button. Must still be understood as 'yes' by the bot.", 20),
        LabelSpec("no", "Button: No", "Voice-note confirmation button. Must still be understood as 'no'.", 20),
        LabelSpec("another", "Button: Check another SO", "Shown after a status. Re-opens the order choices.", 20),
        LabelSpec("done", "Button: Done", "Shown after a status / not found. Ends the conversation.", 20),
        LabelSpec("my_orders", "Button: Show my orders", "Shown after 'not found'. Re-opens the order choices.", 20),
        LabelSpec("menu", "Button: Main menu", "Goes back to the main menu.", 20),
        LabelSpec("select_so", "List button: Select SO", "The button that opens the SO list.", 20),
        LabelSpec("select_item", "List button: Select item", "The button that opens the FG item list.", 20),
        LabelSpec("your_orders", "List section title: Your orders", "Section title above the SO rows.", 24),
        LabelSpec("items_of_so", "List section title: Items in SO {so}", "Section title above the item rows.", 24, frozenset({"so"})),
        LabelSpec("n_items", "Row description: {n} items", "Under each SO row when it has several items.", 60, frozenset({"n"})),
        LabelSpec("one_item", "Row description: 1 item", "Under each SO row with a single item.", 60),
        LabelSpec("more_hint", "List footer: Not listed? Type your SO number.", "Footer shown when the customer has more than 10 SOs.", 60),
        LabelSpec("type_hint", "List footer: Or type the code.", "Footer under the item list.", 60),
    ]
}

# button labels that stand for an intent: the parser recognises the CURRENT label text (intent.label_intent)
LABEL_INTENTS = {
    "yes": "confirm_yes", "no": "confirm_no",
    "another": "order_status", "my_orders": "order_status", "order_status": "order_status",
    "menu": "menu", "done": "bye", "change_language": "change_language", "contact_us": "contact_us",
    "lang_en": "lang_en", "lang_hi": "lang_hi", "lang_gu": "lang_gu",
}
_LABEL_INTENT = LABEL_INTENTS
# buttons an admin may put under a message or a custom reply
CUSTOM_BUTTON_CHOICES = ("order_status", "change_language", "contact_us", "another", "my_orders", "menu", "done")
BUTTON_CHOICES = CUSTOM_BUTTON_CHOICES

# Plain-language names for placeholders, shown as chips in the editor instead of {so_no}
PLACEHOLDER_LABELS = {
    "customer_name": "Customer name",
    "so_no": "Order number (SO)",
    "po_no": "PO number",
    "fg_code": "Item code",
    "real_status": "Real status",
    "n_items": "Number of items",
    "value": "Number heard in the voice note",
    "support": "Support contact",
    "item": "Item part (added automatically)",
}


@dataclass(frozen=True)
class ButtonSlot:
    """Which buttons may appear under a message. `fixed` slots cannot be changed
    (Yes/No after a voice note is required by the confirmation logic)."""

    key: str
    default: tuple
    fixed: bool = False
    min_count: int = 0  # >0 where a customer who only taps would otherwise have no way forward


BUTTON_SLOTS: dict[str, ButtonSlot] = {
    s.key: s
    for s in [
        ButtonSlot("ask_language", ("lang_en", "lang_hi", "lang_gu"), fixed=True),
        ButtonSlot("main_menu", ("order_status", "change_language", "contact_us"), min_count=1),
        ButtonSlot("contact_us", ("order_status", "menu"), min_count=1),
        ButtonSlot("so_none", ("menu", "contact_us"), min_count=1),
        ButtonSlot("result", ("another", "menu", "done"), min_count=1),
        ButtonSlot("not_found", ("my_orders", "menu"), min_count=1),
        ButtonSlot("ask_so", ()),
        ButtonSlot("ask_fg", ()),
        ButtonSlot("ask_fg_retry", ()),
        ButtonSlot("bye", ()),
        ButtonSlot("voice_off", ("order_status", "menu")),
        ButtonSlot("welcome_first", ()),
        ButtonSlot("confirm_so", ("yes", "no"), fixed=True),
        ButtonSlot("confirm_po", ("yes", "no"), fixed=True),
        ButtonSlot("confirm_fg", ("yes", "no"), fixed=True),
    ]
}

# ---------------- conversation flow map (drives the visual editor) ----------------
# Customer nodes are grey bubbles the admin cannot edit; bot nodes open the editor.
FLOW_NODES = [
    {"key": "_in_first", "kind": "customer", "title": "First message of the window", "text": "hi / any message after 30 min of silence", "col": 0, "row": 0},
    {"key": "welcome_first", "kind": "bot", "col": 1, "row": 0},
    {"key": "ask_language", "kind": "bot", "col": 2, "row": 0},
    {"key": "main_menu", "kind": "bot", "col": 3, "row": 0},
    {"key": "ask_so_list", "kind": "bot", "col": 4, "row": 0},
    {"key": "ask_fg_list", "kind": "bot", "col": 5, "row": 0},
    {"key": "result", "kind": "bot", "col": 6, "row": 0},
    {"key": "contact_us", "kind": "bot", "col": 4, "row": 1},
    {"key": "so_none", "kind": "bot", "col": 4, "row": 2},
    {"key": "ask_fg_retry", "kind": "bot", "col": 5, "row": 1},
    {"key": "not_found", "kind": "bot", "col": 5, "row": 2},
    {"key": "bye", "kind": "bot", "col": 6, "row": 1},
    {"key": "_in_voice", "kind": "customer", "title": "Customer sends a voice note", "text": "🎤 voice message", "col": 3, "row": 3},
    {"key": "confirm_so", "kind": "bot", "col": 4, "row": 3},
    {"key": "_in_unknown", "kind": "customer", "title": "Unknown number writes", "text": "any message", "col": 0, "row": 2},
    {"key": "verify_failed", "kind": "bot", "col": 1, "row": 2},
    {"key": "new_customer_pending", "kind": "bot", "col": 1, "row": 3},
]

FLOW_EDGES = [
    {"from": "_in_first", "to": "welcome_first", "label": "number found in the customer Excel"},
    {"from": "welcome_first", "to": "ask_language", "label": "sent together"},
    {"from": "ask_language", "to": "main_menu", "label": "taps a language"},
    {"from": "main_menu", "to": "ask_so_list", "label": "taps Order status"},
    {"from": "main_menu", "to": "contact_us", "label": "taps Contact us"},
    {"from": "main_menu", "to": "ask_language", "label": "taps Change language"},
    {"from": "ask_so_list", "to": "so_none", "label": "no orders under this name"},
    {"from": "ask_so_list", "to": "ask_fg_list", "label": "taps an order with several items"},
    {"from": "ask_so_list", "to": "result", "label": "taps an order with one item"},
    {"from": "ask_so_list", "to": "not_found", "label": "types an unknown order number"},
    {"from": "ask_fg_list", "to": "result", "label": "taps an item"},
    {"from": "ask_fg_list", "to": "ask_fg_retry", "label": "wrong item code"},
    {"from": "ask_fg_retry", "to": "ask_fg_list", "label": "tries again"},
    {"from": "ask_fg_retry", "to": "not_found", "label": "wrong twice"},
    {"from": "result", "to": "bye", "label": "taps Done"},
    {"from": "result", "to": "ask_so_list", "label": "taps Check another SO"},
    {"from": "result", "to": "main_menu", "label": "taps Main menu"},
    {"from": "not_found", "to": "ask_so_list", "label": "taps Show my orders"},
    {"from": "_in_voice", "to": "confirm_so", "label": "transcribed"},
    {"from": "confirm_so", "to": "result", "label": "taps Yes"},
    {"from": "confirm_so", "to": "ask_so_list", "label": "taps No"},
    {"from": "_in_unknown", "to": "verify_failed", "label": "number not in Excel"},
    {"from": "_in_unknown", "to": "new_customer_pending", "label": "signed up through a workflow"},
]

FLOW_KEYS = {n["key"] for n in FLOW_NODES if n["kind"] == "bot"}



@dataclass
class CustomReply:
    key: str
    title: str
    triggers: list[str]
    texts: dict[str, str]
    buttons: list[str] = field(default_factory=list)  # label keys from CUSTOM_BUTTON_CHOICES
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------- helpers ----------------
_FMT = string.Formatter()
_PUNCT = re.compile(r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~।]")


def placeholders_in(text: str) -> set[str]:
    out = set()
    for _, name, _, _ in _FMT.parse(text):
        if name is not None:
            out.add(name.split(".")[0].split("[")[0])
    return out


def norm_trigger(s: str) -> str:
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", s)).strip().casefold()


def trigger_matches(trigger_norm: str, message_norm: str) -> bool:
    """The one rule for 'does this custom trigger fire on this message'. Used by the matcher AND by
    validation, so a trigger can never be accepted that would then behave differently at runtime.
    A short trigger must be the whole message; from 4 characters it may appear inside one."""
    if not trigger_norm or not message_norm:
        return False
    return message_norm == trigger_norm or (len(trigger_norm) >= 4 and trigger_norm in message_norm)


def button_phrases(lang: str | None = None) -> list[tuple[str, str]]:
    """Every menu-button title the bot must keep understanding: [(normalised, human description)]."""
    out: list[tuple[str, str]] = []
    for key in LABEL_INTENTS:
        spec = LABEL_SPECS.get(key)
        for lg in ((lang,) if lang else LANGS):
            try:
                text = registry.label(key, lg)
            except KeyError:
                continue
            n = norm_trigger(text)
            if n:
                out.append((n, f'{spec.title if spec else key} ("{text}")'))
    return out


def _defaults():
    from .menus import DEFAULT_LABELS
    from .replies import DEFAULTS

    return DEFAULTS, DEFAULT_LABELS


# ---------------- validation ----------------
def validate_template(key: str, lang: str, text: str) -> list[str]:
    spec = TEMPLATE_SPECS.get(key)
    if not spec:
        return [f"unknown template '{key}'"]
    errors = []
    t = text or ""
    if not t.strip():
        errors.append("text is empty")
    if len(t) > spec.max_len:
        errors.append(f"too long ({len(t)} > {spec.max_len} characters)")
    try:
        used = placeholders_in(t)
    except ValueError as e:
        return errors + [f"unbalanced braces: {e}"]
    unknown = used - spec.allowed
    if unknown:
        errors.append("unknown placeholder(s): " + ", ".join(f"{{{u}}}" for u in sorted(unknown)) + ". Allowed: " + ", ".join(f"{{{a}}}" for a in sorted(spec.allowed)))
    missing = spec.required - used
    if missing:
        errors.append("missing required placeholder(s): " + ", ".join(f"{{{m}}}" for m in sorted(missing)))
    if not errors:
        try:
            t.format(**SAMPLE)
        except (KeyError, IndexError, ValueError) as e:
            errors.append(f"cannot render: {e}")
    return errors


def validate_label(key: str, lang: str, text: str) -> list[str]:
    spec = LABEL_SPECS.get(key)
    if not spec:
        return [f"unknown label '{key}'"]
    errors = []
    t = (text or "").strip()
    if not t:
        errors.append("text is empty")
    try:
        used = placeholders_in(t)
    except ValueError as e:
        return errors + [f"unbalanced braces: {e}"]
    if used - spec.placeholders:
        errors.append("unknown placeholder(s): " + ", ".join(f"{{{u}}}" for u in sorted(used - spec.placeholders)))
    if spec.placeholders - used:
        errors.append("missing placeholder(s): " + ", ".join(f"{{{m}}}" for m in sorted(spec.placeholders - used)))
    rendered = t
    if not errors:
        try:
            rendered = t.format(so="45240", n=10)
        except (KeyError, IndexError, ValueError) as e:
            errors.append(f"cannot render: {e}")
    if len(rendered) > spec.max_len:
        errors.append(f"too long ({len(rendered)} > {spec.max_len} characters; WhatsApp limit)")
    intent = _LABEL_INTENT.get(key)
    if intent and not errors:
        from .intent import regex_parse

        p = regex_parse(rendered, use_labels=False)
        if p.so_no or p.po_no or p.fg_code or p.bare_codes:
            errors.append("a button label must not look like an SO / PO / item code")
        elif p.intent not in ("other", intent):
            errors.append(f"this text already means '{p.intent}' to the bot; it cannot double as '{intent}'")
        # must not collide with another intent button's label in the same language
        n = norm_trigger(rendered)
        for other, other_intent in _LABEL_INTENT.items():
            if other != key and other_intent != intent and norm_trigger(registry.label(other, lang)) == n:
                errors.append(f"same text as the '{LABEL_SPECS[other].title}' button")
                break
        # ...and must not fall into an existing custom keyword reply, which would swallow the tap
        for c in registry.custom.values():
            if not c.enabled:
                continue
            if any(trigger_matches(norm_trigger(trig), n) for trig in c.triggers):
                errors.append(f"this text matches the custom reply '{c.title}', so tapping the button would send that reply instead")
                break
    return errors


def validate_custom(reply: CustomReply) -> list[str]:
    errors = []
    if not re.fullmatch(r"[a-z0-9_\-]{2,40}", reply.key or ""):
        errors.append("key must be 2-40 characters: lowercase letters, digits, - or _")
    if reply.key in TEMPLATE_SPECS:
        errors.append("key clashes with a built-in template")
    if not (reply.title or "").strip():
        errors.append("title is empty")
    trig = [norm_trigger(t) for t in reply.triggers if norm_trigger(t)]
    if not trig:
        errors.append("at least one trigger word is required")
    if any(len(t) < 2 for t in trig):
        errors.append("trigger words must be at least 2 characters")
    if not any((reply.texts.get(lg) or "").strip() for lg in LANGS):
        errors.append("text is required in at least one language")
    for lg in LANGS:
        t = reply.texts.get(lg) or ""
        if len(t) > 1024:
            errors.append(f"{LANG_NAMES[lg]} text too long ({len(t)} > 1024)")
        try:
            used = placeholders_in(t)
        except ValueError as e:
            errors.append(f"{LANG_NAMES[lg]}: unbalanced braces: {e}")
            continue
        if used - {"support"}:
            errors.append(f"{LANG_NAMES[lg]}: only {{support}} is allowed as a placeholder")
    bad = [b for b in reply.buttons if b not in CUSTOM_BUTTON_CHOICES]
    if bad:
        errors.append("unknown button(s): " + ", ".join(bad))
    if len(reply.buttons) > 3:
        errors.append("at most 3 buttons")
    # A trigger that also fires on a menu button would silently disable that button for everyone,
    # because a tapped button arrives as its own title.
    from .intent import regex_parse

    phrases = button_phrases()
    for t in trig:
        for phrase, human in phrases:
            if trigger_matches(t, phrase):
                errors.append(f"'{t}' also matches the {human} button - a customer tapping it would get this reply "
                              "instead of the menu. Use a longer or more specific word.")
                break
        else:
            p = regex_parse(t, use_labels=False)
            if p.so_no or p.po_no or p.fg_code or p.bare_codes:
                errors.append(f"'{t}' looks like an order or item code, so it cannot be a trigger word")
            elif p.intent != "other":
                errors.append(f"'{t}' is already a built-in keyword (the bot reads it as '{p.intent}')")
    return errors


def audit_custom_conflicts() -> list[dict]:
    """Custom replies ALREADY saved whose trigger now shadows a menu button (e.g. the button was
    renamed afterwards). Surfaced on the Go-live page; validation only guards new saves."""
    out: list[dict] = []
    phrases = button_phrases()
    for c in registry.custom.values():
        if not c.enabled:
            continue
        for trig in c.triggers:
            t = norm_trigger(trig)
            for phrase, human in phrases:
                if trigger_matches(t, phrase):
                    out.append({"key": c.key, "title": c.title, "trigger": trig, "button": human})
                    break
    return out


def validate_buttons(template_key: str, keys: list[str]) -> list[str]:
    slot = BUTTON_SLOTS.get(template_key)
    if slot is None:
        return [f"'{template_key}' cannot have buttons"]
    if slot.fixed:
        return ["the Yes / No buttons of a voice confirmation cannot be changed (you can still rename them)"]
    errors = []
    if len(keys) < slot.min_count:
        errors.append("at least one button is needed here, otherwise a customer who only taps has no way to continue")
    if len(keys) > 3:
        errors.append("WhatsApp allows at most 3 buttons")
    unknown = [k for k in keys if k not in BUTTON_CHOICES]
    if unknown:
        errors.append("unknown button(s): " + ", ".join(unknown))
    if len(set(keys)) != len(keys):
        errors.append("the same button is used twice")
    return errors


async def save_buttons(template_key: str, keys: list[str]) -> list[str]:
    errors = validate_buttons(template_key, keys)
    if errors:
        return errors
    slot = BUTTON_SLOTS[template_key]
    async with session_scope() as db:
        row = (await db.execute(select(Template).where(Template.kind == "buttons", Template.key == template_key, Template.lang == "meta"))).scalar_one_or_none()
        if list(keys) == list(slot.default):
            if row:
                await _history(db, "buttons", template_key, "meta", row.text, "reset")
                await db.delete(row)
        elif row:
            if row.text != json.dumps(keys):
                await _history(db, "buttons", template_key, "meta", row.text, "save")
                row.text = json.dumps(keys)
        else:
            await _history(db, "buttons", template_key, "meta", None, "save")
            db.add(Template(kind="buttons", key=template_key, lang="meta", text=json.dumps(keys)))
    await load_from_db()
    return []


# ---------------- registry (in-memory cache) ----------------
class Registry:
    def __init__(self) -> None:
        self.templates: dict[tuple[str, str], str] = {}
        self.labels: dict[tuple[str, str], str] = {}
        self.custom: dict[str, CustomReply] = {}
        self.buttons_cfg: dict[str, list[str]] = {}
        self.loaded_at = None

    # reads (hot path)
    def text(self, key: str, lang: str) -> str:
        defaults, _ = _defaults()
        t = self.templates.get((key, lang))
        if t:
            return t
        d = defaults.get(key)
        if d is None:
            raise KeyError(f"unknown template {key}")
        return d.get(lang) or d["en"]

    def label(self, key: str, lang: str) -> str:
        _, defaults = _defaults()
        t = self.labels.get((key, lang))
        if t:
            return t
        d = defaults[key]
        return d.get(lang) or d["en"]

    def custom_text(self, key: str, lang: str) -> str:
        c = self.custom[key]
        return c.texts.get(lang) or c.texts.get("en") or next(t for t in c.texts.values() if t)

    def match_custom(self, message: str) -> CustomReply | None:
        m = norm_trigger(message or "")
        if not m:
            return None
        for c in self.custom.values():
            if not c.enabled:
                continue
            for trig in c.triggers:
                t = norm_trigger(trig)
                if not t:
                    continue
                if trigger_matches(t, m):
                    return c
        return None

    def buttons(self, template_key: str) -> list[str]:
        """Label keys of the buttons shown under a message (admin override or the built-in default)."""
        slot = BUTTON_SLOTS.get(template_key)
        if slot is None:
            return []
        if slot.fixed:
            return list(slot.default)
        cfg = self.buttons_cfg.get(template_key)
        return list(cfg) if cfg is not None else list(slot.default)

    def buttons_overridden(self, template_key: str) -> bool:
        return template_key in self.buttons_cfg

    def is_overridden(self, kind: str, key: str, lang: str) -> bool:
        store = self.templates if kind == "template" else self.labels
        return (key, lang) in store


registry = Registry()


async def load_from_db() -> None:
    templates: dict[tuple[str, str], str] = {}
    labels: dict[tuple[str, str], str] = {}
    custom_rows: dict[str, dict] = {}
    buttons_cfg: dict[str, list[str]] = {}
    async with session_scope() as db:
        rows = (await db.execute(select(Template))).scalars().all()
    for r in rows:
        if r.kind == "template":
            templates[(r.key, r.lang)] = r.text
        elif r.kind == "label":
            labels[(r.key, r.lang)] = r.text
        elif r.kind == "custom":
            custom_rows.setdefault(r.key, {})[r.lang] = r.text
        elif r.kind == "buttons":
            try:
                val = json.loads(r.text)
                if isinstance(val, list):
                    buttons_cfg[r.key] = [str(x) for x in val]
            except ValueError:
                log.warning("bad_button_config", key=r.key)
    custom: dict[str, CustomReply] = {}
    for key, per_lang in custom_rows.items():
        try:
            meta = json.loads(per_lang.get("meta") or "{}")
        except ValueError:
            meta = {}
        custom[key] = CustomReply(
            key=key, title=meta.get("title") or key, triggers=list(meta.get("triggers") or []),
            texts={lg: per_lang.get(lg, "") for lg in LANGS}, buttons=list(meta.get("buttons") or []), enabled=bool(meta.get("enabled", True)),
        )
    registry.templates, registry.labels, registry.custom = templates, labels, custom
    registry.buttons_cfg = buttons_cfg
    registry.loaded_at = utcnow()
    log.info("templates_loaded", templates=len(templates), labels=len(labels), custom=len(custom), buttons=len(buttons_cfg))


async def _history(db, kind: str, key: str, lang: str, old_text: str | None, action: str) -> None:
    db.add(TemplateHistory(kind=kind, key=key, lang=lang, text=old_text, action=action))


async def save_text(kind: str, key: str, lang: str, text: str) -> list[str]:
    """Save one template/label override. Returns validation errors (empty = saved)."""
    if lang not in LANGS:
        return [f"unknown language '{lang}'"]
    errors = validate_template(key, lang, text) if kind == "template" else validate_label(key, lang, text)
    if errors:
        return errors
    text = text.strip() if kind == "label" else text
    defaults, default_labels = _defaults()
    default = (defaults if kind == "template" else default_labels)[key][lang]
    async with session_scope() as db:
        row = (await db.execute(select(Template).where(Template.kind == kind, Template.key == key, Template.lang == lang))).scalar_one_or_none()
        if text == default:
            # identical to the default -> drop the override
            if row:
                await _history(db, kind, key, lang, row.text, "reset")
                await db.delete(row)
        elif row:
            if row.text != text:
                await _history(db, kind, key, lang, row.text, "save")
                row.text = text
        else:
            await _history(db, kind, key, lang, None, "save")
            db.add(Template(kind=kind, key=key, lang=lang, text=text))
    await load_from_db()
    return []


async def reset_key(kind: str, key: str) -> None:
    """Remove all language overrides of a template/label (back to defaults)."""
    async with session_scope() as db:
        rows = (await db.execute(select(Template).where(Template.kind == kind, Template.key == key))).scalars().all()
        for r in rows:
            await _history(db, kind, key, r.lang, r.text, "reset")
            await db.delete(r)
    await load_from_db()


async def save_custom(reply: CustomReply) -> list[str]:
    reply.triggers = [t.strip() for t in reply.triggers if t and t.strip()]
    errors = validate_custom(reply)
    if errors:
        return errors
    meta = json.dumps({"title": reply.title.strip(), "triggers": reply.triggers, "buttons": reply.buttons, "enabled": reply.enabled}, ensure_ascii=False)
    async with session_scope() as db:
        existing = {r.lang: r for r in (await db.execute(select(Template).where(Template.kind == "custom", Template.key == reply.key))).scalars()}
        for lang, text in [(lg, reply.texts.get(lg, "") or "") for lg in LANGS] + [("meta", meta)]:
            row = existing.get(lang)
            if row:
                if row.text != text:
                    if lang != "meta":
                        await _history(db, "custom", reply.key, lang, row.text, "save")
                    row.text = text
            else:
                db.add(Template(kind="custom", key=reply.key, lang=lang, text=text))
    await load_from_db()
    return []


async def delete_custom(key: str) -> bool:
    async with session_scope() as db:
        rows = (await db.execute(select(Template).where(Template.kind == "custom", Template.key == key))).scalars().all()
        if not rows:
            return False
        for r in rows:
            if r.lang != "meta":
                await _history(db, "custom", key, r.lang, r.text, "delete")
        await db.execute(delete(Template).where(Template.kind == "custom", Template.key == key))
    await load_from_db()
    return True


async def history(kind: str, key: str, limit: int = 50) -> list[dict]:
    async with session_scope() as db:
        rows = (await db.execute(select(TemplateHistory).where(TemplateHistory.kind == kind, TemplateHistory.key == key).order_by(TemplateHistory.id.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "lang": r.lang, "text": r.text, "action": r.action, "changed_at": r.changed_at.isoformat()} for r in rows]


# ---------------- preview / catalog for the editor ----------------
def _support() -> str:
    from ..config import get_settings

    return get_settings().support_contact


def sample_values() -> dict:
    """Sample order data, but the real support contact - the preview must match what is sent."""
    return {**SAMPLE, "support": _support()}


def render_sample(kind: str, key: str, lang: str, text: str) -> str:
    if kind == "label":
        try:
            return text.format(so="45240", n=3)
        except Exception:  # noqa: BLE001
            return text
    from .replies import ReplyContext, render_text

    ctx = ReplyContext(real_status=SAMPLE["real_status"], so_no=SAMPLE["so_no"], po_no=SAMPLE["po_no"], fg_code=SAMPLE["fg_code"],
                       n_items=SAMPLE["n_items"], value=SAMPLE["value"], customer_name=SAMPLE["customer_name"], support=_support())
    try:
        return render_text(text, lang, ctx)
    except Exception as e:  # noqa: BLE001
        return f"<cannot render: {e}>"


def sample_options(template_key: str, lang: str):
    """A realistic menu for previews and test sends: real (edited) label texts, sample data."""
    from . import menus

    from ..config import get_settings

    spec = TEMPLATE_SPECS.get(template_key)
    menu = spec.menu if spec else ""
    style = get_settings().so_menu_style
    if menu == "language":
        return menus.language_buttons(lang)
    if menu == "so_options":
        if style == "auto":
            return menus.Options(kind="buttons", items=[menus.Option(title=f"SO {so}") for so in ("45240", "45231")])
        return menus.Options(
            kind="list",
            items=[
                menus.Option(title="SO 45240", description=menus.label("n_items", lang, n=3) + " · PO PO-8801"),
                menus.Option(title="SO 45231", description=menus.label("one_item", lang) + " · PO PO-7781"),
            ],
            button_text=menus.label("select_so", lang),
            section_title=menus.label("your_orders", lang),
        )
    if menu == "fg_options":
        codes = ("FG-2001", "FG-2002", "FG-2003")
        if style == "auto":
            return menus.Options(kind="buttons", items=[menus.Option(title=c) for c in codes])
        return menus.Options(
            kind="list",
            items=[menus.Option(title=c) for c in codes],
            button_text=menus.label("select_item", lang),
            section_title=menus.label("items_of_so", lang, so=SAMPLE["so_no"]),
            footer=menus.label("type_hint", lang),
        )
    if menu == "confirm":
        return menus.confirm_buttons(lang)
    return menus.buttons_for(template_key, lang)


def catalog() -> dict:
    defaults, default_labels = _defaults()
    templates = []
    for spec in TEMPLATE_SPECS.values():
        langs = {}
        for lg in LANGS:
            cur = registry.text(spec.key, lg)
            langs[lg] = {"default": defaults[spec.key][lg], "text": cur, "overridden": registry.is_overridden("template", spec.key, lg)}
        slot = BUTTON_SLOTS.get(spec.key)
        templates.append({"key": spec.key, "title": spec.title, "when": spec.when, "allowed": sorted(spec.allowed), "required": sorted(spec.required),
                          "max_len": spec.max_len, "trilingual": spec.trilingual, "neutral": spec.neutral, "menu": spec.menu, "langs": langs,
                          "buttons": registry.buttons(spec.key), "buttons_editable": bool(slot) and not slot.fixed,
                          "buttons_default": list(slot.default) if slot else [], "buttons_overridden": registry.buttons_overridden(spec.key),
                          "in_flow": spec.key in FLOW_KEYS})
    labels = []
    for spec in LABEL_SPECS.values():
        langs = {}
        for lg in LANGS:
            langs[lg] = {"default": default_labels[spec.key][lg], "text": registry.label(spec.key, lg), "overridden": registry.is_overridden("label", spec.key, lg)}
        labels.append({"key": spec.key, "title": spec.title, "when": spec.when, "max_len": spec.max_len, "placeholders": sorted(spec.placeholders),
                       "intent": _LABEL_INTENT.get(spec.key), "langs": langs})
    custom = [c.to_dict() for c in registry.custom.values()]
    nodes = []
    for n in FLOW_NODES:
        node = dict(n)
        if n["kind"] == "bot":
            spec = TEMPLATE_SPECS[n["key"]]
            node["title"] = spec.title
            node["when"] = spec.when
        nodes.append(node)
    from ..config import get_settings

    return {"templates": templates, "labels": labels, "custom": custom, "sample": sample_values(), "languages": LANG_NAMES,
            "button_choices": [{"key": k, "label": registry.label(k, "en")} for k in BUTTON_CHOICES],
            "so_menu_style": get_settings().so_menu_style,
            "placeholder_labels": PLACEHOLDER_LABELS,
            "flow": {"nodes": nodes, "edges": FLOW_EDGES},
            "loaded_at": registry.loaded_at.isoformat() if registry.loaded_at else None}


def default_label(key: str, lang: str) -> str:
    _, d = _defaults()
    return d[key].get(lang) or d[key]["en"]
