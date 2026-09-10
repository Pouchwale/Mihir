# WhatsApp Order Status Bot

A self-service WhatsApp assistant that lets verified customers check the production status of their own
orders. Customers pick from tappable menus in English, Hindi or Gujarati; the bot verifies who they are,
looks up only their own orders, and replies with the current status.

Two moving parts: **WATI** carries the WhatsApp messages, and **this service** holds all the logic, the
data connections and the admin dashboard. No workflow builder, no automation platform, no containers.

```
Customer message ─▶ WATI ─── webhook ──▶  Order Status Bot
                                            1. verify the sender against the customer master (SAP export)
                                            2. read that customer's rows from the production table
                                               — matching the customer name exactly
                                            3. offer their order numbers as tappable options
                                            4. offer the items within the chosen order
                                            5. reply with the production status
Customer        ◀─── WATI ◀───────────────────────────────────────────────────────────┘
```

**Only the production status field is ever sent to a customer.** Internal fields and other customers'
data never leave the server, and there is a test that proves it.

---

## Contents

1. [Features](#1-features) · 2. [Quick start](#2-quick-start) · 3. [The conversation](#3-the-conversation) ·
4. [Editing what customers read](#4-editing-what-customers-read) · 5. [Connecting WATI](#5-connecting-wati) ·
6. [Data sources](#6-data-sources) · 7. [Going live](#7-going-live) ·
8. [Operations](#8-operations) · 9. [Security](#9-security) · 10. [Development](#10-development)

---

## 1. Features

- **Tap-driven conversation** in English, Hindi and Gujarati, using WhatsApp reply buttons and list
  messages, with typed input accepted everywhere as well.
- **Two-stage verification**: the sender's number must appear in the customer master, and the customer
  name on an order must match that record exactly before anything is disclosed.
- **Every message is editable** from the dashboard — wording, button labels and which buttons appear —
  with live preview and validation. No restart, no code change.
- **Pluggable data sources**: the order table can come from an API, a SQL query or a file (JSON, CSV,
  Excel or HTML), and the customer master from a folder, a network share, Dropbox or an HTTPS link.
- **Operational tooling**: a go-live readiness check, a diagnostics view of both message directions, a
  command-line health check, scheduled imports, alerting and backups.

## 2. Quick start

Runs locally with no WATI account and no production data — WhatsApp is simulated and sample data is
loaded automatically.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open **http://localhost:8000/admin** and sign in with the `ADMIN_KEY` value from `backend/.env`.

Use **Simulator** to hold a full conversation as a customer: pick a sample customer, send `hi`, choose a
language, then tap through to a status. What you see there is exactly what WhatsApp will show.

Rebuilding the dashboard is only needed after changing `dashboard/src`:

```powershell
cd dashboard
npm install
npm run build        # outputs into backend/app/static/admin, served by the backend
```

## 3. The conversation

```
first message of a window ─▶ greeting
                             "Please choose your language"   [English] [हिंदी] [ગુજરાતી]
                       ─▶ "Hello <customer>, how can we help you today?"
                             [Order status] [Change language] [Contact us]
Order status ─▶ the customer's own order numbers ─▶ the items in that order
             ─▶ the production status              [Check another] [Main menu] [Done]
```

| Situation | Reply |
|---|---|
| First message after 30 minutes of silence, or after *Done* | Greeting, then the language question |
| Language chosen, or "menu" | Main menu |
| Order status | The customer's own orders — buttons for three or fewer, otherwise a list |
| An order with several items | The item codes, buttons or list |
| Status delivered | The status, with options to check another or finish |
| Order or item not found | An apology with a way back to their order list |
| Sender not in the customer master, or a name mismatch | A polite refusal in all three languages, with your support contact |

A tapped option arrives from WATI as its own text, so tapped and typed answers pass through exactly the
same parser and the same checks. If WATI declines an interactive message, the same content is re-sent as
plain text listing the options, so the customer can always reply.

The chosen language stays for the whole conversation regardless of what the customer types next.

## 4. Editing what customers read

**Dashboard → Messages.** Built for someone who does not write code, and nothing here needs a restart.

- **Conversation map** — the whole chat drawn out; click any bot message to edit it.
- **Change the words** in all three languages, with a live WhatsApp-style preview.
- **Insert real values** by clicking a chip (order number, item code, status, support contact) — no
  placeholder syntax to remember.
- **Choose the buttons** under each message, within WhatsApp's limits. Renaming is safe: the bot learns
  the new name, so a renamed *Done* still ends the conversation.
- **Send a test** to a real WhatsApp number, including unsaved edits.
- **History** of every change, and **Restore built-in** at any time.
- **Custom replies** answer your own keywords (opening hours, and so on) without touching the order flow.

Every save is validated — placeholders, WhatsApp length limits, and labels the bot would no longer
understand — and a change that would break a message is refused with a plain explanation.

## 5. Connecting WATI

All conversation logic lives in this service; WATI is used only to carry messages. Any chatbot,
auto-reply or keyword action configured inside WATI must be switched **off**, otherwise WATI answers
before this service sees the message.

**Credentials** (WATI → *Connector → API → Create API Token*; older accounts: *Settings → API Docs*):

| Value | Goes in | Notes |
|---|---|---|
| Tenant API endpoint | `WATI_BASE_URL` | must end with your own tenant id, e.g. `https://live-mt-server.wati.io/123456` |
| Access token | `WATI_TOKEN` | shown only once |
| Webhook secret | `WATI_WEBHOOK_TOKEN` | you choose this; WATI does not provide it |
| Public address | `PUBLIC_BASE_URL` | the HTTPS address customers' messages arrive on |

**Scopes.** WATI's tokens are scope-limited and WATI does not publish a full list, so match the picker to
the calls this service makes:

| Call | Purpose | Required |
|---|---|---|
| `POST /api/v1/sendSessionMessage/{number}` | every text reply | yes |
| `POST /api/v1/sendInteractiveButtonsMessage` | button menus | yes |
| `POST /api/v1/sendInteractiveListMessage` | list menus | yes |
| `GET`/`POST /api/v2/webhookEndpoints` | registering the webhook from the dashboard | yes, unless registered in the WATI portal |
| `GET /api/v1/getContacts` | connection test only | optional |
| `GET /api/v1/getMedia` | voice notes | only when `VOICE_NOTES=true` |

A token that cannot read contacts is reported as a warning, not a failure — sending is unaffected. All of
these endpoints are available on the Growth, Pro and Business plans.

**Webhook.** Register `https://<your-domain>/webhook/wati?token=<WATI_WEBHOOK_TOKEN>` for the event
*Message received*, either in the WATI portal or with **Register webhook in WATI** on the Go live page.

WATI retries any non-200 response and stops delivering events after repeated failures, so this service
answers `200` to everything it can absorb, reserving rejection for callers that cannot prove they are
WATI. Delivery health is shown on the Go live page.

## 6. Data sources

**Dashboard → Data sources.** Both connections are configured on screen — no file editing, no restart.
Saved values are stored in the database and take precedence over `.env`; **Reset** restores the `.env`
value. Passwords are encrypted before storage and never displayed again.

**Order data.** Choose an API endpoint (URL, method, key placement), a read-only database query, or a
file this server can read. Formats JSON, CSV, Excel and HTML tables are detected automatically. **Test
connection** fetches without saving and lets you map the columns from the names actually found. Order
number, customer name and status are required. Set the refresh interval and the staleness warning.

**Customer master.** Choose a folder or network share, Dropbox, or an HTTPS download link. Map the code,
name and phone columns, set the daily import time, and use **Test connection** to see exactly which rows
would be accepted or skipped, and why.

Phone numbers are normalised; invalid and duplicate entries are rejected and listed per run. Customer
names are stored **exactly** as supplied — the exact-match rule depends on it. Every import is listed
with the columns seen, the rows skipped and the reason.

## 7. Going live

**Dashboard → Go live** is the checklist. It tests the WATI connection, both data sources, the security
settings and the server, and prints the exact fix for anything that is not ready.

1. **Server** — any host reachable over HTTPS (WATI will not call plain HTTP and cannot reach a private
   address). PostgreSQL, MySQL and SQLite are all supported; paste the connection string your host
   gives you into `DATABASE_URL` and the async driver and TLS settings are applied for you.

   **On Render, Heroku, Cloud Run and similar the filesystem is wiped on every deploy.** SQLite there
   loses your customers, sessions, chat log and your edited messages each time you deploy, and the
   generated `backend/.secret_key` is recreated so saved connection passwords stop working. Use the
   host's managed PostgreSQL and set `SECRET_KEY` explicitly. The Go live page reports both as
   failures when it detects such a host.

   Self-hosting on MySQL instead:
   ```sql
   CREATE DATABASE order_bot CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
   ```
2. **Configure** `backend/.env` — see `.env.example`, which marks every value required for go-live. In
   production the service **refuses to start** while the admin key, webhook token, tenant URL or support
   contact is still an example value, and says which.
3. **HTTPS** — a certificate on the service directly, or a reverse proxy in front. Set `PUBLIC_BASE_URL`.
4. **Run at boot**, always as a **single** worker process (the message queue and the scheduled imports run
   inside the web process):
   - Windows: `backend\scripts\install_windows_task.ps1`
   - Linux: `backend/scripts/order-status-bot.service`
5. **Register the webhook** and switch off WATI's own automations.
6. **Verify** — press *Test the webhook address*, work through Go live until nothing is red, then message
   the number from a phone in the customer master and complete one lookup.

## 8. Operations

| Tool | Purpose |
|---|---|
| **Go live** | Every readiness check, each with its fix |
| **Diagnostics** | Both directions — what WATI sent us and what we sent WATI, including refusals and why |
| **Chat log** | Every message in and out, with the outcome |
| **Sessions** | Where each customer currently is in the conversation |
| **Mismatches** | Orders skipped because the customer name did not match exactly |
| `python -m app.doctor` | The same checks from the command line; prints no secrets |
| `GET /health` | Liveness, last import times and a staleness flag, for uptime monitoring |

Backups: `backend/scripts/backup_db.ps1` (Windows Task Scheduler) or `backup_db.sh` (cron) — both handle
MySQL and SQLite. Failures raise an alert in the dashboard, and to Slack when `ALERT_SLACK_WEBHOOK` is set.

## 9. Security

- The webhook is authenticated by a secret in its URL, compared in constant time, with size limits and
  flood protection for unauthenticated callers.
- The dashboard requires an admin key; in production the API documentation and cross-origin access are
  disabled.
- Connection passwords are encrypted at rest; secrets are never written to logs or shown in the UI.
- Only the production status field can reach a customer, enforced in code and covered by tests.
- Voice notes are off by default: speech-to-text can misread digits, and looking up the wrong order is
  worse than asking the customer to type.

## 10. Development

```powershell
cd backend
.\.venv\Scripts\python -m pytest -q          # set TEST_DATABASE_URL to run the same suite on MySQL
```

The suite covers the conversation in all three languages by tap and by typing, every failure path, the
WATI transport, production configuration, and a replay of the whole conversation after every message and
button has been renamed. Sample data used by the tests is generated by `backend/scripts/make_fixtures.py`;
keep your own test data in `backend/local/`, which is never committed.

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/webhook/wati?token=…` | webhook token | WATI entry point — deduplicates, queues, answers in under a second |
| GET | `/health` | none | liveness, last import times, staleness |
| GET | `/admin/` | admin key | dashboard |
| * | `/admin/api/*` | `X-Admin-Key` | overview, readiness, diagnostics, sessions, messages, imports, data sources, simulator, template editor |

### Layout

```
backend/app/
  main.py            startup: schema, settings, template cache, queue worker, scheduler; serves the dashboard
  config.py          every setting, read from .env
  models.py          customers, orders, sessions, message and webhook logs, queue, imports, templates
  routers/           webhook.py, admin.py, templates.py, connections.py, health.py
  services/          processor, state_machine, verify, intent, menus, replies, templates,
                     wati, stt, preflight, rate_limit, alerts, crypto, settings_store
  jobs/              queue_worker, customer_sync, order_refresh, session_cleanup, scheduler
  adapters/          order sources (http, sql, file) and parsers (json, csv, excel, html)
  doctor.py          command-line health check
backend/scripts/     fixture generator, service installers, backup scripts
backend/tests/       conversation, transport, configuration and regression suites
dashboard/           React + TypeScript + Tailwind, built into backend/app/static/admin
```
