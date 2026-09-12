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

import re

from urllib.parse import quote, unquote, urlsplit

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import get_settings
from .menus import LIST_ROWS_MAX, ROW_DESC_MAX, ROW_TITLE_MAX, SECTION_TITLE_MAX, BODY_MAX, FOOTER_MAX, HEADER_MAX, Options

log = structlog.get_logger(__name__)

V1_BUTTONS = "/api/v1/sendInteractiveButtonsMessage"
V1_LIST = "/api/v1/sendInteractiveListMessage"
V3_INTERACTIVE = "/api/ext/v3/conversations/messages/interactive"
WHATSAPP_TEMPLATES = "/api/v1/whatsapp-templates"
MESSAGE_TEMPLATES = "/api/ext/v3/messagetemplates"
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


FILE_MAX_BYTES = 16 * 1024 * 1024  # WhatsApp's media limit


def file_name_of(url: str) -> str:
    """The last part of a link's path, which is what WhatsApp shows as a document's name."""
    name = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(name)[:120] or "file"


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
    def buttons_payload(body: str, options: Options, media: dict | None = None) -> dict:
        payload: dict = {"body": body[:BODY_MAX], "buttons": [{"text": o.title} for o in options.items[:3]]}
        head = WatiClient.media_header(media)
        if head:
            payload["header"] = head
        elif options.header:
            payload["header"] = {"type": "Text", "text": options.header[:HEADER_MAX]}
        if options.footer:
            payload["footer"] = options.footer[:FOOTER_MAX]
        return payload

    @staticmethod
    def media_header(media: dict | None) -> dict:
        """A picture, video or document above a buttons message, in WATI's v1 header shape:
        {"type": "Image", "media": {"url": ...}} - a document also carries its file name."""
        if not isinstance(media, dict) or not media.get("url"):
            return {}
        kind = str(media.get("type") or "image").lower()
        if kind not in ("image", "video", "document"):
            return {}
        head: dict = {"type": kind.capitalize(), "media": {"url": str(media["url"])}}
        if kind == "document":
            head["media"]["fileName"] = file_name_of(str(media["url"]))
        return head

    @staticmethod
    def list_payload(body: str, options: Options, sections: list[dict] | None = None) -> dict:
        """`sections` groups the rows under their own headings, as a workflow list draws them. Without
        it every row sits in one section, which is all the order-status bot ever needs."""
        if sections:
            groups, left = [], LIST_ROWS_MAX
            for sec in sections:
                rows = [{"title": str(r.get("title") or "")[:ROW_TITLE_MAX],
                         "description": str(r.get("description") or "")[:ROW_DESC_MAX]}
                        for r in (sec.get("rows") or []) if isinstance(r, dict)][:left]
                if rows:
                    title = str(sec.get("title") or options.section_title or "Options")[:SECTION_TITLE_MAX]
                    groups.append({"title": title, "rows": rows})
                    left -= len(rows)
        else:
            groups = [{"title": options.section_title or "Options",
                       "rows": [{"title": o.title, "description": o.description} for o in options.items[:10]]}]
        return {
            "header": options.header[:HEADER_MAX],
            "body": body[:BODY_MAX],
            "footer": options.footer[:FOOTER_MAX],
            "buttonText": options.button_text or "Select",
            "sections": groups,
        }

    @classmethod
    def v3_payload(cls, phone: str, body: str, options: Options, media: dict | None = None,
                   sections: list[dict] | None = None) -> dict:
        """WATI API v3 (/api/ext/v3/conversations/messages/interactive): recipient in the body as `target`."""
        if options.kind == "buttons":
            return {"target": phone, "type": "buttons", "button_message": cls.buttons_payload(body, options, media)}
        p = cls.list_payload(body, options, sections)
        return {
            "target": phone,
            "type": "list",
            "list_message": {"header": p["header"], "body": p["body"], "footer": p["footer"], "button_text": p["buttonText"], "sections": p["sections"]},
        }

    async def _send_interactive(self, phone: str, body: str, options: Options, media: dict | None = None,
                                sections: list[dict] | None = None) -> dict:
        s = get_settings()
        if s.wati_api_version == "v3":
            return await self._post(V3_INTERACTIVE, json=self.v3_payload(phone, body, options, media, sections))
        path = V1_BUTTONS if options.kind == "buttons" else V1_LIST
        payload = (self.buttons_payload(body, options, media) if options.kind == "buttons"
                   else self.list_payload(body, options, sections))
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

    async def send_buttons(self, phone: str, body: str, options: Options, media: dict | None = None) -> dict:
        if self.mocked:
            log.info("wati_mock_send_buttons", phone=phone, text=body, buttons=options.titles())
            return self._record(phone, body, kind="buttons",
                                extra={"options": options.to_dict(), **({"media": media} if media else {})})
        try:
            data = await self._send_interactive(phone, body, options, media=media)
            self._record(phone, body, kind="buttons", extra={"options": options.to_dict(), "sent": True})
            return data
        except Exception as e:  # noqa: BLE001 - never leave the customer without a way to answer
            from . import alerts

            log.warning("wati_buttons_failed_fallback_text", error=str(e))
            await alerts.notify_throttled("wati_interactive_degraded", "WhatsApp buttons were refused - sending plain text",
                                          str(e), level="warning")
            return await self.send_text(phone, body + "\n\n" + options.as_text())

    async def send_list(self, phone: str, body: str, options: Options, sections: list[dict] | None = None) -> dict:
        if self.mocked:
            log.info("wati_mock_send_list", phone=phone, text=body, rows=options.titles())
            return self._record(phone, body, kind="list",
                                extra={"options": options.to_dict(), **({"sections": sections} if sections else {})})
        try:
            data = await self._send_interactive(phone, body, options, sections=sections)
            self._record(phone, body, kind="list", extra={"options": options.to_dict(), "sent": True})
            return data
        except Exception as e:  # noqa: BLE001
            from . import alerts

            log.warning("wati_list_failed_fallback_text", error=str(e))
            await alerts.notify_throttled("wati_interactive_degraded", "WhatsApp list was refused - sending plain text",
                                          str(e), level="warning")
            return await self.send_text(phone, body + "\n\n" + options.as_text())

    async def send_options(self, phone: str, body: str, options: Options | None, *, media: dict | None = None,
                           sections: list[dict] | None = None) -> dict:
        """Dispatch: buttons / list / plain text. `media` (buttons) and `sections` (list) are what a
        workflow question can add around its choices."""
        if options is None or not options.items:
            return await self.send_text(phone, body)
        if options.kind == "buttons":
            return await self.send_buttons(phone, body, options, media=media)
        return await self.send_list(phone, body, options, sections=sections)

    # ---- what a workflow step can do to a conversation in WATI ----
    # Endpoints and bodies per docs.wati.io (API v3 unless noted). In dry-run mode each is recorded
    # in the outbox instead, exactly like a sent message, so the dashboard can show it happened.
    async def _call(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
                    files: dict | None = None) -> dict:
        headers = self._headers()
        if files is not None:
            headers.pop("Content-Type", None)  # httpx writes the multipart boundary itself
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.request(method, self._url(path), headers=headers, params=params, json=json,
                                         files=files)
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
        if isinstance(data, dict) and (data.get("ok") is False or data.get("result") is False):
            reason = str(data.get("info") or data.get("message") or data)
            raise WatiRejected(f"wati refused: {reason[:300]}")
        return data if isinstance(data, dict) else {"raw": data}

    async def _act(self, phone: str, kind: str, detail: dict, method: str, path: str, **kw) -> dict:
        if self.mocked:
            log.info("wati_mock_action", phone=phone, kind=kind, **detail)
            return self._record(phone, kind, kind=kind, extra={"detail": detail})
        data = await self._call(method, path, **kw)
        self._record(phone, kind, kind=kind, extra={"detail": detail, "sent": True})
        return data

    async def add_tag(self, phone: str, tag: str) -> dict:
        """The tag must already exist in WATI - applying an unknown one is refused (404)."""
        return await self._act(phone, "tag_add", {"tag": tag}, "POST", f"/api/ext/v3/conversations/{phone}/tags",
                               json={"tag_name": tag})

    async def remove_tag(self, phone: str, tag: str) -> dict:
        return await self._act(phone, "tag_remove", {"tag": tag}, "DELETE",
                               f"/api/ext/v3/conversations/{phone}/tags/{quote(tag, safe='/')}")

    async def assign_operator(self, phone: str, email: str | None) -> dict:
        """No email hands the conversation back to the bot operator, per WATI's docs."""
        return await self._act(phone, "assign_operator", {"email": email or ""}, "PUT",
                               f"/api/ext/v3/conversations/{phone}/operator", json={"assignee_email": email or None})

    async def assign_teams(self, phone: str, teams: list[str]) -> dict:
        return await self._act(phone, "assign_teams", {"teams": teams}, "PUT", "/api/ext/v3/contacts/teams",
                               json={"target": phone, "teams": teams})

    async def set_chat_status(self, phone: str, status: str) -> dict:
        """open | pending | solved | block. WATI only allows the first three within 24 hours of the
        customer's last message."""
        return await self._act(phone, "chat_status", {"status": status}, "PUT",
                               f"/api/ext/v3/conversations/{phone}/status", json={"new_status": status})

    async def set_contact_attributes(self, phone: str, attributes: dict[str, str]) -> dict:
        """v1 updateContactAttributes: custom fields on the WATI contact."""
        params = [{"name": k, "value": str(v)} for k, v in attributes.items()]
        return await self._act(phone, "contact_attributes", {"attributes": attributes}, "POST",
                               f"/api/v1/updateContactAttributes/{phone}", json={"customParams": params})

    async def send_template_message(self, phone: str, name: str, params: dict[str, str], broadcast: str) -> dict:
        """v1 sendTemplateMessage. WATI's schema asks for the business number as channel_number; it is
        sent when set in Settings and left out otherwise, which single-number accounts accept."""
        body: dict = {"template_name": name, "broadcast_name": broadcast[:60] or name,
                      "parameters": [{"name": k, "value": str(v)} for k, v in params.items()]}
        channel = get_settings().wati_channel_number.strip()
        if channel:
            body["channel_number"] = channel
        return await self._act(phone, "template_send", {"template": name, "params": params}, "POST",
                               "/api/v1/sendTemplateMessage", params={"whatsappNumber": phone}, json=body)

    async def send_file_url(self, phone: str, url: str, caption: str = "", media_type: str = "image") -> dict:
        """A picture, video, document or voice note from a public link.

        WATI's sendSessionFile only takes an uploaded file, so the file is fetched first (public
        addresses only, 16 MB at most). If that fails the customer still gets the caption and the
        link as text - never silence."""
        if self.mocked:
            log.info("wati_mock_send_file", phone=phone, url=url, caption=caption)
            return self._record(phone, caption, kind="file", extra={"url": url, "media_type": media_type})
        from .workflow.actions import check_url  # lazy: keeps the client free of workflow imports

        try:
            problem = check_url(url)
            if problem:
                raise WatiRejected(problem)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                got = await client.get(url)
            if got.status_code >= 400:
                raise WatiRejected(f"the file link answered {got.status_code}")
            if len(got.content) > FILE_MAX_BYTES:
                raise WatiRejected("the file is over 16 MB")
            files = {"file": (file_name_of(url), got.content,
                              got.headers.get("content-type") or "application/octet-stream")}
            data = await self._call("POST", f"/api/v1/sendSessionFile/{phone}",
                                    params={"caption": caption} if caption else None, files=files)
            self._record(phone, caption, kind="file", extra={"url": url, "media_type": media_type, "sent": True})
            return data
        except Exception as e:  # noqa: BLE001 - a broken link must not leave the customer with nothing
            log.warning("wati_file_failed_fallback_text", url=url, error=str(e))
            return await self.send_text(phone, "\n".join(x for x in (caption, url) if x))

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

    # ---- Meta-approved message templates ----
    # These are the only way to message someone who has NOT written in the last 24 hours. WATI
    # forwards them to Meta for approval, which is why creating one returns a status rather than a
    # finished template. Endpoints per docs.wati.io; the create call is still v1.
    @staticmethod
    def template_payload(name: str, language: str, body: str, *, category: str = "UTILITY",
                         header: dict | str | None = None, footer: str = "",
                         buttons: list[dict] | None = None, samples: dict | None = None) -> dict:
        """The body WATI expects for a new template. Pure, so the shape is unit-tested without a
        network call - a field invented here would pass our own tests and fail against Meta.

        `samples` matters more than it looks: Meta reviews a template by reading it with the example
        values filled in, so {{name}} with no example is reviewed as a literal "{{name}}" and is a
        common reason for a rejection nobody can explain."""
        payload: dict = {
            "type": "template",
            "category": category,
            "subCategory": "STANDARD",
            "elementName": name,
            "language": language,
            "body": body,
            "footer": footer or "",
            "buttonsType": WatiClient._buttons_type(buttons or []),
            "creationMethod": 0,  # HUMAN
        }
        head = WatiClient.header_payload(header)
        if head:
            payload["header"] = head
        if buttons:
            payload["buttons"] = [WatiClient.button_payload(b) for b in buttons[:3]]
        variables = WatiClient.template_variables(body)
        if variables:
            payload["customParams"] = [{"paramName": v, "paramValue": str((samples or {}).get(v, "")) or v}
                                       for v in variables]
        return payload

    @staticmethod
    def header_payload(header: dict | str | None) -> dict:
        """Text or media at the top of a template. Media is given to WATI as a public link."""
        if not header:
            return {}
        if isinstance(header, str):
            return {"type": "TEXT", "text": header}
        kind = str(header.get("type") or "TEXT").upper()
        if kind == "TEXT":
            text = str(header.get("text") or "")
            return {"type": "TEXT", "text": text} if text else {}
        link = str(header.get("link") or "")
        if kind not in ("IMAGE", "VIDEO", "DOCUMENT") or not link:
            return {}
        return {"type": kind, "link": link, "mediaHeaderId": str(header.get("media_header_id") or "")}

    @staticmethod
    def button_payload(b: dict) -> dict:
        """A template button. WATI splits these into a quick reply, a link and a phone call, each
        with its own required field - a URL button with no url is silently useless."""
        kind = str(b.get("type") or "quick_reply").lower()
        text = str(b.get("text") or "")
        if kind == "url":
            return {"type": "url", "parameter": {"text": text, "url": str(b.get("url") or ""),
                                                 "phoneNumber": "", "urlType": "static"}}
        if kind in ("call", "phone"):
            return {"type": "call", "parameter": {"text": text, "phoneNumber": str(b.get("phone") or ""),
                                                  "url": "", "urlType": "none"}}
        return {"type": "quick_reply", "parameter": {"text": text}}

    @staticmethod
    def _buttons_type(buttons: list[dict]) -> str:
        kinds = {str(b.get("type") or "quick_reply").lower() for b in buttons}
        if not kinds:
            return "NONE"
        cta = {"url", "call", "phone"}
        if kinds <= {"quick_reply"}:
            return "quick_reply"
        if kinds <= cta:
            return "call_to_action"
        return "quick_reply_and_call_to_action"

    @staticmethod
    def template_variables(body: str) -> list[str]:
        """Meta templates use {{name}}, not the {name} the bot's own messages use. Kept separate so
        nobody 'fixes' one into the other."""
        return sorted({m.group(1) for m in re.finditer(r"\{\{([A-Za-z0-9_]{1,40})\}\}", body or "")})

    async def create_template(self, payload: dict) -> dict:
        """Submit a template to WATI, which passes it to Meta. Approval is not instant and is not
        ours to grant - the response only says it was accepted for review."""
        if self.mocked:
            self._record("", payload.get("elementName", ""), kind="template_create", extra={"payload": payload})
            return {"ok": True, "mocked": True, "result": {"newStatus": "PENDING"}}
        data = await self._post(WHATSAPP_TEMPLATES, json=payload)
        log.info("wati_template_created", name=payload.get("elementName"))
        return data

    async def list_templates(self, page: int = 1, page_size: int = 100) -> list[dict]:
        """Every template with its Meta approval state. Returns [] rather than raising when the
        account cannot list them, so the page still renders."""
        if self.mocked:
            return []
        data = await self._get(MESSAGE_TEMPLATES, params={"page_number": page, "page_size": page_size})
        for field in ("result", "data", "items", "messageTemplates"):
            got = data.get(field) if isinstance(data, dict) else None
            if isinstance(got, list):
                return got
        return data if isinstance(data, list) else []

    async def delete_template(self, name: str, waba_id: str, language: str = "") -> dict:
        """WATI needs the WABA id here, which is the one template setting that is not the API token."""
        if not waba_id:
            raise WatiRejected(
                "Deleting a template needs your WhatsApp Business Account id (WABA id), which is not "
                "set. Add it in Settings - WATI shows it under your channel."
            )
        if self.mocked:
            self._record("", name, kind="template_delete")
            return {"ok": True, "mocked": True}
        path = f"{WHATSAPP_TEMPLATES}/{waba_id}/{name}" + (f"/{language}" if language else "")
        return await self._delete(path)

    async def _delete(self, path: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.delete(self._url(path), headers=self._headers())
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
        return data if isinstance(data, dict) else {"raw": data}


wati = WatiClient()
