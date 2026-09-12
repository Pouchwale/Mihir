"""Common pieces for every orders source: the row shape and the column map."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

REQUIRED = ("so_no", "customer_name", "real_status")
OPTIONAL = ("po_no", "fg_item_code", "fg_description", "connection_status")
FIELDS = REQUIRED + OPTIONAL


@dataclass
class OrderRow:
    so_no: str
    customer_name: str
    real_status: str | None
    po_no: str | None = None
    fg_item_code: str | None = None
    fg_description: str | None = None
    connection_status: str | None = None


@dataclass
class FetchResult:
    raw_rows: list[dict]
    description: str  # e.g. "http GET https://... (json)"
    headers: list[str] = field(default_factory=list)


@dataclass
class MappedResult:
    rows: list[OrderRow]
    headers: list[str]
    resolved: dict[str, str]  # field -> actual header used
    warnings: list[str]
    missing: list[str]  # required fields with no header
    skipped: int = 0


class Source(Protocol):
    async def fetch(self) -> FetchResult: ...

    def describe(self) -> str: ...


def _norm_header(h: object) -> str:
    return re.sub(r"\s+", " ", str(h or "")).strip().casefold()


def cell_to_str(v: object, *, preserve: bool = False) -> str | None:
    """Excel/JSON cells to text. `preserve=True` keeps the string byte-exact (customer_name)."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int,)):
        return str(v)
    s = str(v)
    if preserve:
        return s
    return s.strip()


def resolve_headers(headers: list[str], column_map: dict[str, str]) -> tuple[dict[str, str], list[str], list[str]]:
    """field -> header. Exact match first, then case/space-insensitive with a warning."""
    resolved: dict[str, str] = {}
    warnings: list[str] = []
    missing: list[str] = []
    normed = {_norm_header(h): h for h in headers}
    for fld in FIELDS:
        wanted = column_map.get(fld)
        if not wanted:
            if fld in REQUIRED:
                missing.append(fld)
            continue
        if wanted in headers:
            resolved[fld] = wanted
            continue
        alt = normed.get(_norm_header(wanted))
        if alt is not None:
            resolved[fld] = alt
            warnings.append(f"column '{wanted}' matched '{alt}' case/space-insensitively")
        elif fld in REQUIRED:
            missing.append(fld)
        else:
            warnings.append(f"optional column '{wanted}' ({fld}) not found")
    return resolved, warnings, missing


def map_rows(raw_rows: list[dict], column_map: dict[str, str]) -> MappedResult:
    headers: list[str] = []
    seen = set()
    for r in raw_rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                headers.append(str(k))
    resolved, warnings, missing = resolve_headers(headers, column_map)
    rows: list[OrderRow] = []
    skipped = 0
    if missing:
        return MappedResult([], headers, resolved, warnings, missing, 0)
    for r in raw_rows:
        so = cell_to_str(r.get(resolved["so_no"]))
        name = cell_to_str(r.get(resolved["customer_name"]), preserve=True)
        if not so or name is None or name == "":
            skipped += 1
            continue
        rows.append(
            OrderRow(
                so_no=so,
                customer_name=name,
                real_status=cell_to_str(r.get(resolved["real_status"])),
                po_no=cell_to_str(r.get(resolved["po_no"])) if "po_no" in resolved else None,
                fg_item_code=cell_to_str(r.get(resolved["fg_item_code"])) if "fg_item_code" in resolved else None,
                fg_description=cell_to_str(r.get(resolved["fg_description"])) if "fg_description" in resolved else None,
                connection_status=cell_to_str(r.get(resolved["connection_status"])) if "connection_status" in resolved else None,
            )
        )
    if skipped:
        warnings.append(f"{skipped} row(s) skipped: empty SO or customer name")
    return MappedResult(rows, headers, resolved, warnings, missing, skipped)
