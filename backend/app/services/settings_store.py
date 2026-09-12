"""Connection settings edited in the dashboard (orders API + SAP customer Excel).

Saved values live in the `app_settings` table and are layered over .env by config.apply_overrides,
so the whole app keeps reading plain `get_settings().orders_api_url` and nothing else had to change.
Passwords are encrypted at rest and are never sent back to the browser.

Only the keys in FIELDS can be written from the dashboard - never the admin key, the WATI token or
the database URL.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import delete, select

from ..config import DEFAULT_COLUMN_MAP, apply_overrides, get_settings
from ..db import session_scope
from ..models import AppSetting
from . import crypto

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Field:
    key: str
    group: str  # orders | customers
    type: str  # str | int | choice | map
    secret: bool = False
    choices: tuple = ()
    min: int | None = None
    max: int | None = None


_F = [
    # ---- orders (BOM PPC table) ----
    Field("orders_source", "orders", "choice", choices=("http", "sql", "file")),
    Field("orders_api_url", "orders", "str"),
    Field("orders_api_method", "orders", "choice", choices=("GET", "POST")),
    Field("orders_api_key", "orders", "str", secret=True),
    Field("orders_api_key_in", "orders", "choice", choices=("header", "query", "bearer")),
    Field("orders_api_key_name", "orders", "str"),
    Field("orders_api_body", "orders", "str"),
    Field("orders_format", "orders", "choice", choices=("auto", "json", "csv", "xlsx", "html")),
    Field("orders_sheet", "orders", "str"),
    Field("orders_sql_url", "orders", "str", secret=True),
    Field("orders_sql_query", "orders", "str"),
    Field("orders_file_path", "orders", "str"),
    Field("orders_column_map", "orders", "map"),
    Field("order_refresh_minutes", "orders", "int", min=1, max=1440),
    Field("orders_stale_minutes", "orders", "int", min=2, max=10080),
    # ---- customers (SAP B1 Excel) ----
    Field("customers_source", "customers", "choice", choices=("local", "dropbox", "url")),
    Field("customers_file_path", "customers", "str"),
    Field("customers_url", "customers", "str"),
    Field("customers_url_key", "customers", "str", secret=True),
    Field("customers_url_key_in", "customers", "choice", choices=("header", "query", "bearer")),
    Field("customers_url_key_name", "customers", "str"),
    Field("dropbox_app_key", "customers", "str"),
    Field("dropbox_app_secret", "customers", "str", secret=True),
    Field("dropbox_refresh_token", "customers", "str", secret=True),
    Field("dropbox_file_path", "customers", "str"),
    Field("customers_sheet", "customers", "str"),
    Field("customers_col_code", "customers", "str"),
    Field("customers_col_name", "customers", "str"),
    Field("customers_col_contact", "customers", "str"),
    Field("customer_sync_cron", "customers", "str"),
    # ---- conversation ----
    Field("wati_waba_id", "conversation", "str"),
    Field("wati_channel_number", "conversation", "str"),
    Field("workflow_test_numbers", "conversation", "str"),
    Field("agent_handover_hours", "conversation", "int", min=0, max=720),
    Field("session_timeout_min", "conversation", "int", min=1, max=1440),
    Field("so_menu_style", "conversation", "choice", choices=("auto", "list")),
    # ---- AI (Groq) ----
    Field("groq_api_key", "ai", "str", secret=True),
    Field("ai_builder_model", "ai", "str"),
    Field("ai_live_model", "ai", "str"),
    Field("translate_provider", "ai", "choice", choices=("auto", "ai", "free")),
]
FIELDS: dict[str, Field] = {f.key: f for f in _F}
SECRET_KEYS = {f.key for f in _F if f.secret}


# ---------------- validation ----------------
def _coerce(f: Field, value: Any) -> Any:
    if f.type == "int":
        n = int(value)
        if f.min is not None and n < f.min:
            raise ValueError(f"must be at least {f.min}")
        if f.max is not None and n > f.max:
            raise ValueError(f"must be at most {f.max}")
        return n
    if f.type == "choice":
        v = str(value)
        if v not in f.choices:
            raise ValueError(f"must be one of {', '.join(f.choices)}")
        return v
    if f.type == "map":
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("must be a set of column names")
        out = {k: str(v) for k, v in value.items() if k in DEFAULT_COLUMN_MAP}
        missing = [k for k in ("so_no", "customer_name", "real_status") if not out.get(k)]
        if missing:
            raise ValueError("these columns are required: " + ", ".join(missing))
        return out
    return str(value)


def validate(values: dict) -> tuple[dict, dict[str, str]]:
    """Returns (clean values, {key: error})."""
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, value in values.items():
        f = FIELDS.get(key)
        if f is None:
            errors[key] = "this setting cannot be changed here"
            continue
        if value is None:  # not touched by the form
            continue
        try:
            clean[key] = _coerce(f, value)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            errors[key] = str(e)
    # cross-field checks: a source is only usable if its own fields are filled in
    merged = {**current_values(), **clean}
    if "orders_source" in clean or any(k.startswith("orders_") for k in clean):
        src = merged.get("orders_source")
        if src == "http" and not str(merged.get("orders_api_url") or "").strip():
            errors["orders_api_url"] = "an endpoint URL is required for an API connection"
        if src == "sql" and not str(merged.get("orders_sql_query") or "").strip():
            errors["orders_sql_query"] = "a SQL query is required"
        if src == "file" and not str(merged.get("orders_file_path") or "").strip():
            errors["orders_file_path"] = "a file path is required"
    if "customers_source" in clean or any(k.startswith(("customers_", "dropbox_")) for k in clean):
        src = merged.get("customers_source")
        if src == "url" and not str(merged.get("customers_url") or "").strip():
            errors["customers_url"] = "a download link is required"
        if src == "local" and not str(merged.get("customers_file_path") or "").strip():
            errors["customers_file_path"] = "a file path is required"
        if src == "dropbox" and not all(str(merged.get(k) or "").strip() for k in ("dropbox_app_key", "dropbox_app_secret", "dropbox_refresh_token", "dropbox_file_path")):
            errors["dropbox_app_key"] = "app key, app secret, refresh token and file path are all required for Dropbox"
    return clean, errors


# ---------------- load / save ----------------
def current_values() -> dict:
    """Everything the app is using right now (.env with saved settings layered on top)."""
    s = get_settings()
    return {k: getattr(s, k) for k in FIELDS}


async def load_from_db() -> None:
    async with session_scope() as db:
        rows = (await db.execute(select(AppSetting))).scalars().all()
    values: dict[str, Any] = {}
    for r in rows:
        f = FIELDS.get(r.key)
        if f is None:
            continue
        raw = crypto.decrypt(r.value) if r.is_secret else r.value
        try:
            values[r.key] = json.loads(raw)
        except ValueError:
            log.warning("bad_setting_value", key=r.key)
    apply_overrides(values)
    log.info("connection_settings_loaded", count=len(values))


async def save(values: dict) -> dict[str, str]:
    """Saves the given keys. A key that is absent stays as it is; an empty secret clears it.
    Returns {key: error}; empty means saved."""
    clean, errors = validate(values)
    if errors:
        return errors
    async with session_scope() as db:
        existing = {r.key: r for r in (await db.execute(select(AppSetting))).scalars()}
        for key, value in clean.items():
            f = FIELDS[key]
            payload = json.dumps(value, ensure_ascii=False)
            if f.secret:
                payload = crypto.encrypt(payload)
            row = existing.get(key)
            if row:
                row.value = payload
                row.is_secret = f.secret
            else:
                db.add(AppSetting(key=key, value=payload, is_secret=f.secret))
    await load_from_db()
    log.info("connection_settings_saved", keys=sorted(clean))
    return {}


async def reset(keys: list[str] | None = None) -> None:
    """Drop saved values so the .env value applies again."""
    async with session_scope() as db:
        stmt = delete(AppSetting)
        if keys:
            stmt = stmt.where(AppSetting.key.in_([k for k in keys if k in FIELDS]))
        await db.execute(stmt)
    await load_from_db()


async def saved_keys() -> set[str]:
    async with session_scope() as db:
        return {r.key for r in (await db.execute(select(AppSetting))).scalars()}


# ---------------- views ----------------
async def public_view() -> dict:
    """Config for the dashboard. Passwords are masked, never sent in full."""
    values = current_values()
    saved = await saved_keys()
    out: dict[str, Any] = {}
    for key, f in FIELDS.items():
        v = values.get(key)
        out[key] = {
            "value": crypto.mask(str(v or "")) if f.secret else v,
            "is_set": bool(v) if f.secret else None,
            "secret": f.secret,
            "from_db": key in saved,
            "choices": list(f.choices),
            "type": f.type,
        }
    return out


def draft_settings(draft: dict):
    """A throwaway settings object with unsaved form values applied - used by the Test buttons
    so nothing is written and the running bot is not disturbed."""
    clean, errors = validate(draft)
    if errors:
        raise ValueError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    # a secret left blank in the form means "keep the saved one"
    clean = {k: v for k, v in clean.items() if not (FIELDS[k].secret and v == "")}
    return get_settings().model_copy(update=clean)
