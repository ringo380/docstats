"""Authentication routes: login, signup, logout, GitHub OAuth."""

from __future__ import annotations

import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from docstats.auth import get_current_user, hash_password, verify_password
from docstats.oauth import (
    GITHUB_ENABLED,
    github_authorize_url,
    github_exchange_code,
    github_get_emails,
    github_get_user,
    primary_github_email,
)
from docstats.routes._common import render
from docstats.storage import get_storage
from docstats.storage_base import StorageBase, normalize_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    current_user: dict | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse("/", status_code=303)
    return render("login.html", {
        "request": request,
        "active_page": None,
        "saved_count": 0,
        "user": None,
        "error": request.session.pop("flash_error", None),
        "github_enabled": GITHUB_ENABLED,
    })


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    storage: StorageBase = Depends(get_storage),
):
    email = normalize_email(email)
    if not email or not password:
        return render("login.html", {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": "Email and password are required.",
            "github_enabled": GITHUB_ENABLED,
        })

    user = storage.get_user_by_email(email)
    if not user or not user.get("password_hash") or not verify_password(password, user["password_hash"]):
        return render("login.html", {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": "Invalid email or password.",
            "github_enabled": GITHUB_ENABLED,
        })

    request.session["user_id"] = user["id"]
    request.session.pop("anon_searches", None)
    storage.update_last_login(user["id"])
    return RedirectResponse("/", status_code=303)


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(
    request: Request,
    current_user: dict | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse("/", status_code=303)
    return render("signup.html", {
        "request": request,
        "active_page": None,
        "saved_count": 0,
        "user": None,
        "error": None,
        "github_enabled": GITHUB_ENABLED,
    })


@router.post("/signup", response_class=HTMLResponse)
async def signup_post(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    storage: StorageBase = Depends(get_storage),
):
    email = normalize_email(email)

    def _err(msg: str):
        return render("signup.html", {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": msg,
            "github_enabled": GITHUB_ENABLED,
        })

    if not email or not password:
        return _err("Email and password are required.")
    if len(password) < 8:
        return _err("Password must be at least 8 characters.")
    if password != confirm_password:
        return _err("Passwords do not match.")
    if storage.get_user_by_email(email):
        return _err("An account with that email already exists.")

    user_id = storage.create_user(email, hash_password(password))
    request.session["user_id"] = user_id
    request.session.pop("anon_searches", None)
    return RedirectResponse("/onboarding", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/github")
async def github_login(request: Request):
    if not GITHUB_ENABLED:
        return RedirectResponse("/auth/login", status_code=303)
    state = secrets.token_urlsafe(16)
    request.session["github_state"] = state
    return RedirectResponse(github_authorize_url(state), status_code=303)


@router.get("/github/callback")
async def github_callback(
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
    storage: StorageBase = Depends(get_storage),
):
    expected_state = request.session.pop("github_state", None)
    if not code or not state or state != expected_state:
        return RedirectResponse("/auth/login?error=oauth", status_code=303)

    try:
        async with httpx.AsyncClient() as client:
            token_data = await github_exchange_code(code, client)
            access_token = token_data.get("access_token")
            if not access_token:
                return RedirectResponse("/auth/login?error=oauth", status_code=303)

            gh_user = await github_get_user(access_token, client)
            gh_emails = await github_get_emails(access_token, client)
    except Exception:
        logger.exception("GitHub OAuth error")
        return RedirectResponse("/auth/login?error=oauth", status_code=303)

    email = primary_github_email(gh_emails) or gh_user.get("email")
    display_name = gh_user.get("name") or gh_user.get("login")
    user_id = storage.upsert_github_user(
        github_id=str(gh_user["id"]),
        github_login=gh_user["login"],
        email=email,
        display_name=display_name,
    )
    request.session["user_id"] = user_id
    request.session.pop("anon_searches", None)
    user = storage.get_user_by_id(user_id)
    if user and user.get("terms_accepted_at"):
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/onboarding", status_code=303)
