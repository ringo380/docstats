"""FastAPI web application for docstats."""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request, Response
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
from docstats.routes._common import MAPBOX_TOKEN, US_STATES, get_client, render, saved_count  # noqa: F401 — get_client re-exported for test compatibility
from docstats.routes.auth import router as auth_router
from docstats.routes.onboarding import router as onboarding_router
from docstats.routes.profile import router as profile_router
from docstats.routes.providers import router as providers_router
from docstats.routes.saved import router as saved_router
from docstats.routes.search import router as search_router
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

app = FastAPI(title="docstats", description="NPI Registry lookup for HMO referrals")

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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return HTMLResponse(content="Internal Server Error", status_code=500)


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    """Block all crawlers."""
    return "User-agent: *\nDisallow: /\n"


# --- Include routers (order matters: specific routes before parameterized) ---

app.include_router(auth_router)
app.include_router(onboarding_router)
app.include_router(profile_router)
app.include_router(search_router)
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
        None if current_user
        else max(0, ANON_SEARCH_LIMIT - get_anon_search_count(request))
    )
    return render("index.html", {
        "request": request,
        "active_page": "search",
        "states": US_STATES,
        "q": {},
        "initial_results": False,
        "mapbox_token": MAPBOX_TOKEN,
        "saved_count": saved_count(storage, user_id),
        "user": current_user,
        "anon_searches_remaining": anon_remaining,
    })


@app.get("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    limit: int = Query(50),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    entries = storage.get_history(limit=limit, user_id=user_id)
    return render("history.html", {
        "request": request,
        "active_page": "history",
        "entries": entries,
        "saved_count": saved_count(storage, user_id),
        "user": current_user,
    })
