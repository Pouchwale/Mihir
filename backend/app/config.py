"""Application settings. Every secret and tunable comes from environment / .env."""
from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_DIR / ".env"
# .env is read once, when the first Settings object is built. Editing it while the server runs has no
# effect (uvicorn --reload watches .py files only), which looks exactly like "my credentials are
# ignored". Remember when we read it so the dashboard can say "restart, or press Apply".
PROCESS_STARTED = time.time()

DEFAULT_COLUMN_MAP = {
    "so_no": "SO No",
    "po_no": "PO No",
    "fg_item_code": "FG Item Code",
    "fg_description": "FG Description",
    "customer_name": "Customer Name",
    "connection_status": "Connection Status",
    "real_status": "Real Status (PPC)",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore",
        # Settings saved in the dashboard are applied with setattr (see get_settings below). Without
        # this they would skip every validator, so a value from the database behaved differently from
        # the same value in .env - a column map saved before a column existed stayed short, and the
        # column was never read.
        validate_assignment=True,
    )

    app_mode: Literal["dev", "prod"] = "dev"
    database_url: str = "sqlite+aiosqlite:///./order_bot.db"
    log_level: str = "INFO"

    # The address the outside world reaches this server on, e.g. https://bot.example.com
    # Set it when the bot sits behind a reverse proxy / tunnel: the app cannot reliably guess its own
    # public address, and a wrong guess means the webhook URL handed to WATI is wrong.
    public_base_url: str = ""

    admin_key: str = "change-me-admin-key"
    support_contact: str = "[phone/email]"
    # Encrypts connection passwords stored in the database. Auto-created in backend/.secret_key if unset.
    secret_key: str = ""
    # Emergency escape hatch: start in prod even with an unsafe config. Never use it on a real server.
    allow_insecure_prod: bool = False

    # WATI
    wati_base_url: str = "https://live-mt-server.wati.io/tenant"
    wati_token: str = ""
    wati_webhook_token: str = "change-me-webhook-token"
    wati_dry_run: bool | None = None  # None = auto (true when token empty)
    # Your WhatsApp Business Account id. Needed ONLY to delete a Meta-approved template; sending
    # and creating work without it. WATI shows it under your channel.
    wati_waba_id: str = ""
    # The WhatsApp business number customers message. WATI requires it to send a Meta-approved template.
    wati_channel_number: str = ""
    # The owner's own phones: a published workflow answers only these until it is switched on for everyone.
    workflow_test_numbers: str = ""
    # After an Assign step the bot is quiet for that customer. WATI does not say when an agent solves a
    # chat, so it takes the chat back after this many hours without a customer message. 0 = only by hand.
    agent_handover_hours: int = 24
    wati_api_version: Literal["v1", "v3"] = "v1"  # interactive (list/buttons) endpoints: v1 = documented default, v3 = /api/ext/v3

    # AI
    # Voice notes are OFF by default: speech-to-text can misread digits, so a wrong SO number would be
    # looked up silently. When off, a voice note gets the editable "voice_off" reply asking to type.
    voice_notes: bool = False
    groq_api_key: str = ""
    groq_stt_model: str = "whisper-large-v3-turbo"
    # The workflow AI assistant, review, translation and the two live features, all through Groq with
    # the same key. The address is here only so tests can point it at a stand-in server.
    groq_base_url: str = "https://api.groq.com/openai/v1"
    ai_builder_model: str = "openai/gpt-oss-120b"
    ai_live_model: str = "openai/gpt-oss-20b"
    # steps written per call when the assistant builds a big workflow (Groq's free plan: 8,000 tokens a minute)
    ai_batch_steps: int = 8
    # translate buttons in the editor: auto = AI when a Groq key is set, else the free translators
    translate_provider: Literal["auto", "ai", "free"] = "auto"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # Customers
    # where the SAP customer Excel comes from: local (folder / network share) | dropbox | url
    customers_source: Literal["local", "dropbox", "url"] = "local"
    dropbox_app_key: str = ""
    dropbox_app_secret: str = ""
    dropbox_refresh_token: str = ""
    dropbox_file_path: str = "/SAP/customers.xlsx"
    customers_file_path: str = "fixtures/customers_dummy.xlsx"
    customers_url: str = ""
    customers_url_key: str = ""
    customers_url_key_in: Literal["header", "query", "bearer"] = "header"
    customers_url_key_name: str = "X-API-Key"
    customers_sheet: str = ""
    customers_col_code: str = "Customer Code"
    customers_col_name: str = "Customer Name"
    customers_col_contact: str = "Contact"
    customer_sync_cron: str = "10 6 * * *"

    # Orders source
    orders_source: Literal["file", "http", "sql"] = "file"
    orders_file_path: str = "fixtures/orders_dummy.json"
    orders_api_url: str = ""
    orders_api_method: Literal["GET", "POST"] = "GET"
    orders_api_key: str = ""
    orders_api_key_in: Literal["header", "query", "bearer"] = "header"
    orders_api_key_name: str = "X-API-Key"
    orders_api_body: str = ""
    orders_format: Literal["auto", "json", "csv", "xlsx", "html"] = "auto"
    orders_sheet: str = ""
    orders_sql_url: str = ""
    orders_sql_query: str = ""
    # NoDecode: the raw text reaches the validator below, so an empty or half-typed ORDERS_COLUMN_MAP
    # is a clear message rather than a crash at boot - on a hosted service that is a service that
    # never comes back up, over a value someone cleared in a web form.
    orders_column_map: Annotated[dict[str, str], NoDecode] = DEFAULT_COLUMN_MAP.copy()
    order_refresh_minutes: int = 5
    orders_stale_minutes: int = 30

    # Session / limits
    # A new "window" starts after this much silence: the customer gets the greeting + language question again.
    session_timeout_min: int = 30
    # How SO / item choices are shown: auto = buttons when 3 or fewer, list otherwise; list = always a list
    so_menu_style: Literal["auto", "list"] = "auto"
    # A customer asking the same thing over and over is not being served: the answer is not going to
    # change by being asked again. After this many identical requests in a row the bot stops and says
    # so, rather than repeating itself. 0 = never stop.
    repeat_limit: int = 3
    # ...and what "stop" means: end the conversation, or quieten the bot and let your team take over.
    repeat_action: Literal["end", "person"] = "end"
    fg_max_attempts: int = 2
    rate_limit_msgs: int = 20
    rate_limit_window_min: int = 10
    # One customer message may never block the queue for longer than this.
    queue_item_timeout_sec: int = 90
    # Flood protection on the public webhook, per calling IP per 10 seconds.
    webhook_max_per_10s: int = 120
    # Rotating log file (empty = stdout only, which systemd/journald already rotates).
    log_file: str = ""
    log_max_mb: int = 20
    log_backups: int = 5

    # Alerts
    alert_slack_webhook: str = ""

    @field_validator("database_url", mode="before")
    @classmethod
    def _async_driver(cls, v):
        """Accept the URL a host gives you and make it work.

        Managed Postgres (Render, Heroku, Supabase) hands out `postgres://` or `postgresql://`, which
        SQLAlchemy would open with a blocking driver - this app is async, so it needs
        `postgresql+asyncpg://`. `sslmode=` is psycopg syntax that asyncpg rejects outright, so it is
        stripped here and turned into a connect argument in db.py."""
        if not isinstance(v, str) or not v.strip():
            return v
        url = v.strip()
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url[len(prefix):]
                break
        if url.startswith("mysql://"):
            url = "mysql+aiomysql://" + url[len("mysql://"):]
        if url.startswith("sqlite://") and "+aiosqlite" not in url:
            url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        if "asyncpg" in url and "sslmode" in url:
            head, _, query = url.partition("?")
            keep = [kv for kv in query.split("&") if not kv.lower().startswith(("sslmode=", "channel_binding="))]
            url = head + ("?" + "&".join(keep) if keep else "")
        return url

    @field_validator("wati_dry_run", mode="before")
    @classmethod
    def _empty_bool(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("orders_column_map", mode="before")
    @classmethod
    def _parse_map(cls, v):
        """The column map, filled in from the defaults for anything it does not mention.

        A map written before a column existed would otherwise leave that column silently unread -
        the value is simply never there, and nothing says why. A column the owner deliberately does
        not want is mapped to "" rather than left out, and that is honoured."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return DEFAULT_COLUMN_MAP.copy()
            v = json.loads(v)
        if isinstance(v, dict):
            return {**DEFAULT_COLUMN_MAP, **{k: val for k, val in v.items() if k in DEFAULT_COLUMN_MAP}}
        return v

    # ---- derived ----
    @property
    def is_dev(self) -> bool:
        return self.app_mode == "dev"

    @property
    def wati_base_url_ok(self) -> bool:
        """True only when the URL carries a real tenant id, not the .env.example placeholder.
        WATI's base URL looks like https://live-mt-server.wati.io/123456 - the last path segment is
        the tenant id. Sending to `.../<tenantId>` or `.../tenant` 404s on every call."""
        u = (self.wati_base_url or "").strip().rstrip("/")
        if not u.lower().startswith(("http://", "https://")) or "<" in u or ">" in u:
            return False
        tail = urlsplit(u).path.strip("/").split("/")[-1].lower()
        return bool(tail) and tail not in {"tenant", "tenantid", "tenant_id", "your-tenant-id"}

    @property
    def wati_config_problem(self) -> str:
        """'' when WATI is fully configured, else the one thing to fix (shown in the dashboard)."""
        if not self.wati_token:
            return "No WATI token set - messages are simulated, nothing is sent to WhatsApp."
        if not self.wati_base_url_ok:
            return (f"WATI_BASE_URL is still a placeholder ({self.wati_base_url!r}). Put your own tenant id at the end, "
                    "e.g. https://live-mt-server.wati.io/123456 - copy it from WATI -> Settings -> API Docs.")
        return ""

    @property
    def wati_mocked(self) -> bool:
        if self.wati_dry_run is not None:
            return self.wati_dry_run
        # A token with a placeholder base URL would 404 on every send; mock instead of failing silently.
        return not (self.wati_token and self.wati_base_url_ok)

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def db_needs_ssl(self) -> bool:
        """Render's *external* Postgres address requires TLS; the internal one does not. The internal
        host has no dots in it, which is the simplest reliable signal."""
        if not self.is_postgres:
            return False
        host = urlsplit(self.database_url).hostname or ""
        return "." in host and "localhost" not in host

    @property
    def dropbox_configured(self) -> bool:
        return bool(self.dropbox_app_key and self.dropbox_app_secret and self.dropbox_refresh_token)

    @property
    def customers_source_effective(self) -> str:
        """Back-compat: an .env that only set Dropbox credentials still means 'dropbox'."""
        if self.customers_source == "local" and self.dropbox_configured:
            return "dropbox"
        return self.customers_source

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else BACKEND_DIR / path


# Connection settings saved in the dashboard live here and win over .env.
# services.settings_store loads them at startup and after every save.
_overrides: dict[str, object] = {}


def apply_overrides(values: dict) -> None:
    global _overrides
    _overrides = dict(values)
    get_settings.cache_clear()


def overrides() -> dict:
    return dict(_overrides)


def env_changed_since_start() -> bool:
    """True when backend/.env has been edited since this process read it, i.e. the values you are
    looking at in the file are NOT the ones the bot is using."""
    try:
        return ENV_FILE.stat().st_mtime > PROCESS_STARTED
    except OSError:
        return False


def reload_env() -> None:
    """Re-read backend/.env without restarting. Settings saved in the dashboard still win.
    Things fixed at import time (CORS, /docs, logging) still need a real restart."""
    global PROCESS_STARTED
    get_settings.cache_clear()
    get_settings()  # rebuild now, so a bad .env fails here rather than mid-conversation
    PROCESS_STARTED = time.time()


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for k, v in _overrides.items():
        if hasattr(s, k):
            try:
                setattr(s, k, v)
            except Exception:  # noqa: BLE001 - a bad stored value must never stop the app booting
                pass
    return s
