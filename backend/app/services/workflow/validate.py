"""Is this graph safe to publish?

Two levels, deliberately: a "fail" is something that would break a real conversation, a "warn" is
something the owner probably did not mean. Half-built drafts are full of warnings and that is fine -
they only stop being publishable when a customer could actually get stuck.

Every message says what is wrong AND names the thing at fault, because the owner reading it is not
the person who wrote this file.
"""
from __future__ import annotations

import re
from typing import Any

from .. import menus
from ..intent import regex_parse
from ..templates import norm_trigger
from .schema import (AI_ANSWER_MAX, AI_ANSWER_MIN, AI_FAQ_MAX, AI_TONES, CHAT_STATUSES, CONDITION_OPS,
                     DATA_FINDS, DATA_GROUPS, DATA_KEY_FIELDS, DATA_OPS, DATA_ROWS_MAX, DATA_SORTS,
                     DATA_SHOW, DATA_SOURCES, DELAY_MAX_SEC, HTTP_METHODS, INPUT_KINDS, WAITING_TYPES,
                     LANGS, MEDIA_TYPES, NODE_TYPES, SYSTEM_VARS, VALIDATE_TYPES, _NODE_ID, _VAR_NAME,
                     LANGUAGE_NAMES, LOOKUP_SOURCES, Issue, answer_extras, data_answer_extras, data_fields,
                     data_node_of, data_vars, language_label, offered_languages,
                     ports_of, text_of, to_person,
                     walk_strings)


def validate_graph(doc: dict, *, workflow_key: str = "") -> list[Issue]:
    """Every problem with a drawn workflow, worst first. [] means safe to publish."""
    issues: list[Issue] = []
    nodes = [n for n in (doc.get("nodes") or []) if isinstance(n, dict)]
    if not nodes:
        return [Issue("fail", "This workflow has no steps yet. Add at least one message.")]

    by_id: dict[str, dict] = {}
    for n in nodes:
        nid = str(n.get("id") or "")
        if not _NODE_ID.match(nid):
            issues.append(Issue("fail", f"A step has an unusable id ({nid!r}).", nid or None))
            continue
        if nid in by_id:
            issues.append(Issue("fail", f"Two steps share the id {nid!r}.", nid))
            continue
        by_id[nid] = n
        kind = n.get("type")
        if kind not in NODE_TYPES:
            issues.append(Issue("fail", f"Unknown step type {kind!r}.", nid))

    _check_start(doc, by_id, issues)
    reach = _reachable(doc, by_id)
    _check_data_order(doc, by_id, reach, issues)
    _check_edges(doc, by_id, issues, reach)
    wired = {(str(e.get("from") or ""), str(e.get("port") or "next"))
             for e in doc.get("edges") or [] if isinstance(e, dict)}
    for nid, node in by_id.items():
        _check_node(nid, node, by_id, issues, wired)
    _check_text_limits(doc, issues)
    _check_variables(doc, by_id, issues)
    for nid, node in by_id.items():
        if nid not in reach:
            issues.append(Issue("warn", f"{_name(node, nid)} cannot be reached from the start, so customers "
                                        "will never see it.", nid))
    _check_loops(doc, by_id, issues)
    _check_collisions(by_id, issues)
    # The same text in three languages used to raise the same line three times.
    unique: dict[tuple, Issue] = {}
    for i in issues:
        unique.setdefault((i.level, i.message, i.node_id, i.field), i)
    return sorted(unique.values(), key=lambda i: 0 if i.level == "fail" else 1)


def _check_start(doc: dict, by_id: dict, issues: list[Issue]) -> None:
    start = str(doc.get("start") or "")
    if not start:
        issues.append(Issue("fail", "No starting step is set. Mark one step as the start."))
    elif start not in by_id:
        issues.append(Issue("fail", f"The starting step {start!r} does not exist."))


def _check_edges(doc: dict, by_id: dict, issues: list[Issue], reach: set[str]) -> None:
    seen: set[tuple[str, str]] = set()
    for e in doc.get("edges") or []:
        if not isinstance(e, dict):
            continue
        src, dst = str(e.get("from") or ""), str(e.get("to") or "")
        port = str(e.get("port") or "next")
        if src not in by_id:
            issues.append(Issue("fail", f"A connection starts at a step that does not exist ({src!r})."))
            continue
        if dst not in by_id:
            issues.append(Issue("fail", "A connection leads to a step that no longer exists. "
                                        "Delete the connection or point it somewhere.", src))
            continue
        valid = {p for p, _ in ports_of(by_id[src])}
        if port not in valid:
            if to_person(by_id[src]) and port == "next":
                issues.append(Issue("fail", f"{_name(by_id[src], src)} hands the chat to a person, and the bot says "
                                            "nothing after that. Remove the connection after it - put any message "
                                            "before it instead.", src))
            else:
                issues.append(Issue("fail", f"A connection leaves {_name(by_id[src], src)} by an exit that no "
                                            "longer exists - probably a button you deleted.", src))
            continue
        if (src, port) in seen:
            issues.append(Issue("fail", f"{_name(by_id[src], src)} has two connections from the same exit. "
                                        "Each answer can only lead to one place.", src))
        seen.add((src, port))

    # every exit must go somewhere: a button that leads nowhere is a tap into silence. A step no one
    # can reach strands no one, so its loose ends are left to the "cannot be reached" warning.
    for nid, node in by_id.items():
        if nid not in reach:
            continue
        for port, label in ports_of(node):
            if (nid, port) in seen:
                continue
            if node.get("type") == "question" and port.startswith("opt:"):
                issues.append(Issue("fail", f'"{label}" in {_name(node, nid)} is not connected to anything. '
                                            "A customer who taps it would get no reply.", nid, port))
            elif port in ("failed", "on_error", "unsure", "none", "empty"):
                # Someone else's outage must not strand a customer mid-conversation.
                issues.append(Issue("fail", f'"{label}" in {_name(node, nid)} is not connected to anything. '
                                            "If this step fails the customer would be left with silence, so it "
                                            "needs somewhere to go.", nid, port))
            elif port.startswith("route:"):
                issues.append(Issue("fail", f'The response rule "{label}" in {_name(node, nid)} leads nowhere.',
                                    nid, port))
            elif port in ("next", "else", "success", "answered", "found"):
                issues.append(Issue("warn", f"{_name(node, nid)} has nothing after it. The conversation "
                                            "stops there. Add an End step to make that deliberate.", nid, port))


def _check_node(nid: str, node: dict, by_id: dict, issues: list[Issue], wired: set | None = None) -> None:
    kind = node.get("type")
    if node.get("unsupported"):
        issues.append(Issue("fail", f"{_name(node, nid)} came from WATI as a kind of step this builder cannot "
                                    "run yet. Its settings were kept so nothing is lost; replace it before "
                                    "publishing.", nid))
    if kind == "question":
        _check_question(nid, node, issues, wired or set(), by_id)
    elif kind == "data":
        _check_data(nid, node, issues)
    elif kind == "condition":
        _check_condition(nid, node, issues)
    elif kind in ("delay", "tags", "assign", "chat_status", "subscribe", "api_request", "template", "jump"):
        _check_action(nid, node, issues)
    elif kind == "message":
        _check_media(nid, node, node.get("media"), issues)
    elif kind == "ai_reply":
        _check_ai_reply(nid, node, issues)
    elif kind == "product_list":
        if not str(node.get("catalog_id") or "").strip() or not str(node.get("set_id") or "").strip():
            issues.append(Issue("warn", f"{_name(node, nid)} has no catalogue product set chosen. WhatsApp can "
                                        "only show a product list from a catalogue connected to your WhatsApp "
                                        "Business account in WATI.", nid))
        # Said once per step, so nobody publishes expecting a catalogue card and gets text.
        issues.append(Issue("warn", f"{_name(node, nid)} goes out as its text when live: WATI's API has no call "
                                    "for sending catalogue products, so customers see the header and message "
                                    "without the product cards.", nid))
    elif kind == "set_var":
        assign = node.get("assign")
        if not isinstance(assign, dict) or not assign:
            issues.append(Issue("fail", f"{_name(node, nid)} does not set anything.", nid))
        else:
            for name in assign:
                if not _VAR_NAME.match(str(name)):
                    issues.append(Issue("fail", f"{name!r} is not a usable answer name. Use lowercase "
                                                "letters, numbers and underscores, e.g. company_name.", nid))


def _check_action(nid: str, node: dict, issues: list[Issue]) -> None:
    """The steps that reach outside the conversation. Each one can be half-filled in the editor, and
    a half-filled one fails silently at the worst moment - when a real customer is waiting."""
    kind = node.get("type")
    name = _name(node, nid)

    if kind == "delay":
        secs = node.get("seconds")
        try:
            secs = int(secs)
        except (TypeError, ValueError):
            issues.append(Issue("fail", f"{name} does not say how long to wait.", nid))
            return
        if secs < 1 or secs > DELAY_MAX_SEC:
            issues.append(Issue("fail", f"{name} waits {secs} seconds. Use between 1 second and "
                                        f"{DELAY_MAX_SEC // 60} minutes - WhatsApp conversations are live, and a "
                                        "customer left waiting longer assumes the bot is broken.", nid))

    elif kind == "tags":
        if not [t for t in (node.get("tags") or []) if str(t).strip()]:
            issues.append(Issue("fail", f"{name} has no tags on it.", nid))

    elif kind == "assign":
        to = node.get("to") or "operator"
        if to == "operator" and not str(node.get("email") or "").strip():
            issues.append(Issue("fail", f"{name} does not say which person to assign the chat to. WATI "
                                        "identifies an operator by their email address.", nid))
        if to == "team" and not [t for t in (node.get("teams") or []) if str(t).strip()]:
            issues.append(Issue("fail", f"{name} does not name a team.", nid))

    elif kind == "chat_status":
        if (node.get("status") or "") not in CHAT_STATUSES:
            issues.append(Issue("fail", f"{name} must set the conversation to one of: "
                                        f"{', '.join(CHAT_STATUSES)}.", nid))

    elif kind == "template":
        if not str(node.get("template_name") or "").strip():
            issues.append(Issue("fail", f"{name} does not say which approved template to send.", nid))

    elif kind == "jump":
        if not str(node.get("workflow") or "").strip():
            issues.append(Issue("fail", f"{name} does not say which workflow to continue in.", nid))

    elif kind == "api_request":
        url = str(node.get("url") or "").strip()
        if not url:
            issues.append(Issue("fail", f"{name} has no address to call.", nid))
        elif "{" not in url and not url.lower().startswith(("http://", "https://")):
            issues.append(Issue("fail", f"{name} must call a full address starting with https://", nid))
        elif url.lower().startswith("http://"):
            issues.append(Issue("warn", f"{name} calls an http:// address. Anything sent, including any "
                                        "key in the headers, travels unencrypted.", nid))
        if str(node.get("method") or "GET").upper() not in HTTP_METHODS:
            issues.append(Issue("fail", f"{name} must use {' or '.join(HTTP_METHODS)}.", nid))
        for var in (node.get("save") or {}):
            if not _VAR_NAME.match(str(var)):
                issues.append(Issue("fail", f"{var!r} is not a usable answer name in {name}. Use lowercase "
                                            "letters, numbers and underscores.", nid))
        body = node.get("body")
        if isinstance(body, str) and body.strip():
            # A {placeholder} lives inside a quoted JSON string, so the template is still valid JSON
            # and can be checked as it stands.
            import json as _json
            try:
                _json.loads(body)
            except ValueError:
                issues.append(Issue("fail", f"The body of {name} is not valid JSON. Placeholders go "
                                            'inside the quotes, like {"company": "{company}"}.', nid))
        ids = [str(r.get("id") or "") for r in (node.get("routes") or []) if isinstance(r, dict)]
        if len(ids) != len(set(ids)):
            issues.append(Issue("fail", f"Two response rules in {name} share an id.", nid))


def _check_ai_reply(nid: str, node: dict, issues: list[Issue]) -> None:
    """The AI answers only from what the owner wrote, so an empty FAQ is an AI with nothing to say."""
    name = _name(node, nid)
    faq = str(node.get("faq") or "").strip()
    if not faq:
        issues.append(Issue("fail", f"{name} has no FAQ text. The AI answers only from what you write there - "
                                    "your pouch types, minimum order, delivery times, how pricing works.", nid, "faq"))
    elif len(faq) > AI_FAQ_MAX:
        issues.append(Issue("fail", f"The FAQ text in {name} is {len(faq)} characters. Keep it under {AI_FAQ_MAX}: "
                                    "split it over two steps, one per topic.", nid, "faq"))
    if (node.get("tone") or "friendly") not in AI_TONES:
        issues.append(Issue("fail", f"{name} must answer in a {' or '.join(AI_TONES)} tone.", nid))
    try:
        length = int(node.get("max_chars") or 600)
    except (TypeError, ValueError):
        length = 0
    if not AI_ANSWER_MIN <= length <= AI_ANSWER_MAX:
        issues.append(Issue("fail", f"{name} must allow answers of {AI_ANSWER_MIN} to {AI_ANSWER_MAX} characters "
                                    "(WhatsApp's limit).", nid, "max_chars"))
    store = node.get("store")
    if store and not _VAR_NAME.match(str(store)):
        issues.append(Issue("fail", f"{store!r} is not a usable answer name. Use lowercase letters, numbers and "
                                    "underscores, e.g. question.", nid, "store"))


def _check_question(nid: str, node: dict, issues: list[Issue], wired: set | None = None,
                    by_id: dict | None = None) -> None:
    wired = wired or set()
    spec = node.get("input") or {}
    kind = spec.get("kind")
    if kind not in INPUT_KINDS:
        issues.append(Issue("fail", f"{_name(node, nid)} has no answer type set.", nid))
        return
    store = node.get("store")
    if store and not _VAR_NAME.match(str(store)):
        issues.append(Issue("fail", f"{store!r} is not a usable answer name. Use lowercase letters, "
                                    "numbers and underscores, e.g. company_name.", nid, "store"))
    vtype = (node.get("validate") or {}).get("type", "any")
    if vtype not in VALIDATE_TYPES:
        issues.append(Issue("fail", f"{_name(node, nid)} checks the answer against an unknown rule "
                                    f"({vtype!r}).", nid))
    if vtype == "regex":
        pattern = str((node.get("validate") or {}).get("pattern") or "")
        if not pattern:
            issues.append(Issue("fail", f"{_name(node, nid)} checks the answer against a pattern, but no "
                                        "pattern is set.", nid))
        elif len(pattern) > 200:
            issues.append(Issue("fail", f"The pattern in {_name(node, nid)} is over 200 characters.", nid))
        else:
            try:
                re.compile(pattern)
            except re.error as e:
                issues.append(Issue("fail", f"The pattern in {_name(node, nid)} is not valid ({e}).", nid))
    if vtype == "lookup":
        source = str((node.get("validate") or {}).get("source") or "")
        if source not in LOOKUP_SOURCES:
            issues.append(Issue("fail", f"{_name(node, nid)} checks the answer against your data but does not say "
                                        "what to check - an SO number, a PO number, an item code or a customer code.", nid))
        elif not store:
            issues.append(Issue("warn", f"{_name(node, nid)} checks the answer against your data, but the answer has no "
                                        "name, so later steps cannot use what was found. Name it, e.g. order, and write "
                                        "{order_status}.", nid, "store"))
    if node.get("header_media"):
        if kind == "list":
            issues.append(Issue("fail", f"{_name(node, nid)} has a picture header, but WhatsApp lists can only "
                                        "have a text header. Use buttons, or a text header.", nid))
        else:
            _check_media(nid, node, node.get("header_media"), issues)

    if kind == "data_list":
        _check_data_list(nid, node, spec, issues, by_id or {})
        return
    if kind == "language":
        _check_language(nid, node, spec, issues)
    if kind in ("text", "language"):
        # With "Not valid" connected a wrong answer goes there, so nobody is asked for ever.
        if (kind == "text" and not int(node.get("max_retries") or 0) and vtype != "any"
                and (nid, "invalid") not in wired):
            issues.append(Issue("warn", f"{_name(node, nid)} rejects an answer it does not like but never "
                                        "gives up. A customer could be asked forever - set a number of tries, "
                                        "or connect Not valid.", nid))
        return

    options = [o for o in (spec.get("options") or []) if isinstance(o, dict)]
    if not options:
        issues.append(Issue("fail", f"{_name(node, nid)} offers no choices.", nid))
        return
    cap = menus.BUTTONS_MAX if kind == "buttons" else menus.LIST_ROWS_MAX
    if len(options) > cap:
        word = "tap buttons" if kind == "buttons" else "list rows"
        issues.append(Issue("fail", f"{_name(node, nid)} has {len(options)} choices. WhatsApp allows at "
                                    f"most {cap} {word}." +
                            (" Switch it to a list to allow up to 10." if kind == "buttons" else ""), nid))

    values: set[str] = set()
    for i, o in enumerate(options):
        value = str(o.get("value") or "")
        if not value:
            issues.append(Issue("fail", f"Choice {i + 1} in {_name(node, nid)} has no id.", nid))
        elif value in values:
            issues.append(Issue("fail", f"Two choices in {_name(node, nid)} share the id {value!r}.", nid))
        values.add(value)

    # Two choices whose visible text matches: the customer's tap is ambiguous and the first wins.
    for lg in LANGS:
        seen: dict[str, int] = {}
        for i, o in enumerate(options):
            n = norm_trigger(text_of(o.get("label"), lg))
            if not n:
                continue
            if n in seen:
                issues.append(Issue("fail", f"Choices {seen[n] + 1} and {i + 1} in {_name(node, nid)} read the "
                                            f'same in {lg.upper()} ("{text_of(o.get("label"), lg)}"). The bot '
                                            "could not tell which one the customer tapped.", nid))
            seen[n] = i

    if kind == "list":
        titles = [text_of(o.get("section"), "en").strip() for o in options]
        if len({t for t in titles if t}) > 1 and not all(titles):
            issues.append(Issue("fail", f"Some rows in {_name(node, nid)} are in a section and some are not. When "
                                        "a list has more than one section, WhatsApp needs a title on every one.",
                                nid))


def _check_language(nid: str, node: dict, spec: dict, issues: list[Issue]) -> None:
    """The language buttons are the owner's to word and choose, within WhatsApp's rules."""
    chosen = spec.get("languages")
    if chosen is not None:
        unknown = [str(c) for c in chosen if c not in LANGS]
        if unknown:
            issues.append(Issue("fail", f"{_name(node, nid)} offers a language the bot does not speak "
                                        f"({', '.join(unknown)}).", nid))
        if len({c for c in chosen if c in LANGS}) < 2:
            issues.append(Issue("fail", f"{_name(node, nid)} offers fewer than two languages. A language question "
                                        "needs at least two to choose from.", nid))
    seen: dict[str, str] = {}
    for code in offered_languages(spec):
        label = language_label(spec, code, "en")
        if len(label) > menus.BUTTON_TEXT_MAX:
            issues.append(Issue("fail", f'The {LANGUAGE_NAMES[code]} button in {_name(node, nid)} says "{label}", '
                                        f"which is {len(label)} characters. WhatsApp allows {menus.BUTTON_TEXT_MAX}.",
                                nid, f"input.language_labels.{code}"))
        n = norm_trigger(label)
        if n in seen:
            issues.append(Issue("fail", f"The {seen[n]} and {LANGUAGE_NAMES[code]} buttons in {_name(node, nid)} both "
                                        f'say "{label}". The bot could not tell which one was tapped.', nid))
        seen[n] = LANGUAGE_NAMES[code]


def _check_media(nid: str, node: dict, media: Any, issues: list[Issue]) -> None:
    """A picture, video, document or voice note. WhatsApp fetches it from a public https address."""
    if not isinstance(media, dict) or not (media.get("url") or media.get("type")):
        return
    if media.get("type") not in MEDIA_TYPES:
        issues.append(Issue("fail", f"{_name(node, nid)} has an attachment of an unknown kind "
                                    f"({media.get('type')!r}).", nid))
    url = str(media.get("url") or "").strip()
    if not url:
        issues.append(Issue("fail", f"{_name(node, nid)} has an attachment with no file address.", nid))
    elif "{" not in url and not url.lower().startswith("https://"):
        issues.append(Issue("fail", f"{_name(node, nid)} attaches a file from {url[:60]!r}. WhatsApp only "
                                    "fetches files from a public https:// address.", nid))


def _check_condition(nid: str, node: dict, issues: list[Issue]) -> None:
    branches = [b for b in (node.get("branches") or []) if isinstance(b, dict)]
    if not branches:
        issues.append(Issue("fail", f"{_name(node, nid)} has no rules, so it always takes Otherwise.", nid))
    ids: set[str] = set()
    for b in branches:
        bid = str(b.get("id") or "")
        if not bid:
            issues.append(Issue("fail", f"A rule in {_name(node, nid)} has no id.", nid))
        elif bid in ids:
            issues.append(Issue("fail", f"Two rules in {_name(node, nid)} share the id {bid!r}.", nid))
        ids.add(bid)
        when = b.get("when") or {}
        op = when.get("op")
        if op not in CONDITION_OPS:
            issues.append(Issue("fail", f"A rule in {_name(node, nid)} uses an unknown test ({op!r}).", nid))
        if not str(when.get("var") or ""):
            issues.append(Issue("fail", f"A rule in {_name(node, nid)} does not say which answer to check.", nid))


def _check_text_limits(doc: dict, issues: list[Issue]) -> None:
    # A WATI-style flow writes one text per step and branches by language instead. Asking it for
    # Hindi and Gujarati on every step would bury the real problems under warnings.
    multilingual = (doc.get("settings") or {}).get("multilingual", True) is not False
    steps = {str(n.get("id") or ""): _name(n, str(n.get("id") or "")) for n in doc.get("nodes") or []
             if isinstance(n, dict)}
    for s in walk_strings(doc):
        where = f'{s["label"]} in {steps.get(s["node_id"], "a step")}'
        for lg in LANGS:
            text = s["value"].get(lg) or ""
            if len(text) > s["max"]:
                issues.append(Issue("fail", f'{where} is {len(text)} characters in {LANGUAGE_NAMES[lg]}. '
                                            f'WhatsApp allows {s["max"]}.', s["node_id"], f'{s["path"]}.{lg}'))
        if not (s["value"].get("en") or "").strip():
            if s.get("required", True):
                why = ("English is what every other language falls back to, so it cannot be empty."
                       if multilingual else "The customer would get an empty message.")
                lead = "English text" if multilingual else "text"
                issues.append(Issue("fail", f'{where} has no {lead}. {why}',
                                    s["node_id"], f'{s["path"]}.en'))
            continue
        if not multilingual:
            continue
        # One line per text, naming both languages - not one per language.
        missing = [LANGUAGE_NAMES[lg] for lg in ("hi", "gu") if not (s["value"].get(lg) or "").strip()]
        if missing:
            issues.append(Issue("warn", f'{where} has no {" or ".join(missing)} yet, so those customers see the '
                                        "English. Translate fills it in.", s["node_id"], f'{s["path"]}.hi'))


def _check_variables(doc: dict, by_id: dict, issues: list[Issue]) -> None:
    """An answer used before anything sets it renders as empty - catch it while it is still cheap."""
    defined: set[str] = set(SYSTEM_VARS)
    for node in by_id.values():
        if node.get("type") in WAITING_TYPES and node.get("store"):
            defined.add(str(node["store"]))
            defined.update(answer_extras(node))
        if node.get("type") == "data":
            defined.update(data_vars(node))
        if node.get("type") == "question" and (node.get("input") or {}).get("kind") == "data_list":
            defined.update(data_answer_extras(node, data_node_of(by_id, node.get("input") or {})))
        if node.get("type") == "set_var":
            defined.update(str(k) for k in (node.get("assign") or {}))

    for node in by_id.values():
        nid = str(node.get("id"))
        for name in _placeholders_in(node):
            if name not in defined:
                issues.append(Issue("warn", f"{_name(node, nid)} uses {{{name}}}, which nothing in this "
                                            "workflow sets. It would come out blank.", nid))
        if node.get("type") == "condition":
            for b in node.get("branches") or []:
                var = str((b.get("when") or {}).get("var") or "")
                if var and var not in defined:
                    issues.append(Issue("warn", f"{_name(node, nid)} tests {var!r}, which nothing in this "
                                                "workflow sets. That rule can never be true.", nid))


# Keys the customer never sees: an internal title, and the raw settings kept from a WATI import
# (whose own {{placeholders}} would otherwise read as ours and raise false warnings).
_NOT_SENT = ("title", "wati_raw", "id", "type", "unsupported", "list_line")


def _placeholders_in(node: dict) -> set[str]:
    import re as _re

    found: set[str] = set()
    for value in _strings_of({k: v for k, v in node.items() if k not in _NOT_SENT}):
        for m in _re.finditer(r"\{([a-zA-Z][a-zA-Z0-9_.]{0,40})\}", value):
            found.add(m.group(1))
    return found


def _strings_of(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        for v in node.values():
            out.extend(_strings_of(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_strings_of(v))
    return out


def _reachable(doc: dict, by_id: dict, without: str = "") -> set[str]:
    """Steps a customer can actually get to. With no usable start every step counts, so a missing
    start (already its own failure) does not also bury every other check under warnings."""
    start = str(doc.get("start") or "")
    if without and start == without:
        return set()  # nothing at all is reached without the step the conversation starts at
    if start not in by_id:
        return set(by_id)
    routes: dict[str, list[str]] = {}
    for e in doc.get("edges") or []:
        if isinstance(e, dict) and str(e.get("from")) in by_id:
            routes.setdefault(str(e["from"]), []).append(str(e.get("to")))
    seen = {start}
    stack = [start]
    while stack:
        for nxt in routes.get(stack.pop(), []):
            # `without` walks the flow as if one step were not there, to see what still gets reached
            if nxt in by_id and nxt not in seen and nxt != without:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _check_loops(doc: dict, by_id: dict, issues: list[Issue]) -> None:
    """A loop with nothing that waits for the customer spins until the run cap trips."""
    waits = {nid for nid, n in by_id.items() if n.get("type") in WAITING_TYPES}
    routes: dict[str, list[str]] = {}
    for e in doc.get("edges") or []:
        if isinstance(e, dict):
            routes.setdefault(str(e.get("from")), []).append(str(e.get("to")))

    colour: dict[str, int] = {}

    def walk(nid: str, path: list[str]) -> None:
        colour[nid] = 1
        path.append(nid)
        for nxt in routes.get(nid, []):
            if nxt not in by_id:
                continue
            if colour.get(nxt) == 1:  # back edge: everything from nxt onwards is the cycle
                cycle = path[path.index(nxt):]
                if not any(c in waits for c in cycle):
                    names = " -> ".join(_name(by_id[c], c) for c in cycle)
                    issues.append(Issue("fail", "These steps loop forever without ever waiting for the "
                                                f"customer: {names}. Put a question in the loop, or break it.",
                                        nid))
            elif nxt not in colour:
                walk(nxt, path)
        path.pop()
        colour[nid] = 2

    for nid in by_id:
        if nid not in colour:
            walk(nid, [])


def _check_collisions(by_id: dict, issues: list[Issue]) -> None:
    """A choice the customer taps must not read as an order or item code.

    A label shared with a built-in menu button ("Yes", "Order status") is fine: inside a running
    workflow the workflow reads the tap first. Keywords that START a workflow are checked against the
    order-status bot separately, in runtime.check_routing."""
    for nid, node in by_id.items():
        if node.get("type") != "question":
            continue
        for o in (node.get("input") or {}).get("options") or []:
            if not isinstance(o, dict):
                continue
            for lg in LANGS:
                text = text_of(o.get("label"), lg)
                if not norm_trigger(text):
                    continue
                parsed = regex_parse(text, use_labels=False)
                if parsed.so_no or parsed.po_no or parsed.fg_code or parsed.bare_codes:
                    issues.append(Issue("fail", f'"{text}" reads as an order or item code, which the bot '
                                                "handles separately. Use wording that is not a code.", nid))


def collides_with_custom(label: str) -> str:
    """Would an enabled custom reply swallow this label? Empty when it is clear.

    Asks the runtime matcher rather than re-deriving the rule, so this can never accept a label the
    live bot would then hand to a custom reply instead."""
    from ..templates import registry

    hit = registry.match_custom(label)
    return hit.key if hit else ""


def _name(node: dict, nid: str) -> str:
    title = str(node.get("title") or "").strip()
    return f'"{title}"' if title else f"Step {nid}"


# ---------------- finding things in the business's own data ----------------
def _field_problem(nid: str, name: str, field: str, fields: tuple, where: str) -> Issue | None:
    """One rule for every place a field can be named, so the answer is always the same sentence."""
    if field in fields:
        return None
    if field == "connection_status":
        return Issue("fail", f"{name} uses Connection Status in {where}. That is your internal status and is "
                             "never shown to a customer - use the real status instead.", nid)
    return Issue("fail", f'{name} uses "{field}" in {where}, which is not one of the fields it can read '
                         f"({', '.join(fields)}).", nid)


def _check_data(nid: str, node: dict, issues: list[Issue]) -> None:
    """A Find-in-your-data step: it reads the business's own orders, so every field it names, and
    every name it saves under, has to be one the engine can actually give it."""
    name = _name(node, nid)
    source = str(node.get("source") or "")
    if source not in DATA_SOURCES:
        issues.append(Issue("fail", f"{name} does not say where to look. Choose their orders, or their record in "
                                    "your customer list.", nid, "source"))
        return
    fields = data_fields(source)

    if source == "orders":
        find = str(node.get("find") or "all")
        if find not in DATA_FINDS:
            issues.append(Issue("fail", f"{name} does not say what to find.", nid, "find"))
        elif find != "all" and not str(node.get("match") or "").strip():
            issues.append(Issue("fail", f"{name} looks up a number but does not say which one. Use the answer you "
                                        "saved earlier, e.g. {order}.", nid, "match"))
        if str(node.get("group") or "so") not in DATA_GROUPS:
            issues.append(Issue("fail", f"{name} must show one row per order or one per order line.", nid, "group"))
    if str(node.get("sort") or "newest") not in DATA_SORTS:
        issues.append(Issue("fail", f"{name} has an unknown order for its rows.", nid, "sort"))

    store = str(node.get("store") or "")
    if not store:
        issues.append(Issue("fail", f"{name} has no name for what it finds. Name it, e.g. orders, and later steps "
                                    "can write {orders_count} and {orders_list}.", nid, "store"))
    elif not _VAR_NAME.match(store):
        issues.append(Issue("fail", f"{store!r} is not a usable name. Use lowercase letters, numbers and "
                                    "underscores, e.g. their_orders.", nid, "store"))

    try:
        limit = int(node.get("limit") or DATA_ROWS_MAX)
    except (TypeError, ValueError):
        limit = 0
    if not 1 <= limit <= DATA_ROWS_MAX:
        issues.append(Issue("fail", f"{name} shows {node.get('limit')!r} rows. Use 1 to {DATA_ROWS_MAX} - WhatsApp "
                                    f"shows at most {DATA_ROWS_MAX} rows in a list.", nid, "limit"))

    for f in node.get("filter") or []:
        if not isinstance(f, dict):
            continue
        problem = _field_problem(nid, name, str(f.get("field") or ""), fields, "a rule")
        if problem:
            issues.append(problem)
        if str(f.get("op") or "eq") not in DATA_OPS:
            issues.append(Issue("fail", f"A rule in {name} uses an unknown test ({f.get('op')!r}).", nid))

    for var, field in (node.get("save") or {}).items():
        if not _VAR_NAME.match(str(var)):
            issues.append(Issue("fail", f"{var!r} is not a usable answer name in {name}. Use lowercase letters, "
                                        "numbers and underscores.", nid, "save"))
        problem = _field_problem(nid, name, str(field), fields, "a value to remember")
        if problem:
            issues.append(problem)

    for used in _placeholders_in({"line": str(node.get("list_line") or "")}):
        problem = _field_problem(nid, name, used, fields, "the line each row reads")
        if problem:
            issues.append(problem)


def _check_data_list(nid: str, node: dict, spec: dict, issues: list[Issue], by_id: dict) -> None:
    """A question whose choices are the rows a data step found."""
    name = _name(node, nid)
    want = str(spec.get("from") or "").strip()
    if not want:
        issues.append(Issue("fail", f"{name} shows rows from a Find-in-your-data step, but does not say which one.",
                            nid, "input.from"))
        return
    data_node = data_node_of(by_id, spec)
    if data_node is None:
        issues.append(Issue("fail", f'{name} shows the rows of "{want}", but no Find-in-your-data step is called '
                                    "that. Check the name on that step.", nid, "input.from"))
        return
    fields = data_fields(str(data_node.get("source") or "orders"))
    title = str(spec.get("title_field") or "")
    if not title:
        issues.append(Issue("fail", f"{name} does not say what each row should read. Choose the field the customer "
                                    "sees, e.g. the SO number.", nid, "input.title_field"))
    for key, where in (("title_field", "row title"), ("description_field", "row description"),
                       ("section_field", "row heading")):
        got = str(spec.get(key) or "")
        if got:
            problem = _field_problem(nid, name, got, fields, where)
            if problem:
                issues.append(problem)
    if str(spec.get("show") or "auto") not in DATA_SHOW:
        issues.append(Issue("fail", f"{name} must show its rows as buttons, as a list, or automatically.",
                            nid, "input.show"))
    if title in fields and title not in DATA_KEY_FIELDS:
        issues.append(Issue("warn", f'{name} shows "{title}" on every row. Two orders reading the same cannot be '
                                    "told apart, so one of them is left out - a number like the SO is safer.", nid))
    if not str(node.get("store") or ""):
        issues.append(Issue("warn", f"{name} does not name what the customer picked, so later steps cannot say "
                                    "{order} or {order_status}.", nid, "store"))


def _check_data_order(doc: dict, by_id: dict, reach: set[str], issues: list[Issue]) -> None:
    """A data list can only show what its data step found, so every path to it must pass that step."""
    for nid, node in by_id.items():
        spec = node.get("input") or {}
        if node.get("type") != "question" or spec.get("kind") != "data_list" or nid not in reach:
            continue
        data_node = data_node_of(by_id, spec)
        if data_node is None:
            continue
        skipped = _reachable(doc, by_id, without=str(data_node.get("id") or ""))
        if nid in skipped:
            issues.append(Issue("warn", f"{_name(node, nid)} can be reached without passing "
                                        f"{_name(data_node, str(data_node.get('id')))} first. On that path it has "
                                        "nothing to show and takes its Nothing-to-show exit.", nid))
