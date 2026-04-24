"""FastAPI web application for docstats."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import (
    http_exception_handler as fastapi_default_http_exception_handler,
)
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from docstats.auth import (
    ANON_SEARCH_LIMIT,
    AuthRequiredException,
    get_anon_search_count,
    get_current_user,
    require_user,
)
from docstats.domain.seed import seed_platform_defaults
from docstats.routes._common import MAPBOX_TOKEN, US_STATES, get_client, render, saved_count  # noqa: F401 — get_client re-exported for test compatibility
from docstats.routes.admin import router as admin_router
from docstats.routes.admin_deliveries import router as admin_deliveries_router
from docstats.routes.api import router as api_router
from docstats.routes.api_v2 import (
    http_exception_handler as api_v2_http_exception_handler,
    router as api_v2_router,
)
from docstats.routes.auth import router as auth_router
from docstats.routes.attachments import router as attachments_router
from docstats.routes.exports import router as exports_router
from docstats.routes.imports import router as imports_router
from docstats.routes.invite import router as invite_router
from docstats.routes.onboarding import router as onboarding_router
from docstats.routes.patients import router as patients_router
from docstats.routes.profile import router as profile_router
from docstats.routes.referrals import router as referrals_router
from docstats.routes.delivery import router as delivery_router
from docstats.routes.share import router as share_router
from docstats.routes.webhooks_vendor import router as webhooks_vendor_router
from docstats.routes.providers import router as providers_router
from docstats.routes.saved import router as saved_router
from docstats.routes.search import router as search_router
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)


# --- Lifespan: idempotent boot-time seeding of platform-default rules.
# Replaces the deprecated @app.on_event("startup") hook. Failure is
# non-fatal — a Supabase blip during deploy shouldn't knock the web up.
#
# Tests that use ``TestClient(app)`` trigger the lifespan, which calls
# ``get_storage()`` directly (lifespan runs BEFORE per-request dependency
# overrides, so ``app.dependency_overrides[get_storage]`` doesn't apply
# here). To prevent real-DB mutation from test runs, set
# ``DOCSTATS_SKIP_BOOT_SEED=1`` in the test environment. Route-scoped
# tests don't need the lifespan to seed anyway — they either stub the
# storage via DI or seed explicitly via ``seed_platform_defaults``.


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if os.environ.get("DOCSTATS_SKIP_BOOT_SEED") == "1":
        logger.debug("DOCSTATS_SKIP_BOOT_SEED=1 — skipping boot-time rule seed")
    else:
        try:
            storage = get_storage()
            counts = seed_platform_defaults(storage)
            logger.info("seeded platform defaults: %s", counts)
        except Exception:
            logger.exception("seed_platform_defaults failed at boot (continuing)")

    # Phase 9.A: delivery dispatcher. Disabled under tests
    # (DOCSTATS_SKIP_DELIVERY_DISPATCHER=1) so TestClient doesn't start
    # a long-running background task that survives the test. Prod /
    # Railway leaves the variable unset so the dispatcher runs.
    dispatcher_task: "asyncio.Task | None" = None
    dispatcher_stop = asyncio.Event()
    if os.environ.get("DOCSTATS_SKIP_DELIVERY_DISPATCHER") != "1":
        from docstats.delivery.dispatcher import run as _dispatcher_run

        async def _render_packet_wire(_delivery):  # type: ignore[no-untyped-def]
            # Dispatcher-side packet render is stubbed in 9.A — no
            # channels are live, so the dispatcher never reaches a
            # successful ``Channel.send()`` call where render output
            # matters. 9.B wires this to ``exports.render_packet``.
            return b""

        try:
            dispatcher_task = asyncio.create_task(
                _dispatcher_run(
                    get_storage(),
                    render_packet=_render_packet_wire,
                    stop_event=dispatcher_stop,
                ),
                name="delivery-dispatcher",
            )
            logger.info("Delivery dispatcher task scheduled")
        except Exception:
            logger.exception("Failed to start delivery dispatcher (continuing)")

    # Phase 10.C: attachment retention sweep.  Disabled under tests via
    # DOCSTATS_SKIP_ATTACHMENT_RETENTION=1.  Policy: daily sweep; tuned
    # lower in dev/sandbox envs via ATTACHMENT_RETENTION_INTERVAL_SECONDS.
    retention_task: "asyncio.Task | None" = None
    retention_stop = asyncio.Event()
    if os.environ.get("DOCSTATS_SKIP_ATTACHMENT_RETENTION") != "1":
        from docstats.storage_files.factory import get_file_backend
        from docstats.storage_files.retention import run as _retention_run

        try:
            retention_task = asyncio.create_task(
                _retention_run(
                    get_storage(),
                    get_file_backend(),
                    stop_event=retention_stop,
                ),
                name="attachment-retention",
            )
            logger.info("Attachment retention sweep scheduled")
        except Exception:
            logger.exception("Failed to start attachment retention sweep (continuing)")

    try:
        yield
    finally:
        if dispatcher_task is not None:
            dispatcher_stop.set()
            try:
                await asyncio.wait_for(dispatcher_task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning("Delivery dispatcher did not shut down cleanly")
        if retention_task is not None:
            retention_stop.set()
            try:
                await asyncio.wait_for(retention_task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning("Attachment retention sweep did not shut down cleanly")


app = FastAPI(
    title="docstats",
    description="NPI Registry lookup for HMO referrals",
    lifespan=_lifespan,
)

# --- Static files ---
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# --- Session middleware (must be added before @app.middleware decorators) ---
_SESSION_SECRET = os.environ.get("SESSION_SECRET_KEY") or secrets.token_hex(32)
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    max_age=604800,  # 7 days
    https_only=os.environ.get("RAILWAY_ENVIRONMENT") == "production",
    same_site="lax",
)


# --- X-Robots-Tag (keep crawlers out) ---
@app.middleware("http")
async def add_robots_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# --- Exception handlers ---


@app.exception_handler(AuthRequiredException)
async def auth_exception_handler(request: Request, exc: AuthRequiredException):
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/auth/login"})
    return RedirectResponse("/auth/login", status_code=303)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Path-scoped HTTPException handler.

    /api/v2/* routes run through the content-negotiated handler so FHIR
    clients get OperationOutcome bodies with the right Content-Type on
    401 / 403 / 404 / 409 etc. Every other path falls through to
    FastAPI's default handler, which preserves the existing
    ``{"detail": "..."}`` shape for web routes.
    """
    if request.url.path.startswith("/api/v2/"):
        return await api_v2_http_exception_handler(request, exc)
    return await fastapi_default_http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return HTMLResponse(content="Internal Server Error", status_code=500)


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    """Block all crawlers."""
    return "User-agent: *\nDisallow: /\n"


# --- /saved → /rolodex legacy redirects (Phase 2.E).
# Users bookmarked the old paths pre-rename; 301-redirect permanently so the
# browser and search engines update their cache. Keeping these as dedicated
# routes (rather than a catch-all middleware) keeps the surface auditable.
# Query strings are forwarded so UTM params, filter/sort params, etc.
# survive the rename.


def _rolodex_redirect(request: Request, target: str) -> RedirectResponse:
    qs = request.url.query
    dest = f"{target}?{qs}" if qs else target
    return RedirectResponse(dest, status_code=301)


@app.get("/saved")
async def _saved_redirect(request: Request):
    return _rolodex_redirect(request, "/rolodex")


@app.get("/saved/export")
async def _saved_export_redirect(request: Request):
    return _rolodex_redirect(request, "/rolodex/export")


@app.get("/saved/export/csv")
async def _saved_export_csv_redirect(request: Request):
    return _rolodex_redirect(request, "/rolodex/export/csv")


@app.get("/saved/export/json")
async def _saved_export_json_redirect(request: Request):
    return _rolodex_redirect(request, "/rolodex/export/json")


# --- Include routers.
# Order matters: specific routes before parameterized ones sharing a prefix.
# Historical examples:
#   - ``saved_router`` before ``providers_router`` so ``/saved/export/csv``
#     beats ``/provider/{npi}``.
#   - ``exports_router`` before ``referrals_router``: both use
#     ``prefix="/referrals"``. ``/referrals/{id}/export.pdf`` has two segments
#     and doesn't collide with ``/referrals/{id}`` today, but a future 5.B/5.C
#     single-segment export route could shadow referral detail if the order
#     were reversed. Keep exports first as the 5.x surface grows.

app.include_router(auth_router)
app.include_router(onboarding_router)
app.include_router(profile_router)
app.include_router(search_router)
app.include_router(api_router)
app.include_router(api_v2_router)
app.include_router(admin_router)
app.include_router(admin_deliveries_router)
app.include_router(invite_router)
app.include_router(patients_router)
app.include_router(imports_router)
app.include_router(exports_router)
app.include_router(delivery_router)
app.include_router(share_router)
app.include_router(webhooks_vendor_router)
app.include_router(attachments_router)
app.include_router(referrals_router)
app.include_router(saved_router)
app.include_router(providers_router)


# --- Home and history (kept in web.py — simple, depend on shared state) ---


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    current_user: dict | None = Depends(get_current_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"] if current_user else None
    anon_remaining = (
        None if current_user else max(0, ANON_SEARCH_LIMIT - get_anon_search_count(request))
    )
    return render(
        "index.html",
        {
            "request": request,
            "active_page": "search",
            "states": US_STATES,
            "q": {},
            "initial_results": False,
            "mapbox_token": MAPBOX_TOKEN,
            "saved_count": saved_count(storage, user_id),
            "user": current_user,
            "anon_searches_remaining": anon_remaining,
        },
    )


@app.get("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    limit: int = Query(50),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    entries = storage.get_history(limit=limit, user_id=user_id)
    return render(
        "history.html",
        {
            "request": request,
            "active_page": "history",
            "entries": entries,
            "saved_count": saved_count(storage, user_id),
            "user": current_user,
        },
    )
