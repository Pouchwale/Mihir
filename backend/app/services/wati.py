"""WATI client: session text, interactive reply buttons, interactive list, media download.

Verified against WATI's OpenAPI (docs.wati.io, 2026):
  POST /api/v1/sendSessionMessage/{whatsappNumber}?messageText=...
  POST /api/v1/sendInteractiveButtonsMessage?whatsappNumber=...   body {header?{type:"Text",text}, body, footer?, buttons[{text}]}
  POST /api/v1/sendInteractiveListMessage?whatsappNumber=...      body {header, body, footer, buttonText, sections[{title, rows[{title, description}]}]}
  GET  /api/v1/getMedia?fileName=...
  Responses: {"ok": true} on success, {"ok": false, "result": "<error>"} on failure (HTTP 200 is possible either way).
  Optional (WATI_API_VERSION=v3): POST /api/ext/v3/conversations/messages/interactive  body {target, type, list_message|button_message}

Dry-run (mock) mode when no token: nothing leaves the server; every send is recorded in an
in-memory outbox (shown in the dashboard) and, by the processor, in message_log.
Interactive sends fall back to plain text (with the options listed) if WATI rejects them, so the
customer can always answer by typing.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import get_settings
from .menus import BODY_MAX, FOOTER_MAX, HEADER_MAX, Options

log = structlog.get_logger(__name__)

V1_BUTTONS = "/api/v1/sendInteractiveButtonsMessage"
V1_LIST = "/api/v1/sendInteractiveListMessage"
V3_INTERACTIVE = "/api/ext/v3/conversations/messages/interactive"
WEBHOOK_ENDPOINTS = "/api/v2/webhookEndpoints"  # register where WATI posts incoming messages


class WatiError(Exception):
    """Transient (retryable) WATI failure."""


class WatiRejected(Exception):
    """WATI answered but refused the message (4xx or ok=false). Not retried."""


def explain(status: int | None, body: str) -> str:
    """Turn a WATI failure into something an operator can act on. These strings end up in the
    dashboard and in Slack, so they say what to DO, not just what happened."""
    text = (body or "").lower()
    if status in (401, 403):
        return (
            "WATI rejected the token (this is authentication, not the number or the message). WATI documents three "
            "causes: the token is missing/expired/incorrect, the Authorization header is absent, or the account "
            "password was changed (which invalidates existing tokens). Note WATI also ran a security key rotation "
            "completed in February 2026 - tokens that made no API call during it were expired and must be replaced. "
            "Fix: in WATI go to Connector -> API -> Create API Token, generate a new token with the message and "
            "contact scopes, put it in WATI_TOKEN, and restart the bot."
        )
    if status == 404:
        return ("WATI has no such endpoint. WATI_BASE_URL must end with your own tenant id, "
                "e.g. https://live-mt-server.wati.io/123456 (WATI -> Settings -> API Docs).")
    if status == 429:
        return "WATI is rate limiting us. The message is retried automatically."
    if status is not None and status >= 500:
        return "WATI is having trouble at their end. The message is retried automatically."
    if any(w in text for w in ("24 hour", "24-hour", "24h", "session expired", "outside the session", "no active session")):
        return ("Outside WhatsApp's 24-hour window: a business may only reply within 24 hours of the customer's last "
                "message. The customer must message you again, or you must use an approved template message.")
    if any(w in text for w in ("not a valid whatsapp", "invalid number", "not found on whatsapp")):
        return "That number is not on WhatsApp. Check the Contact column in the customer Excel."
    if any(w in text for w in ("interactive", "not supported", "not allowed", "upgrade")):
        return ("Your WATI plan refused an interactive (buttons/list) message. The bot has already re-sent it as plain "
                "text, so the customer can still answer by typing.")
    if "template" in text:
        return "WATI wants an approved template for this message. Inside the 24-hour window no template is needed."
    return "Check the WATI dashboard for this number and the message log there."


class WatiClient:
    def __init__(self) -> None:
        self.outbox: deque[dict] = deque(maxlen=500)

    # ---- helpers ----
    @property
    def mocked(self) -> bool:
        return get_settings().wati_mocked

    def _headers(self) -> dict[str, str]:
        tok = get_settings().wati_token.strip()
        if tok and not tok.lower().startswith("bearer "):
            tok = f"Bearer {tok}"
        return {"Authorization": tok, "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return get_settings().wati_base_url.rstrip("/") + path

    def _record(self, phone: str, text: str, kind: str = "text", extra: dict | None = None) -> dict:
        item = {"phone": phone, "text": text, "kind": kind, "at": datetime.now(timezone.utc).isoformat(), **(extra or {})}
        self.outbox.append(item)
        return item

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=0.5, max=5), retry=retry_if_exception_type(WatiError), reraise=True)
    async def _post(self, path: str, params: dict | None = None, json: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(self._url(path), headers=self._headers(), params=params, json=json)
        except httpx.HTTPError as e:
            raise WatiError(str(e)) from e
        if r.status_code >= 500 or r.status_code == 429:
            raise WatiError(f"wati {r.status_code}: {r.text[:200]} -- {explain(r.status_code, r.text)}")
        if r.status_code >= 400:
            raise WatiRejected(f"wati {r.status_code}: {r.text[:300]} -- {explain(r.status_code, r.text)}")
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        if isinstance(data, dict) and data.get("ok") is False:
            reason = str(data.get("result") or data.get("info") or data.get("message") or data)
            raise WatiRejected(f"wati refused: {reason[:300]} -- {explain(None, reason)}")
        return data if isinstance(data, dict) else {"raw": data}

    # ---- payload builders (pure; unit-tested) ----
    @staticmethod
    def buttons_payload(body: str, options: Options) -> dict:
        payload: dict = {"body": body[:BODY_MAX], "buttons": [{"text": o.title} for o in options.items[:3]]}
        if options.header:
            payload["header"] = {"type": "Text", "text": options.header[:HEADER_MAX]}
        if options.footer:
            payload["footer"] = options.footer[:FOOTER_MAX]
        return payload

    @staticmethod
    def list_payload(body: str, options: Options) -> dict:
        rows = [{"title": o.title, "description": o.description} for o in options.items[:10]]
        return {
            "header": options.header[:HEADER_MAX],
            "body": body[:BODY_MAX],
            "footer": options.footer[:FOOTER_MAX],
            "buttonText": options.button_text or "Select",
            "sections": [{"title": options.section_title or "Options", "rows": rows}],
        }

    @classmethod
    def v3_payload(cls, phone: str, body: str, options: Options) -> dict:
        """WATI API v3 (/api/ext/v3/conversations/messages/interactive): recipient in the body as `target`."""
        if options.kind == "buttons":
            return {"target": phone, "type": "buttons", "button_message": cls.buttons_payload(body, options)}
        p = cls.list_payload(body, options)
        return {
            "target": phone,
            "type": "list",
            "list_message": {"header": p["header"], "body": p["body"], "footer": p["footer"], "button_text": p["buttonText"], "sections": p["sections"]},
        }

    async def _send_interactive(self, phone: str, body: str, options: Options) -> dict:
        s = get_settings()
        if s.wati_api_version == "v3":
            return await self._post(V3_INTERACTIVE, json=self.v3_payload(phone, body, options))
        path = V1_BUTTONS if options.kind == "buttons" else V1_LIST
        payload = self.buttons_payload(body, options) if options.kind == "buttons" else self.list_payload(body, options)
        return await self._post(path, params={"whatsappNumber": phone}, json=payload)

    # ---- API ----
    async def send_text(self, phone: str, text: str) -> dict:
        if self.mocked:
            log.info("wati_mock_send", phone=phone, text=text)
            return self._record(phone, text)
        try:
            data = await self._post(f"/api/v1/sendSessionMessage/{phone}", params={"messageText": text})
        except Exception as e:  # noqa: BLE001 - nothing below plain text to fall back to, so be loud
            from . import alerts  # lazy: alerts imports config only, but keep the import graph flat

            self._record(phone, text, extra={"sent": False, "error": str(e)[:300]})
            log.error("wati_send_failed", phone=phone, error=str(e))
            await alerts.notify_throttled("wati_send_failed", "WhatsApp message could not be sent",
                                          f"phone={phone}\n{e}\n\nCheck WATI_BASE_URL / WATI_TOKEN and the WATI dashboard.")
            raise
        self._record(phone, text, extra={"sent": True})
        return data

    async def send_buttons(self, phone: str, body: str, options: Options) -> dict:
        if self.mocked:
            log.info("wati_mock_send_buttons", phone=phone, text=body, buttons=options.titles())
            return self._record(phone, body, kind="buttons", extra={"options": options.to_dict()})
        try:
            data = await self._send_interactive(phone, body, options)
            self._record(phone, body, kind="buttons", extra={"options": options.to_dict(), "sent": True})
            return data
        except Exception as e:  # noqa: BLE001 - never leave the customer without a way to answer
            from . import alerts

            log.warning("wati_buttons_failed_fallback_text", error=str(e))
            await alerts.notify_throttled("wati_interactive_degraded", "WhatsApp buttons were refused - sending plain text",
                                          str(e), level="warning")
            return await self.send_text(phone, body + "\n\n" + options.as_text())

    async def send_list(self, phone: str, body: str, options: Options) -> dict:
        if self.mocked:
            log.info("wati_mock_send_list", phone=phone, text=body, rows=options.titles())
            return self._record(phone, body, kind="list", extra={"options": options.to_dict()})
        try:
            data = await self._send_interactive(phone, body, options)
            self._record(phone, body, kind="list", extra={"options": options.to_dict(), "sent": True})
            return data
        except Exception as e:  # noqa: BLE001
            from . import alerts

            log.warning("wati_list_failed_fallback_text", error=str(e))
            await alerts.notify_throttled("wati_interactive_degraded", "WhatsApp list was refused - sending plain text",
                                          str(e), level="warning")
            return await self.send_text(phone, body + "\n\n" + options.as_text())

    async def send_options(self, phone: str, body: str, options: Options | None) -> dict:
        """Dispatch: buttons / list / plain text."""
        if options is None or not options.items:
            return await self.send_text(phone, body)
        if options.kind == "buttons":
            return await self.send_buttons(phone, body, options)
        return await self.send_list(phone, body, options)

    async def _probe(self, path: str, params: dict | None = None) -> tuple[int | None, str]:
        """One read call. Returns (status, error text) - never raises."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(self._url(path), headers=self._headers(), params=params)
            return r.status_code, r.text[:200]
        except httpx.HTTPError as e:
            return None, str(e)

    async def check(self) -> dict:
        """Is the WATI connection usable? Never raises - the dashboard shows whatever comes back.

        WATI's newer tokens are scope-limited, so a perfectly good send-only token can be refused by
        the read endpoint we probe with. Judging the token on one probe would report a working bot as
        broken, so a 401/403 is confirmed against a second, unrelated endpoint before we blame the
        token."""
        st = get_settings()
        if self.mocked:
            detail = st.wati_config_problem or "WATI_DRY_RUN is on - messages are simulated, nothing is sent to WhatsApp."
            return {"connected": False, "mocked": True, "detail": detail, "scope_warning": "",
                    "base_url": st.wati_base_url, "api_version": st.wati_api_version}
        base = {"mocked": False, "base_url": st.wati_base_url, "api_version": st.wati_api_version, "scope_warning": ""}

        status, text = await self._probe("/api/v1/getContacts", {"pageSize": 1, "pageNumber": 1})
        if status is None:
            return {**base, "connected": False, "detail": f"Cannot reach WATI: {text}"}
        if status == 404:
            return {**base, "connected": False,
                    "detail": "Endpoint not found (404). Check WATI_BASE_URL - it must end with your tenant id."}
        if status < 400:
            return {**base, "connected": True,
                    "detail": "Token accepted by WATI. Messages you save here are sent to customers through this connection."}
        if status not in (401, 403):
            return {**base, "connected": False, "detail": f"WATI returned {status}: {text}"}

        # 401/403 on a *read* endpoint: is the token bad, or just missing the contacts scope?
        second, second_text = await self._probe(WEBHOOK_ENDPOINTS)
        if second is not None and second < 400:
            return {**base, "connected": True,
                    "scope_warning": ("The token works, but it was created without the contacts read scope, so the "
                                      "contacts endpoint is refused. Sending messages is unaffected."),
                    "detail": "Token accepted by WATI (verified against the webhook API; it cannot read contacts)."}
        if second in (404, 405):
            # The second endpoint does not exist on this account, so we genuinely cannot tell the two
            # apart. Say so rather than declaring a possibly-fine token dead.
            return {**base, "connected": False,
                    "detail": (f"WATI refused the contacts endpoint ({status}). Either the token is invalid, or it was "
                               "created without the contacts read scope - in which case sending still works. "
                               "Send a test message from the Messages page to find out which."),
                    "scope_warning": ""}
        return {**base, "connected": False, "detail": f"WATI rejected the token ({status}). {explain(status, text)}"}

    async def get_media(self, file_name: str) -> bytes:
        if self.mocked:
            fixture = get_settings().resolve_path("fixtures/voice_sample.ogg")
            return fixture.read_bytes() if fixture.exists() else b""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(self._url("/api/v1/getMedia"), headers=self._headers(), params={"fileName": file_name})
        except httpx.HTTPError as e:
            raise WatiError(str(e)) from e
        if r.status_code != 200:
            raise WatiError(f"getMedia {r.status_code}")
        return r.content


    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=0.5, max=5), retry=retry_if_exception_type(WatiError), reraise=True)
    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(self._url(path), headers=self._headers(), params=params)
        except httpx.HTTPError as e:
            raise WatiError(str(e)) from e
        if r.status_code >= 500 or r.status_code == 429:
            raise WatiError(f"wati {r.status_code}: {r.text[:200]} -- {explain(r.status_code, r.text)}")
        if r.status_code >= 400:
            raise WatiRejected(f"wati {r.status_code}: {r.text[:300]} -- {explain(r.status_code, r.text)}")
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        return data if isinstance(data, dict) else {"result": data}

    # ---- webhooks (so the bot can be wired up without the WATI web interface) ----
    async def list_webhooks(self) -> list[dict] | None:
        """Every webhook WATI will call for this account, or None when WATI does not offer a way to
        ask. Only POST /api/v2/webhookEndpoints is documented, so a GET may simply not exist - that
        is not an error, it just means we cannot verify from here."""
        try:
            data = await self._get(WEBHOOK_ENDPOINTS)
        except WatiRejected as e:
            if "404" in str(e) or "405" in str(e):
                log.info("wati_webhook_listing_unavailable")
                return None
            raise
        result = data.get("result") if isinstance(data, dict) else data
        return result if isinstance(result, list) else []

    async def register_webhook(self, url: str, phone_number: str = "", event_types: tuple[str, ...] = ("message",)) -> dict:
        """Point WATI at this server, without opening the WATI dashboard.

        The body is a LIST of endpoints (WATI's documented shape); status 1 = enabled. When no
        channel number is given we reuse the one WATI already reports, so a single-number account
        needs no extra input."""
        if not phone_number:
            for hook in (await self.list_webhooks() or []):
                phone_number = hook.get("channelPhoneNumber") or hook.get("phoneNumber") or ""
                if phone_number:
                    break
        if not phone_number:
            raise WatiRejected(
                "Tell us which WhatsApp business number to register - the one your customers message. Type it in "
                "the box beside the Register webhook button. WATI could not be asked for it: this account does not "
                "answer a request to list webhooks (WATI only documents creating them)."
            )
        body = [{"phoneNumber": phone_number, "status": 1, "url": url, "eventTypes": list(event_types)}]
        data = await self._post(WEBHOOK_ENDPOINTS, json=body)
        log.info("wati_webhook_registered", url=url, phone_number=phone_number)
        return data


wati = WatiClient()
