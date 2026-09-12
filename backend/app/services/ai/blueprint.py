"""The AI assistant's working format: a blueprint of steps, compiled into a real workflow.

The model never writes the workflow document itself. It writes steps - what each says, the choices
and where each leads - in a fixed JSON shape Groq enforces, and this module turns that into nodes,
ports and connections exactly as the editor would. The same checks the editor runs then decide
whether it is right, and what they find goes back to the model to fix.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque

from ..workflow.schema import (BUILTIN_ORDER_STATUS, CONDITION_OPS, DATA_FINDS, DATA_LIST_LINE, DATA_ROWS_MAX,
                               DATA_SOURCES, DELAY_MAX_SEC, LANGS, _NODE_ID, _VAR_NAME,
                               branch_port, option_port, text_of, triggers_of)

STEP_TYPES = ("message", "question", "condition", "remember", "tag", "assign", "chat_status", "template",
              "wait", "call_api", "go_to", "ai_reply", "find_data", "end")
CHECKS = ("any", "number", "email", "phone", "date", "time", "url", "file", "location", "so", "po", "item",
          "customer_code")
_CHECK_RULES = {
    "any": {"type": "any"}, "number": {"type": "number"}, "email": {"type": "email"}, "phone": {"type": "phone"},
    "date": {"type": "date"}, "time": {"type": "time"}, "url": {"type": "url"}, "file": {"type": "file"},
    "location": {"type": "location"}, "so": {"type": "lookup", "source": "so"},
    "po": {"type": "lookup", "source": "po"}, "item": {"type": "lookup", "source": "fg"},
    "customer_code": {"type": "lookup", "source": "customer_code"},
}
_TO_NODE = {"message": "message", "question": "question", "condition": "condition", "remember": "set_var",
            "tag": "tags", "assign": "assign", "chat_status": "chat_status", "template": "template",
            "wait": "delay", "call_api": "api_request", "go_to": "jump", "ai_reply": "ai_reply",
            "find_data": "data", "end": "end"}
_TO_STEP = {v: k for k, v in _TO_NODE.items()}
# Steps a blueprint cannot describe (a catalogue list, subscribe): an edit leaves them exactly as they are.
KEPT_AS_IS = ("product_list", "subscribe")
FAQ_MAX = 8000


# ---------------- the shapes Groq is held to ----------------
def _str() -> dict:
    return {"type": "string"}


def _nstr() -> dict:
    return {"type": ["string", "null"]}


def _obj(props: dict) -> dict:
    # strict mode: every property listed, nothing extra
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _nenum(values: list) -> dict:
    return {"type": ["string", "null"], "enum": [*values, None]}


TEXT = _obj({"en": _str(), "hi": _str(), "gu": _str()})
_NTEXT = {"anyOf": [TEXT, {"type": "null"}]}
_PAIR = _obj({"name": _str(), "value": _str()})
_CHOICE = _obj({"label": TEXT, "description": _nstr(), "next": _nstr()})
_ANSWER = _obj({
    "kind": {"type": "string", "enum": ["buttons", "list", "text", "language", "data_list"]},
    "check": {"type": "string", "enum": list(CHECKS)},
    "save_as": _nstr(),
    "choices": {"type": "array", "items": _CHOICE},
    "list_button": _nstr(),
    "anything_else": _nstr(),
    "not_valid": _nstr(),
    "retries": {"type": "integer"},
    "wrong_answer_text": _NTEXT,
    # a data list: whose rows to show, what each row reads, and where to go when there are none
    "from_step": _nstr(),
    "title_field": _nstr(),
    "description_field": _nstr(),
    "nothing_found": _nstr(),
})
_BRANCH = _obj({"label": _str(), "var": _str(), "op": {"type": "string", "enum": list(CONDITION_OPS)},
                "value": _str(), "next": _nstr()})
_DETAILS = _obj({
    "tags": {"type": "array", "items": _str()}, "remove_tags": {"type": "boolean"},
    "assign_to": _nenum(["team", "operator", "bot"]), "teams": {"type": "array", "items": _str()},
    "email": _nstr(), "status": _nenum(["open", "pending", "solved"]), "template_name": _nstr(),
    "template_params": {"type": "array", "items": _PAIR}, "seconds": {"type": ["integer", "null"]},
    "url": _nstr(), "method": _nenum(["GET", "POST"]), "workflow": _nstr(),
    "remember": {"type": "array", "items": _PAIR}, "save_on_contact": {"type": "boolean"}, "faq": _nstr(),
    # find_data: where to look, what to look for, and what to keep from the first row
    "look_in": _nenum(["orders", "customer"]),
    "find_by": _nenum(["all", "so", "po", "fg"]),
    "find_value": _nstr(),
    "one_row_per": _nenum(["so", "line"]),
    "keep": {"type": "array", "items": _PAIR},
    "line_reads": _nstr(),
})
STEP = _obj({
    "id": _str(), "type": {"type": "string", "enum": list(STEP_TYPES)}, "title": _str(), "text": _NTEXT,
    "next": _nstr(), "on_fail": _nstr(), "answer": {"anyOf": [_ANSWER, {"type": "null"}]},
    "branches": {"type": "array", "items": _BRANCH}, "else_next": _nstr(),
    "details": {"anyOf": [_DETAILS, {"type": "null"}]},
})
TRIGGERS = _obj({"keywords": {"type": "array", "items": _str()}, "menu_label": _nstr(), "new_numbers": {"type": "boolean"}})

OUTLINE_SCHEMA = _obj({
    "title": _str(), "summary": _str(), "three_languages": {"type": "boolean"}, "triggers": TRIGGERS,
    "start": _str(),
    "sections": {"type": "array", "items": _obj({
        "name": _str(),
        "steps": {"type": "array", "items": _obj({"id": _str(), "type": {"type": "string", "enum": list(STEP_TYPES)},
                                                  "title": _str(), "purpose": _str()})},
    })},
})
STEPS_SCHEMA = _obj({"steps": {"type": "array", "items": STEP}})
PATCH_SCHEMA = _obj({
    "summary": _str(), "title": _nstr(), "start": _nstr(), "three_languages": {"type": ["boolean", "null"]},
    "triggers": {"anyOf": [TRIGGERS, {"type": "null"}]},
    "upsert": {"type": "array", "items": STEP}, "remove": {"type": "array", "items": _str()},
})
REVIEW_SCHEMA = _obj({
    "summary": _str(),
    "findings": {"type": "array", "items": _obj({
        "step_id": _nstr(), "severity": {"type": "string", "enum": ["problem", "suggestion"]},
        "problem": _str(), "suggestion": _str(),
    })},
})


# ---------------- blueprint -> workflow ----------------
def compile_blueprint(bp: dict, base: dict | None = None) -> tuple[dict, list[str]]:
    """The workflow document, and notes on anything that could not be followed (a `next` naming a
    step that does not exist). With `base`, steps keep what the blueprint cannot say - positions,
    headers, pictures, synonyms - and steps it cannot describe are left untouched."""
    notes: list[str] = []
    three = bp.get("three_languages") is not False
    steps = [s for s in bp.get("steps") or [] if isinstance(s, dict)]
    base_nodes = [n for n in (base or {}).get("nodes") or [] if isinstance(n, dict)]
    old = {str(n.get("id")): n for n in base_nodes}
    kept = {nid: n for nid, n in old.items() if n.get("type") in KEPT_AS_IS}

    ids: dict[str, str] = {}
    order: list[str] = []
    taken = set(kept)
    for i, s in enumerate(steps):
        raw = str(s.get("id") or "").strip()
        nid = _safe_id(raw) or f"step{i + 1}"
        n = 2
        while nid in taken:
            nid = f"{_safe_id(raw) or 'step'}_{n}"
            n += 1
        taken.add(nid)
        order.append(nid)
        if raw and raw not in ids:
            ids[raw] = nid

    def ref(target, where: str) -> str | None:
        t = str(target or "").strip()
        if not t:
            return None
        if t in ids:
            return ids[t]
        if t in kept:
            return t
        notes.append(f'{where} leads to "{t}", which is not one of the steps.')
        return None

    nodes: list[dict] = []
    edges: list[dict] = []
    for s, nid in zip(steps, order):
        node, links = _node(s, nid, three)
        for port, target in links:
            dst = ref(target, f'"{node["title"]}"')
            if dst:
                edges.append({"id": "", "from": nid, "port": port, "to": dst})
        prev = old.get(nid)
        if prev and prev.get("type") == node["type"]:
            node = _merge(prev, node)
        nodes.append(node)
    for nid, n in kept.items():
        nodes.append(n)
        edges.extend(dict(e) for e in (base or {}).get("edges") or [] if isinstance(e, dict) and e.get("from") == nid)
    for i, e in enumerate(edges):
        e["id"] = f"e{i + 1}"

    start = ref(bp.get("start"), "The start") or (order[0] if order else "")
    doc = {"schema": 1, "start": start, "settings": _settings(bp, base, three), "nodes": nodes, "edges": edges}
    _place(doc, old)
    return doc, notes


def _node(s: dict, nid: str, three: bool) -> tuple[dict, list[tuple[str, object]]]:
    kind = s.get("type") if s.get("type") in STEP_TYPES else "message"
    title = str(s.get("title") or "").strip()[:60] or kind.replace("_", " ").capitalize()
    node: dict = {"id": nid, "type": _TO_NODE[kind], "title": title, "x": 0, "y": 0}
    d = s.get("details") if isinstance(s.get("details"), dict) else {}
    text = _text(s.get("text"), three)
    nxt, fail = s.get("next"), s.get("on_fail")
    links: list[tuple[str, object]] = []

    if kind == "message":
        node["text"] = text or _blank()
        links.append(("next", nxt))
    elif kind == "end":
        if text:
            node["text"] = text
    elif kind == "question":
        node.update(_question(s, text, three, links))
    elif kind == "condition":
        node["branches"] = []
        for i, b in enumerate(b for b in s.get("branches") or [] if isinstance(b, dict)):
            bid = f"b{i + 1}"
            node["branches"].append({"id": bid, "label": str(b.get("label") or f"Rule {i + 1}")[:40],
                                     "when": {"var": str(b.get("var") or ""),
                                              "op": b.get("op") if b.get("op") in CONDITION_OPS else "eq",
                                              "value": str(b.get("value") or "")}})
            links.append((branch_port(bid), b.get("next")))
        links.append(("else", s.get("else_next") or nxt))
    elif kind == "remember":
        node["assign"] = {_var(p.get("name")): str(p.get("value") or "")
                          for p in d.get("remember") or [] if isinstance(p, dict) and _var(p.get("name"))}
        if d.get("save_on_contact"):
            node["to_contact"] = True
        links.append(("next", nxt))
    elif kind == "tag":
        node["tags"] = [str(t).strip()[:40] for t in d.get("tags") or [] if str(t).strip()]
        node["remove"] = bool(d.get("remove_tags"))
        links.append(("next", nxt))
    elif kind == "assign":
        to = d.get("assign_to") if d.get("assign_to") in ("team", "operator", "bot") else "team"
        node.update({"to": to, "teams": [str(t).strip() for t in d.get("teams") or [] if str(t).strip()],
                     "email": str(d.get("email") or "")})
        if to == "bot":
            links.append(("next", nxt))
        links.append(("on_error", fail))
    elif kind == "chat_status":
        node["status"] = d.get("status") if d.get("status") in ("open", "pending", "solved") else "pending"
        links.append(("next", nxt))
    elif kind == "template":
        node["template_name"] = str(d.get("template_name") or "")
        node["params"] = {str(p.get("name")): str(p.get("value") or "")
                          for p in d.get("template_params") or [] if isinstance(p, dict) and p.get("name")}
        links += [("next", nxt), ("on_error", fail)]
    elif kind == "wait":
        node["seconds"] = max(1, min(_int(d.get("seconds"), 5), DELAY_MAX_SEC))
        links.append(("next", nxt))
    elif kind == "call_api":
        node.update({"url": str(d.get("url") or ""), "method": "POST" if d.get("method") == "POST" else "GET",
                     "headers": {}, "body": "", "save": {}, "routes": []})
        links += [("success", nxt), ("failed", fail)]
    elif kind == "go_to":
        node["workflow"] = str(d.get("workflow") or BUILTIN_ORDER_STATUS)
    elif kind == "ai_reply":
        node.update({"faq": str(d.get("faq") or "")[:FAQ_MAX], "tone": "friendly", "max_chars": 600})
        if text:
            node["text"] = text
        links += [("answered", nxt), ("unsure", fail)]
    elif kind == "find_data":
        node.update({"source": d.get("look_in") if d.get("look_in") in DATA_SOURCES else "orders",
                     "find": d.get("find_by") if d.get("find_by") in DATA_FINDS else "all",
                     "match": str(d.get("find_value") or ""),
                     "group": d.get("one_row_per") if d.get("one_row_per") in ("so", "line") else "so",
                     "sort": "newest", "limit": DATA_ROWS_MAX,
                     # the step's own id names what it found, so a list question can point at it
                     "store": _var(nid) or "rows",
                     "list_line": str(d.get("line_reads") or DATA_LIST_LINE),
                     "save": {_var(p.get("name")): str(p.get("value") or "")
                              for p in d.get("keep") or [] if isinstance(p, dict) and _var(p.get("name"))}})
        links += [("found", nxt), ("none", fail)]
    return node, links


def _question(s: dict, text: dict | None, three: bool, links: list) -> dict:
    a = s.get("answer") if isinstance(s.get("answer"), dict) else {}
    kind = a.get("kind") if a.get("kind") in ("buttons", "list", "text", "language", "data_list") else "text"
    check = a.get("check") if a.get("check") in CHECKS else "any"
    out: dict = {"text": text or _blank(), "input": {"kind": kind}}
    nxt = s.get("next")
    if kind == "data_list":
        # the rows come from a find_data step, so there is nothing to write here but where to look
        out["input"].update({"from": _var(a.get("from_step")),
                             "title_field": str(a.get("title_field") or "so_no"),
                             "description_field": str(a.get("description_field") or "")})
        out["validate"] = {"type": "any"}
        out["max_retries"] = max(0, min(_int(a.get("retries"), 1), 3))
        if _var(a.get("save_as")):
            out["store"] = _var(a.get("save_as"))
        links += [("next", nxt), ("empty", a.get("nothing_found")), ("default", a.get("anything_else"))]
        return out
    if kind in ("buttons", "list"):
        options, seen = [], set()
        for i, c in enumerate(c for c in a.get("choices") or [] if isinstance(c, dict)):
            label = _text(c.get("label"), three) or _blank()
            value = _slug(text_of(label, "en")) or f"c{i + 1}"
            while value in seen:
                value = f"{value}_{i + 1}"
            seen.add(value)
            option: dict = {"value": value, "label": label}
            desc = str(c.get("description") or "").strip()
            if kind == "list" and desc:
                option["description"] = {"en": desc[:72], "hi": "", "gu": ""}
            options.append(option)
            links.append((option_port(value), c.get("next") or nxt))  # a choice with no next of its own
        out["input"]["options"] = options
        if kind == "list":
            out["input"]["button_text"] = {"en": str(a.get("list_button") or "Choose")[:20], "hi": "", "gu": ""}
        if a.get("anything_else"):
            links.append(("default", a.get("anything_else")))
        check = "any"
    elif kind == "language":
        links.extend((option_port(code), nxt) for code in LANGS)
        check = "any"
    else:
        links.append(("next", nxt))
    out["validate"] = dict(_CHECK_RULES[check])
    store = _var(a.get("save_as"))
    if store:
        out["store"] = store
    retries = max(0, min(_int(a.get("retries"), 0), 5))
    if check != "any" and not retries and not a.get("not_valid"):
        retries = 2  # never ask for ever
    out["max_retries"] = retries
    if check != "any":
        out["invalid_text"] = _text(a.get("wrong_answer_text"), three) or {
            "en": "Sorry, that doesn't look right. Please try again.",
            "hi": "क्षमा करें, यह सही नहीं लगता। कृपया फिर से प्रयास करें।" if three else "",
            "gu": "માફ કરશો, આ સાચું લાગતું નથી. કૃપા કરીને ફરી પ્રયાસ કરો." if three else ""}
        if a.get("not_valid"):
            links.append(("invalid", a.get("not_valid")))
    return out


def _merge(prev: dict, new: dict) -> dict:
    """Keep what the blueprint cannot say (position, header, picture, synonyms...) from the step as it was."""
    merged = {**prev, **new, "x": prev.get("x", 0), "y": prev.get("y", 0)}
    if new.get("type") == "question" and isinstance(prev.get("input"), dict):
        was = {str(o.get("value")): o for o in prev["input"].get("options") or [] if isinstance(o, dict)}
        merged["input"] = {**prev["input"], **new["input"]}
        if "options" in new["input"]:
            merged["input"]["options"] = [{**was.get(o["value"], {}), **o} for o in new["input"]["options"]]
    return merged


def _settings(bp: dict, base: dict | None, three: bool) -> dict:
    out = dict((base or {}).get("settings") or {})
    out["multilingual"] = three
    tr = bp.get("triggers")
    if isinstance(tr, dict):
        label = str(tr.get("menu_label") or "").strip()[:24]
        out["triggers"] = {
            "keywords": [{"text": str(k).strip()[:60], "match": "exact"} for k in tr.get("keywords") or [] if str(k).strip()],
            "menu": {"enabled": bool(label), "label": {"en": label, "hi": "", "gu": ""}},
            "unknown_customer": bool(tr.get("new_numbers")),
        }
    return out


def _place(doc: dict, old: dict) -> None:
    """Columns by distance from the start, for steps that had no place before. The editor tidies
    the whole flow with Arrange when the owner applies it."""
    ahead: dict[str, list[str]] = defaultdict(list)
    for e in doc["edges"]:
        ahead[e["from"]].append(e["to"])
    ids = [n["id"] for n in doc["nodes"]]
    depth: dict[str, int] = {doc["start"]: 0} if doc["start"] in ids else {}
    queue = deque(depth)
    while queue:
        at = queue.popleft()
        for nxt in ahead[at]:
            if nxt not in depth:
                depth[nxt] = depth[at] + 1
                queue.append(nxt)
    far = max(depth.values(), default=0) + 1
    rows: dict[int, int] = defaultdict(int)
    for n in doc["nodes"]:
        prev = old.get(n["id"])
        if prev is not None and "x" in prev:
            n["x"], n["y"] = prev.get("x", 0), prev.get("y", 0)
            continue
        d = depth.get(n["id"], far)
        n["x"], n["y"] = 80 + d * 380, 80 + rows[d] * 260
        rows[d] += 1


# ---------------- workflow -> blueprint (to change an existing one) ----------------
def doc_to_blueprint(doc: dict) -> dict:
    """The workflow as the assistant sees it. Step ids are the node ids, so an edit that keeps a
    step keeps its place, its connections and everything the blueprint does not describe."""
    routes = {(str(e.get("from")), str(e.get("port"))): str(e.get("to"))
              for e in doc.get("edges") or [] if isinstance(e, dict)}
    three = (doc.get("settings") or {}).get("multilingual", True) is not False
    steps = []
    for n in doc.get("nodes") or []:
        if not isinstance(n, dict) or n.get("type") not in _TO_STEP:
            continue
        nid = str(n.get("id"))
        kind = _TO_STEP[n["type"]]

        def r(port: str, _nid: str = nid) -> str | None:
            return routes.get((_nid, port))

        step: dict = {"id": nid, "type": kind, "title": str(n.get("title") or ""), "text": _plain(n.get("text")),
                      "next": None, "on_fail": None, "answer": None, "branches": [], "else_next": None,
                      "details": None}
        if kind in ("message", "remember", "tag", "chat_status", "wait"):
            step["next"] = r("next")
        if kind == "question":
            spec = n.get("input") or {}
            k = spec.get("kind") if spec.get("kind") in ("buttons", "list", "text", "language", "data_list") else "text"
            choices = [{"label": _plain(o.get("label")) or {"en": "", "hi": "", "gu": ""},
                        "description": text_of(o.get("description"), "en") or None,
                        "next": r(option_port(str(o.get("value"))))}
                       for o in spec.get("options") or [] if isinstance(o, dict)] if k in ("buttons", "list") else []
            step["answer"] = {"kind": k, "check": _check_of(n.get("validate") or {}), "save_as": n.get("store") or None,
                              "choices": choices, "list_button": text_of(spec.get("button_text"), "en") or None,
                              "anything_else": r("default"), "not_valid": r("invalid"),
                              "retries": _int(n.get("max_retries"), 0), "wrong_answer_text": _plain(n.get("invalid_text")),
                              "from_step": spec.get("from") or None, "title_field": spec.get("title_field") or None,
                              "description_field": spec.get("description_field") or None,
                              "nothing_found": r("empty")}
            step["next"] = r("next") if k == "text" else (r("opt:en") if k == "language" else None)
        elif kind == "condition":
            step["branches"] = [{"label": str(b.get("label") or ""), "var": str((b.get("when") or {}).get("var") or ""),
                                 "op": (b.get("when") or {}).get("op") or "eq",
                                 "value": str((b.get("when") or {}).get("value") or ""),
                                 "next": r(branch_port(str(b.get("id"))))}
                                for b in n.get("branches") or [] if isinstance(b, dict)]
            step["else_next"] = r("else")
        elif kind == "remember":
            step["details"] = _details(remember=[{"name": k, "value": str(v)} for k, v in (n.get("assign") or {}).items()],
                                       save_on_contact=bool(n.get("to_contact")))
        elif kind == "tag":
            step["details"] = _details(tags=list(n.get("tags") or []), remove_tags=bool(n.get("remove")))
        elif kind == "assign":
            step["details"] = _details(assign_to=n.get("to") or "operator", teams=list(n.get("teams") or []),
                                       email=n.get("email") or None)
            step["next"], step["on_fail"] = r("next"), r("on_error")
        elif kind == "chat_status":
            step["details"] = _details(status=n.get("status") if n.get("status") in ("open", "pending", "solved") else None)
        elif kind == "template":
            step["details"] = _details(template_name=n.get("template_name") or None,
                                       template_params=[{"name": k, "value": str(v)} for k, v in (n.get("params") or {}).items()])
            step["next"], step["on_fail"] = r("next"), r("on_error")
        elif kind == "wait":
            step["details"] = _details(seconds=_int(n.get("seconds"), 5))
        elif kind == "call_api":
            step["details"] = _details(url=n.get("url") or None, method=n.get("method") or "GET")
            step["next"], step["on_fail"] = r("success"), r("failed")
        elif kind == "go_to":
            step["details"] = _details(workflow=n.get("workflow") or None)
        elif kind == "ai_reply":
            step["details"] = _details(faq=n.get("faq") or None)
            step["next"], step["on_fail"] = r("answered"), r("unsure")
        elif kind == "find_data":
            step["details"] = _details(look_in=n.get("source") or "orders", find_by=n.get("find") or "all",
                                       find_value=n.get("match") or None, one_row_per=n.get("group") or "so",
                                       line_reads=n.get("list_line") or None,
                                       keep=[{"name": k, "value": str(v)} for k, v in (n.get("save") or {}).items()])
            step["next"], step["on_fail"] = r("found"), r("none")
        steps.append(step)
    t = triggers_of(doc)
    return {"title": "", "summary": "", "three_languages": three, "start": str(doc.get("start") or ""),
            "triggers": {"keywords": [k["text"] for k in t["keywords"]],
                         "menu_label": t["menu"]["label"]["en"] if t["menu"]["enabled"] else None,
                         "new_numbers": t["unknown_customer"]},
            "steps": steps}


def apply_patch(bp: dict, patch: dict) -> dict:
    """A blueprint with the model's changes: whole steps replaced or added by id, others removed."""
    out = {**bp, "steps": [dict(s) for s in bp.get("steps") or [] if isinstance(s, dict)]}
    for key in ("title", "start", "summary"):
        if patch.get(key):
            out[key] = patch[key]
    if patch.get("three_languages") is not None:
        out["three_languages"] = bool(patch["three_languages"])
    if isinstance(patch.get("triggers"), dict):
        out["triggers"] = patch["triggers"]
    at = {str(s.get("id")): i for i, s in enumerate(out["steps"])}
    for s in patch.get("upsert") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        if sid in at:
            out["steps"][at[sid]] = s
        else:
            at[sid] = len(out["steps"])
            out["steps"].append(s)
    gone = {str(x) for x in patch.get("remove") or []}
    out["steps"] = [s for s in out["steps"] if str(s.get("id")) not in gone]
    return out


def has_content(doc: dict | None) -> bool:
    """Is there anything to change, or is this the blank canvas a new workflow starts with?"""
    nodes = [n for n in (doc or {}).get("nodes") or [] if isinstance(n, dict)]
    return len(nodes) > 1 or any(text_of(n.get("text"), "en").strip() for n in nodes)


# ---------------- small helpers ----------------
def _details(**given) -> dict:
    base = {"tags": [], "remove_tags": False, "assign_to": None, "teams": [], "email": None, "status": None,
            "template_name": None, "template_params": [], "seconds": None, "url": None, "method": None,
            "workflow": None, "remember": [], "save_on_contact": False, "faq": None,
            "look_in": None, "find_by": None, "find_value": None, "one_row_per": None, "keep": [],
            "line_reads": None}
    return {**base, **given}


def _check_of(rule: dict) -> str:
    kind = rule.get("type") or "any"
    if kind == "lookup":
        return {"so": "so", "po": "po", "fg": "item", "customer_code": "customer_code"}.get(str(rule.get("source")), "any")
    return kind if kind in CHECKS else "any"


def _text(value, three: bool) -> dict | None:
    if isinstance(value, str):
        value = {"en": value}
    if not isinstance(value, dict):
        return None
    en = str(value.get("en") or "").strip()
    if not en:
        return None
    return {"en": en, "hi": str(value.get("hi") or "").strip() if three else "",
            "gu": str(value.get("gu") or "").strip() if three else ""}


def _plain(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {lg: str(value.get(lg) or "") for lg in LANGS}


def _blank() -> dict:
    return {"en": "", "hi": "", "gu": ""}


def _safe_id(raw: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", raw or "").strip("_")[:48]
    return s if s and _NODE_ID.match(s) else ""


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")[:24]


def _var(name) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", str(name or "").strip().lower()).strip("_")[:30]
    if s and not s[0].isalpha():
        s = f"v_{s}"[:30]
    return s if _VAR_NAME.match(s) else ""


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
