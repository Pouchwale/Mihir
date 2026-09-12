"""Bring a chatbot built in WATI into this builder - and take a workflow back out as a file.

WATI exports a chatbot as JSON: `flowNodes` (each with a `flowNodeType`) and `flowEdges`, whose
source is "<node>" for a step's single exit or "<node>__<button id>" for one of its buttons, and
"<node>__<node>-default" for what happens when the customer types instead of tapping. This module
maps that onto the builder's own document step for step, keeping every WATI id so the result can be
compared side by side with the original.

Nothing is dropped silently. Anything that had to change, anything that cannot run here yet, and
anything worth knowing - a credential stored in a webhook, say - goes into the import report.
"""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .schema import SCHEMA_VERSION

NATIVE_FORMAT = "order-bot-workflow"
MAX_NODES = 2000

# WATI contact attributes the builder already knows under another name.
_ATTRIBUTE_ALIASES = {
    "name": "sys.customer_name", "contact_name": "sys.customer_name", "customer_name": "sys.customer_name",
    "phone": "sys.phone", "phone_number": "sys.phone", "whatsappnumber": "sys.phone",
    "whatsapp_number": "sys.phone", "waid": "sys.phone", "wa_id": "sys.phone",
}
_VALIDATION = {"none": "any", "": "any", "number": "number", "email": "email", "phone": "phone",
               "phonenumber": "phone", "mobile": "phone", "regex": "regex", "regularexpression": "regex"}
_MEDIA = {"image": "image", "video": "video", "document": "document", "file": "document",
          "audio": "audio", "voice": "audio"}
_CHOICE_TYPES = ("InteractiveButtons", "InteractiveList")
_SECRET_WORDS = ("auth", "token", "key", "secret", "password")


class NotAWorkflow(ValueError):
    """The file is neither a WATI chatbot export nor a workflow exported from this builder."""


@dataclass
class Report:
    source: str  # wati | native
    name: str = ""
    description: str = ""
    found: dict[str, int] = field(default_factory=dict)  # WATI step type -> how many
    steps: int = 0
    connections: int = 0
    adapted: list[str] = field(default_factory=list)  # changed to fit; still works
    warnings: list[str] = field(default_factory=list)  # needs the owner's attention

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------- WATI's rich text ----------------
_ANY_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")
_BREAK = re.compile(r"<\s*(?:br|/p|/div|/h[1-6])\s*/?\s*>", re.I)


def html_to_whatsapp(raw: Any) -> str:
    """WATI's editor stores HTML; WhatsApp writes *bold*, _italic_, ~strike~ and plain line breaks."""
    if raw is None:
        return ""
    text = str(raw)
    if not _ANY_TAG.search(text):
        return _tidy(html.unescape(text).replace("\xa0", " "))  # already plain text: keep its line breaks
    text = text.replace("\r", "").replace("\n", " ")  # in HTML a raw newline is only whitespace
    text = re.sub(r"<\s*li[^>]*>", "\n• ", text, flags=re.I)
    text = _BREAK.sub("\n", text)
    text = re.sub(r"<\s*a\b[^>]*?href\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)<\s*/\s*a\s*>", _link, text,
                  flags=re.I | re.S)
    for tags, mark in ((("strong", "b"), "*"), (("em", "i"), "_"), (("s", "del", "strike"), "~"),
                       (("code",), "```")):
        pattern = r"<\s*(%s)\b[^>]*>(.*?)<\s*/\s*\1\s*>" % "|".join(tags)
        text = re.sub(pattern, lambda m, mark=mark: _wrap(m.group(2), mark), text, flags=re.I | re.S)
    text = _ANY_TAG.sub("", text)  # anything else: drop the tag, keep the words
    return _tidy(html.unescape(text).replace("\xa0", " "))


def _visible(fragment: str) -> str:
    return html.unescape(_ANY_TAG.sub("", fragment)).replace("\xa0", " ").strip()


def _wrap(inner: str, mark: str) -> str:
    """Formatting markers hug the words, one line at a time: WhatsApp's *bold* cannot span a line
    break, and "* word *" with inner spaces is not bold at all."""
    out = []
    for line in inner.split("\n"):
        if not _visible(line):
            out.append(line)
            continue
        lead = line[: len(line) - len(line.lstrip())]
        trail = line[len(line.rstrip()):]
        out.append(f"{lead}{mark}{line.strip()}{mark}{trail}")
    return "\n".join(out)


def _link(m: re.Match) -> str:
    url = html.unescape(m.group(1)).strip()
    text = _visible(m.group(2))
    if not url:
        return text
    if not text or text.rstrip("/") == url.rstrip("/"):
        return url
    return f"{text} ({url})"  # WhatsApp makes the address tappable by itself


def _tidy(text: str) -> str:
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _title(text: str, fallback: str) -> str:
    """A short name for the canvas card: the first line the customer would read, without WhatsApp's
    formatting marks. Placeholders are left whole - stripping the "_" from {sys.customer_name} is
    exactly the kind of quiet damage that turns into a confusing warning later."""
    for line in (text or "").split("\n"):
        parts = re.split(r"(\{[^{}]*\})", line)
        clean = "".join(p if p.startswith("{") else re.sub(r"[*_~`]", "", p) for p in parts).strip()
        if clean:
            return clean if len(clean) <= 40 else clean[:39].rstrip() + "…"
    return fallback


# ---------------- WATI -> builder ----------------
class _FromWati:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.report = Report(source="wati", name=str(data.get("name") or "").strip())
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.entry: dict[str, str] = {}  # WATI id -> builder step where incoming connections land
        self.exit: dict[str, str] = {}  # WATI id -> builder step its own exits leave from
        self.kind: dict[str, str] = {}  # WATI id -> flowNodeType
        self.items: dict[str, dict[str, str]] = {}  # choice step -> {button/row id: nodeResultId}
        self.defaults: dict[str, str] = {}  # choice step -> default nodeResultId
        self.renamed: dict[str, str] = {}
        self.no_catalogue = 0
        self.unsupported: dict[str, int] = {}
        self.dropped = 0
        self._edge_n = 0

    def run(self) -> tuple[dict, Report]:
        raw = [n for n in self.data.get("flowNodes") or [] if isinstance(n, dict) and n.get("id")]
        if not raw:
            raise NotAWorkflow("This WATI export has no steps in it.")
        if len(raw) > MAX_NODES:
            raise NotAWorkflow(f"This export has {len(raw)} steps; the builder takes at most {MAX_NODES}.")
        for n in raw:
            t = str(n.get("flowNodeType") or "?")
            self.report.found[t] = self.report.found.get(t, 0) + 1
            self._node(n)
        self._connect()

        start = next((str(n["id"]) for n in raw if n.get("isStartNode")), None)
        if start is None:
            start = str(raw[0]["id"])
            self.report.adapted.append("No step was marked as the start in WATI, so the first one is.")
        self._summarise()
        doc = {"schema": SCHEMA_VERSION, "start": self.entry.get(start, start),
               # WATI writes one text per step and branches by language; the editor matches that.
               "settings": {"multilingual": False, "imported_from": "wati"},
               "nodes": self.nodes, "edges": self.edges}
        self.report.steps, self.report.connections = len(self.nodes), len(self.edges)
        return doc, self.report

    # ----- steps -----
    def _node(self, n: dict) -> None:
        wid, t = str(n["id"]), str(n.get("flowNodeType") or "")
        self.kind[wid] = t
        pos = self._pos(n)
        if t == "Message":
            self._message(wid, n, pos)
        elif t == "Question":
            self._question(wid, n, pos)
        elif t == "InteractiveButtons":
            self._buttons(wid, n, pos)
        elif t == "InteractiveList":
            self._list(wid, n, pos)
        elif t == "Webhook":
            self._webhook(wid, n, pos)
        elif t == "InteractiveProductList":
            self._product_list(wid, n, pos)
        else:
            self._unsupported(wid, n, pos, t)

    def _message(self, wid: str, n: dict, pos: dict) -> None:
        parts = self._parts(n) or [{"kind": "text", "text": ""}]
        ids = [self._part_node(wid if i == 0 else f"{wid}-{i + 1}", p, pos, i) for i, p in enumerate(parts)]
        self._chain(ids)
        if len(ids) > 1:
            self.report.adapted.append(f'"{self._name(ids[0])}" sent {len(ids)} messages in one WATI step; '
                                       "each became its own step, joined in the same order.")
        self.entry[wid], self.exit[wid] = ids[0], ids[-1]

    def _question(self, wid: str, n: dict, pos: dict) -> None:
        parts = self._parts(n)
        media = [p for p in parts if p["kind"] == "media"]
        # pictures sent with the question go out first, as their own steps
        ids = [self._part_node(wid if i == 0 else f"{wid}-m{i + 1}", p, pos, i) for i, p in enumerate(media)]
        qid = f"{wid}-q" if ids else wid
        text = "\n\n".join(p["text"] for p in parts if p["kind"] == "text" and p["text"])
        node: dict = {"id": qid, "type": "question", "title": _title(text, "Question"),
                      "x": pos["x"], "y": pos["y"] + len(ids) * 190,
                      "text": {"en": text}, "input": {"kind": "text"}}
        store = self._safe(n.get("userInputVariable"))
        if store:
            node["store"] = store
        rule = n.get("answerValidation") or {}
        raw_type = str(rule.get("type") or "None")
        vtype = _VALIDATION.get(raw_type.replace(" ", "").lower())
        if vtype is None:
            self.report.adapted.append(f'"{node["title"]}" checked answers as "{raw_type}" in WATI. That check '
                                       "does not exist here yet, so any answer is accepted.")
            vtype = "any"
        if vtype != "any":
            check: dict = {"type": vtype}
            if vtype == "number":
                check["min"], check["max"] = str(rule.get("minValue") or ""), str(rule.get("maxValue") or "")
            if vtype == "regex":
                check["pattern"] = str(rule.get("regex") or "")
            node["validate"] = check
            try:
                node["max_retries"] = max(1, min(int(rule.get("failsCount") or 3), 5))
            except (TypeError, ValueError):
                node["max_retries"] = 3
            node["invalid_text"] = {"en": self._text(rule.get("fallback"))
                                    or "Sorry, I didn't understand. Please try again."}
        if n.get("isMediaAccepted"):
            self.report.adapted.append(f'"{node["title"]}" accepted a photo or file in WATI; here only a typed '
                                       "answer is read.")
        self.nodes.append(node)
        ids.append(qid)
        self._chain(ids)
        self.entry[wid], self.exit[wid] = ids[0], qid

    def _buttons(self, wid: str, n: dict, pos: dict) -> None:
        body = self._text(n.get("interactiveButtonsBody"))
        node: dict = {"id": wid, "type": "question", "title": _title(body, "Buttons"), "x": pos["x"],
                      "y": pos["y"], "text": {"en": body}, "input": {"kind": "buttons", "options": []}}
        self._header(node, n.get("interactiveButtonsHeader"), allow_media=True)
        self._footer(node, n.get("interactiveButtonsFooter"))
        items = [i for i in n.get("interactiveButtonsItems") or [] if isinstance(i, dict) and i.get("id")]
        for it in items:
            node["input"]["options"].append({"value": str(it["id"]),
                                             "label": {"en": str(it.get("buttonText") or "").strip()}})
        self._choice(wid, n, node, {str(i["id"]): str(i.get("nodeResultId") or "") for i in items},
                     n.get("interactiveButtonsUserInputVariable"), n.get("interactiveButtonsDefaultNodeResultId"))

    def _list(self, wid: str, n: dict, pos: dict) -> None:
        body = self._text(n.get("interactiveListBody"))
        button = str(n.get("interactiveListButtonText") or "").strip()
        node: dict = {"id": wid, "type": "question", "title": _title(body, "List"), "x": pos["x"],
                      "y": pos["y"], "text": {"en": body},
                      "input": {"kind": "list", "options": [], "button_text": {"en": button or "Menu"}}}
        if not button:
            self.report.adapted.append(f'"{node["title"]}" had no list button text; it now says "Menu".')
        self._header(node, n.get("interactiveListHeader"), allow_media=False)
        self._footer(node, n.get("interactiveListFooter"))
        sections = [s for s in n.get("interactiveListSections") or [] if isinstance(s, dict)]
        several = len(sections) > 1
        if len(sections) == 1 and str(sections[0].get("title") or "").strip():
            node["input"]["section_title"] = {"en": str(sections[0]["title"]).strip()}
        targets: dict[str, str] = {}
        for sec in sections:
            heading = str(sec.get("title") or "").strip()
            for row in sec.get("rows") or []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                opt: dict = {"value": str(row["id"]), "label": {"en": str(row.get("title") or "").strip()}}
                if str(row.get("description") or "").strip():
                    opt["description"] = {"en": str(row["description"]).strip()}
                if several and heading:
                    opt["section"] = {"en": heading}
                node["input"]["options"].append(opt)
                targets[str(row["id"])] = str(row.get("nodeResultId") or "")
        self._choice(wid, n, node, targets, n.get("interactiveListUserInputVariable"),
                     n.get("interactiveListDefaultNodeResultId"))

    def _choice(self, wid: str, n: dict, node: dict, targets: dict[str, str], variable: Any, default: Any) -> None:
        store = self._safe(variable)
        if store:
            node["store"] = store
        self.items[wid] = targets
        if default:
            self.defaults[wid] = str(default)
        self.nodes.append(node)
        self.entry[wid] = self.exit[wid] = wid

    def _webhook(self, wid: str, n: dict, pos: dict) -> None:
        method = str(n.get("methodType") or "Get").upper()
        url = self._vars(str(n.get("url") or "").strip())
        host = urlsplit(url).hostname or ""
        title = f"Call {host}" if host else "Call an API"
        if method not in ("GET", "POST"):
            self.report.adapted.append(f'"{title}" used {method}; the builder calls APIs with GET or POST, so it '
                                       "now uses POST.")
            method = "POST"
        headers: dict[str, str] = {}
        for h in n.get("headers") or []:
            if not isinstance(h, dict) or not str(h.get("headerName") or "").strip():
                continue
            hname = str(h["headerName"]).strip()
            headers[hname] = self._vars(str(h.get("headerValue") or ""))
            if any(w in hname.lower() for w in _SECRET_WORDS):
                self.report.warnings.append(
                    f'"{title}" sends a {hname} header. That credential is now stored in this workflow in plain '
                    "text, so anyone who can open this dashboard can read it. If the WATI export file itself "
                    "was shared, rotate the credential with the service that issued it.")
        save: dict[str, str] = {}
        for rv in n.get("responseVariables") or []:
            if isinstance(rv, dict):
                var = rv.get("variable") or rv.get("variableName") or rv.get("name") or rv.get("key")
                path = rv.get("path") or rv.get("jsonPath") or rv.get("responseKey") or rv.get("field")
                if var and path:
                    save[self._safe(var)] = str(path)
        if n.get("expectedStatuses"):
            self.report.adapted.append(f'"{title}" routed by HTTP status in WATI; here it has two exits, '
                                       "Worked and Failed, plus any response rules you add.")
        self.nodes.append({"id": wid, "type": "api_request", "title": title, "x": pos["x"], "y": pos["y"],
                           "method": method, "url": url, "headers": headers,
                           "body": self._vars(str(n.get("body") or "")), "save": save, "routes": []})
        self.entry[wid] = self.exit[wid] = wid

    def _product_list(self, wid: str, n: dict, pos: dict) -> None:
        header = str(n.get("interactiveProductListHeaderText") or "").strip()
        body = self._text(n.get("interactiveProductListBodyText"))
        node = {"id": wid, "type": "product_list", "title": _title(header or body, "Product list"),
                "x": pos["x"], "y": pos["y"], "text": {"en": body}, "header": {"en": header},
                "catalog_id": str(n.get("catalogId") or ""), "set_id": str(n.get("setId") or "")}
        if not node["catalog_id"] or not node["set_id"]:
            self.no_catalogue += 1
        self.nodes.append(node)
        self.entry[wid] = self.exit[wid] = wid

    def _unsupported(self, wid: str, n: dict, pos: dict, t: str) -> None:
        """Kept, not dropped: the raw WATI settings ride along so nothing is lost, and the validator
        refuses to publish until the step is replaced."""
        self.unsupported[t] = self.unsupported.get(t, 0) + 1
        self.nodes.append({"id": wid, "type": "message", "title": f"WATI step: {t}", "x": pos["x"],
                           "y": pos["y"], "text": {"en": f"[{t} step from WATI - not supported here yet]"},
                           "unsupported": t, "wati_raw": n})
        self.entry[wid] = self.exit[wid] = wid

    # ----- connections -----
    def _connect(self) -> None:
        taken = {(e["from"], e["port"]) for e in self.edges}  # the joins made for multi-part steps

        def add(src: str, port: str | None, dst: str) -> None:
            if port is None or src not in self.exit or dst not in self.entry:
                self.dropped += 1
                return
            key = (self.exit[src], port)
            if key in taken:
                return
            taken.add(key)
            self.edges.append({"id": self._eid(), "from": self.exit[src], "port": port, "to": self.entry[dst]})

        for e in self.data.get("flowEdges") or []:
            if not isinstance(e, dict):
                continue
            base, _, handle = str(e.get("sourceNodeId") or "").partition("__")
            add(base, self._port(base, handle), str(e.get("targetNodeId") or ""))
        # WATI also writes each button's target onto the button itself; honour it where no edge exists
        for wid, targets in self.items.items():
            for item, dst in targets.items():
                if dst:
                    add(wid, f"opt:{item}", dst)
            if self.defaults.get(wid):
                add(wid, "default", self.defaults[wid])

    def _port(self, base: str, handle: str) -> str | None:
        t = self.kind.get(base)
        if t is None:
            return None
        if not handle:
            if t in _CHOICE_TYPES:
                return None  # a menu's exits are its buttons; a bare edge from one has no meaning
            return "success" if t == "Webhook" else "next"
        if handle.endswith("-default"):
            return "default" if t in _CHOICE_TYPES else None
        if t in _CHOICE_TYPES:
            return f"opt:{handle}" if handle in self.items.get(base, {}) else None
        if t == "Webhook":
            return "failed" if any(w in handle.lower() for w in ("fail", "error", "else")) else "success"
        return "next"

    # ----- helpers -----
    def _part_node(self, nid: str, part: dict, pos: dict, i: int) -> str:
        node: dict = {"id": nid, "type": "message", "x": pos["x"], "y": pos["y"] + i * 190}
        if part["kind"] == "text":
            node["text"], node["title"] = {"en": part["text"]}, _title(part["text"], "Message")
        else:
            node["text"] = {"en": ""}
            node["media"] = {"type": part["type"], "url": part["url"], "caption": {"en": part["caption"]}}
            node["title"] = _title(part["caption"], part["type"].title())
        self.nodes.append(node)
        return nid

    def _parts(self, n: dict) -> list[dict]:
        out = []
        for r in n.get("flowReplies") or []:
            if not isinstance(r, dict):
                continue
            rtype = str(r.get("flowReplyType") or "Text")
            key = rtype.lower()
            if key == "text":
                out.append({"kind": "text", "text": self._text(r.get("data"))})
            elif key in _MEDIA:
                out.append({"kind": "media", "type": _MEDIA[key], "url": str(r.get("data") or "").strip(),
                            "caption": self._text(r.get("caption"))})
            else:
                self.report.adapted.append(f'A "{rtype}" reply is not supported here; its text was kept as a '
                                           "plain message.")
                out.append({"kind": "text", "text": self._text(r.get("data") or r.get("caption"))})
        return out

    def _header(self, node: dict, head: Any, *, allow_media: bool) -> None:
        if not isinstance(head, dict):
            return
        htype = str(head.get("type") or "Text").lower()
        if htype == "text":
            text = self._text(head.get("text"))
            if text:
                node["header"] = {"en": text}
            return
        media = head.get("media")
        url = str((media.get("url") or media.get("link") if isinstance(media, dict) else media)
                  or head.get("link") or "").strip()
        if htype in _MEDIA and url and allow_media:
            node["header_media"] = {"type": _MEDIA[htype], "url": url}
        elif url:
            self.report.adapted.append(f'"{node["title"]}" had a {htype} header, which WhatsApp lists cannot show; '
                                       "it was left off.")

    def _footer(self, node: dict, raw: Any) -> None:
        text = self._text(raw)
        if text:
            node["footer"] = {"en": text}

    def _chain(self, ids: list[str]) -> None:
        for a, b in zip(ids, ids[1:]):
            self.edges.append({"id": self._eid(), "from": a, "port": "next", "to": b})

    def _text(self, raw: Any) -> str:
        return self._vars(html_to_whatsapp(raw))

    def _vars(self, text: str) -> str:
        """WATI's {{name}} becomes the builder's {name}; WATI contact attributes map to built-ins."""
        def sub(m: re.Match) -> str:
            raw = m.group(1).strip()
            alias = _ATTRIBUTE_ALIASES.get(raw.lower().replace(" ", "_"))
            return "{" + (alias or self._safe(raw)) + "}"

        return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", sub, text or "")

    def _safe(self, raw: Any) -> str:
        """A variable name the builder accepts: lowercase letters, numbers and underscores."""
        name = str(raw or "").strip()
        if not name:
            return ""
        safe = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]+", "_", name.lower())).strip("_")
        if not safe or not safe[0].isalpha():
            safe = f"v_{safe}".rstrip("_")
        safe = safe[:30]
        if safe != name:
            self.renamed[name] = safe
        return safe

    def _pos(self, n: dict) -> dict:
        p = n.get("flowNodePosition") or {}
        try:
            return {"x": round(float(p.get("posX") or 0)), "y": round(float(p.get("posY") or 0))}
        except (TypeError, ValueError):
            return {"x": 0, "y": 0}

    def _name(self, nid: str) -> str:
        return next((n.get("title") or nid for n in self.nodes if n["id"] == nid), nid)

    def _eid(self) -> str:
        self._edge_n += 1
        return f"w{self._edge_n}"

    def _summarise(self) -> None:
        r = self.report
        r.adapted.insert(0, "WATI writes one text per step and makes a separate path for each language, so "
                            "this workflow is set to one text per step. You can switch it to three languages "
                            "per step in the editor at any time.")
        if self.renamed:
            r.adapted.append("Variable names were adjusted to letters, numbers and underscores: "
                             + ", ".join(f"{a} → {b}" for a, b in sorted(self.renamed.items())) + ".")
        if self.no_catalogue:
            r.warnings.append(f"{self.no_catalogue} product-list steps have no catalogue product set chosen - "
                              "they had none in WATI either. WhatsApp can only show them once a catalogue is "
                              "connected to your WhatsApp Business account in WATI.")
        for t, count in sorted(self.unsupported.items()):
            r.warnings.append(f'{count} "{t}" step{"s" if count > 1 else ""} could not be converted yet. '
                              "They were kept with their WATI settings so nothing is lost, but the workflow "
                              "cannot be published until they are replaced.")
        if self.dropped:
            r.warnings.append(f"{self.dropped} connection{'s' if self.dropped > 1 else ''} pointed at a step or "
                              "button that is not in the file, and "
                              f"{'were' if self.dropped > 1 else 'was'} left out.")


# ---------------- our own format ----------------
def export_native(title: str, description: str, doc: dict) -> dict:
    return {"format": NATIVE_FORMAT, "version": 1, "title": title, "description": description or "",
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "doc": doc}


def _from_native(data: dict) -> tuple[dict, Report]:
    doc = data["doc"] if isinstance(data.get("doc"), dict) else data
    if not isinstance(doc.get("nodes"), list):
        raise NotAWorkflow("This file has no steps in it.")
    if len(doc["nodes"]) > MAX_NODES:
        raise NotAWorkflow(f"This file has {len(doc['nodes'])} steps; the builder takes at most {MAX_NODES}.")
    report = Report(source="native", name=str(data.get("title") or "").strip(),
                    description=str(data.get("description") or ""))
    clean = {"schema": doc.get("schema", SCHEMA_VERSION), "start": str(doc.get("start") or ""),
             "settings": dict(doc.get("settings") or {}),
             "nodes": [n for n in doc["nodes"] if isinstance(n, dict)],
             "edges": [e for e in doc.get("edges") or [] if isinstance(e, dict)]}
    report.steps, report.connections = len(clean["nodes"]), len(clean["edges"])
    return clean, report


def import_any(data: Any) -> tuple[dict, Report]:
    """Whatever the owner uploads: a WATI chatbot export, or a file exported from this builder."""
    if not isinstance(data, dict):
        raise NotAWorkflow("That file is not a workflow: it should contain a JSON object.")
    if isinstance(data.get("flowNodes"), list):
        return _FromWati(data).run()
    if data.get("format") == NATIVE_FORMAT or isinstance(data.get("nodes"), list) \
            or isinstance((data.get("doc") or {}).get("nodes"), list):
        return _from_native(data)
    raise NotAWorkflow('This is not a WATI chatbot export (which contains "flowNodes") or a workflow '
                       "exported from this builder.")
