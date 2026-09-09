"""Data connections configured from the dashboard: storage, encryption, validation, live testing."""
import json

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import session_scope
from app.jobs import customer_sync
from app.main import app
from app.models import AppSetting, Customer
from app.services import crypto
from app.services import settings_store as store

H = {"X-Admin-Key": "test-admin"}
from tests.conftest import BACKEND, CUSTOMERS_XLSX as _CX, FIXTURES as _FX

# Generated dummy data (tests/_generated), never backend/fixtures - those are the owner's to edit.
FIXTURES = _FX.relative_to(BACKEND).as_posix()
CUSTOMERS_XLSX = _CX.relative_to(BACKEND).as_posix()


@pytest.fixture
async def clean_settings():
    async def wipe():
        async with session_scope() as db:
            await db.execute(delete(AppSetting))
        await store.load_from_db()

    await wipe()
    yield
    await wipe()


# ---------------- encryption ----------------
def test_secret_roundtrip_and_masking():
    token = "sk-live-abcdef123456"
    blob = crypto.encrypt(token)
    assert blob.startswith("enc:") and token not in blob
    assert crypto.decrypt(blob) == token
    assert crypto.decrypt("") == "" and crypto.encrypt("") == ""
    assert crypto.decrypt("plain-legacy-value") == "plain-legacy-value"
    assert crypto.decrypt("enc:not-a-real-token") == ""  # never raises
    assert crypto.mask(token).endswith("3456") and token[:6] not in crypto.mask(token)
    assert crypto.mask("short") == "••••" and crypto.mask("") == ""


# ---------------- store ----------------
@pytest.mark.asyncio
async def test_saved_values_override_env_and_survive_reload(clean_settings):
    assert get_settings().order_refresh_minutes == 5
    assert await store.save({"order_refresh_minutes": 2, "orders_api_key": "sekret", "orders_source": "http", "orders_api_url": "https://ppc.example.com/orders"}) == {}
    assert get_settings().order_refresh_minutes == 2
    assert get_settings().orders_api_key == "sekret"

    # the key is encrypted on disk, and comes back after a fresh load
    async with session_scope() as db:
        row = (await db.execute(select(AppSetting).where(AppSetting.key == "orders_api_key"))).scalar_one()
    assert row.is_secret and "sekret" not in row.value
    await store.load_from_db()
    assert get_settings().orders_api_key == "sekret"

    # resetting brings the .env value back
    await store.reset(["order_refresh_minutes"])
    assert get_settings().order_refresh_minutes == 5
    assert get_settings().orders_api_key == "sekret"


@pytest.mark.asyncio
async def test_only_allowlisted_keys_can_be_written(clean_settings):
    errors = await store.save({"admin_key": "hacked", "wati_token": "x", "database_url": "y"})
    assert set(errors) == {"admin_key", "wati_token", "database_url"}
    assert get_settings().admin_key == "test-admin"
    assert "admin_key" not in store.FIELDS


@pytest.mark.asyncio
async def test_validation(clean_settings):
    assert "order_refresh_minutes" in await store.save({"order_refresh_minutes": 0})
    assert "order_refresh_minutes" in await store.save({"order_refresh_minutes": "abc"})
    assert "orders_source" in await store.save({"orders_source": "ftp"})
    assert "orders_api_url" in await store.save({"orders_source": "http", "orders_api_url": "  "})
    assert "customers_url" in await store.save({"customers_source": "url", "customers_url": ""})
    assert "dropbox_app_key" in await store.save({"customers_source": "dropbox"})
    bad_map = await store.save({"orders_column_map": {"so_no": "SO", "customer_name": ""}})
    assert "orders_column_map" in bad_map
    # a good column map is kept, unknown fields inside it dropped
    assert await store.save({"orders_column_map": {"so_no": "A", "customer_name": "B", "real_status": "C", "junk": "D"}}) == {}
    assert get_settings().orders_column_map == {"so_no": "A", "customer_name": "B", "real_status": "C"}
    # nothing was written for the rejected saves
    assert get_settings().order_refresh_minutes == 5


@pytest.mark.asyncio
async def test_public_view_never_leaks_a_password(clean_settings):
    await store.save({"orders_api_key": "super-secret-value", "dropbox_refresh_token": "drop-token-1234"})
    view = await store.public_view()
    blob = json.dumps(view)
    assert "super-secret-value" not in blob and "drop-token-1234" not in blob
    assert view["orders_api_key"]["is_set"] is True and view["orders_api_key"]["secret"] is True
    assert view["orders_api_key"]["value"].startswith("••••")
    assert view["orders_api_url"]["secret"] is False


# ---------------- customer Excel sources ----------------
@pytest.mark.asyncio
async def test_customer_excel_from_local_file(clean_settings):
    s = get_settings().model_copy(update={"customers_source": "local", "customers_file_path": CUSTOMERS_XLSX})
    body, desc = await customer_sync.fetch_excel(s)
    assert body[:2] == b"PK" and desc.startswith("file ")
    r = await customer_sync.test_connection(s)
    assert r["ok"] and r["accepted"] == 8 and r["rejected"] == 2
    assert "Customer Name" in r["headers"] and r["sample"][0]["phone"].startswith("91")


@pytest.mark.asyncio
async def test_customer_excel_from_https_link(clean_settings):
    xlsx = (get_settings().resolve_path(CUSTOMERS_XLSX)).read_bytes()
    s = get_settings().model_copy(update={
        "customers_source": "url", "customers_url": "https://sp.example.com/customers.xlsx",
        "customers_url_key": "k1", "customers_url_key_in": "header", "customers_url_key_name": "X-API-Key",
    })
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://sp.example.com/customers.xlsx").mock(return_value=Response(200, content=xlsx))
        r = await customer_sync.test_connection(s)
    assert route.calls.last.request.headers["X-API-Key"] == "k1"
    assert r["ok"] and r["accepted"] == 8 and r["source"].startswith("link ")

    with respx.mock:
        respx.get("https://sp.example.com/customers.xlsx").mock(return_value=Response(404, text="nope"))
        bad = await customer_sync.test_connection(s)
    assert bad["ok"] is False and "404" in bad["error"]


@pytest.mark.asyncio
async def test_customer_excel_errors_are_explained(clean_settings):
    s = get_settings().model_copy(update={"customers_source": "local", "customers_file_path": "fixtures/does_not_exist.xlsx"})
    r = await customer_sync.test_connection(s)
    assert r["ok"] is False and "file not found" in r["error"]

    s = get_settings().model_copy(update={"customers_source": "dropbox", "dropbox_app_key": "", "dropbox_app_secret": "", "dropbox_refresh_token": ""})
    r = await customer_sync.test_connection(s)
    assert r["ok"] is False and "Dropbox" in r["error"]

    # wrong column name -> the headers that WERE found are reported so it can be fixed
    s = get_settings().model_copy(update={"customers_source": "local", "customers_file_path": CUSTOMERS_XLSX, "customers_col_name": "Party Name"})
    r = await customer_sync.test_connection(s)
    assert r["ok"] is False and "Party Name" in r["error"] and "Customer Name" in r["headers"]


@pytest.mark.asyncio
async def test_saved_source_is_used_by_the_real_import(clean_settings):
    xlsx = (get_settings().resolve_path(CUSTOMERS_XLSX)).read_bytes()
    assert await store.save({"customers_source": "url", "customers_url": "https://sp.example.com/c.xlsx"}) == {}
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://sp.example.com/c.xlsx").mock(return_value=Response(200, content=xlsx))
        run = await customer_sync.run()
    assert run.ok and run.accepted == 8 and run.source.startswith("link ")
    async with session_scope() as db:
        assert (await db.execute(select(Customer))).scalars().all()


# ---------------- API ----------------
@pytest.mark.asyncio
async def test_connections_api(clean_settings):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/api/connections")).status_code == 401

        got = (await c.get("/admin/api/connections", headers=H)).json()
        assert got["fields"]["orders_source"]["value"] == "file"
        assert got["fields"]["orders_api_key"]["secret"] is True
        assert got["required_columns"] == ["so_no", "customer_name", "real_status"]

        r = (await c.put("/admin/api/connections", headers=H, json={"values": {"order_refresh_minutes": 3, "orders_api_key": "abc12345678"}})).json()
        assert r["ok"] is True and r["fields"]["orders_api_key"]["value"].startswith("••••")
        assert get_settings().order_refresh_minutes == 3

        r = (await c.put("/admin/api/connections", headers=H, json={"values": {"order_refresh_minutes": 99999}})).json()
        assert r["ok"] is False and "order_refresh_minutes" in r["errors"]
        assert get_settings().order_refresh_minutes == 3  # unchanged

        r = (await c.post("/admin/api/connections/reset", headers=H, json={"keys": ["order_refresh_minutes"]})).json()
        assert r["ok"] is True and get_settings().order_refresh_minutes == 5


@pytest.mark.asyncio
async def test_test_endpoints_use_the_draft_and_save_nothing(clean_settings):
    csv = (get_settings().resolve_path(f"{FIXTURES}/orders_dummy.csv")).read_bytes()
    xlsx = (get_settings().resolve_path(CUSTOMERS_XLSX)).read_bytes()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://ppc.example.com/orders").mock(return_value=Response(200, content=csv, headers={"content-type": "text/csv"}))
            r = (await c.post("/admin/api/connections/orders/test", headers=H, json={"values": {
                "orders_source": "http", "orders_api_url": "https://ppc.example.com/orders", "orders_api_key": "k", "orders_format": "auto"}})).json()
        assert r["ok"] is True and r["mapped"] == 10 and "SO No" in r["headers"]

        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://sp.example.com/c.xlsx").mock(return_value=Response(200, content=xlsx))
            r = (await c.post("/admin/api/connections/customers/test", headers=H, json={"values": {
                "customers_source": "url", "customers_url": "https://sp.example.com/c.xlsx"}})).json()
        assert r["ok"] is True and r["accepted"] == 8 and len(r["rejected_rows"]) == 2

        # the draft was never persisted
        assert get_settings().orders_source == "file"
        assert get_settings().customers_source == "local"
        assert await store.saved_keys() == set()

        # an invalid draft is reported, not raised
        r = (await c.post("/admin/api/connections/orders/test", headers=H, json={"values": {"orders_source": "nope"}})).json()
        assert r["ok"] is False and "orders_source" in r["error"]
