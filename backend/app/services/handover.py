"""A person has taken the chat, so the bot stays quiet for that customer.

WATI's own bot stops once a chat is assigned to an agent. WATI does not tell anyone when the agent
solves it - there is no webhook for that, and no API that reports a conversation's status - so the
bot takes the chat back when:
  * someone clicks "Hand back to bot" in Live sessions,
  * a workflow step assigns the chat back to the bot or marks it solved,
  * or the customer has sent nothing for `agent_handover_hours` (Settings; 0 = only by hand).
"""
from __future__ import annotations

from datetime import timedelta

import structlog
from sqlalchemy import select

from ..config import get_settings
from ..models import AgentHandover, Customer, WorkflowRun, utcnow

log = structlog.get_logger(__name__)


async def active(db, phone: str) -> AgentHandover | None:
    """The hand-over in force for this number, if any. One that has gone quiet for longer than the
    setting is closed here, so an agent forgetting a chat never silences the bot for good."""
    row = await db.get(AgentHandover, phone)
    if row is None or not row.active:
        return None
    hours = get_settings().agent_handover_hours
    last = row.last_message_at or row.started_at
    if hours and last is not None and utcnow() - last > timedelta(hours=hours):
        _close(row, f"no message from the customer for {hours} hours")
        return None
    return row


async def start(db, phone: str, assignee: str = "", source: str = "") -> AgentHandover:
    row = await db.get(AgentHandover, phone)
    if row is None:
        row = AgentHandover(phone_e164=phone)
        db.add(row)
    now = utcnow()
    row.active, row.assignee, row.source = True, assignee[:255] or None, source[:120] or None
    row.started_at, row.last_message_at, row.ended_at, row.end_reason = now, now, None, None
    # Whatever workflow the customer was in stops here; after the hand-back they start fresh.
    run = await db.get(WorkflowRun, phone)
    if run is not None and run.status != "done":
        run.status, run.due_at = "done", None
        run.last_key, run.last_finished_at = run.workflow_key, now
    log.info("handed_to_person", phone=phone, assignee=assignee, source=source)
    return row


async def end(db, phone: str, reason: str) -> bool:
    row = await db.get(AgentHandover, phone)
    if row is None or not row.active:
        return False
    _close(row, reason)
    return True


def _close(row: AgentHandover, reason: str) -> None:
    row.active, row.ended_at, row.end_reason = False, utcnow(), reason[:120]
    log.info("handed_back_to_bot", phone=row.phone_e164, reason=reason)


async def list_active(db) -> list[dict]:
    """Chats a person has right now, for Live sessions."""
    rows = (await db.execute(select(AgentHandover).where(AgentHandover.active.is_(True))
                             .order_by(AgentHandover.started_at.desc()))).scalars().all()
    hours = get_settings().agent_handover_hours
    out = []
    for r in rows:
        if await active(db, r.phone_e164) is None:  # went quiet past the limit just now
            continue
        customer = await db.get(Customer, r.phone_e164)
        last = r.last_message_at or r.started_at
        out.append({
            "phone": r.phone_e164, "customer_name": customer.customer_name if customer else None,
            "assignee": r.assignee or "", "source": r.source or "",
            "started_at": _iso(r.started_at), "last_message_at": _iso(r.last_message_at),
            "returns_at": _iso(last + timedelta(hours=hours)) if hours and last else None,
        })
    return out


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None
