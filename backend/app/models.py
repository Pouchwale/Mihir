"""Tables per spec section 5.3 plus inbound_queue and sync_runs."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Customer(Base):
    __tablename__ = "customers"
    phone_e164: Mapped[str] = mapped_column(String(15), primary_key=True)
    customer_code: Mapped[str | None] = mapped_column(String(20))
    customer_name: Mapped[str] = mapped_column(String(255))  # EXACTLY as in Excel
    raw_contact: Mapped[str | None] = mapped_column(String(50))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrderCache(Base):
    __tablename__ = "orders_cache"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    so_no: Mapped[str] = mapped_column(String(50), index=True)
    po_no: Mapped[str | None] = mapped_column(String(50), index=True)
    fg_item_code: Mapped[str | None] = mapped_column(String(50))
    fg_description: Mapped[str | None] = mapped_column(String(255))  # what the item IS, for a customer to read
    customer_name: Mapped[str] = mapped_column(String(255), index=True)  # EXACTLY as returned by source
    connection_status: Mapped[str | None] = mapped_column(String(100))  # internal only, never sent
    real_status: Mapped[str | None] = mapped_column(String(100))  # the only field shown to customer
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


STEPS = ("START", "AWAIT_SO", "CONFIRM", "AWAIT_FG", "DONE", "FAILED")


class Session(Base):
    __tablename__ = "sessions"
    phone_e164: Mapped[str] = mapped_column(String(15), primary_key=True)
    step: Mapped[str] = mapped_column(String(20), default="START")
    so_no: Mapped[str | None] = mapped_column(String(50))
    po_no: Mapped[str | None] = mapped_column(String(50))
    fg_code: Mapped[str | None] = mapped_column(String(50))
    pending_value: Mapped[str | None] = mapped_column(String(100))  # awaiting Yes/No after STT
    pending_kind: Mapped[str | None] = mapped_column(String(10))  # so | po | fg
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    language: Mapped[str] = mapped_column(String(5), default="en")
    lang_chosen: Mapped[bool] = mapped_column(Boolean, default=False)  # picked from the language buttons this window
    # the same request, over and over: what it was, and how many times in a row (services/repeat.py)
    repeat_key: Mapped[str | None] = mapped_column(String(120))
    repeat_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MessageLog(Base):
    __tablename__ = "message_log"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    wati_msg_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    phone_e164: Mapped[str] = mapped_column(String(15), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    msg_type: Mapped[str] = mapped_column(String(20))  # text | audio | interactive | button | ...
    text: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    options: Mapped[str | None] = mapped_column(Text)  # JSON: interactive buttons/list sent with an outbound message
    outcome: Mapped[str | None] = mapped_column(String(30), index=True)
    step_after: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class InboundQueue(Base):
    __tablename__ = "inbound_queue"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    message_log_id: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"))
    phone_e164: Mapped[str] = mapped_column(String(15), index=True)
    payload: Mapped[str] = mapped_column(Text)  # raw WATI webhook JSON
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)  # queued|processing|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NameMismatchLog(Base):
    __tablename__ = "name_mismatch_log"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    phone_e164: Mapped[str] = mapped_column(String(15), index=True)
    excel_name: Mapped[str | None] = mapped_column(String(255))
    api_name: Mapped[str | None] = mapped_column(String(255))
    so_no: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(12), index=True)  # customers | orders
    source: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    rejected_rows: Mapped[str | None] = mapped_column(Text)  # JSON list
    raw_headers: Mapped[str | None] = mapped_column(Text)  # JSON list
    warnings: Mapped[str | None] = mapped_column(Text)  # JSON list
    error: Mapped[str | None] = mapped_column(Text)


class Template(Base):
    """Admin overrides for customer-facing text. kind: template | label | custom.
    For custom replies, one row per language holds the text and a row with lang='meta' holds JSON
    {title, triggers, buttons, enabled}."""

    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(10))
    key: Mapped[str] = mapped_column(String(60))
    lang: Mapped[str] = mapped_column(String(5))
    text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("kind", "key", "lang", name="uq_templates_kind_key_lang"),)


class TemplateHistory(Base):
    __tablename__ = "template_history"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(10))
    key: Mapped[str] = mapped_column(String(60), index=True)
    lang: Mapped[str] = mapped_column(String(5))
    text: Mapped[str | None] = mapped_column(Text)  # the text that was replaced (None = was default)
    action: Mapped[str] = mapped_column(String(10))  # save | reset | delete
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    """Connection settings edited in the dashboard. Secrets are stored encrypted (see services.crypto).
    Values are JSON so types survive a round trip."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


Index("ix_orders_so_fg", OrderCache.so_no, OrderCache.fg_item_code)


class WebhookLog(Base):
    """Every call WATI makes to /webhook/wati - including the ones we REFUSE.

    Without this a rejected call is invisible on both sides: WATI's log shows "401" and the bot shows
    nothing at all, so there is no way to tell whether the problem is the token WATI sends or the one
    the bot expects. The token itself is never stored, only whether it matched."""

    __tablename__ = "webhook_log"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(45))
    status: Mapped[int] = mapped_column(Integer, index=True)  # HTTP status we replied with
    outcome: Mapped[str] = mapped_column(String(30), index=True)  # queued | duplicate | ignored | rejected
    reason: Mapped[str | None] = mapped_column(String(255))  # plain English, safe to show
    phone_e164: Mapped[str | None] = mapped_column(String(15))
    wati_msg_id: Mapped[str | None] = mapped_column(String(100))
    event_type: Mapped[str | None] = mapped_column(String(40))
    body_bytes: Mapped[int] = mapped_column(Integer, default=0)


class Workflow(Base):
    """One conversation the owner drew in the visual editor.

    The row is only identity plus which snapshot is live; the graph itself lives in
    WorkflowVersion, so publishing and rolling back never rewrite or lose a draft."""

    __tablename__ = "workflows"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)  # slug, stable across renames
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # published is not the same as live
    published_version: Mapped[int | None] = mapped_column(Integer)
    draft_version: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lowest first, when triggers compete
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WorkflowVersion(Base):
    """An immutable snapshot of a graph. Never updated once published.

    Rollback repoints Workflow.published_version at an older row rather than copying nodes back,
    so every version the owner ever published stays intact and inspectable."""

    __tablename__ = "workflow_versions"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft | published | archived
    graph: Mapped[str] = mapped_column(Text)  # the JSON document the editor produced
    notes: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (UniqueConstraint("workflow_id", "version", name="uq_wfver_workflow_version"),)


class WorkflowRun(Base):
    """Where one customer is inside a published workflow. One row per phone: a customer is in at most
    one workflow at a time, and the order-status bot's own `sessions` row is left untouched.

    The run is pinned to the version it started on, so publishing a change mid-conversation never
    pulls the ground from under a customer halfway through a question. `last_key` remembers the
    workflow that finished most recently, so a greeting for new numbers fires once per conversation
    rather than on every message."""

    __tablename__ = "workflow_runs"
    phone_e164: Mapped[str] = mapped_column(String(15), primary_key=True)
    workflow_key: Mapped[str] = mapped_column(String(60), index=True)
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text, default="{}")  # engine.RunState as JSON
    status: Mapped[str] = mapped_column(String(10), default="active", index=True)  # active | waiting | running | done
    due_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # when a Wait step is over
    trigger: Mapped[str | None] = mapped_column(String(80))  # how it started, for the dashboard
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_key: Mapped[str | None] = mapped_column(String(60))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class AgentHandover(Base):
    """A person has the chat, so the bot stays quiet for this customer until it is handed back
    (services/handover.py says when)."""

    __tablename__ = "agent_handovers"
    phone_e164: Mapped[str] = mapped_column(String(15), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    assignee: Mapped[str | None] = mapped_column(String(255))  # operator email or team names
    source: Mapped[str | None] = mapped_column(String(120))  # which workflow, or the dashboard
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime)  # the customer's latest message
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    end_reason: Mapped[str | None] = mapped_column(String(120))


class NewCustomer(Base):
    """A number that signed up through a workflow (the "numbers not in your customer list" trigger).

    Kept apart from `customers` on purpose: that table is replaced by every sync of the customer
    Excel, and it is what decides who may see orders - nothing a stranger types into a workflow may
    ever grant that."""

    __tablename__ = "new_customers"
    phone_e164: Mapped[str] = mapped_column(String(15), primary_key=True)
    whatsapp_name: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[str] = mapped_column(Text, default="{}")  # the answers they gave, as JSON
    workflow_key: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class QuestionPreset(Base):
    """A question the owner saved to reuse: offered under "What are you asking?" in every workflow."""

    __tablename__ = "question_presets"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(40))
    spec: Mapped[str] = mapped_column(Text)  # the question's settings as JSON (see workflow/presets.py)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
