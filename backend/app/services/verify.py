"""Verification logic, spec section 5.4.

1. customer = customers.find(phone_e164 = waId)            none -> verify_failed
2. rows = orders_cache.where(so_no = SO) [or po_no = PO]   none -> not_found
3. keep rows where row.customer_name == customer.customer_name (byte-exact)
                                                            none -> log mismatch, verify_failed
4. one row -> real_status ; many -> ask FG, later filter by fg_item_code == given
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Customer, NameMismatchLog, NewCustomer, OrderCache

LookupKind = Literal["ok", "not_found", "mismatch"]


@dataclass
class Lookup:
    kind: LookupKind
    rows: list[OrderCache] = field(default_factory=list)
    so_no: str | None = None  # resolved SO (from PO if needed)


async def find_customer(db: AsyncSession, phone_e164: str) -> Customer | None:
    return await db.get(Customer, phone_e164)


async def customer_orders(db: AsyncSession, customer: Customer) -> list[OrderCache]:
    """All cached rows whose API customer name is byte-identical to the customer's Excel name.
    SQL equality may be collation-lenient on MySQL (case / trailing space), so re-filter in Python."""
    rows = (await db.execute(select(OrderCache).where(OrderCache.customer_name == customer.customer_name))).scalars()
    return [r for r in rows if name_matches(customer.customer_name, r.customer_name)]


def name_matches(excel_name: str | None, api_name: str | None) -> bool:
    """Strict byte-for-byte equality. No trim, no case fold."""
    if excel_name is None or api_name is None:
        return False
    return excel_name.encode("utf-8") == api_name.encode("utf-8")


async def lookup_orders(
    db: AsyncSession,
    customer: Customer,
    so_no: str | None = None,
    po_no: str | None = None,
) -> Lookup:
    if not so_no and not po_no:
        return Lookup("not_found")
    col, given, prefix = (OrderCache.so_no, so_no, "SO") if so_no else (OrderCache.po_no, po_no, "PO")
    rows = list((await db.execute(select(OrderCache).where(col == given))).scalars())
    if not rows:
        # lenient key match: ignore case, spaces, dashes and an optional SO/PO prefix ("po-7781" == "PO7781" == "7781")
        variants = key_variants(given, prefix)
        norm_col = func.upper(func.replace(func.replace(func.replace(col, "-", ""), " ", ""), "/", ""))
        rows = list((await db.execute(select(OrderCache).where(norm_col.in_(variants)))).scalars())
    if not rows:
        return Lookup("not_found")

    kept = [r for r in rows if name_matches(customer.customer_name, r.customer_name)]
    if not kept:
        # log every distinct API name we saw for this SO
        seen: set[str] = set()
        for r in rows:
            if r.customer_name in seen:
                continue
            seen.add(r.customer_name)
            db.add(
                NameMismatchLog(
                    phone_e164=customer.phone_e164,
                    excel_name=customer.customer_name,
                    api_name=r.customer_name,
                    so_no=r.so_no,
                )
            )
        await db.flush()
        return Lookup("mismatch", rows=[], so_no=rows[0].so_no)

    resolved_so = kept[0].so_no
    # PO may map to several SOs; keep the rows of the first SO deterministically
    if not so_no:
        kept = [r for r in kept if r.so_no == resolved_so]
    return Lookup("ok", rows=kept, so_no=resolved_so)


def _norm_key(s: str | None) -> str:
    return re.sub(r"[\s\-/]", "", (s or "")).upper()


def key_variants(given: str, prefix: str) -> list[str]:
    """Normalized forms to compare against: with and without the type prefix."""
    n = _norm_key(given)
    bare = n[len(prefix):] if n.startswith(prefix) else n
    return sorted({n, bare, prefix + bare} - {""})


def filter_fg(rows: list[OrderCache], fg_code: str) -> list[OrderCache]:
    """Exact match first (spec); then lenient (case / dash / 'FG' prefix insensitive).
    Rows are already restricted to the verified customer's own SO, so leniency here is safe."""
    exact = [r for r in rows if (r.fg_item_code or "") == fg_code]
    if exact:
        return exact
    variants = set(key_variants(fg_code, "FG"))
    return [r for r in rows if _norm_key(r.fg_item_code) in variants]


async def find_new_customer(db: AsyncSession, phone_e164: str) -> NewCustomer | None:
    """A number that signed up through a workflow (Data -> New customers). It never grants access to
    orders: those are matched only against the customer Excel."""
    return await db.get(NewCustomer, phone_e164)


_NAME_KEYS = ("company", "company_name", "business", "business_name", "firm", "name", "full_name")


def new_customer_name(row: NewCustomer) -> str:
    """The name to greet a new customer by: what they told the workflow, else their WhatsApp name."""
    try:
        details = json.loads(row.details or "{}")
    except ValueError:
        details = {}
    if isinstance(details, dict):
        for key in _NAME_KEYS:
            got = str(details.get(key) or "").strip()
            if got:
                return got[:120]
    return (row.whatsapp_name or "").strip()
