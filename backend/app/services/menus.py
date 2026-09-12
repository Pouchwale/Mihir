"""Interactive menu builders (WhatsApp list / reply buttons via WATI).

Hard WhatsApp limits are enforced here so the WATI call never fails on length:
  reply buttons: max 3, title <= 20 chars
  list: max 10 rows total, row title <= 24, row description <= 72, section title <= 24,
        list button text <= 20, header/footer <= 60, body <= 1024
Row/button titles are what the customer's phone sends back as text, so every title must be
something the intent parser understands ("SO 45240", "FG-2002", "Yes", "Check another SO", ...).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

BUTTONS_MAX = 3
BUTTON_TEXT_MAX = 20
LIST_ROWS_MAX = 10
ROW_TITLE_MAX = 24
ROW_DESC_MAX = 72
SECTION_TITLE_MAX = 24
LIST_BUTTON_MAX = 20
HEADER_MAX = 60
FOOTER_MAX = 60
BODY_MAX = 1024


@dataclass
class Option:
    title: str
    description: str = ""


@dataclass
class Options:
    kind: Literal["buttons", "list"]
    items: list[Option] = field(default_factory=list)
    button_text: str = "Select"  # list only: the button that opens the list
    section_title: str = ""  # list only
    header: str = ""
    footer: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def titles(self) -> list[str]:
        return [o.title for o in self.items]

    def as_text(self) -> str:
        """Plain-text fallback when the interactive send is not possible.

        Deliberately NOT numbered: a customer who answers "1" would be typing something the bot has
        to read as a whole message, and the reply we want back is the option's own text (an SO
        number, an item code, a button name), which is what the parser understands."""
        lines = []
        for o in self.items:
            lines.append(f"• {o.title}" + (f" — {o.description}" if o.description else ""))
        return "\n".join(lines)


def _cut(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


DEFAULT_LABELS: dict[str, dict[str, str]] = {
    # language buttons: each shows its own native name whatever the current language
    "lang_en": {"en": "English", "hi": "English", "gu": "English"},
    "lang_hi": {"en": "हिंदी", "hi": "हिंदी", "gu": "હિન્દી"},
    "lang_gu": {"en": "ગુજરાતી", "hi": "गुजराती", "gu": "ગુજરાતી"},
    # main menu
    "order_status": {"en": "Order status", "hi": "ऑर्डर स्टेटस", "gu": "ઓર્ડર સ્ટેટસ"},
    "change_language": {"en": "Change language", "hi": "भाषा बदलें", "gu": "ભાષા બદલો"},
    "contact_us": {"en": "Contact us", "hi": "संपर्क करें", "gu": "સંપર્ક કરો"},
    "yes": {"en": "Yes", "hi": "हाँ", "gu": "હા"},
    "no": {"en": "No", "hi": "नहीं", "gu": "ના"},
    "another": {"en": "Check another SO", "hi": "दूसरा SO देखें", "gu": "બીજો SO જુઓ"},
    "done": {"en": "Done", "hi": "हो गया", "gu": "થઈ ગયું"},
    "my_orders": {"en": "Show my orders", "hi": "मेरे ऑर्डर दिखाएं", "gu": "મારા ઓર્ડર બતાવો"},
    "menu": {"en": "Main menu", "hi": "मुख्य मेनू", "gu": "મુખ્ય મેનુ"},
    "select_so": {"en": "Select SO", "hi": "SO चुनें", "gu": "SO પસંદ કરો"},
    "select_item": {"en": "Select item", "hi": "आइटम चुनें", "gu": "આઇટમ પસંદ કરો"},
    "your_orders": {"en": "Your orders", "hi": "आपके ऑर्डर", "gu": "તમારા ઓર્ડર"},
    "items_of_so": {"en": "Items in SO {so}", "hi": "SO {so} के आइटम", "gu": "SO {so} ના આઇટમ"},
    "n_items": {"en": "{n} items", "hi": "{n} आइटम", "gu": "{n} આઇટમ"},
    "one_item": {"en": "1 item", "hi": "1 आइटम", "gu": "1 આઇટમ"},
    "more_hint": {"en": "Not listed? Type your SO number.", "hi": "सूची में नहीं? अपना SO नंबर लिखें।", "gu": "યાદીમાં નથી? તમારો SO નંબર લખો."},
    "type_hint": {"en": "Or type the code.", "hi": "या कोड लिखें।", "gu": "અથવા કોડ લખો."},
}


LABELS = DEFAULT_LABELS  # backwards-compatible alias (defaults only)


def label(key: str, lang: str, **fmt) -> str:
    """Label text, honouring admin overrides from the template editor."""
    from .templates import registry  # lazy: templates imports DEFAULT_LABELS from here

    lang = lang if lang in ("en", "hi", "gu") else "en"
    try:
        return registry.label(key, lang).format(**fmt)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_LABELS[key][lang].format(**fmt)


def buttons_for(template_key: str, lang: str) -> Options | None:
    """Buttons configured for a message in the template editor (or its built-in default)."""
    from .templates import registry

    return custom_buttons(registry.buttons(template_key), lang)


def custom_buttons(keys: list[str], lang: str) -> Options | None:
    items = [Option(_cut(label(k, lang), BUTTON_TEXT_MAX)) for k in keys[:BUTTONS_MAX] if k in DEFAULT_LABELS]
    return Options(kind="buttons", items=items) if items else None


# ---------------- builders ----------------
def so_sort_key(so: str):
    """Newest first: SO numbers count down, anything else sorts by name after them."""
    return (0, -int(so)) if so.isdigit() else (1, so)


_so_sort_key = so_sort_key  # the name the built-in bot's builders below already use


def options_from(rows: list[tuple[str, str]], kind: str = "list", *, button_text: str = "",
                 section_title: str = "", header: str = "", footer: str = "") -> Options | None:
    """A menu from plain (title, description) rows - what a workflow's data step found.

    Titles are cut to WhatsApp's limit and repeats dropped: two rows that read the same are
    impossible to tell apart once the customer taps one, and the first would always win."""
    buttons = kind == "buttons"
    items: list[Option] = []
    seen: set[str] = set()
    for title, description in rows:
        t = _cut(title, BUTTON_TEXT_MAX if buttons else ROW_TITLE_MAX)
        if not t or t.casefold() in seen:
            continue
        seen.add(t.casefold())
        items.append(Option(t, "" if buttons else _cut(description, ROW_DESC_MAX)))
        if len(items) >= (BUTTONS_MAX if buttons else LIST_ROWS_MAX):
            break
    if not items:
        return None
    return Options(kind="buttons" if buttons else "list", items=items,
                   button_text=_cut(button_text, LIST_BUTTON_MAX) or "Select",
                   section_title=_cut(section_title, SECTION_TITLE_MAX),
                   header=_cut(header, HEADER_MAX), footer=_cut(footer, FOOTER_MAX))


def so_list(rows, lang: str) -> Options | None:
    """One row per distinct SO of this customer. Newest (highest) SO first, max 10."""
    by_so: dict[str, list] = {}
    for r in rows:
        by_so.setdefault(r.so_no, []).append(r)
    if not by_so:
        return None
    sos = sorted(by_so, key=_so_sort_key)
    dropped = max(0, len(sos) - LIST_ROWS_MAX)
    if dropped:
        log.info("so_list_truncated", total=len(sos), shown=LIST_ROWS_MAX)
    items = []
    for so in sos[:LIST_ROWS_MAX]:
        items_in = by_so[so]
        n = len({r.fg_item_code for r in items_in})
        po = next((r.po_no for r in items_in if r.po_no), None)
        desc = label("one_item", lang) if n == 1 else label("n_items", lang, n=n)
        if po:
            desc += f" · PO {po}"
        items.append(Option(title=_cut(f"SO {so}", ROW_TITLE_MAX), description=_cut(desc, ROW_DESC_MAX)))
    return Options(
        kind="list",
        items=items,
        button_text=_cut(label("select_so", lang), LIST_BUTTON_MAX),
        section_title=_cut(label("your_orders", lang), SECTION_TITLE_MAX),
        footer=_cut(label("more_hint", lang), FOOTER_MAX) if dropped else "",
    )


def fg_list(rows, lang: str) -> Options | None:
    """One row per FG item of one SO. Returns None if more than 10 (caller falls back to text)."""
    codes: list[str] = []
    for r in rows:
        if r.fg_item_code and r.fg_item_code not in codes:
            codes.append(r.fg_item_code)
    if not codes or len(codes) > LIST_ROWS_MAX:
        if codes:
            log.info("fg_list_too_long_fallback_text", n=len(codes))
        return None
    so = rows[0].so_no
    return Options(
        kind="list",
        items=[Option(title=_cut(c, ROW_TITLE_MAX)) for c in codes],
        button_text=_cut(label("select_item", lang), LIST_BUTTON_MAX),
        section_title=_cut(label("items_of_so", lang, so=so), SECTION_TITLE_MAX),
        footer=_cut(label("type_hint", lang), FOOTER_MAX),
    )


def language_buttons(lang: str) -> Options:
    return Options(kind="buttons", items=[Option(_cut(label(k, lang), BUTTON_TEXT_MAX)) for k in ("lang_en", "lang_hi", "lang_gu")])


def _distinct_sos(rows) -> list[str]:
    return sorted({r.so_no for r in rows}, key=_so_sort_key)


def so_options(rows, lang: str, style: str = "auto") -> Options | None:
    """SO choices as tap-buttons when there are 3 or fewer (style=auto), otherwise as a list.
    A single tap either way; buttons just look simpler for small accounts."""
    sos = _distinct_sos(rows)
    if not sos:
        return None
    if style == "auto" and len(sos) <= BUTTONS_MAX:
        return Options(kind="buttons", items=[Option(_cut(f"SO {so}", BUTTON_TEXT_MAX)) for so in sos])
    return so_list(rows, lang)


def fg_options(rows, lang: str, style: str = "auto") -> Options | None:
    """Item choices: buttons when 3 or fewer (style=auto), else a list; None when more than 10."""
    codes: list[str] = []
    for r in rows:
        if r.fg_item_code and r.fg_item_code not in codes:
            codes.append(r.fg_item_code)
    if style == "auto" and 0 < len(codes) <= BUTTONS_MAX:
        return Options(kind="buttons", items=[Option(_cut(c, BUTTON_TEXT_MAX)) for c in codes])
    return fg_list(rows, lang)


def confirm_buttons(lang: str) -> Options:
    return Options(kind="buttons", items=[Option(_cut(label("yes", lang), BUTTON_TEXT_MAX)), Option(_cut(label("no", lang), BUTTON_TEXT_MAX))])


def after_result_buttons(lang: str) -> Options:
    return Options(kind="buttons", items=[Option(_cut(label("another", lang), BUTTON_TEXT_MAX)), Option(_cut(label("done", lang), BUTTON_TEXT_MAX))])


def not_found_buttons(lang: str) -> Options:
    return Options(kind="buttons", items=[Option(_cut(label("my_orders", lang), BUTTON_TEXT_MAX)), Option(_cut(label("done", lang), BUTTON_TEXT_MAX))])


def validate(o: Options) -> list[str]:
    """Returns a list of limit violations (empty = OK). Used by tests and the WATI client."""
    problems = []
    if o.kind == "buttons":
        if not 1 <= len(o.items) <= BUTTONS_MAX:
            problems.append(f"buttons count {len(o.items)} not in 1..{BUTTONS_MAX}")
        problems += [f"button '{i.title}' > {BUTTON_TEXT_MAX}" for i in o.items if len(i.title) > BUTTON_TEXT_MAX]
    else:
        if not 1 <= len(o.items) <= LIST_ROWS_MAX:
            problems.append(f"rows count {len(o.items)} not in 1..{LIST_ROWS_MAX}")
        problems += [f"row title '{i.title}' > {ROW_TITLE_MAX}" for i in o.items if len(i.title) > ROW_TITLE_MAX]
        problems += [f"row desc '{i.description}' > {ROW_DESC_MAX}" for i in o.items if len(i.description) > ROW_DESC_MAX]
        if len(o.button_text) > LIST_BUTTON_MAX or not o.button_text:
            problems.append("list button text length")
        if len(o.section_title) > SECTION_TITLE_MAX:
            problems.append("section title length")
    if len(o.header) > HEADER_MAX:
        problems.append("header length")
    if len(o.footer) > FOOTER_MAX:
        problems.append("footer length")
    titles = [i.title for i in o.items]
    if len(set(titles)) != len(titles):
        problems.append("duplicate titles")
    return problems
