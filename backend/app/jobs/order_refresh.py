"""Order refresh: pull the whole table from the configured source, map columns, replace orders_cache."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import structlog
from sqlalchemy import delete, func, select

from ..adapters import get_source, map_rows
from ..config import get_settings
from ..db import session_scope
from ..models import OrderCache, SyncRun, utcnow
from ..services import alerts

log = structlog.get_logger(__name__)
_lock = asyncio.Lock()


@dataclass
class Preview:
    ok: bool
    source: str = ""
    headers: list[str] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    total_raw: int = 0
    mapped: int = 0
    sample: list[dict] = field(default_factory=list)
    error: str | None = None
    at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


last_preview: Preview | None = None


async def test_fetch(settings=None) -> Preview:
    """Fetch + map without writing anything. Used by the dashboard 'Test' buttons.
    Pass a draft settings object to try values that have not been saved yet."""
    global last_preview
    s = settings or get_settings()
    src = get_source(s)
    try:
        fr = await src.fetch()
        mr = map_rows(fr.raw_rows, s.orders_column_map)
        sample = [asdict(r) for r in mr.rows[:5]]
        p = Preview(ok=not mr.missing, source=fr.description, headers=mr.headers, resolved=mr.resolved, warnings=mr.warnings,
                    missing=mr.missing, total_raw=len(fr.raw_rows), mapped=len(mr.rows), sample=sample, at=utcnow().isoformat())
        if mr.missing:
            p.error = "required column(s) not found: " + ", ".join(f"{m} -> '{s.orders_column_map.get(m)}'" for m in mr.missing)
    except Exception as e:  # noqa: BLE001
        p = Preview(ok=False, source=src.describe(), error=str(e), at=utcnow().isoformat())
    if settings is None:
        last_preview = p
    return p


async def run() -> SyncRun:
    s = get_settings()
    async with _lock:
        run_row = SyncRun(kind="orders", started_at=utcnow())
        src = get_source(s)
        run_row.source = src.describe()
        try:
            fr = await src.fetch()
            mr = map_rows(fr.raw_rows, s.orders_column_map)
            run_row.raw_headers = json.dumps(mr.headers, ensure_ascii=False)
            run_row.warnings = json.dumps(mr.warnings, ensure_ascii=False)
            run_row.total_rows = len(fr.raw_rows)
            run_row.accepted = len(mr.rows)
            run_row.rejected = mr.skipped
            if mr.missing:
                raise ValueError("required column(s) not found: " + ", ".join(f"{m} -> '{s.orders_column_map.get(m)}'" for m in mr.missing) + f". Headers seen: {mr.headers}")
            if not mr.rows:
                raise ValueError("source returned 0 usable rows; keeping last good cache")
            now = utcnow()
            async with session_scope() as db:
                await db.execute(delete(OrderCache))
                db.add_all(
                    OrderCache(so_no=r.so_no, po_no=r.po_no, fg_item_code=r.fg_item_code,
                               fg_description=r.fg_description, customer_name=r.customer_name,
                               connection_status=r.connection_status, real_status=r.real_status, fetched_at=now)
                    for r in mr.rows
                )
                run_row.ok = True
                run_row.finished_at = utcnow()
                db.add(run_row)
            log.info("order_refresh_done", rows=len(mr.rows), source=fr.description)
        except Exception as e:  # noqa: BLE001
            run_row.ok = False
            run_row.error = str(e)
            run_row.finished_at = utcnow()
            async with session_scope() as db:
                db.add(run_row)
            log.error("order_refresh_failed", error=str(e))
            await alerts.notify("Order refresh failed (last good cache kept)", str(e))
        return run_row


async def last_fetched_at():
    async with session_scope() as db:
        return await db.scalar(select(func.max(OrderCache.fetched_at)))


async def check_stale() -> bool:
    s = get_settings()
    last = await last_fetched_at()
    if last is None:
        return True
    stale = utcnow() - last > timedelta(minutes=s.orders_stale_minutes)
    if stale:
        await alerts.notify("Orders cache is stale", f"last fetch {last.isoformat()} (> {s.orders_stale_minutes} min)", level="warning")
    return stale


async def count() -> int:
    async with session_scope() as db:
        return (await db.scalar(select(func.count(OrderCache.id)))) or 0
