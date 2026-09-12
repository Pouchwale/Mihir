"""Meta-approved WhatsApp templates.

These are the only messages that reach a customer who has NOT written in the last 24 hours - order
updates, dispatch notices, reminders. WATI forwards them to Meta, and Meta decides: creating one
gets it reviewed, not approved. The dashboard says so plainly, because a template sitting in PENDING
is not a bug we can fix.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..services.wati import WatiError, WatiRejected, wati
from .admin import require_admin

router = APIRouter(prefix="/admin/api/wa-templates", dependencies=[Depends(require_admin)])

CATEGORIES = ("UTILITY", "MARKETING", "AUTHENTICATION")
# The codes WhatsApp accepts. Not exhaustive - these are the ones this business actually uses, plus
# the regional variants Meta insists on for English.
LANGUAGES = ("en", "en_US", "en_GB", "hi", "gu", "mr", "ta", "te", "bn", "kn", "ml", "pa")
# Meta's own rule for a template name; rejecting it here saves a round trip and a confusing refusal.
NAME_HELP = "Use lowercase letters, numbers and underscores only, e.g. order_ready."


class HeaderIn(BaseModel):
    type: str = "TEXT"           # TEXT | IMAGE | VIDEO | DOCUMENT
    text: str = ""
    link: str = ""               # a public URL, for the media types


class ButtonIn(BaseModel):
    type: str = "quick_reply"    # quick_reply | url | call
    text: str = ""
    url: str = ""
    phone: str = ""


class CreateIn(BaseModel):
    name: str
    language: str = "en"
    body: str
    category: str = "UTILITY"
    header: HeaderIn | None = None
    footer: str = ""
    buttons: list[ButtonIn] = []
    # Meta reads the template WITH these filled in. Missing examples are a common cause of a
    # rejection that looks inexplicable.
    samples: dict[str, str] = {}


@router.get("")
async def list_templates():
    s = get_settings()
    if s.wati_mocked:
        return {"ok": False, "templates": [],
                "detail": "WATI is in simulation mode, so there are no real templates to show. "
                          "Set WATI_TOKEN and a real WATI_BASE_URL to manage them."}
    try:
        raw = await wati.list_templates()
    except (WatiError, WatiRejected) as e:
        return {"ok": False, "templates": [], "detail": str(e)}
    return {"ok": True, "templates": [_summarise(t) for t in raw], "waba_id_set": bool(s.wati_waba_id)}


@router.post("")
async def create_template(body: CreateIn):
    problems = _problems(body)
    if problems:
        return {"ok": False, "detail": problems[0], "problems": problems}
    payload = wati.template_payload(
        body.name.strip(), body.language, body.body, category=body.category,
        header=body.header.model_dump() if body.header else None, footer=body.footer,
        buttons=[b.model_dump() for b in body.buttons], samples=body.samples)
    try:
        result = await wati.create_template(payload)
    except (WatiError, WatiRejected) as e:
        return {"ok": False, "detail": str(e)}
    return {"ok": True, "result": result,
            "detail": "Sent to Meta for review. Approval usually takes minutes but can take a day, "
                      "and the decision is Meta's - this page will show the outcome."}


@router.delete("/{name}")
async def delete_template(name: str, language: str = ""):
    try:
        return {"ok": True, "result": await wati.delete_template(name, get_settings().wati_waba_id, language)}
    except WatiRejected as e:
        raise HTTPException(400, str(e)) from None
    except WatiError as e:
        raise HTTPException(502, f"WATI could not be reached: {e}") from None


@router.post("/preview")
async def preview(body: CreateIn):
    """What the template will look like, and which values it will need each time it is sent."""
    variables = wati.template_variables(body.body)
    filled = body.body
    for v in variables:
        filled = filled.replace("{{" + v + "}}", (body.samples or {}).get(v) or f"[{v}]")
    return {"variables": variables, "problems": _problems(body), "preview": filled,
            "payload": wati.template_payload(
                body.name.strip() or "draft", body.language, body.body, category=body.category,
                header=body.header.model_dump() if body.header else None, footer=body.footer,
                buttons=[b.model_dump() for b in body.buttons], samples=body.samples)}


@router.get("/languages")
async def languages():
    return {"languages": list(LANGUAGES), "categories": list(CATEGORIES)}


def _problems(body: CreateIn) -> list[str]:
    out: list[str] = []
    name = body.name.strip()
    if not name:
        out.append("Give the template a name. " + NAME_HELP)
    elif not all(c.islower() or c.isdigit() or c == "_" for c in name):
        out.append(f"{name!r} is not a usable template name. " + NAME_HELP)
    if not body.body.strip():
        out.append("A template needs some text.")
    if body.category not in CATEGORIES:
        out.append(f"Category must be one of {', '.join(CATEGORIES)}.")
    if len(body.buttons) > 3:
        out.append("WhatsApp allows at most 3 buttons on a template.")
    if "{" in body.body and "{{" not in body.body:
        out.append("Meta templates use {{name}} with double braces, not {name}.")
    if body.language not in LANGUAGES:
        out.append(f"{body.language!r} is not a language code WhatsApp knows, e.g. en, hi, gu, en_US.")

    for b in body.buttons:
        if not b.text.strip():
            out.append("Every button needs a label.")
        elif len(b.text) > 25:
            out.append(f"The button {b.text!r} is too long. Meta allows 25 characters.")
        if b.type == "url" and not b.url.strip():
            out.append(f"The button {b.text!r} is a link button but has no address.")
        if b.type in ("call", "phone") and not b.phone.strip():
            out.append(f"The button {b.text!r} is a call button but has no phone number.")

    if body.header and body.header.type.upper() != "TEXT" and not body.header.link.strip():
        out.append(f"A {body.header.type.lower()} header needs a public link to the file.")

    missing = [v for v in wati.template_variables(body.body) if not (body.samples or {}).get(v, "").strip()]
    if missing:
        # A warning in effect, not a refusal: Meta may still approve it, but it often does not.
        out.append("Give an example value for " + ", ".join(f"{{{{{m}}}}}" for m in missing)
                   + ". Meta reviews the template with the examples filled in, and reviews an "
                     "empty one as the literal text {{" + missing[0] + "}}.")
    return out


def _summarise(t: dict) -> dict:
    """One row of the list, tolerant of the field names differing between WATI's API versions."""
    lang = t.get("language_option") or {}
    return {
        "id": str(t.get("id") or t.get("templateId") or ""),
        "name": t.get("name") or t.get("elementName") or "",
        "status": str(t.get("status") or t.get("templateStatus") or "UNKNOWN").upper(),
        "quality": str(t.get("quality") or "").upper(),
        "category": t.get("category") or "",
        "language": (lang.get("key") if isinstance(lang, dict) else None) or t.get("language") or "",
        "body": t.get("body") or "",
        "footer": t.get("footer") or "",
        "last_modified": t.get("last_modified") or t.get("lastModified") or "",
        "feedback": t.get("feedback") or t.get("rejectedReason") or "",
    }
