"""Admin API (X-Admin-Key). Consumed by the dashboard; spec paths kept as aliases."""
from __future__ import annotations

import hmac
import json
import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import day_of, get_db
from ..jobs import customer_sync, order_refresh, queue_worker, scheduler, session_cleanup
from ..models import Customer, InboundQueue, MessageLog, NameMismatchLog, OrderCache, Session, SyncRun, WebhookLog, utcnow
import structlog

from ..services import alerts, intent, preflight, settings_store
from ..services.state_machine import reset
from ..services.wati import wati
from ..utils.phone import normalize_phone
from .health import status_payload
from .webhook import enqueue

log = structlog.get_logger(__name__)
router = APIRouter()


async def require_admin(x_admin_key: str | None = Header(default=None)):
    s = get_settings()
    if not x_admin_key or not hmac.compare_digest(x_admin_key, s.admin_key):
        raise HTTPException(status_code=401, detail="missing or invalid X-Admin-Key")


api = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])
legacy = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _dt(v):
    return v.isoformat() if v else None


def _session_dict(s: Session) -> dict:
    return {
        "phone": s.phone_e164, "step": s.step, "so_no": s.so_no, "po_no": s.po_no, "fg_code": s.fg_code,
        "pending_value": s.pending_value, "pending_kind": s.pending_kind, "attempts": s.attempts,
        "language": s.language, "lang_chosen": bool(s.lang_chosen), "updated_at": _dt(s.updated_at),
    }


def _msg_dict(m: MessageLog) -> dict:
    return {
        "id": m.id, "wati_msg_id": m.wati_msg_id, "phone": m.phone_e164, "direction": m.direction, "type": m.msg_type,
        "text": m.text, "transcript": m.transcript, "outcome": m.outcome, "step_after": m.step_after, "created_at": _dt(m.created_at),
        "options": json.loads(m.options) if m.options else None,
    }


def _run_dict(r: SyncRun) -> dict:
    return {
        "id": r.id, "kind": r.kind, "source": r.source, "started_at": _dt(r.started_at), "finished_at": _dt(r.finished_at), "ok": r.ok,
        "total_rows": r.total_rows, "accepted": r.accepted, "rejected": r.rejected,
        "rejected_rows": json.loads(r.rejected_rows) if r.rejected_rows else [],
        "raw_headers": json.loads(r.raw_headers) if r.raw_headers else [],
        "warnings": json.loads(r.warnings) if r.warnings else [], "error": r.error,
    }


# ---------------- overview ----------------
@api.get("/readiness")
async def readiness(request: Request, deep: bool = True):
    """Everything that must be true before real customers can use the bot, each with the exact fix.
    deep=false skips the live connection tests (fast enough to poll)."""
    return await preflight.run_checks(deep=deep, base_url=str(request.base_url))


@api.post("/wati/self-test")
async def webhook_self_test(request: Request):
    """Call our own public webhook exactly as WATI would, and report what happened.

    This settles "is it WATI or is it us": it exercises DNS, TLS, any reverse proxy, the token check
    and the queue. The probe carries an eventType the pipeline ignores, so it never invents a
    customer message. A pass here means WATI will get through too, unless WATI's own IPs are blocked."""
    import httpx

    s = get_settings()
    url = preflight.hook_url_for(s, str(request.base_url))
    problem = preflight.hook_url_problem(s, str(request.base_url))
    if problem:
        return {"ok": False, "url": url, "detail": problem}
    payload = {"eventType": "sessionMessageSent", "id": f"selftest-{uuid.uuid4().hex}", "text": "self test"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.post(url, json=payload, headers={"Content-Type": "application/json"})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "detail":
                f"Could not reach {url} from this server: {e}. The address is wrong, DNS/TLS is not set up, "
                "or a firewall is in the way - WATI would fail the same way."}
    if r.status_code == 200:
        return {"ok": True, "url": url, "status": 200,
                "detail": "The webhook address works end to end. WATI can deliver customer messages here."}
    if r.status_code == 401:
        return {"ok": False, "url": url, "status": 401, "detail":
                "The address is reachable but the token was refused - so a 401 in WATI means the ?token= "
                "registered there does not match WATI_WEBHOOK_TOKEN. Press 'Register webhook in WATI' to fix it."}
    return {"ok": False, "url": url, "status": r.status_code,
            "detail": f"The address answered {r.status_code}: {r.text[:200]}"}


def _business_number(raw: str) -> tuple[str, str]:
    """The WhatsApp business number as WATI writes it - digits only, no "+".

    Blank is allowed: register_webhook then reuses whatever WATI already has, which works only on
    accounts where WATI answers a GET on webhookEndpoints. normalize_phone assumes India, so a
    number it rejects is not automatically wrong - fall back to the digits typed."""
    text = raw.strip()
    if not text:
        return "", ""
    pr = normalize_phone(text)
    if pr.ok:
        return pr.phone or "", ""
    digits = re.sub(r"\D+", "", text)
    if not 8 <= len(digits) <= 15:
        return "", (f"'{text}' does not look like a WhatsApp business number. Enter it with the country code "
                    "and no spaces, exactly as WATI shows it, e.g. 919876543210.")
    return digits, ""


class WebhookIn(BaseModel):
    # Your WhatsApp business number. Blank means "reuse whatever WATI already has registered", which
    # only works on accounts where WATI answers a GET on webhookEndpoints - many do not.
    phone_number: str = ""


@api.post("/wati/register-webhook")
async def register_wati_webhook(request: Request, body: WebhookIn):
    """Tell WATI where to post incoming customer messages, over the API.

    The same thing you would do in WATI -> Settings -> Webhooks, for accounts that have API access
    but no dashboard login. Without this the bot can send but never receives."""
    s = get_settings()
    if s.wati_mocked:
        raise HTTPException(400, "WATI is in simulation mode - set WATI_TOKEN and a real WATI_BASE_URL first.")
    url = preflight.hook_url_for(s, str(request.base_url))
    problem = preflight.hook_url_problem(s, str(request.base_url))
    if problem:
        raise HTTPException(400, problem)
    number, bad = _business_number(body.phone_number)
    if bad:
        return {"ok": False, "detail": bad, "url": url}
    try:
        result = await wati.register_webhook(url, number)
    except Exception as e:  # noqa: BLE001 - report it, never 500 the dashboard
        return {"ok": False, "detail": str(e), "url": url}
    return {"ok": True, "url": url, "detail": f"WATI will now post incoming messages to {url}", "result": result}


@api.get("/wati/webhooks")
async def list_wati_webhooks():
    """What WATI currently has registered, so you can see it without the WATI dashboard."""
    if get_settings().wati_mocked:
        return {"ok": False, "detail": "WATI is in simulation mode.", "webhooks": []}
    try:
        return {"ok": True, "webhooks": await wati.list_webhooks()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e), "webhooks": []}


@api.post("/settings/reload")
async def reload_settings(request: Request):
    """Re-read backend/.env without restarting - the usual reason a new WATI token 'does nothing'.
    Values saved in the dashboard still win. CORS, /docs and logging are fixed at start-up and still
    need a real restart; nothing else does."""
    from ..config import reload_env

    reload_env()
    await settings_store.load_from_db()  # dashboard-saved values win, so re-apply them on top
    s = get_settings()
    log.info("env_reloaded", wati_mocked=s.wati_mocked, mode=s.app_mode)
    return {"ok": True, "wati_mocked": s.wati_mocked, "mode": s.app_mode,
            "readiness": await preflight.run_checks(deep=True, base_url=str(request.base_url))}


@api.get("/diagnostics")
async def diagnostics(db: AsyncSession = Depends(get_db), limit: int = 50):
    """One place to see whether a problem is on WATI's side or ours.

    IN  - every call WATI made to our webhook, including the ones we refused (a 401 here means the
          token in WATI's webhook URL does not match ours).
    OUT - every message we tried to send to WATI, including failures with WATI's own reason.
    If IN is empty, WATI is not reaching us at all: the webhook is not registered, points elsewhere,
    or the server is not publicly reachable."""
    s = get_settings()
    calls = (await db.execute(select(WebhookLog).order_by(desc(WebhookLog.id)).limit(limit))).scalars().all()
    inbound = [{
        "at": _dt(c.created_at), "client_ip": c.client_ip, "status": c.status, "outcome": c.outcome,
        "reason": c.reason, "phone": c.phone_e164, "event_type": c.event_type, "wati_msg_id": c.wati_msg_id,
    } for c in calls]

    outbox = list(wati.outbox)[-limit:][::-1]
    outbound = [{
        "at": o.get("at"), "phone": o.get("phone"), "kind": o.get("kind"),
        "sent": o.get("sent", None if s.wati_mocked else False), "error": o.get("error"),
        "text": (o.get("text") or "")[:160], "simulated": s.wati_mocked,
    } for o in outbox]

    failed = (await db.execute(
        select(InboundQueue).where(InboundQueue.status.in_(("failed", "processing"))).order_by(desc(InboundQueue.id)).limit(limit)
    )).scalars().all()

    rejected = [c for c in calls if c.outcome == "rejected"]
    if not calls:
        verdict = ("WATI has never called this server. Either the webhook is not registered, it points at a different "
                   "address, or this server is not reachable from the internet. Check 'Webhook registered in WATI' on the Go live page.")
    elif rejected and rejected[0].id == calls[0].id:
        verdict = f"The most recent call from WATI was REFUSED by this server: {rejected[0].reason}"
    else:
        verdict = f"WATI is reaching this server ({len(calls)} recent calls, {len(rejected)} refused)."

    return {
        "verdict": verdict,
        "wati_mocked": s.wati_mocked,
        "inbound": inbound,
        "outbound": outbound,
        "failed_queue": [{"at": _dt(f.updated_at), "phone": f.phone_e164, "status": f.status, "attempts": f.attempts, "error": f.error} for f in failed],
        "alerts": list(alerts.recent)[-20:][::-1],
    }


@api.get("/overview")
async def overview(db: AsyncSession = Depends(get_db)):
    s = get_settings()
    health = await status_payload()
    since = utcnow() - timedelta(days=14)
    customers = await db.scalar(select(func.count(Customer.phone_e164)))
    orders = await db.scalar(select(func.count(OrderCache.id)))
    distinct_so = await db.scalar(select(func.count(func.distinct(OrderCache.so_no))))
    active_cut = utcnow() - timedelta(minutes=s.session_timeout_min)
    active_sessions = await db.scalar(select(func.count(Session.phone_e164)).where(Session.updated_at >= active_cut, Session.step != "START"))
    mismatches = await db.scalar(select(func.count(NameMismatchLog.id)))
    queued = await db.scalar(select(func.count(InboundQueue.id)).where(InboundQueue.status.in_(["queued", "processing"])))
    failed_q = await db.scalar(select(func.count(InboundQueue.id)).where(InboundQueue.status == "failed"))

    day = day_of(MessageLog.created_at)
    rows = (await db.execute(select(day, MessageLog.direction, func.count(MessageLog.id)).where(MessageLog.created_at >= since).group_by(day, MessageLog.direction).order_by(day))).all()
    per_day: dict[str, dict] = {}
    for d, direction, n in rows:
        key = str(d)
        per_day.setdefault(key, {"date": key, "in": 0, "out": 0})[direction] = n
    outcomes = (await db.execute(select(MessageLog.outcome, func.count(MessageLog.id)).where(MessageLog.created_at >= since, MessageLog.direction == "out", MessageLog.outcome.is_not(None)).group_by(MessageLog.outcome))).all()
    last_runs = {}
    for kind in ("customers", "orders"):
        r = (await db.execute(select(SyncRun).where(SyncRun.kind == kind).order_by(desc(SyncRun.id)).limit(1))).scalar_one_or_none()
        last_runs[kind] = _run_dict(r) if r else None
    return {
        "health": health,
        "counts": {"customers": customers or 0, "orders": orders or 0, "distinct_so": distinct_so or 0, "active_sessions": active_sessions or 0,
                   "mismatches": mismatches or 0, "queued": queued or 0, "failed_queue": failed_q or 0},
        "messages_per_day": list(per_day.values()),
        "outcomes": [{"outcome": o, "count": n} for o, n in outcomes],
        "last_runs": last_runs,
        "jobs": scheduler.jobs_info(),
        "alerts": list(alerts.recent)[-20:],
        "config": {"mode": s.app_mode, "wati_mocked": s.wati_mocked, "orders_source": s.orders_source, "orders_format": s.orders_format,
                   "order_refresh_minutes": s.order_refresh_minutes, "customer_sync_cron": s.customer_sync_cron, "session_timeout_min": s.session_timeout_min,
                   "openai": bool(s.openai_api_key), "openai_paused": intent.breaker_open(),
                   "groq": bool(s.groq_api_key), "dropbox": s.dropbox_configured, "support_contact": s.support_contact},
    }


# ---------------- sessions ----------------
@api.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db), limit: int = 200):
    rows = (await db.execute(select(Session).order_by(desc(Session.updated_at)).limit(limit))).scalars().all()
    names = {c.phone_e164: c.customer_name for c in (await db.execute(select(Customer))).scalars()}
    return [{**_session_dict(r), "customer_name": names.get(r.phone_e164)} for r in rows]


@api.get("/sessions/{phone}")
async def session_detail(phone: str, db: AsyncSession = Depends(get_db)):
    s = await db.get(Session, phone)
    c = await db.get(Customer, phone)
    msgs = (await db.execute(select(MessageLog).where(MessageLog.phone_e164 == phone).order_by(desc(MessageLog.id)).limit(30))).scalars().all()
    return {"session": _session_dict(s) if s else None, "customer": {"name": c.customer_name, "code": c.customer_code} if c else None,
            "messages": [_msg_dict(m) for m in reversed(msgs)]}


@api.post("/sessions/{phone}/reset")
async def reset_session(phone: str, db: AsyncSession = Depends(get_db)):
    s = await db.get(Session, phone)
    if s:
        reset(s)
    return {"ok": True}


# ---------------- messages ----------------
@api.get("/messages")
async def list_messages(db: AsyncSession = Depends(get_db), phone: str | None = None, q: str | None = None, direction: str | None = None,
                        outcome: str | None = None, page: int = 1, page_size: int = Query(default=50, le=500)):
    stmt = select(MessageLog)
    cnt = select(func.count(MessageLog.id))
    conds = []
    if phone:
        conds.append(MessageLog.phone_e164.ilike(f"%{phone}%"))
    if q:
        conds.append((MessageLog.text.ilike(f"%{q}%")) | (MessageLog.transcript.ilike(f"%{q}%")))
    if direction in ("in", "out"):
        conds.append(MessageLog.direction == direction)
    if outcome:
        conds.append(MessageLog.outcome == outcome)
    for c in conds:
        stmt = stmt.where(c)
        cnt = cnt.where(c)
    total = await db.scalar(cnt)
    rows = (await db.execute(stmt.order_by(desc(MessageLog.id)).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total or 0, "page": page, "page_size": page_size, "items": [_msg_dict(m) for m in rows]}


# ---------------- mismatches ----------------
@api.get("/mismatches")
async def list_mismatches(db: AsyncSession = Depends(get_db), limit: int = 500):
    rows = (await db.execute(select(NameMismatchLog).order_by(desc(NameMismatchLog.id)).limit(limit))).scalars().all()
    return [{"id": r.id, "phone": r.phone_e164, "excel_name": r.excel_name, "api_name": r.api_name, "so_no": r.so_no, "created_at": _dt(r.created_at)} for r in rows]


# ---------------- imports / jobs ----------------
@api.get("/imports")
async def list_imports(db: AsyncSession = Depends(get_db), kind: str | None = None, limit: int = 50):
    stmt = select(SyncRun).order_by(desc(SyncRun.id)).limit(limit)
    if kind:
        stmt = stmt.where(SyncRun.kind == kind)
    return [_run_dict(r) for r in (await db.execute(stmt)).scalars().all()]


@api.post("/import-customers")
async def import_customers(file: UploadFile | None = File(default=None)):
    body = await file.read() if file else None
    r = await customer_sync.run(body, source_desc=f"upload {file.filename}" if file else None)
    return _run_dict(r)


@api.post("/refresh-orders")
async def refresh_orders():
    return _run_dict(await order_refresh.run())


@api.post("/test-fetch")
async def test_fetch():
    return (await order_refresh.test_fetch()).to_dict()


@api.get("/orders-source")
async def orders_source_info():
    s = get_settings()
    return {
        "source": s.orders_source, "format": s.orders_format, "file_path": str(s.resolve_path(s.orders_file_path)) if s.orders_source == "file" else None,
        "api_url": s.orders_api_url, "api_method": s.orders_api_method, "api_key_in": s.orders_api_key_in, "api_key_name": s.orders_api_key_name,
        "api_key_set": bool(s.orders_api_key), "sql_url_set": bool(s.orders_sql_url), "column_map": s.orders_column_map,
        "last_preview": order_refresh.last_preview.to_dict() if order_refresh.last_preview else None,
    }


@api.post("/cleanup-sessions")
async def cleanup_sessions():
    return {"reset": await session_cleanup.run()}


@api.get("/queue")
async def queue_status(db: AsyncSession = Depends(get_db), limit: int = 50):
    rows = (await db.execute(select(InboundQueue).order_by(desc(InboundQueue.id)).limit(limit))).scalars().all()
    return [{"id": r.id, "phone": r.phone_e164, "status": r.status, "attempts": r.attempts, "error": r.error, "created_at": _dt(r.created_at), "updated_at": _dt(r.updated_at)} for r in rows]


@api.get("/customers")
async def list_customers(db: AsyncSession = Depends(get_db), q: str | None = None, limit: int = 500):
    stmt = select(Customer).order_by(Customer.customer_name).limit(limit)
    if q:
        stmt = stmt.where((Customer.customer_name.ilike(f"%{q}%")) | (Customer.phone_e164.ilike(f"%{q}%")) | (Customer.customer_code.ilike(f"%{q}%")))
    rows = (await db.execute(stmt)).scalars().all()
    so_by_name: dict[str, set[str]] = {}
    for so, name in (await db.execute(select(OrderCache.so_no, OrderCache.customer_name))).all():
        so_by_name.setdefault(name, set()).add(so)
    return [{"phone": c.phone_e164, "code": c.customer_code, "name": c.customer_name, "raw_contact": c.raw_contact, "imported_at": _dt(c.imported_at),
             "so_numbers": sorted(so_by_name.get(c.customer_name, []))} for c in rows]


@api.get("/orders")
async def list_orders(db: AsyncSession = Depends(get_db), q: str | None = None, limit: int = 500):
    stmt = select(OrderCache).order_by(OrderCache.so_no, OrderCache.fg_item_code).limit(limit)
    if q:
        stmt = stmt.where((OrderCache.so_no.ilike(f"%{q}%")) | (OrderCache.po_no.ilike(f"%{q}%")) | (OrderCache.customer_name.ilike(f"%{q}%")) | (OrderCache.fg_item_code.ilike(f"%{q}%")))
    rows = (await db.execute(stmt)).scalars().all()
    return [{"id": r.id, "so_no": r.so_no, "po_no": r.po_no, "fg_item_code": r.fg_item_code, "customer_name": r.customer_name,
             "connection_status": r.connection_status, "real_status": r.real_status, "fetched_at": _dt(r.fetched_at)} for r in rows]


@api.get("/outbox")
async def outbox(limit: int = 50):
    return list(wati.outbox)[-limit:]


# ---------------- simulator ----------------
class Selection(BaseModel):
    kind: str  # buttons | list
    title: str
    description: str = ""


class SimulateIn(BaseModel):
    phone: str
    text: str = ""
    type: str = "text"  # text | audio | interactive
    selection: Selection | None = None  # a tapped button / list row


def build_sim_payload(body: SimulateIn) -> dict:
    """WATI-shaped webhook payload. For a tapped option we mirror the field names WATI uses
    (listReply / buttonReply) and also set text, exactly like the real webhook does."""
    msg_id = f"sim-{uuid.uuid4().hex[:12]}"
    payload = {
        "id": msg_id, "waId": body.phone.strip(), "type": body.type, "text": body.text if body.type == "text" else None,
        "data": "fixtures/voice_sample.ogg" if body.type == "audio" else None, "senderName": "Simulator", "eventType": "message",
        "owner": False, "timestamp": str(int(utcnow().timestamp())),
    }
    if body.selection:
        sel = body.selection
        payload["text"] = sel.title
        if sel.kind == "list":
            payload["type"] = "interactive"
            payload["listReply"] = {"title": sel.title, "description": sel.description}
        else:
            payload["type"] = "button"
            payload["buttonReply"] = {"text": sel.title}
    return payload


@api.post("/simulate")
async def simulate(body: SimulateIn, db: AsyncSession = Depends(get_db)):
    """Build a WATI-shaped webhook payload and push it through the real path (dedup -> queue -> processor)."""
    payload = build_sim_payload(body)
    res = await enqueue(db, payload)
    if res.get("status") != "queued":
        return {"queued": res, "reply": None}
    item = await queue_worker.wait_for(res["queue_id"], timeout=20)
    # every bot message written after this inbound one (the greeting is two messages)
    replies = (await db.execute(
        select(MessageLog)
        .where(MessageLog.phone_e164 == body.phone.strip(), MessageLog.direction == "out", MessageLog.id > res["message_log_id"])
        .order_by(MessageLog.id)
    )).scalars().all()
    inbound = await db.get(MessageLog, res["message_log_id"])
    sess = await db.get(Session, body.phone.strip())
    return {
        "queued": res,
        "queue_status": item.status if item else "timeout",
        "queue_error": item.error if item else None,
        "inbound": _msg_dict(inbound) if inbound else None,
        "reply": _msg_dict(replies[-1]) if replies else None,
        "replies": [_msg_dict(m) for m in replies],
        "session": _session_dict(sess) if sess else None,
    }


# ---------------- spec aliases ----------------
@legacy.post("/import-customers")
async def legacy_import():
    return _run_dict(await customer_sync.run())


@legacy.post("/refresh-orders")
async def legacy_refresh():
    return _run_dict(await order_refresh.run())


@legacy.get("/mismatches")
async def legacy_mismatches(db: AsyncSession = Depends(get_db)):
    return await list_mismatches(db)


@legacy.get("/sessions/{phone}")
async def legacy_session(phone: str, db: AsyncSession = Depends(get_db)):
    return await session_detail(phone, db)


router.include_router(api)
router.include_router(legacy)
