"""POST /webhook/wati — validate token, dedup, enqueue, return 200 fast."""
from __future__ import annotations

import hmac
import json
import random

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, session_scope
from ..jobs import queue_worker
from ..models import InboundQueue, MessageLog, WebhookLog
from ..services import alerts, rate_limit
from ..services.processor import extract_message

log = structlog.get_logger(__name__)
router = APIRouter()

IGNORED_EVENTS = {"sentMessageDELIVERED", "sentMessageREAD", "sentMessageREPLIED", "templateMessageSent", "sessionMessageSent", "message_sent"}
MAX_BODY = 64 * 1024  # a WATI message payload is a couple of KB; anything larger is not one
KEEP_WEBHOOK_LOGS = 500
# WATI retries a non-200 and disables the webhook after enough CONSECUTIVE failures. We spend a few
# of those retries recovering a message during a blip, then stop, so the registration always survives.
WEBHOOK_RETRY_BUDGET = 3
_consecutive_failures = 0


def _reset_failures() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


def failure_count() -> int:
    return _consecutive_failures


async def _absorb(reason: str, client_ip: str, body_bytes: int = 0):
    """Something we cannot use, but WATI is not at fault and re-sending would not help: record it,
    say so, and still answer 200 so the webhook keeps its good standing."""
    log.warning("webhook_payload_ignored", reason=reason, client_ip=client_ip)
    await _record_call(200, "ignored", reason, client_ip, body_bytes=body_bytes)
    await alerts.notify_throttled("webhook_payload_ignored", "A webhook call could not be used", reason, level="warning")
    return {"ok": True, "status": "ignored", "reason": reason}


async def _internal_failure(exc: Exception, client_ip: str, payload: dict, body_bytes: int):
    """Our fault, not WATI's. Ask for a retry while the budget lasts (the customer's message is then
    not lost), and once it runs out accept the loss rather than lose every future message."""
    global _consecutive_failures
    _consecutive_failures += 1
    detail = f"{type(exc).__name__}: {exc}"
    log.error("webhook_internal_error", error=detail, consecutive=_consecutive_failures)
    if _consecutive_failures <= WEBHOOK_RETRY_BUDGET:
        await _record_call(503, "rejected",
                           f"Could not accept the message ({detail}). Asked WATI to retry "
                           f"({_consecutive_failures} of {WEBHOOK_RETRY_BUDGET}).", client_ip, payload, body_bytes)
        await alerts.notify_throttled("webhook_internal_error", "The webhook could not accept a message", detail)
        raise HTTPException(status_code=503, detail="temporarily unavailable")
    await _record_call(200, "dropped",
                       f"Could not accept the message ({detail}). Retries exhausted, so it was dropped to stop "
                       "WATI disabling the webhook. THIS CUSTOMER MESSAGE IS LOST.", client_ip, payload, body_bytes)
    await alerts.notify_throttled(
        "webhook_dropping", "Customer messages are being DROPPED",
        f"{_consecutive_failures} failures in a row. The bot is answering 200 to keep the WATI webhook "
        f"registered, but messages are being lost. Last error: {detail}", min_interval_s=60)
    return {"ok": True, "status": "dropped"}


async def _record_call(status: int, outcome: str, reason: str = "", client_ip: str = "", payload: dict | None = None, body_bytes: int = 0) -> None:
    """Write down what WATI asked for and what we answered - especially refusals, which are otherwise
    invisible from this side. Never stores the token."""
    payload = payload or {}
    try:
        async with session_scope() as db:
            db.add(WebhookLog(
                client_ip=client_ip[:45] or None, status=status, outcome=outcome, reason=(reason or None) and reason[:255],
                phone_e164=str(payload.get("waId") or payload.get("whatsappNumber") or "")[:15] or None,
                wati_msg_id=str(payload.get("id") or payload.get("whatsappMessageId") or "")[:100] or None,
                event_type=str(payload.get("eventType") or payload.get("type") or "")[:40] or None,
                body_bytes=body_bytes,
            ))
            if random.random() < 0.02:  # occasional prune, no extra job needed
                cutoff = await db.scalar(
                    select(WebhookLog.id).order_by(WebhookLog.id.desc()).offset(KEEP_WEBHOOK_LOGS).limit(1))
                if cutoff:
                    await db.execute(delete(WebhookLog).where(WebhookLog.id <= cutoff))
    except Exception as e:  # noqa: BLE001 - diagnostics must never break the webhook
        log.warning("webhook_log_failed", error=str(e))


async def enqueue(db: AsyncSession, payload: dict) -> dict:
    """Shared by the webhook and the simulator. Returns {"status": ..., "queue_id": ...}."""
    if payload.get("owner") is True or payload.get("eventType") in IGNORED_EVENTS:
        return {"status": "ignored", "reason": "not a customer message"}
    phone, msg_type, text, media = extract_message(payload)
    if not phone:
        return {"status": "ignored", "reason": "no waId"}
    msg_id = str(payload.get("id") or payload.get("whatsappMessageId") or "").strip() or None

    row = MessageLog(wati_msg_id=msg_id, phone_e164=phone, direction="in", msg_type=msg_type, text=text if msg_type == "text" else (text or media))
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        log.info("webhook_duplicate", wati_msg_id=msg_id, phone_e164=phone)
        return {"status": "duplicate", "wati_msg_id": msg_id}
    q = InboundQueue(message_log_id=row.id, phone_e164=phone, payload=json.dumps(payload, ensure_ascii=False))
    db.add(q)
    await db.commit()
    queue_worker.wake.set()
    return {"status": "queued", "queue_id": q.id, "message_log_id": row.id}


async def _bad_token(reason: str, client_ip: str) -> None:
    """The one thing we do refuse. Repeated 401s make WATI disable the webhook, which is the right
    outcome for an unauthenticated caller but means a genuine token fix also needs a re-registration."""
    await _record_call(401, "rejected", reason, client_ip)
    rate_limit.note_webhook_auth_failure(client_ip)
    await alerts.notify_throttled("webhook_bad_token", "A call to the webhook was refused", reason, level="warning")
    raise HTTPException(status_code=401, detail="bad token")


@router.post("/webhook/wati")
async def wati_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Public endpoint.

    WATI retries any non-200 and, after enough consecutive failures, stops sending events to this
    webhook altogether - which would make the bot go silently dead. So the only thing we ever refuse
    is a caller that cannot prove it is WATI. Everything else we can absorb is answered 200 and
    written to webhook_log, and a genuine internal fault gets a few 503s (WATI re-sends, so the
    customer's message survives a blip) before we give up and protect the registration instead."""
    s = get_settings()
    client_ip = request.client.host if request.client else ""
    token = (request.query_params.get("token") or "").strip()
    expected = (s.wati_webhook_token or "").strip()

    # Flood protection applies to callers that FAIL authentication. WATI always presents a valid
    # token, so it can never be throttled - throttling the one legitimate sender would be a fine way
    # to get our own webhook disabled.
    if not rate_limit.allow_webhook(client_ip):
        log.warning("webhook_throttled", client_ip=client_ip)
        await _record_call(429, "rejected", "Too many unauthenticated calls from this address.", client_ip)
        raise HTTPException(status_code=429, detail="too many requests")
    if not expected:
        await _bad_token("No WATI_WEBHOOK_TOKEN is configured on this server.", client_ip)
    if not token:
        await _bad_token("WATI called without a ?token= in the URL. Re-register the webhook including the token.", client_ip)
    if not hmac.compare_digest(token, expected):
        await _bad_token(
            f"The ?token= WATI sent ({len(token)} characters) does not match WATI_WEBHOOK_TOKEN "
            f"({len(expected)} characters) on this server. Re-register the webhook with the current value.",
            client_ip)
    rate_limit.note_webhook_authenticated(client_ip)

    # From here the caller IS WATI, so a non-200 costs us deliveries. Absorb what we can.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        return await _absorb(f"Body too large ({declared} bytes) - ignored.", client_ip)
    body = await request.body()
    if len(body) > MAX_BODY:  # chunked requests carry no content-length
        return await _absorb(f"Body too large ({len(body)} bytes) - ignored.", client_ip, len(body))
    try:
        payload = json.loads(body)
    except Exception:  # noqa: BLE001
        return await _absorb("The body was not valid JSON - ignored.", client_ip, len(body))
    if not isinstance(payload, dict):
        return await _absorb("The body was not a JSON object - ignored.", client_ip, len(body))

    try:
        result = await enqueue(db, payload)
    except Exception as e:  # noqa: BLE001 - a crash here would eventually cost us the webhook
        return await _internal_failure(e, client_ip, payload, len(body))
    _reset_failures()
    await _record_call(200, result.get("status", "queued"), result.get("reason", ""), client_ip, payload, len(body))
    return {"ok": True, **result}
