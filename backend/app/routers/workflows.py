"""The visual workflow builder's API.

Everything here is behind the admin key. A draft is only ever walked by the dry-run simulator; a
PUBLISHED and switched-on workflow also answers real customers, through workflow/runtime.py, next to
the order-status bot.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sqlalchemy import select

from ..config import DEFAULT_COLUMN_MAP, get_settings
from ..db import get_session_factory
from ..models import OrderCache
from ..services import translate as mt
from ..services.ai import builder as ai_builder
from ..services.ai import live as ai_live
from ..services.ai.groq_client import NO_KEY, AiUnavailable, configured as ai_configured
from ..services.verify import find_customer
from ..services.workflow import engine, presets, runtime, schema, store, wati_import
from ..services.workflow import lookup as data_lookup
from ..services.workflow.validate import validate_graph
from .admin import require_admin
from fastapi import Depends

router = APIRouter(prefix="/admin/api/workflows", dependencies=[Depends(require_admin)])


class CreateIn(BaseModel):
    title: str
    description: str = ""
    from_example: str = ""


class RenameIn(BaseModel):
    title: str = ""
    description: str | None = None


class DraftIn(BaseModel):
    doc: dict
    base_version: int | None = None


class PublishIn(BaseModel):
    version: int | None = None
    notes: str = ""


class ValidateIn(BaseModel):
    doc: dict
    key: str = ""  # given, the checks also cover how it sits among the live workflows


class LiveIn(BaseModel):
    live: bool


class TranslateIn(BaseModel):
    texts: list[str]
    targets: list[str] = ["hi", "gu"]


class ReorderIn(BaseModel):
    keys: list[str]


class SimulateIn(BaseModel):
    """One turn of a test conversation. `state` is what the previous turn returned, so the client
    holds the conversation and the server stays stateless - a test chat can never touch a real one."""

    text: str = ""
    state: dict | None = None
    language: str = "en"
    use: str = "draft"  # draft | published
    contact_name: str = "Test customer"  # what {sys.customer_name} shows in a test chat
    phone: str = ""  # test as this customer: their name, and their orders for data checks
    media: dict | None = None  # a picture, document or location sent instead of text


class PresetIn(BaseModel):
    label: str
    spec: dict


class AiBuildIn(BaseModel):
    instruction: str
    doc: dict | None = None  # the workflow as it stands in the editor, unsaved changes included
    history: list[str] = []  # what the owner asked earlier in this conversation with the assistant


class AiReviewIn(BaseModel):
    doc: dict


_INSTRUCTION_MAX = 6000


def _instruction(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "Describe what the workflow should do.")
    if len(text) > _INSTRUCTION_MAX:
        raise HTTPException(400, f"That description is {len(text)} characters - keep it under {_INSTRUCTION_MAX}, "
                                 "or build it in parts.")
    if not ai_configured():
        raise HTTPException(503, NO_KEY)
    return text


@router.get("")
async def list_all():
    return await store.list_workflows()


@router.get("/examples")
async def list_examples():
    return [{"key": e["key"], "title": e["title"], "description": e["description"]} for e in store.examples()]


@router.post("/validate")
async def validate_doc(body: ValidateIn):
    issues = validate_graph(body.doc) + (await runtime.check_routing(body.doc, body.key) if body.key else [])
    issues.sort(key=lambda i: 0 if i.level == "fail" else 1)
    return {"ok": not any(i.level == "fail" for i in issues),
            "issues": [i.to_dict() for i in issues]}


@router.post("/translate")
async def translate_texts(body: TranslateIn):
    """English into Hindi and Gujarati with a free translator, for the editor to fill in. The owner
    checks the result - a machine can misread a two-word button."""
    if len(body.texts) > 300:
        raise HTTPException(400, "Too many texts at once - translate a few steps at a time.")
    texts = [t[:2000] for t in body.texts]
    try:
        return {"translations": {lg: await mt.translate(texts, lg) for lg in body.targets if lg in mt.TARGETS}}
    except mt.TranslateUnavailable as e:
        raise HTTPException(503, str(e)) from None


class ImportIn(BaseModel):
    data: dict
    title: str = ""


@router.post("/import")
async def import_workflow(body: ImportIn):
    """Bring in a chatbot exported from WATI, or a workflow exported from this builder.

    It always lands as a new draft - nothing live is replaced - with a report of everything that
    had to change and everything worth checking."""
    try:
        doc, report = wati_import.import_any(body.data)
    except wati_import.NotAWorkflow as e:
        raise HTTPException(400, str(e)) from None
    title = body.title.strip() or report.name or "Imported workflow"
    created = await store.create_from_doc(title, report.description, doc)
    return {**created, "report": report.to_dict()}


@router.post("")
async def create(body: CreateIn):
    if not body.title.strip():
        raise HTTPException(400, "Give the workflow a name.")
    return await store.create(body.title, body.description, body.from_example)


@router.post("/reorder")
async def reorder(body: ReorderIn):
    return await store.reorder(body.keys)


@router.get("/question-presets")
async def list_question_presets():
    """Questions the owner saved to reuse, shown under "What are you asking?"."""
    return await presets.list_presets()


@router.post("/question-presets")
async def save_question_preset(body: PresetIn):
    try:
        return await presets.save(body.label, body.spec)
    except presets.PresetProblem as e:
        raise HTTPException(400, str(e)) from None


@router.delete("/question-presets/{preset_id}")
async def delete_question_preset(preset_id: int):
    if not await presets.remove(preset_id):
        raise HTTPException(404, "no such saved question")
    return {"ok": True}


@router.get("/data-fields")
async def data_fields():
    """What a Find-in-your-data step can look at, under the owner's own column names.

    The field NAMES are the bot's own and never change; only the labels follow the column map, so a
    workflow cannot break when the owner renames a column in their spreadsheet. Connection Status is
    not here at all - it is internal, and this list is what the editor offers."""
    s = get_settings()
    columns = {**DEFAULT_COLUMN_MAP, **(s.orders_column_map or {})}
    statuses: list[str] = []
    try:
        async with get_session_factory()() as db:
            got = await db.execute(select(OrderCache.real_status).distinct().limit(20))
            statuses = sorted({str(v) for v in got.scalars().all() if v})
    except Exception:  # noqa: BLE001 - the editor still works without the suggestions
        statuses = []

    def field(name: str, label: str, values: list[str] | None = None) -> dict:
        return {"name": name, "label": label, "values": values or []}

    return {
        "sources": [
            {"value": "orders", "label": "Their orders",
             "finds": [{"value": "all", "label": "All of this customer's orders"},
                       {"value": "so", "label": "The order with an SO number"},
                       {"value": "po", "label": "The order with a PO number"},
                       {"value": "fg", "label": "The orders with an item code"}],
             "fields": [field("so_no", columns.get("so_no", "SO No")),
                        field("po_no", columns.get("po_no", "PO No")),
                        field("fg_item_code", columns.get("fg_item_code", "FG Item Code")),
                        field("real_status", columns.get("real_status", "Real Status"), statuses),
                        field("customer_name", columns.get("customer_name", "Customer Name"))]},
            {"value": "customer", "label": "Their record in your customer list",
             "finds": [{"value": "all", "label": "Their own record"}],
             "fields": [field("customer_code", s.customers_col_code or "Customer Code"),
                        field("customer_name", s.customers_col_name or "Customer Name")]},
        ],
        "ops": [{"value": "eq", "label": "is"}, {"value": "ne", "label": "is not"},
                {"value": "contains", "label": "contains"}, {"value": "is_set", "label": "is filled in"},
                {"value": "is_empty", "label": "is blank"}],
        "sorts": [{"value": "newest", "label": "Newest first"}, {"value": "oldest", "label": "Oldest first"},
                  {"value": "as_is", "label": "As they come"}],
        "groups": [{"value": "so", "label": "One row per order"}, {"value": "line", "label": "One row per order line"}],
    }


@router.get("/routing")
async def routing():
    """How an incoming message finds its way: what each live workflow starts on, what the
    order-status main menu shows, and anything that clashes."""
    return await runtime.overview()


@router.get("/runs")
async def runs():
    """Customers inside a workflow right now."""
    return await runtime.active_runs()


@router.delete("/runs/{phone}")
async def end_run(phone: str):
    """Let a customer out of the workflow they are in; their next message is routed afresh."""
    return {"ok": await runtime.end_run(phone)}


# ---------------- the AI assistant (Groq) ----------------
@router.get("/ai/status")
async def ai_status():
    """Whether the assistant can be used - never the key itself."""
    return ai_builder.status()


@router.post("/ai/create")
async def ai_create(body: AiBuildIn):
    """A new workflow from a description. Runs in the background; poll /ai/jobs/{id}. The result is
    saved as a draft - nothing is published."""
    instruction = _instruction(body.instruction)
    return ai_builder.start_job(lambda progress: ai_builder.create_workflow(instruction, progress)).view()


@router.get("/ai/jobs/{job_id}")
async def ai_job(job_id: str):
    got = ai_builder.job_view(job_id)
    if got is None:
        raise HTTPException(404, "That AI job is no longer known - the server may have restarted. Try again.")
    return got


@router.post("/{key}/ai/build")
async def ai_build(key: str, body: AiBuildIn):
    """Build or change this workflow from a description. The result is a preview for the editor to
    show; nothing is saved until the owner applies it."""
    instruction = _instruction(body.instruction)
    history = [h[:1000] for h in body.history[-6:]]
    return ai_builder.start_job(lambda progress: ai_builder.build(
        instruction, base=body.doc, key=key, history=history, progress=progress)).view()


@router.post("/{key}/ai/review")
async def ai_review(key: str, body: AiReviewIn):
    """What a customer would stumble on, beyond what the automatic checks find."""
    if not ai_configured():
        raise HTTPException(503, NO_KEY)
    try:
        return await ai_builder.review(body.doc)
    except AiUnavailable as e:
        raise HTTPException(503, str(e)) from None


@router.get("/{key}")
async def get_one(key: str):
    got = await store.get_workflow(key)
    if got is None:
        raise HTTPException(404, f"no workflow called {key!r}")
    return got


@router.patch("/{key}")
async def rename(key: str, body: RenameIn):
    if not body.title.strip() and body.description is None:
        raise HTTPException(400, "Give the workflow a name.")
    try:
        return await store.rename(key, body.title, body.description)
    except KeyError:
        raise HTTPException(404, f"no workflow called {key!r}") from None


@router.delete("/{key}")
async def delete(key: str):
    try:
        return await store.remove(key)
    except KeyError:
        raise HTTPException(404, f"no workflow called {key!r}") from None


@router.put("/{key}/draft")
async def save_draft(key: str, body: DraftIn):
    try:
        return await store.save_draft(key, body.doc, body.base_version)
    except store.Conflict as e:
        # 409 so the editor can offer to reload rather than silently overwrite someone's work.
        raise HTTPException(409, str(e)) from None
    except KeyError:
        raise HTTPException(404, f"no workflow called {key!r}") from None


@router.post("/{key}/publish")
async def publish(key: str, body: PublishIn):
    try:
        return await store.publish(key, body.version, body.notes)
    except store.NotPublishable as e:
        # 422 with the full list: the editor renders these, not the bare detail string.
        raise HTTPException(422, {"detail": "This workflow is not ready to publish.", "issues": e.issues}) from None
    except KeyError as e:
        raise HTTPException(404, f"not found: {e}") from None


@router.post("/{key}/unpublish")
async def unpublish(key: str):
    try:
        return await store.unpublish(key)
    except KeyError:
        raise HTTPException(404, f"no workflow called {key!r}") from None


@router.post("/{key}/live")
async def set_live(key: str, body: LiveIn):
    """Switch a published workflow on or off for customers, without publishing anything new."""
    try:
        return await store.set_live(key, body.live)
    except store.NotPublishable as e:
        raise HTTPException(422, {"detail": "Publish this workflow before switching it on.", "issues": e.issues}) from None
    except KeyError:
        raise HTTPException(404, f"no workflow called {key!r}") from None


@router.get("/{key}/versions")
async def versions(key: str):
    return await store.versions(key)


@router.get("/{key}/versions/{version}")
async def version_doc(key: str, version: int):
    doc = await store.version_doc(key, version)
    if doc is None:
        raise HTTPException(404, "no such version")
    return {"version": version, "doc": doc}


@router.get("/{key}/export")
async def export_workflow(key: str, use: str = "draft"):
    """The workflow as a file: a backup, or a way to move it to another installation."""
    got = await store.get_workflow(key)
    if got is None:
        raise HTTPException(404, f"no workflow called {key!r}")
    doc = got["published_doc"] if use == "published" else got["doc"]
    if doc is None:
        raise HTTPException(400, "This workflow has not been published yet.")
    return wati_import.export_native(got["title"], got["description"], doc)


@router.post("/{key}/duplicate")
async def duplicate(key: str):
    got = await store.get_workflow(key)
    if got is None:
        raise HTTPException(404, f"no workflow called {key!r}")
    return await store.create_from_doc(f"Copy of {got['title']}"[:120], got["description"], got["doc"])


@router.post("/{key}/simulate")
async def simulate(key: str, body: SimulateIn):
    """Walk the workflow as a customer would, without sending anything.

    No WATI call, no session row, no queue: this is a dry run of the graph, so an owner can test a
    draft that real customers cannot yet reach."""
    got = await store.get_workflow(key)
    if got is None:
        raise HTTPException(404, f"no workflow called {key!r}")
    doc = got["published_doc"] if body.use == "published" else got["doc"]
    if doc is None:
        raise HTTPException(400, "This workflow has not been published yet, so there is nothing live to test.")

    graph = schema.parse(doc)
    phone = "".join(c for c in body.phone if c.isdigit())
    # Read-only and never committed: a test can check real orders, but leaves no trace behind.
    async with get_session_factory()() as db:
        customer = await find_customer(db, phone) if phone else None
        # The chosen customer, or a stand-in, so {sys.customer_name} reads naturally in a test.
        contact = {"customer_name": customer.customer_name if customer else (body.contact_name.strip() or "Test customer"),
                   "phone": phone or "919999999999", "verified": "yes" if customer else "no", **schema.clock_vars()}

        async def check(rule: dict, value: str) -> dict | None:
            return await data_lookup.find(db, phone, str(rule.get("source") or ""), value)

        async def rows(spec: dict, state) -> list[dict] | None:
            return await data_lookup.find_rows(db, phone, spec)

        if body.state:
            # The AI really runs in a test chat (only the FAQ, the buttons and what was typed are sent),
            # so the owner sees exactly what a customer would get.
            turn = await engine.advance(graph, engine.RunState.from_dict(body.state), body.text, system=contact,
                                        lookup=check, media=body.media, matcher=ai_live.match_choice,
                                        answerer=ai_live.answer_faq, data=rows)
        else:
            turn = await engine.start(graph, language=body.language, system=contact, data=rows)
        await db.rollback()
    return {"messages": [m.to_dict() for m in turn.messages],
            "state": turn.state.to_dict(),
            "stopped": turn.stopped,
            "ai": turn.ai,
            "ai_ready": ai_live.available(),
            # What the step WOULD have done. A dry run never assigns a real chat or tags a real
            # customer, so these are shown rather than performed.
            "actions": [a.to_dict() for a in turn.actions],
            "jump_to": turn.jump_to,
            "node": _node_summary(graph, turn.state.node)}


def _node_summary(graph, node_id: str | None) -> dict | None:
    node = graph.node(node_id or "")
    if node is None:
        return None
    return {"id": node.get("id"), "type": node.get("type"), "title": node.get("title") or ""}
