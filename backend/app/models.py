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
