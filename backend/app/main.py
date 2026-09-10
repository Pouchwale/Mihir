from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import dispose_db, init_db
from .jobs import customer_sync, order_refresh, queue_worker, scheduler
from .logging_setup import setup_logging
from .routers import admin, connections, health, templates as templates_router, webhook
from .services import preflight, settings_store, templates

log = structlog.get_logger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static" / "admin"


async def _dev_autoload() -> None:
    """In dev, make `uvicorn` alone enough: load the dummy fixtures if tables are empty."""
    try:
        if await customer_sync.count() == 0:
            await customer_sync.run()
        if await order_refresh.count() == 0:
            await order_refresh.run()
    except Exception as e:  # noqa: BLE001
        log.warning("dev_autoload_failed", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    # In prod, refuse to serve with an unsafe configuration - an open dashboard or an open webhook is
    # worse than being down. Checked before anything else starts.
    problems = preflight.fatal_problems(s)
    if problems:
        for p in problems:
            log.error("startup_blocked", problem=p)
        if not s.allow_insecure_prod:
            raise RuntimeError(
                "Refusing to start in production mode until these are fixed in backend/.env:\n  - "
                + "\n  - ".join(problems)
                + "\n(Set ALLOW_INSECURE_PROD=true to start anyway - not on a real server.)"
            )
        log.warning("startup_insecure_allowed", count=len(problems))
    await init_db()
    await settings_store.load_from_db()
    await templates.load_from_db()
    worker = asyncio.create_task(queue_worker.run_forever())
    scheduler.start()
    if s.is_dev:
        await _dev_autoload()
    elif await order_refresh.count() == 0:
        asyncio.create_task(order_refresh.run())
    log.info("app_started", mode=s.app_mode, wati_mocked=s.wati_mocked, orders_source=s.orders_source, db=s.database_url.split("@")[-1])
    try:
        yield
    finally:
        scheduler.shutdown()
        queue_worker.stop()
        worker.cancel()
        await dispose_db()


_startup = get_settings()  # app_mode cannot be changed from the dashboard, so reading it once is safe

# In production the dashboard is served from this same origin, so CORS is not needed at all, and the
# API docs would only advertise the admin surface to strangers.
app = FastAPI(
    title="WhatsApp Order Status Bot",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _startup.is_dev else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _startup.is_dev else None,
)
if _startup.is_dev:
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])
app.include_router(webhook.router)
app.include_router(health.router)
app.include_router(admin.router)
app.include_router(templates_router.router)
app.include_router(connections.router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin/")


if STATIC_DIR.exists():
    app.mount("/admin/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="admin-assets")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    @app.get("/admin/{path:path}", include_in_schema=False)
    async def admin_spa(request: Request, path: str = ""):
        if path.startswith("api/"):
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        candidate = STATIC_DIR / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
else:

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_missing():
        return {"detail": "Dashboard not built. Run `npm run build` in /dashboard (outputs to backend/app/static/admin)."}
