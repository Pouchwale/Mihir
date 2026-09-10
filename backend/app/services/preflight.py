"""Go-live checks: is this server actually ready to talk to real customers?

Two entry points over the same rules:
  fatal_problems()  - cheap, no network. Called at start-up; in prod the server refuses to boot.
  run_checks()      - the full list behind the dashboard's "Go live" page, each with the exact fix.

Everything here delegates to the services that already know how to test a connection
(wati.check, order_refresh.test_fetch, customer_sync.test_connection) - nothing is re-implemented,
so the page can never disagree with what the bot really does.
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import structlog

from urllib.parse import urlsplit

from ..config import env_changed_since_start, get_settings

log = structlog.get_logger(__name__)

Status = Literal["pass", "warn", "fail"]

INSECURE_DEFAULTS = {
    "admin_key": "change-me-admin-key",
    "wati_webhook_token": "change-me-webhook-token",
    "support_contact": "[phone/email]",
}
MIN_ADMIN_KEY = 12
MIN_WEBHOOK_TOKEN = 16
DEEP_CHECK_TIMEOUT = 25.0
# "we did not look" (shallow run) has to be distinguishable from "WATI cannot tell us" (None).
NOT_CHECKED = object()


@dataclass
class Check:
    key: str
    title: str
    status: Status
    detail: str  # what we actually found
    fix: str = ""  # what to do about it (warn / fail only)
    where: str = ""  # where to do it
    group: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "status": self.status, "detail": self.detail,
                "fix": self.fix, "where": self.where, "group": self.group}


# ---------------- start-up guard ----------------
def fatal_problems(s=None) -> list[str]:
    """Configuration that makes a production boot unsafe. [] means safe to start.
    Deliberately no network calls: start-up must not hang because WATI is slow."""
    s = s or get_settings()
    if s.is_dev:
        return []
    problems: list[str] = []
    if s.admin_key == INSECURE_DEFAULTS["admin_key"] or len(s.admin_key) < MIN_ADMIN_KEY:
        problems.append(f"ADMIN_KEY is still the example value or shorter than {MIN_ADMIN_KEY} characters - "
                        "anyone who finds the dashboard could open it. Set a long random value.")
    if s.wati_webhook_token == INSECURE_DEFAULTS["wati_webhook_token"] or len(s.wati_webhook_token) < MIN_WEBHOOK_TOKEN:
        problems.append(f"WATI_WEBHOOK_TOKEN is still the example value or shorter than {MIN_WEBHOOK_TOKEN} characters - "
                        "the webhook is a public URL, so anyone could post fake customer messages. Set a long random value.")
    if s.wati_token and not s.wati_base_url_ok:
        problems.append(s.wati_config_problem)
    if s.support_contact == INSECURE_DEFAULTS["support_contact"] or "XXXX" in s.support_contact:
        problems.append("SUPPORT_CONTACT is still a placeholder, and it is printed to customers in the "
                        "'contact us' and 'could not verify' messages. Set your real number or email.")
    return problems


# ---------------- full readiness ----------------
def _c(checks: list[Check], group: str):
    def add(key, title, status: Status, detail: str, fix: str = "", where: str = "") -> None:
        checks.append(Check(key, title, status, detail, fix, where, group))

    return add


def ephemeral_host() -> str:
    """The name of the platform when this runs on a container with a throwaway filesystem.

    On these, anything written to disk - the SQLite file, the generated .secret_key - is gone at the
    next deploy, so advice that is fine on a normal server is actively wrong here."""
    import os

    if os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"):
        return "Render"
    if os.environ.get("DYNO"):
        return "Heroku"
    if os.environ.get("K_SERVICE"):
        return "Cloud Run"
    return ""


def _public_host_problem(base_url: str) -> str:
    """WATI calls the webhook from the internet. Say plainly when this address cannot work."""
    if not base_url:
        return ""
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    private = (
        host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
        or host.startswith(("10.", "192.168.", "169.254."))
        or (host.startswith("172.") and len(host.split(".")) > 1 and host.split(".")[1].isdigit() and 16 <= int(host.split(".")[1]) <= 31)
    )
    if private:
        return (f"This dashboard is open at {host}, which only exists on this machine. WATI calls the webhook "
                "from the internet, so it can never reach that address. Publish the bot on a domain with HTTPS, "
                "or for a quick test run a tunnel (e.g. `cloudflared tunnel --url http://localhost:8000`) and use "
                "the https address it prints.")
    if parts.scheme != "https":
        return "WATI only calls https:// addresses. Put a certificate on this server, or a reverse proxy in front of it."
    return ""


def public_base(s, base_url: str = "") -> str:
    """The address the outside world uses. PUBLIC_BASE_URL wins - behind a proxy or tunnel the request
    the browser made says nothing about how WATI reaches us."""
    return (s.public_base_url or base_url or "").rstrip("/")


def hook_url_problem(s, base_url: str = "") -> str:
    """Why the webhook address cannot be built yet - naming the ONE value at fault.

    "set PUBLIC_BASE_URL and a real WATI_WEBHOOK_TOKEN" is useless when one of the two is already
    correct: it sends you checking a setting that was never wrong."""
    base = public_base(s, base_url)
    if not base:
        return ("PUBLIC_BASE_URL is not set. Set it to this service's public address, e.g. "
                "https://your-app.onrender.com - the app cannot work it out from behind a proxy.")
    host_problem = _public_host_problem(base)
    if host_problem:
        return f"PUBLIC_BASE_URL is {base}, which WATI cannot use. {host_problem}"
    token = s.wati_webhook_token
    if token == INSECURE_DEFAULTS["wati_webhook_token"]:
        return "WATI_WEBHOOK_TOKEN is still the example value. Set it to a long random secret you invent."
    if len(token) < MIN_WEBHOOK_TOKEN:
        return (f"WATI_WEBHOOK_TOKEN is only {len(token)} characters. Use at least {MIN_WEBHOOK_TOKEN} - "
                "it is the only thing protecting a public URL.")
    shape = webhook_token_problem(token)
    if shape:
        return f"WATI_WEBHOOK_TOKEN is not usable. {shape}"
    return ""


def hook_url_for(s, base_url: str = "") -> str:
    """The exact address WATI must call. Placeholders appear only for the part that is wrong, so the
    URL still shows what is already correct."""
    base = public_base(s, base_url)
    shown = base if (base and not _public_host_problem(base)) else "https://<PUBLIC_BASE_URL>"
    weak = (s.wati_webhook_token == INSECURE_DEFAULTS["wati_webhook_token"]
            or len(s.wati_webhook_token) < MIN_WEBHOOK_TOKEN
            or bool(webhook_token_problem(s.wati_webhook_token)))
    return f"{shown}/webhook/wati?token={'<WATI_WEBHOOK_TOKEN>' if weak else s.wati_webhook_token}"


def _looks_like_wati_token(value: str) -> bool:
    """WATI API tokens are JWTs: 'eyJ' then exactly two dots. Shared by the two checks below so they
    cannot disagree about what one looks like."""
    return value.startswith("eyJ") and value.count(".") == 2


def suggest_webhook_token() -> str:
    """A webhook secret the owner can use as-is. Fresh every call and unrelated to any other secret
    in the process - the point is that nobody has to invent one, not that this one is special."""
    return "wati_hook_" + secrets.token_hex(16)


def token_shape_problem(token: str) -> str:
    """Paste mistakes that produce a 401 but are invisible in an editor. Never returns the token."""
    if not token:
        return ""
    if token != token.strip():
        return "The token has a space or line break around it. Remove it and keep the value on one line."
    # "Bearer xxx" is accepted (the client normalises it), so judge the token itself
    body = token[7:] if token.lower().startswith("bearer ") else token
    if any(ch.isspace() for ch in body):
        return ("The token contains a space or line break in the middle - it was probably wrapped when you pasted it. "
                "WATI tokens are one long unbroken string; put it on a single line.")
    if body[:1] in ("\"", "'") or body[-1:] in ("\"", "'"):
        return "The token is wrapped in quotes. In .env write it without quotes: WATI_TOKEN=eyJhbGciOi..."
    if not _looks_like_wati_token(body):
        return ("This does not look like a WATI API token. They start with 'eyJ' and contain exactly two dots. "
                "Copy the whole value from WATI -> Connector -> API (it is shown only once).")
    return ""


def webhook_token_problem(token: str) -> str:
    """Paste mistakes in the webhook secret. The commonest is pasting the whole webhook URL, which
    produces `...?token=https://.../webhook/wati?token=xxx` and can never match what WATI sends."""
    if not token or token == INSECURE_DEFAULTS["wati_webhook_token"]:
        return ""
    if "://" in token or "/webhook/wati" in token:
        return ("This is the whole webhook address, not the secret. WATI_WEBHOOK_TOKEN must be ONLY the value after "
                "'token=' - the app builds the full address around it. Set it to just the secret and register the "
                "webhook again.")
    if token != token.strip() or any(ch.isspace() for ch in token):
        return "The secret has a space or line break in it. Keep it on one line with no spaces."
    # Before the ?&= branch: a JWT can carry '=' padding, and "that is your API token" is the more
    # useful answer than "that character belongs to a URL".
    if _looks_like_wati_token(token):
        return ("That is your WATI API token, not the webhook secret. The API token belongs in WATI_TOKEN; "
                "WATI_WEBHOOK_TOKEN is a separate secret that you invent - WATI does not issue it.")
    if "?" in token or "&" in token or "=" in token:
        return "The secret contains ? & or =, which belong to the URL around it, not to the token itself."
    if len(token) > 128:
        return (f"This is {len(token)} characters long. A webhook secret only needs to be unguessable - 32 to 64 "
                "characters is plenty, and a very long one is usually a URL or a token pasted twice.")
    return ""


def _settings_location() -> str:
    """Where the owner actually changes a setting. On a hosted platform backend/.env is not it: the
    value lives in the service's own environment, and editing it needs a redeploy to take effect."""
    platform = ephemeral_host()
    return f"{platform} -> Environment (redeploy to apply)" if platform else "backend/.env"


def _security(s, base_url: str) -> list[Check]:
    out: list[Check] = []
    add = _c(out, "Security")
    where = _settings_location()
    add("app_mode", "Application mode", "pass" if not s.is_dev else "warn",
        f"APP_MODE={s.app_mode}",
        "Dev mode shows the yellow banner and simulates WhatsApp. Set APP_MODE=prod when you go live." if s.is_dev else "",
        where)
    stale = env_changed_since_start()
    add("env_fresh", "Settings file loaded", "fail" if stale else "pass",
        "backend/.env has been edited since the bot started - the bot is still using the OLD values" if stale
        else "the bot is using the current backend/.env",
        "Restart the bot so it re-reads the file (on a hosted service, redeploy). Editing .env while it runs "
        "changes nothing on its own: the file is read once at start-up." if stale else "",
        "backend/.env")
    weak_admin = s.admin_key == INSECURE_DEFAULTS["admin_key"] or len(s.admin_key) < MIN_ADMIN_KEY
    add("admin_key", "Dashboard password", "fail" if weak_admin else "pass",
        "still the example value" if s.admin_key == INSECURE_DEFAULTS["admin_key"] else f"{len(s.admin_key)} characters",
        f"Set ADMIN_KEY to a long random value (at least {MIN_ADMIN_KEY} characters)." if weak_admin else "",
        where)
    weak_hook = s.wati_webhook_token == INSECURE_DEFAULTS["wati_webhook_token"] or len(s.wati_webhook_token) < MIN_WEBHOOK_TOKEN
    host_problem = _public_host_problem(public_base(s, base_url))
    hook_url = hook_url_for(s, base_url)
    shape = webhook_token_problem(s.wati_webhook_token)
    # Whatever the specific mistake, always say who issues this value and hand over a usable one.
    # Naming only the mistake sends people into the WATI portal looking for a value it never had.
    hook_fix = ""
    if weak_hook or shape:
        # the shape message sometimes says this already - do not say it twice
        owner = ("" if "you invent" in shape else
                 "WATI_WEBHOOK_TOKEN is a secret you invent - WATI does not issue it, so there is nothing to look up.")
        hook_fix = "\n".join(filter(None, [
            shape,
            owner,
            "Use this value, or any 32 to 64 random characters of your own:",
            f"  {suggest_webhook_token()}",
            "Changing it changes the webhook address, so register the webhook again afterwards.",
        ]))
    add("webhook_token", "Webhook token", "fail" if (weak_hook or shape) else "pass",
        "still the example value" if s.wati_webhook_token == INSECURE_DEFAULTS["wati_webhook_token"]
        else (shape[:90] if shape else f"{len(s.wati_webhook_token)} characters"),
        hook_fix, where)
    add("webhook_url", "Webhook address WATI will call", "fail" if host_problem else "pass",
        hook_url,
        host_problem or "Paste this exact address into WATI -> Settings -> Webhooks for the event 'Message received'.",
        "the WATI portal")
    if host_problem and not s.public_base_url:
        add("public_base_url", "Public address", "fail", "not configured, and this request came from a private address",
            "Set PUBLIC_BASE_URL to the https address customers' messages arrive on, e.g. "
            "https://bot.yourdomain.com. Behind a reverse proxy or tunnel the server cannot work this out by itself.",
            where)
    platform = ephemeral_host()
    if not s.secret_key and platform:
        add("secret_key", "Password encryption key", "fail", f"auto-generated file, on {platform}",
            f"{platform} regenerates this file on every deploy, so any connection password saved in the dashboard "
            "becomes unreadable and silently stops working. Set SECRET_KEY to a long random value in the "
            "environment - any phrase will do, it just has to stay the same.", where)
    else:
        add("secret_key", "Password encryption key", "pass" if s.secret_key else "warn",
            "SECRET_KEY set" if s.secret_key else "auto-generated file backend/.secret_key",
            "" if s.secret_key else "Fine for one server that keeps its disk. Back up backend/.secret_key, or set "
                                    "SECRET_KEY - without it, connection passwords saved in the dashboard must be "
                                    "typed in again.", where)
    return out


def _whatsapp(s, wati_status: dict | None, hooks=NOT_CHECKED, expected_url: str = "", health: dict | None = None) -> list[Check]:
    out: list[Check] = []
    add = _c(out, "WhatsApp (WATI)")
    where = _settings_location()
    add("wati_token", "WATI token", "pass" if s.wati_token else "fail",
        "set" if s.wati_token else "empty - nothing is sent to WhatsApp",
        "" if s.wati_token else "In WATI go to Connector -> API -> Create API Token (or Settings -> API Docs on older accounts), generate a token with the message and contact scopes, and put it in WATI_TOKEN. It is shown only once.",
        where)
    shape = token_shape_problem(s.wati_token)
    if s.wati_token:
        add("wati_token_format", "Token format", "fail" if shape else "pass",
            shape or "looks like a WATI token", shape, where)
    add("wati_base_url", "WATI tenant URL", "pass" if s.wati_base_url_ok else "fail",
        s.wati_base_url,
        "" if s.wati_base_url_ok else "WATI_BASE_URL must end with your own tenant id, e.g. "
                                      "https://live-mt-server.wati.io/123456 (copy it from WATI -> Settings -> API Docs).",
        where)
    if wati_status is not None:
        ok = bool(wati_status.get("connected"))
        scope_warning = wati_status.get("scope_warning") or ""
        # A missing OPTIONAL scope must never block go-live: the bot only needs to send.
        if ok and scope_warning:
            status, fix = "warn", scope_warning
        elif ok:
            status, fix = "pass", ""
        else:
            status, fix = "fail", "Fix the token / tenant URL above, then press 'Run the checks again'."
        add("wati_connection", "WATI connection", status, wati_status.get("detail", ""), fix, where)
    if hooks is not NOT_CHECKED:
        # Without this, outgoing messages work but the bot never HEARS a customer - the single most
        # confusing way for a WhatsApp bot to look broken.
        if isinstance(hooks, Exception):
            add("webhook_registered", "Webhook registered in WATI", "warn", f"could not ask WATI: {hooks}",
                "Fix the token first, then run the checks again.", where)
        elif hooks is None:
            # WATI documents creating webhooks but not listing them, so on some accounts we simply
            # cannot check. Never fail for something we are unable to see.
            add("webhook_registered", "Webhook registered in WATI", "warn",
                "WATI does not offer a way to list webhooks on this account",
                "Cannot be verified from here. Press 'Register webhook in WATI' to be certain it is set to: "
                + expected_url, "the WATI account")
        else:
            urls = [h.get("url", "") for h in hooks]
            live = [h for h in hooks if h.get("url") == expected_url and h.get("status") in (1, "1", True)]
            matching = [h for h in hooks if h.get("url") == expected_url]
            if live:
                add("webhook_registered", "Webhook registered in WATI", "pass",
                    f"WATI will post incoming messages to {expected_url}")
            elif matching:
                add("webhook_registered", "Webhook registered in WATI", "fail",
                    "registered but not enabled",
                    "Press 'Register webhook in WATI' to enable it.", "the WATI account")
            else:
                add("webhook_registered", "Webhook registered in WATI", "fail",
                    f"not registered ({len(urls)} other webhook(s) configured)" if urls else "no webhook is registered",
                    "Customer messages cannot reach the bot until WATI knows this address. Press "
                    "'Register webhook in WATI' to add it over the API: " + expected_url,
                    "the WATI account")

    if health is not None:
        if health["calls"] == 0:
            add("delivery_health", "Deliveries from WATI", "warn", "WATI has not called this server yet",
                "Nothing is wrong yet - it just means no customer message has arrived. Send yourself one to check.",
                "Dashboard -> Diagnostics")
        elif health["trailing_bad"]:
            add("delivery_health", "Deliveries from WATI", "fail",
                f"the last {min(3, health['calls'])} calls from WATI were all refused",
                "WATI retries a non-200 and stops sending events after enough failures in a row. Fix this now or the "
                f"bot goes silent. Last reason: {health['last_reason']}", "Dashboard -> Diagnostics")
        elif health["bad"]:
            add("delivery_health", "Deliveries from WATI", "warn",
                f"{health['bad']} of the last {health['calls']} calls were refused",
                f"Each refusal counts towards WATI disabling the webhook. Last reason: {health['last_reason']}",
                "Dashboard -> Diagnostics")
        else:
            add("delivery_health", "Deliveries from WATI", "pass",
                f"the last {health['calls']} calls from WATI were all accepted")

    bad_support = s.support_contact == INSECURE_DEFAULTS["support_contact"] or "XXXX" in s.support_contact
    add("support_contact", "Support contact", "fail" if bad_support else "pass",
        s.support_contact,
        "Customers are told to contact this. Set SUPPORT_CONTACT to your real number or email." if bad_support else "",
        where)
    return out


def _data(s, orders_preview, customers_test, orders_n: int, customers_n: int, stale: bool, conflicts: list[dict], empty_slots: list[str]) -> list[Check]:
    out: list[Check] = []
    add = _c(out, "Order and customer data")
    if orders_preview is not None:
        ok = bool(getattr(orders_preview, "ok", False))
        add("orders_source", "Order table connection", "pass" if ok else "fail",
            f"{getattr(orders_preview, 'source', '')} - {getattr(orders_preview, 'mapped', 0)} rows mapped" if ok
            else str(getattr(orders_preview, "error", "could not read the order table")),
            "" if ok else "Check the source, URL/key and column names, then press 'Test fetch'.",
            "Dashboard -> Data sources")
    add("orders_cached", "Orders loaded", "pass" if orders_n and not stale else ("fail" if not orders_n else "warn"),
        f"{orders_n} rows in the cache" + (" (stale)" if stale and orders_n else ""),
        "No order rows: run 'Refresh orders now'." if not orders_n
        else (f"The cache has not refreshed for over {s.orders_stale_minutes} minutes. Check the order source." if stale else ""),
        "Dashboard -> Data sources")
    if customers_test is not None:
        ok = bool(customers_test.get("ok")) and customers_test.get("accepted", 0) > 0
        add("customers_source", "Customer Excel connection", "pass" if ok else "fail",
            f"{customers_test.get('source', '')} - {customers_test.get('accepted', 0)} usable numbers, {customers_test.get('rejected', 0)} rejected"
            if customers_test.get("ok") else str(customers_test.get("error", "could not read the customer Excel")),
            "" if ok else "Check the file location / Dropbox credentials and the column names.",
            "Dashboard -> Data sources")
    add("customers_imported", "Customers imported", "pass" if customers_n else "fail",
        f"{customers_n} customers",
        "No customers: no one can be verified, so every message gets the 'could not verify' reply. "
        "Run 'Import customers now'." if not customers_n else "",
        "Dashboard -> Data sources")
    add("templates", "Messages and buttons", "fail" if (conflicts or empty_slots) else "pass",
        "; ".join([f"custom reply '{c['title']}' (trigger '{c['trigger']}') blocks the {c['button']} button" for c in conflicts]
                  + [f"'{k}' has no buttons" for k in empty_slots]) or "no conflicts",
        "Rename or remove the trigger word, or add the missing buttons back." if (conflicts or empty_slots) else "",
        "Dashboard -> Messages")
    return out


def _runtime(s, sqlite: bool, jobs: list[dict], queued: int, failed: int, ai_paused: bool, dashboard_built: bool,
             running_server: bool = True) -> list[Check]:
    out: list[Check] = []
    add = _c(out, "Server")
    platform = ephemeral_host()
    if sqlite and platform:
        add("database", "Database", "fail", f"SQLite file on {platform}",
            f"{platform} gives each deploy a fresh filesystem, so this database - your customers, sessions, chat "
            "log AND your edited messages - is deleted every time you deploy or the service restarts. Create a "
            f"{platform} Postgres and set DATABASE_URL to its Internal Database URL. The app accepts the URL exactly "
            "as given.", "backend/.env (DATABASE_URL)")
    else:
        add("database", "Database", "warn" if sqlite else "pass",
            "SQLite file" if sqlite else ("PostgreSQL" if s.is_postgres else "MySQL"),
            "SQLite is fine for a single server that keeps its disk. Back it up with scripts/backup_db.ps1, or move "
            "to PostgreSQL or MySQL for production." if sqlite else "",
            "backend/.env (DATABASE_URL)")
    names = {j["id"] for j in jobs if j.get("next_run")}
    missing = [j for j in ("customer_sync", "order_refresh") if j not in names]
    if not running_server:
        # from the command line there is no scheduler to inspect; saying "not scheduled" would be a lie
        add("scheduler", "Background jobs", "pass", "not checked from the command line",
            "Open the dashboard's Go live page to see the schedule.", "")
    else:
        add("scheduler", "Background jobs", "fail" if missing else "pass",
            ", ".join(f"{j['id']} next {j['next_run']}" for j in jobs) or "no jobs scheduled",
            f"These jobs are not scheduled: {', '.join(missing)}. Restart the server." if missing else "",
            "server")
    add("queue", "Message queue", "warn" if (queued > 20 or failed) else "pass",
        f"{queued} waiting, {failed} failed",
        "Messages are piling up or failing - open Chat log and check the errors." if (queued > 20 or failed) else "",
        "Dashboard -> Chat log")
    add("voice_notes", "Voice notes", "pass" if (s.voice_notes and s.groq_api_key) else "warn",
        "on" if (s.voice_notes and s.groq_api_key) else ("on, but GROQ_API_KEY is missing" if s.voice_notes else "off"),
        "" if (s.voice_notes and s.groq_api_key) else
        ("Set GROQ_API_KEY or switch voice notes off - right now a voice note gets the 'please type' reply."
         if s.voice_notes else "Customers who send a voice note are asked to type instead. Set GROQ_API_KEY and turn "
                               "voice notes on in Settings -> Conversation to accept them."),
        "Dashboard -> Settings")
    add("openai", "Smart text understanding", "pass" if (s.openai_api_key and not ai_paused) else "warn",
        "on" if (s.openai_api_key and not ai_paused) else ("paused after repeated failures" if s.openai_api_key else "off"),
        "" if (s.openai_api_key and not ai_paused) else
        "Optional. Buttons, order numbers and item codes all work without it; only unusual free text is understood less well.",
        "backend/.env (OPENAI_API_KEY)")
    add("alerts", "Failure alerts", "pass" if s.alert_slack_webhook else "warn",
        "Slack webhook set" if s.alert_slack_webhook else "dashboard only",
        "" if s.alert_slack_webhook else "Set ALERT_SLACK_WEBHOOK to be told about failures without opening the dashboard.",
        "backend/.env")
    add("dashboard_built", "Dashboard build", "pass" if dashboard_built else "warn",
        "built" if dashboard_built else "not built",
        "" if dashboard_built else "Run `npm run build` in the dashboard folder.",
        "dashboard/")
    return out


async def delivery_health(limit: int = 20) -> dict:
    """How WATI's recent calls went. Answering non-200 repeatedly gets the webhook disabled, and a
    disabled webhook is silent - so this has to be visible before it happens."""
    from sqlalchemy import select

    from ..db import session_scope
    from ..models import WebhookLog

    async with session_scope() as db:
        rows = (await db.execute(select(WebhookLog).order_by(WebhookLog.id.desc()).limit(limit))).scalars().all()
    last_ok = next((r for r in rows if r.status == 200), None)
    bad = [r for r in rows if r.status != 200]
    return {"calls": len(rows), "bad": len(bad),
            "last_ok_at": last_ok.created_at.isoformat() if last_ok else None,
            "trailing_bad": len(rows) and all(r.status != 200 for r in rows[:3]) and len(rows) >= 3,
            "last_reason": bad[0].reason if bad else None}


async def _not_checked():
    return NOT_CHECKED


async def _safe(coro, label: str):
    """Never let one slow or broken connection stop the page from rendering."""
    try:
        return await asyncio.wait_for(coro, timeout=DEEP_CHECK_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.warning("readiness_check_failed", check=label, error=str(e))
        return e


async def run_checks(deep: bool = True, base_url: str = "", running_server: bool = True) -> dict:
    """Every go-live check. `deep` also tests the live connections (WATI, orders, customer Excel).
    `running_server` is False when called from the command line, where there is no scheduler."""
    from sqlalchemy import func, select

    from ..db import is_sqlite, session_scope
    from ..jobs import customer_sync, order_refresh, scheduler
    from ..models import InboundQueue
    from ..routers.health import status_payload
    from . import intent, templates
    from .wati import wati

    s = get_settings()
    wati_status = orders_preview = customers_test = None
    hooks = NOT_CHECKED
    if deep:
        wati_status, orders_preview, customers_test, hooks = await asyncio.gather(
            _safe(wati.check(), "wati"),
            _safe(order_refresh.test_fetch(), "orders"),
            _safe(customer_sync.test_connection(s), "customers"),
            _safe(wati.list_webhooks(), "webhooks") if not s.wati_mocked else _not_checked(),
        )
        if isinstance(wati_status, Exception):
            wati_status = {"connected": False, "detail": f"check failed: {wati_status}"}
        if isinstance(orders_preview, Exception):
            orders_preview = None
        if isinstance(customers_test, Exception):
            customers_test = {"ok": False, "error": str(customers_test)}

    health = await status_payload()
    orders_n = await order_refresh.count()
    customers_n = await customer_sync.count()
    async with session_scope() as db:
        queued = await db.scalar(select(func.count()).select_from(InboundQueue).where(InboundQueue.status.in_(("queued", "processing")))) or 0
        failed = await db.scalar(select(func.count()).select_from(InboundQueue).where(InboundQueue.status == "failed")) or 0

    empty_slots = [k for k, slot in templates.BUTTON_SLOTS.items() if slot.min_count and not templates.registry.buttons(k)]
    dashboard_built = (Path(__file__).resolve().parent.parent / "static" / "admin" / "index.html").exists()

    checks = (
        _security(s, base_url)
        + _whatsapp(s, wati_status, hooks, hook_url_for(s, base_url), await delivery_health())
        + _data(s, orders_preview, customers_test, orders_n, customers_n, bool(health.get("orders_stale")),
                templates.audit_custom_conflicts(), empty_slots)
        + _runtime(s, is_sqlite(), scheduler.jobs_info(), int(queued), int(failed), intent.breaker_open(), dashboard_built, running_server)
    )
    counts = {st: sum(1 for c in checks if c.status == st) for st in ("pass", "warn", "fail")}
    return {
        "ready": counts["fail"] == 0,
        "mode": s.app_mode,
        "deep": deep,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "checks": [c.to_dict() for c in checks],
    }
