"""Generate the dummy fixtures: 10 customers (xlsx) + 10 orders (json/csv/xlsx/html) + placeholder voice note.

Run:  python scripts/make_fixtures.py
"""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent.parent
FX = ROOT / "fixtures"
FX.mkdir(exist_ok=True)

# ---- customers: SAP B1 export layout ----
CUSTOMER_HEADERS = ["Customer Code", "Customer Name", "Contact", "Email", "Active"]
CUSTOMERS = [
    ("C001", "Shree Packaging Pvt Ltd", "9167861236", "shree@example.com", "Y"),      # plain 10 digits
    ("C002", "Mehta Foods", "+91 98765-43210", "mehta@example.com", "Y"),             # +91 with dashes
    ("C003", "Gujarat Polymers", "09898012345", "gp@example.com", "Y"),               # leading 0
    ("C004", "Patel Agro Industries", "91 9925001122", "patel@example.com", "Y"),     # 91 prefix + space
    ("C005", "Sunrise Pharma", "9033445566", "sun@example.com", "Y"),
    ("C006", "Royal Textiles", "+919712345678", "royal@example.com", "Y"),
    ("C007", "Anand Dairy Products", "9081726354", "anand@example.com", "Y"),         # no orders -> not found
    ("C008", "Om Snacks", "9427000111", "om@example.com", "Y"),                       # no orders -> not found
    ("C009", "Bad Number Co", "98765", "bad@example.com", "Y"),                       # invalid -> rejected
    ("C010", "Duplicate Co", "9167861236", "dup@example.com", "Y"),                   # duplicate of C001 -> rejected
]

# ---- orders: whole table as the external API would return it ----
ORDER_HEADERS = ["SO No", "PO No", "FG Item Code", "FG Description", "Customer Name", "Connection Status",
                 "Real Status (PPC)"]
# The description is what the item IS, in the customer's words - an item code means nothing to them.
ORDERS = [
    ("45231", "PO-7781", "FG-1001", "Stand-up pouch 500g, matte, zipper", "Shree Packaging Pvt Ltd", "CONN-PLANT-A-OK", "In Production"),
    ("45232", "PO-7782", "FG-1002", "Stand-up pouch 1kg, gloss", "Shree Packaging Pvt Ltd", "CONN-PLANT-A-OK", "Dispatched"),
    ("45240", "PO-8801", "FG-2001", "Spice pouch 200g, matte", "Mehta Foods", "CONN-PLANT-B-OK", "Ready for Dispatch"),
    ("45240", "PO-8801", "FG-2002", "Spice pouch 500g, gloss", "Mehta Foods", "CONN-PLANT-B-OK", "Printing"),
    ("45240", "PO-8801", "FG-2003", "Namkeen pouch 1kg, metallised", "Mehta Foods", "CONN-PLANT-B-DELAY", "Awaiting Material"),
    ("45250", "PO-9901", "FG-3001", "Shrink sleeve, 500ml bottle", "Gujarat Polymers", "CONN-PLANT-A-OK", "Lamination"),
    ("45250", "PO-9901", "FG-3002", "Shrink sleeve, 1L bottle", "Gujarat Polymers", "CONN-PLANT-A-OK", "Slitting"),
    ("45260", "PO-5555", "FG-4001", "Fertiliser bag 5kg, BOPP", "Patel Agro Industries ", "CONN-PLANT-C-OK", "Cylinder Making"),   # trailing space -> mismatch
    ("45270", "PO-6666", "FG-5001", "Tablet strip label, 40x60mm", "SUNRISE PHARMA", "CONN-PLANT-C-OK", "Quality Check"),          # case differs -> mismatch
    ("45280", "PO-7777", "FG-6001", "Garment label roll, satin", "Royal Textiles", "CONN-PLANT-B-OK", "Delivered"),
]


def write_customers(path: Path | None = None) -> Path:
    path = path or FX / "customers_dummy.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Customers"
    ws.append(CUSTOMER_HEADERS)
    for row in CUSTOMERS:
        ws.append(list(row))
    wb.save(path)
    return path


def write_orders(out_dir: Path | None = None) -> Path:
    out = out_dir or FX
    out.mkdir(parents=True, exist_ok=True)
    rows = [dict(zip(ORDER_HEADERS, r)) for r in ORDERS]
    (out / "orders_dummy.json").write_text(json.dumps({"data": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    # QUOTE_ALL so the deliberate trailing space in "Patel Agro Industries " sits inside quotes and
    # survives editors that strip trailing whitespace on save.
    with (out / "orders_dummy.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(ORDER_HEADERS)
        w.writerows(ORDERS)
    wb = Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(ORDER_HEADERS)
    for r in ORDERS:
        ws.append(list(r))
    wb.save(out / "orders_dummy.xlsx")
    th = "".join(f"<th>{html.escape(h)}</th>" for h in ORDER_HEADERS)
    trs = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>" for r in ORDERS)
    (out / "orders_dummy.html").write_text(
        f"<!doctype html><html><body><h1>Order Status Report</h1><table border=1><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></body></html>",
        encoding="utf-8",
    )
    return out


def write_voice() -> None:
    # Minimal OGG page header. Not a playable file; only used by the mocked WATI getMedia in dev
    # (STT is also mocked without a Groq key, so content does not matter).
    p = FX / "voice_sample.ogg"
    if not p.exists():
        p.write_bytes(b"OggS" + b"\x00" * 60)


if __name__ == "__main__":
    write_customers()
    write_orders()
    write_voice()
    print("fixtures written to", FX)
