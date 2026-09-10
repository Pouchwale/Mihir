r"""One command that says what is wrong and what to do about it.

    cd backend
    .\.venv\Scripts\python -m app.doctor

Reads backend/.env in a fresh process (so it always shows the CURRENT file, not what a running
server loaded), tests the live connections, and prints the fix for everything that is not ready.
No secret is ever printed - only whether it is set, how long it is, and whether it works.
"""
from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit

SYMBOL = {"pass": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]"}


def _mask(value: str, keep: int = 4) -> str:
    if not value:
        return "(empty)"
    return f"set, {len(value)} characters, ends ...{value[-keep:]}" if len(value) > keep else "set (very short)"


async def main() -> int:
    from .config import ENV_FILE, get_settings
    from .db import init_db
    from .services import preflight, settings_store, templates

    print("WhatsApp Order Status Bot - checkup")
    print("=" * 78)
    if not ENV_FILE.exists():
        print(f"\n[FAIL] {ENV_FILE} does not exist.")
        print("       Create it:  copy .env.example .env   then fill in the values.")
        return 1

    s = get_settings()
    parts = urlsplit(s.wati_base_url)
    tenant = parts.path.strip("/")
    print(f"\nReading {ENV_FILE}\n")
    print("  APP_MODE            :", s.app_mode)
    print("  WATI_BASE_URL       :", f"{parts.scheme}://{parts.netloc}/{tenant or '(nothing after the slash)'}")
    print("  ...tenant id        :", "STILL THE PLACEHOLDER <tenantId>" if "<" in tenant else (f"set ({tenant})" if tenant else "MISSING"))
    print("  WATI_TOKEN          :", _mask(s.wati_token))
    print("  WATI_WEBHOOK_TOKEN  :", "STILL THE EXAMPLE VALUE" if s.wati_webhook_token == "change-me-webhook-token" else _mask(s.wati_webhook_token))
    print("  ADMIN_KEY           :", _mask(s.admin_key))
    print("  Messages simulated? :", "YES - nothing reaches WhatsApp" if s.wati_mocked else "no - messages go to real customers")

    try:
        await init_db()
        await settings_store.load_from_db()
        await templates.load_from_db()
    except Exception as e:  # noqa: BLE001
        print(f"\n[FAIL] Cannot open the database: {e}")
        print(f"       Check DATABASE_URL in {ENV_FILE}.")
        return 1

    print("\nChecking everything (this calls WATI, the order source and the customer file)...")
    report = await preflight.run_checks(deep=True, base_url="", running_server=False)
    group = None
    for c in report["checks"]:
        if c["group"] != group:
            group = c["group"]
            print(f"\n{group}")
            print("-" * 78)
        print(f"  {SYMBOL[c['status']]} {c['title']}: {c['detail'][:100]}")
        if c["fix"]:
            where = f" ({c['where']})" if c["where"] else ""
            for line in c["fix"].splitlines():
                print(f"          -> {line}{where}")
                where = ""

    print("\n" + "=" * 78)
    counts = report["counts"]
    if report["ready"]:
        print(f"READY. {counts['pass']} checks passed, {counts['warn']} worth a look.")
    else:
        print(f"NOT READY: {counts['fail']} must be fixed (and {counts['warn']} worth a look).")
        print("Fix the [FAIL] lines above, then RESTART the bot and run this again.")
    print("Everything here is also on the dashboard's 'Go live' page.")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
