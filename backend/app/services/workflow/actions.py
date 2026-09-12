"""The things a workflow does to the world outside the conversation.

The engine never performs these itself. It records what should happen and hands the list back, so
the same graph can be walked as a dry run in the dashboard (nothing happens) or, later, for a real
customer (WATI is called). One code path, two callers, no "test mode" branch buried in the engine.

The one exception is the API request node, which has to run to produce the values a later step reads
back. It is given a fetcher rather than calling out itself, so a test can supply a fake one and the
dry run can be told whether calling a real endpoint is acceptable.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

log = structlog.get_logger(__name__)

API_TIMEOUT_SEC = 12.0
MAX_RESPONSE_BYTES = 256 * 1024
_JSONPATH = re.compile(r"\[(\d+)\]")


@dataclass
class Action:
    """Something the workflow wants done: a tag applied, a chat assigned, a template sent."""

    kind: str  # tags | assign | chat_status | subscribe | template | delay | jump
    detail: dict = field(default_factory=dict)
    node_id: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "node_id": self.node_id,
                "describe": describe(self)}


def describe(a: Action) -> str:
    """One line of plain English, so a dry run can show what would have happened."""
    d = a.detail
    if a.kind == "tags":
        verb = "Remove" if d.get("remove") else "Add"
        return f"{verb} the tag(s) {', '.join(d.get('tags') or []) or '(none set)'}"
    if a.kind == "assign":
        if d.get("to") == "team":
            return f"Assign the chat to the team(s) {', '.join(d.get('teams') or []) or '(none set)'}"
        if d.get("to") == "bot":
            return "Hand the chat back to the bot"
        return f"Assign the chat to {d.get('email') or '(nobody set)'}"
    if a.kind == "chat_status":
        return f"Mark the conversation {d.get('status') or '(not set)'}"
    if a.kind == "subscribe":
        return "Subscribe the contact to campaigns" if d.get("subscribe") else "Unsubscribe the contact"
    if a.kind == "template":
        return f"Send the approved template {d.get('name') or '(none chosen)'}"
    if a.kind == "delay":
        return f"Wait {d.get('seconds')} seconds"
    if a.kind == "attributes":
        pairs = ", ".join(f"{k} = {v}" for k, v in (d.get("attributes") or {}).items())
        return f"Save on the WATI contact: {pairs or '(nothing)'}"
    if a.kind == "jump":
        return f"Continue in the workflow {d.get('workflow') or '(none chosen)'}"
    return a.kind


# ---------------- the API request node ----------------
def check_url(url: str) -> str:
    """Refuse anything that is not a public https/http endpoint.

    The bot runs inside a hosting provider's network, so a URL typed into a workflow could otherwise
    reach the provider's own metadata service or another service on the private network. The owner
    is trusted, but a mistyped or copy-pasted URL should not be able to do that."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "Use a full address starting with https://"
    host = parts.hostname or ""
    if not host:
        return "That address has no host name in it."
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return f"{host} could not be looked up. Check the address."
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return (f"{host} resolves to {ip}, which is inside this server's own network. "
                    "Workflows may only call addresses reachable from the public internet.")
    return ""


async def fetch(method: str, url: str, headers: dict, body: Any) -> dict:
    """Perform one API request. Returns {ok, status, data, error} and never raises."""
    problem = check_url(url)
    if problem:
        return {"ok": False, "status": None, "data": None, "error": problem}
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_SEC, follow_redirects=False) as client:
            r = await client.request(method.upper(), url, headers=headers or None,
                                     json=body if body not in (None, "") and method.upper() != "GET" else None)
    except httpx.TimeoutException:
        return {"ok": False, "status": None, "data": None,
                "error": f"No answer within {int(API_TIMEOUT_SEC)} seconds."}
    except httpx.HTTPError as e:
        return {"ok": False, "status": None, "data": None, "error": str(e)[:200]}

    raw = r.content[:MAX_RESPONSE_BYTES]
    try:
        data = json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        data = {"text": raw.decode("utf-8", "replace")[:2000]}
    return {"ok": 200 <= r.status_code < 300, "status": r.status_code, "data": data,
            "error": "" if 200 <= r.status_code < 300 else f"The service answered {r.status_code}."}


def pick(data: Any, path: str) -> str:
    """Read one value out of a JSON response.

    Supports the same shapes WATI's webhook node documents: a plain key, dot notation for nested
    objects (result.quoteId), and indexes for arrays ($.[0].items[2].status)."""
    if data is None or not path:
        return ""
    cur: Any = data
    cleaned = path.strip().lstrip("$").lstrip(".")
    for part in _JSONPATH.sub(r".\1", cleaned).split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return ""
        elif isinstance(cur, dict):
            if part not in cur:
                return ""
            cur = cur[part]
        else:
            return ""
    if isinstance(cur, (dict, list)):
        return json.dumps(cur, ensure_ascii=False)
    if isinstance(cur, bool):
        return "true" if cur else "false"
    return "" if cur is None else str(cur)
