"""Customer sync: SAP B1 Excel -> normalize -> replace `customers`.

The Excel can come from a folder / network share, Dropbox, or an HTTPS link. Which one is used is
set in the dashboard (Imports -> Customer Excel) and stored in app_settings.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import structlog
from sqlalchemy import delete, func, select

# _norm_header is shared on purpose: "Customer  Name" must match a header the same way here as it
# does for the order table, or the two pages would disagree about what counts as the same column.
from ..adapters.orders_base import _norm_header
from ..adapters.parsers import excel_table
from ..config import get_settings
from ..db import session_scope
from ..models import Customer, SyncRun, utcnow
from ..services import alerts
from ..utils.phone import normalize_phone

log = structlog.get_logger(__name__)

_lock = asyncio.Lock()


# ---------------- fetching ----------------
def _download_dropbox(s) -> bytes:
    import dropbox

    dbx = dropbox.Dropbox(app_key=s.dropbox_app_key, app_secret=s.dropbox_app_secret, oauth2_refresh_token=s.dropbox_refresh_token)
    _, res = dbx.files_download(s.dropbox_file_path)
    return res.content


async def _download_url(s) -> bytes:
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if s.customers_url_key:
        if s.customers_url_key_in == "bearer":
            headers["Authorization"] = f"Bearer {s.customers_url_key}"
        elif s.customers_url_key_in == "query":
            params[s.customers_url_key_name or "apikey"] = s.customers_url_key
        else:
            headers[s.customers_url_key_name or "X-API-Key"] = s.customers_url_key
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        r = await client.get(s.customers_url, headers=headers, params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"download failed with {r.status_code}: {r.text[:200]}")
    return r.content


async def fetch_excel(s) -> tuple[bytes, str]:
    """Downloads the customer Excel from wherever it is configured. Returns (bytes, description)."""
    source = s.customers_source_effective
    if source == "dropbox":
        if not s.dropbox_configured:
            raise RuntimeError("Dropbox is selected but the app key / secret / refresh token are not all set")
        return await asyncio.to_thread(_download_dropbox, s), f"dropbox {s.dropbox_file_path}"
    if source == "url":
        if not s.customers_url:
            raise RuntimeError("A download link is selected but no URL is set")
        return await _download_url(s), f"link {s.customers_url.split('?')[0]}"
    path = s.resolve_path(s.customers_file_path)
    if not path.exists():
        raise RuntimeError(f"file not found: {path}")
    return await asyncio.to_thread(path.read_bytes), f"file {path}"


# ---------------- parsing ----------------
def _resolve(headers: list[str], s) -> tuple[dict[str, str], list[str]]:
    """Which header holds the phone, the name and (if there is one) the customer code.

    The phone and the name are not optional - without them there is nobody to answer and no name to
    match orders on. The code is: plenty of customer lists have none, and refusing to import 500
    customers over a column the bot never needs helps nobody. A code column that is named but not in
    the file is a warning, so the import still happens and the owner is told what was ignored."""
    normed = {_norm_header(h): h for h in headers}

    def find(wanted: str) -> str:
        if not wanted:
            return ""
        return wanted if wanted in headers else normed.get(_norm_header(wanted), "")

    cols = {"contact": find(s.customers_col_contact), "name": find(s.customers_col_name),
            "code": find(s.customers_col_code)}
    warnings: list[str] = []
    missing = [wanted for key, wanted in (("contact", s.customers_col_contact), ("name", s.customers_col_name))
               if not cols[key]]
    if missing:
        raise ValueError("missing column(s): " + ", ".join(missing) + f". Headers seen: {headers}")
    for key, wanted in (("contact", s.customers_col_contact), ("name", s.customers_col_name)):
        if cols[key] != wanted:
            warnings.append(f"column '{wanted}' matched '{cols[key]}' case/space-insensitively")
    if s.customers_col_code and not cols["code"]:
        warnings.append(f"optional column '{s.customers_col_code}' (customer code) not found")
    return cols, warnings


def parse_customers(body: bytes, s=None) -> tuple[list[Customer], list[dict], list[str], list[str]]:
    """Returns (accepted, rejected_rows, headers, warnings)."""
    s = s or get_settings()
    raw = excel_table.parse(body, sheet=s.customers_sheet or None)
    headers: list[str] = []
    for r in raw:
        for k in r:
            if k not in headers:
                headers.append(k)
    cols, warnings = _resolve(headers, s)
    accepted: list[Customer] = []
    rejected: list[dict] = []
    seen: dict[str, str] = {}
    for i, r in enumerate(raw, start=2):  # Excel row number (1 = header)
        raw_contact = r.get(cols["contact"])
        name = r.get(cols["name"])
        code = r.get(cols["code"]) if cols["code"] else None
        name_s = None if name is None else str(name)  # EXACT, no strip
        if isinstance(code, float) and code.is_integer():
            code = int(code)
        code_s = None if code is None else str(code).strip()
        pr = normalize_phone(raw_contact)
        if not pr.ok:
            rejected.append({"row": i, "contact": str(raw_contact), "name": name_s, "code": code_s, "reason": f"invalid phone: {pr.reason}"})
            continue
        if not name_s or not name_s.strip():
            rejected.append({"row": i, "contact": str(raw_contact), "name": name_s, "code": code_s, "reason": "empty customer name"})
            continue
        if pr.phone in seen:
            rejected.append({"row": i, "contact": str(raw_contact), "name": name_s, "code": code_s, "reason": f"duplicate phone {pr.phone} (already used by '{seen[pr.phone]}')"})
            continue
        seen[pr.phone] = name_s
        accepted.append(Customer(phone_e164=pr.phone, customer_code=code_s, customer_name=name_s, raw_contact=str(raw_contact)))
    return accepted, rejected, headers, warnings


# ---------------- run ----------------
async def run(file_bytes: bytes | None = None, source_desc: str | None = None) -> SyncRun:
    s = get_settings()
    async with _lock:
        run_row = SyncRun(kind="customers", started_at=utcnow())
        try:
            if file_bytes is None:
                file_bytes, source_desc = await fetch_excel(s)
            run_row.source = source_desc
            accepted, rejected, headers, warnings = await asyncio.to_thread(parse_customers, file_bytes, s)
            run_row.total_rows = len(accepted) + len(rejected)
            run_row.accepted = len(accepted)
            run_row.rejected = len(rejected)
            run_row.rejected_rows = json.dumps(rejected, ensure_ascii=False)
            run_row.raw_headers = json.dumps(headers, ensure_ascii=False)
            run_row.warnings = json.dumps(warnings, ensure_ascii=False)
            if not accepted:
                raise ValueError("no valid customer rows; keeping existing customers table")
            async with session_scope() as db:
                await db.execute(delete(Customer))
                db.add_all(accepted)
                run_row.ok = True
                run_row.finished_at = utcnow()
                db.add(run_row)
            if rejected:
                await alerts.notify("Customer import: rejected rows", json.dumps(rejected, ensure_ascii=False)[:1500], level="warning")
            log.info("customer_sync_done", accepted=len(accepted), rejected=len(rejected), source=source_desc)
        except Exception as e:  # noqa: BLE001
            run_row.ok = False
            run_row.error = str(e)
            run_row.finished_at = utcnow()
            async with session_scope() as db:
                db.add(run_row)
            log.error("customer_sync_failed", error=str(e))
            await alerts.notify("Customer sync failed", str(e))
        return run_row


async def test_connection(s) -> dict:
    """Download + parse without writing anything. Used by the Test button in the dashboard."""
    try:
        body, desc = await fetch_excel(s)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "source": "", "error": str(e), "headers": [], "sample": [], "accepted": 0, "rejected": 0, "rejected_rows": []}
    try:
        accepted, rejected, headers, warnings = await asyncio.to_thread(parse_customers, body, s)
    except Exception as e:  # noqa: BLE001
        # still show the headers we found, so the column names can be fixed
        try:
            raw = excel_table.parse(body, sheet=s.customers_sheet or None)
            headers = list(raw[0].keys()) if raw else []
        except Exception:  # noqa: BLE001
            headers = []
        return {"ok": False, "source": desc, "error": str(e), "headers": headers, "sample": [], "accepted": 0, "rejected": 0, "rejected_rows": []}
    return {
        "ok": True,
        "source": desc,
        "error": None,
        "headers": headers,
        "warnings": warnings,
        "accepted": len(accepted),
        "rejected": len(rejected),
        "rejected_rows": rejected[:20],
        "sample": [{"phone": c.phone_e164, "code": c.customer_code, "name": c.customer_name, "raw_contact": c.raw_contact} for c in accepted[:5]],
    }


async def count() -> int:
    async with session_scope() as db:
        return (await db.scalar(select(func.count(Customer.phone_e164)))) or 0
