import pytest
import respx
from httpx import Response

from app.adapters import HttpSource, map_rows
from app.adapters.orders_base import resolve_headers
from app.adapters.parsers import detect_format, parse
from app.config import DEFAULT_COLUMN_MAP, get_settings
from tests.conftest import FIXTURES as FX  # generated dummy orders, never the owner's live files


def _load(fmt: str):
    body = (FX / f"orders_dummy.{fmt}").read_bytes()
    return body


@pytest.mark.parametrize("fmt", ["json", "csv", "xlsx", "html"])
def test_all_formats_identical(fmt):
    ref = map_rows(parse(_load("json"), "json"), DEFAULT_COLUMN_MAP).rows
    body = _load(fmt)
    detected = detect_format(None, f"orders.{fmt}", body)
    assert detected == fmt
    got = map_rows(parse(body, detected), DEFAULT_COLUMN_MAP)
    assert not got.missing
    assert [r.__dict__ for r in got.rows] == [r.__dict__ for r in ref]
    assert len(got.rows) == 10
    # trailing space preserved through every parser
    assert any(r.customer_name == "Patel Agro Industries " for r in got.rows)


def test_detect_by_content_type_and_sniff():
    assert detect_format("application/json; charset=utf-8", None, b"") == "json"
    assert detect_format("text/csv", None, b"") == "csv"
    assert detect_format("text/html", None, b"") == "html"
    assert detect_format("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", None, b"") == "xlsx"
    assert detect_format(None, None, b"  [{\"a\":1}]") == "json"
    assert detect_format(None, None, b"<html><table>") == "html"
    assert detect_format(None, None, b"PK\x03\x04") == "xlsx"
    assert detect_format(None, None, b"a,b\n1,2") == "csv"


def test_json_shapes():
    assert len(parse(b'[{"SO No":"1","Customer Name":"A","Real Status (PPC)":"x"}]', "json")) == 1
    assert len(parse(b'{"result":[{"SO No":"1"}]}', "json")) == 1
    assert len(parse(b'{"d":{"results":[{"SO No":"1"}]}}', "json")) == 1
    rows = parse(b'[["SO No","Customer Name"],["1","A"]]', "json")
    assert rows == [{"SO No": "1", "Customer Name": "A"}]
    rows = parse(b'{"SO No":["1","2"],"Customer Name":["A","B"]}', "json")
    assert rows[1] == {"SO No": "2", "Customer Name": "B"}


def test_header_fallback_and_missing():
    headers = ["so no", "customer name", "REAL STATUS (PPC)"]
    resolved, warnings, missing = resolve_headers(headers, DEFAULT_COLUMN_MAP)
    assert not missing and resolved["so_no"] == "so no" and warnings
    resolved, warnings, missing = resolve_headers(["Order", "Name"], DEFAULT_COLUMN_MAP)
    assert set(missing) == {"so_no", "customer_name", "real_status"}
    mr = map_rows([{"Order": "1"}], DEFAULT_COLUMN_MAP)
    assert mr.missing and mr.rows == []


def test_numeric_cells_become_clean_strings():
    mr = map_rows([{"SO No": 45231.0, "Customer Name": "A ", "Real Status (PPC)": " x "}], DEFAULT_COLUMN_MAP)
    assert mr.rows[0].so_no == "45231"
    assert mr.rows[0].customer_name == "A "  # never trimmed
    assert mr.rows[0].real_status == "x"


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["header", "query", "bearer"])
async def test_http_source_api_key_placement(where, monkeypatch):
    monkeypatch.setenv("ORDERS_SOURCE", "http")
    monkeypatch.setenv("ORDERS_API_URL", "https://api.example.com/orders")
    monkeypatch.setenv("ORDERS_API_KEY", "sekret")
    monkeypatch.setenv("ORDERS_API_KEY_IN", where)
    monkeypatch.setenv("ORDERS_API_KEY_NAME", "X-API-Key" if where == "header" else "apikey")
    get_settings.cache_clear()
    try:
        s = get_settings()
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get("https://api.example.com/orders").mock(return_value=Response(200, content=_load("csv"), headers={"content-type": "text/csv"}))
            fr = await HttpSource(s).fetch()
        req = route.calls.last.request
        if where == "header":
            assert req.headers["X-API-Key"] == "sekret"
        elif where == "bearer":
            assert req.headers["Authorization"] == "Bearer sekret"
        else:
            assert "apikey=sekret" in str(req.url)
        assert len(fr.raw_rows) == 10 and "(csv" in fr.description
    finally:
        get_settings.cache_clear()
