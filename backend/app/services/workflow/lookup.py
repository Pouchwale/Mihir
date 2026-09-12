"""Checking a customer's answer against the business's own data: an SO number, a PO number, an item
code or their customer code.

Exactly the rules the order-status bot uses, because it is the same data: a number that is not in
the customer list has nothing to check, orders are matched on the byte-exact company name from the
customer Excel, and only the real status is ever handed back - never the internal connection status,
and never another company's order, whatever was typed.
"""
from __future__ import annotations

from ..intent import regex_parse
from ..menus import so_sort_key
from ..verify import customer_orders, filter_fg, find_customer, lookup_orders
from .schema import DATA_FIELDS, DATA_OPS, DATA_SOURCES, LOOKUP_SOURCES


async def find(db, phone: str, source: str, value: str) -> dict[str, str] | None:
    """What the answer refers to, or None when it is not this customer's. The dict's "value" is the
    code as the business writes it; the rest are saved beside the answer ({order_status}, ...)."""
    if source not in LOOKUP_SOURCES or not phone:
        return None
    customer = await find_customer(db, phone)
    if customer is None:
        return None
    said = (value or "").strip()
    if not said:
        return None
    parsed = regex_parse(said, use_labels=False)
    bare = parsed.bare_codes[0] if parsed.bare_codes else ""

    if source in ("so", "po"):
        code = (parsed.so_no if source == "so" else parsed.po_no) or bare or said
        found = await lookup_orders(db, customer, **({"so_no": code} if source == "so" else {"po_no": code}))
        if found.kind != "ok" or not found.rows:
            return None
        return _describe(found.rows, found.so_no if source == "so" else (found.rows[0].po_no or code))

    if source == "fg":
        code = parsed.fg_code or bare or said
        rows = filter_fg(await customer_orders(db, customer), code)
        return _describe(rows, rows[0].fg_item_code or code) if rows else None

    # customer_code: the code on file for THIS number, so it proves who is writing
    mine = _norm(customer.customer_code)
    if mine and _norm(said) == mine:
        return {"value": customer.customer_code or said, "name": customer.customer_name}
    return None


def _describe(rows, value) -> dict[str, str]:
    items = list(dict.fromkeys(r.fg_item_code for r in rows if r.fg_item_code))
    if len(rows) == 1:
        status = rows[0].real_status or ""
    else:
        status = "; ".join(f"{r.fg_item_code or r.so_no}: {r.real_status or '-'}" for r in rows[:10])
    return {"value": str(value), "status": status, "so": rows[0].so_no or "", "po": rows[0].po_no or "",
            "items": ", ".join(items), "count": str(len(rows))}


def _norm(code: str | None) -> str:
    return "".join((code or "").split()).casefold()


# ---------------- "Find in your data" ----------------
async def find_rows(db, phone: str, spec: dict) -> list[dict[str, str]] | None:
    """The rows a data step asked for - always this customer's own, never anybody else's.

    None means this number is not in the customer list at all; [] means nothing matched. Both take
    the step's "Nothing found" exit; the difference only matters to the log."""
    source = str(spec.get("source") or "")
    if source not in DATA_SOURCES or not phone:
        return None
    customer = await find_customer(db, phone)
    if customer is None:
        return None
    if source == "customer":
        rows = [{"customer_code": customer.customer_code or "", "customer_name": customer.customer_name or ""}]
    else:
        rows = _group(await _matching(db, customer, spec), str(spec.get("group") or "so"))
    kept = [r for r in rows if _passes(r, spec.get("filter") or [])]
    return _sorted(kept, str(spec.get("sort") or "newest"))


async def _matching(db, customer, spec: dict) -> list:
    """The order rows to work from: all of this customer's, or the ones a code points at."""
    find = str(spec.get("find") or "all")
    said = str(spec.get("match") or "").strip()
    if find == "all":
        return await customer_orders(db, customer)
    if not said:
        return []
    parsed = regex_parse(said, use_labels=False)
    bare = parsed.bare_codes[0] if parsed.bare_codes else ""
    if find in ("so", "po"):
        code = (parsed.so_no if find == "so" else parsed.po_no) or bare or said
        found = await lookup_orders(db, customer, **({"so_no": code} if find == "so" else {"po_no": code}))
        return found.rows if found.kind == "ok" else []
    if find == "fg":
        return filter_fg(await customer_orders(db, customer), parsed.fg_code or bare or said)
    return []


def _project(row) -> dict[str, str]:
    """One order row, cut down to what a customer may see.

    connection_status is not here and must never be added: it is the internal status, and this is the
    only door between the order table and a workflow."""
    return {f: str(getattr(row, f, "") or "") for f in DATA_FIELDS["orders"]}


def _group(rows: list, how: str) -> list[dict[str, str]]:
    """One row per order (the default, as the order-status bot lists them), or one per order line."""
    if how == "line":
        return [_project(r) for r in rows]
    by_so: dict[str, list] = {}
    for r in rows:
        by_so.setdefault(r.so_no, []).append(r)
    out = []
    for lines in by_so.values():
        row = _project(lines[0])
        items = list(dict.fromkeys(x.fg_item_code for x in lines if x.fg_item_code))
        statuses = list(dict.fromkeys(x.real_status for x in lines if x.real_status))
        row["fg_item_code"] = ", ".join(items)
        row["real_status"] = statuses[0] if len(statuses) == 1 else "; ".join(statuses[:3])
        out.append(row)
    return out


def _passes(row: dict, filters) -> bool:
    for f in filters:
        if not isinstance(f, dict):
            continue
        op = str(f.get("op") or "eq")
        if op not in DATA_OPS:
            continue
        got = str(row.get(str(f.get("field") or ""), "")).strip().casefold()
        want = str(f.get("value") or "").strip().casefold()
        if ((op == "eq" and got != want) or (op == "ne" and got == want)
                or (op == "contains" and want not in got)
                or (op == "is_set" and not got) or (op == "is_empty" and got)):
            return False
    return True


def _sorted(rows: list[dict[str, str]], how: str) -> list[dict[str, str]]:
    """Newest first by SO number - the same order the order-status bot lists orders in."""
    if how == "as_is":
        return rows
    out = sorted(rows, key=lambda r: so_sort_key(str(r.get("so_no") or "")))
    return out if how == "newest" else list(reversed(out))
