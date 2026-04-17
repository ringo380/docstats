"""Authentication routes: login, signup, logout, GitHub OAuth."""

from __future__ import annotations

import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from docstats.auth import get_current_user, hash_password, verify_password
from docstats.domain import audit
from docstats.domain.audit import client_ip
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
from docstats.storage_base import StorageBase
from docstats.validators import (
    EMAIL_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    ValidationError,
    validate_email,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    current_user: dict | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse("/", status_code=303)
    return render(
        "login.html",
        {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": request.session.pop("flash_error", None),
            "github_enabled": GITHUB_ENABLED,
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    email: str = Form("", max_length=EMAIL_MAX_LENGTH),
    password: str = Form("", max_length=PASSWORD_MAX_LENGTH),
    storage: StorageBase = Depends(get_storage),
):
    if not email.strip() or not password:
        return render(
            "login.html",
            {
                "request": request,
                "active_page": None,
                "saved_count": 0,
                "user": None,
                "error": "Email and password are required.",
                "github_enabled": GITHUB_ENABLED,
            },
        )

    generic_error = render(
        "login.html",
        {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": "Invalid email or password.",
            "github_enabled": GITHUB_ENABLED,
        },
    )

    # Validate format before the storage lookup. Collapse format errors
    # into the generic "invalid email or password" response so malformed
    # input can't be used to probe for account existence
    # (enumeration resistance).
    try:
        email = validate_email(email)
    except ValidationError:
        return generic_error

    user = storage.get_user_by_email(email)
    if (
        not user
        or not user.get("password_hash")
        or not verify_password(password, user["password_hash"])
    ):
        audit.record(
            storage,
            action="user.login_failed",
            request=request,
            metadata={"email_hint": email[:3]} if email else None,
        )
        return generic_error

    _begin_session(request, storage, user["id"])
    storage.update_last_login(user["id"])
    audit.record(
        storage,
        action="user.login",
        request=request,
        actor_user_id=user["id"],
        scope_user_id=user["id"],
    )
    return RedirectResponse("/", status_code=303)


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(
    request: Request,
    current_user: dict | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse("/", status_code=303)
    return render(
        "signup.html",
        {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": None,
            "github_enabled": GITHUB_ENABLED,
        },
    )


@router.post("/signup", response_class=HTMLResponse)
async def signup_post(
    request: Request,
    email: str = Form("", max_length=EMAIL_MAX_LENGTH),
    password: str = Form("", max_length=PASSWORD_MAX_LENGTH),
    confirm_password: str = Form("", max_length=PASSWORD_MAX_LENGTH),
    storage: StorageBase = Depends(get_storage),
):
    def _err(msg: str):
        return render(
            "signup.html",
            {
                "request": request,
                "active_page": None,
                "saved_count": 0,
                "user": None,
                "error": msg,
                "github_enabled": GITHUB_ENABLED,
            },
        )

    if not email.strip() or not password:
        return _err("Email and password are required.")
    try:
        email = validate_email(email)
    except ValidationError as exc:
        return _err(str(exc))
    if len(password) < 8:
        return _err("Password must be at least 8 characters.")
    if password != confirm_password:
        return _err("Passwords do not match.")
    if storage.get_user_by_email(email):
        return _err("An account with that email already exists.")

    user_id = storage.create_user(email, hash_password(password))
    _begin_session(request, storage, user_id)
    audit.record(
        storage,
        action="user.signup",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
    )
    return RedirectResponse("/onboarding", status_code=303)


@router.get("/logout")
async def logout(
    request: Request,
    storage: StorageBase = Depends(get_storage),
):
    prior_user_id = request.session.get("user_id")
    prior_session_id = request.session.get("session_id")
    request.session.clear()
    if prior_session_id:
        try:
            storage.revoke_session(prior_session_id)
        except Exception:
            logger.exception("Failed to revoke session on logout")
    if prior_user_id:
        audit.record(
            storage,
            action="user.logout",
            request=request,
            actor_user_id=prior_user_id,
            scope_user_id=prior_user_id,
        )
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
    _begin_session(request, storage, user_id)
    user = storage.get_user_by_id(user_id)
    audit.record(
        storage,
        action="user.login_github",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
        metadata={"github_login": gh_user.get("login")},
    )
    if user and user.get("terms_accepted_at"):
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/onboarding", status_code=303)


def _begin_session(request: Request, storage: StorageBase, user_id: int) -> None:
    """Open a new server-side session row and seat the cookie.

    Always a fresh session_id per login — guarantees privilege-transition
    rotation. Any stale session_id currently in the cookie is revoked first so
    concurrent uses of the old token die immediately (session fixation defense).

    Also wipes the session dict entirely before seeding user_id/session_id —
    onboarding flags (``pcp_skipped``, ``onboarding_done``), flash messages,
    the anonymous-search counter, and any other per-session state from a
    prior user must not carry over on a re-login without explicit logout
    (e.g., GitHub OAuth callback on an already-authed browser).
    """
    prior = request.session.get("session_id")
    if prior:
        try:
            storage.revoke_session(prior)
        except Exception:
            logger.exception("Failed to revoke prior session during login")

    # Full reset — prior user's flags must not leak into the new session.
    request.session.clear()

    ip = client_ip(request)
    ua = request.headers.get("User-Agent")
    ua = ua[:500] if ua else None
    try:
        session = storage.create_session(user_id=user_id, ip=ip, user_agent=ua)
        request.session["session_id"] = session.id
    except Exception:
        # DB down? Fall back to cookie-only session — degraded but usable.
        # get_current_user grandfathers this in; the cookie upgrades to a
        # proper session row the next time login succeeds with DB reachable.
        logger.exception("Failed to create server-side session; falling back to cookie-only")
    request.session["user_id"] = user_id
