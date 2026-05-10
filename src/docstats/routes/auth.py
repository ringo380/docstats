"""Authentication routes: login, signup, logout, GitHub OAuth."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from docstats.auth import get_current_user, hash_password, require_user, verify_password
from docstats.client import NPPESClient
from docstats.domain import audit
from docstats.domain.audit import client_ip
from docstats.domain.identity import ClinicianVerification, verify_clinician
from docstats.oauth import (
    GITHUB_ENABLED,
    github_authorize_url,
    github_exchange_code,
    github_get_emails,
    github_get_user,
    primary_github_email,
)
from docstats.routes._common import US_STATES, get_client, get_oig_client, render
from docstats.routes._rate_limit import RateLimiter
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

# Phase 15 R-007 — login throttling. Per-IP and per-account independent
# limiters; either tripping returns the same generic 429 to avoid leaking
# which dimension was hit. Limits target credential-stuffing + brute-force,
# not legitimate user typos.
_LOGIN_LIMIT_PER_IP = RateLimiter(max_attempts=20, window_seconds=900)  # 20/15min
_LOGIN_LIMIT_PER_ACCOUNT = RateLimiter(max_attempts=10, window_seconds=900)  # 10/15min

_US_STATE_CODES = frozenset(code for code, _ in US_STATES)
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z\-' .]{0,99}$")
_LICENSE_RE = re.compile(r"^[A-Za-z0-9\-]{1,40}$")
_CREDENTIALS_MAX = 80
_GENERIC_CLINICIAN_REJECT = (
    "We couldn't verify your clinician credentials. Please contact support@referme.help."
)


def _signup_context(
    request: Request,
    *,
    error: str | None = None,
    account_type: str = "patient",
    form: dict | None = None,
) -> dict:
    """Build the signup template context, preserving form state on error."""
    return {
        "request": request,
        "active_page": None,
        "saved_count": 0,
        "user": None,
        "error": error,
        "github_enabled": GITHUB_ENABLED,
        "account_type": account_type,
        "us_states": US_STATES,
        "form": form or {},
    }


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

    throttled_error = render(
        "login.html",
        {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": "Too many login attempts. Please wait a few minutes and try again.",
            "github_enabled": GITHUB_ENABLED,
        },
        status_code=429,
    )

    # Per-IP throttle first — protects against credential stuffing across
    # accounts from the same source. Falls back to "unknown" only if the
    # request truly has no client info (test client + no override).
    ip_key = client_ip(request) or "unknown"
    if not _LOGIN_LIMIT_PER_IP.allow(ip_key):
        audit.record(
            storage,
            action="user.login_throttled",
            request=request,
            metadata={"dimension": "ip"},
        )
        return throttled_error

    # Per-account throttle — keyed on the submitted email (lowercased)
    # before format validation so probing variations of one address still
    # counts. We deliberately count BEFORE looking the user up so the
    # decision doesn't leak account existence.
    account_key = email.strip().lower()
    if not _LOGIN_LIMIT_PER_ACCOUNT.allow(account_key):
        audit.record(
            storage,
            action="user.login_throttled",
            request=request,
            metadata={"dimension": "account", "email_hint": account_key[:3]},
        )
        return throttled_error

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
    type: str = Query("patient"),
    current_user: dict | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse("/", status_code=303)
    initial_type = "clinician" if type == "clinician" else "patient"
    return render("signup.html", _signup_context(request, account_type=initial_type))


@router.post("/signup", response_class=HTMLResponse)
async def signup_post(
    request: Request,
    email: Annotated[str, Form(max_length=EMAIL_MAX_LENGTH)] = "",
    password: Annotated[str, Form(max_length=PASSWORD_MAX_LENGTH)] = "",
    confirm_password: Annotated[str, Form(max_length=PASSWORD_MAX_LENGTH)] = "",
    account_type: Annotated[str, Form(max_length=16)] = "patient",
    first_name: Annotated[str, Form(max_length=100)] = "",
    last_name: Annotated[str, Form(max_length=100)] = "",
    individual_npi: Annotated[str, Form(max_length=10)] = "",
    state_license_state: Annotated[str, Form(max_length=2)] = "",
    state_license_number: Annotated[str, Form(max_length=40)] = "",
    credentials: Annotated[str, Form(max_length=_CREDENTIALS_MAX)] = "",
    attestation: Annotated[str, Form(max_length=4)] = "",
    storage: StorageBase = Depends(get_storage),
    nppes: NPPESClient = Depends(get_client),
):
    chosen_type = "clinician" if account_type == "clinician" else "patient"
    form_state: dict = {
        "first_name": first_name,
        "last_name": last_name,
        "individual_npi": individual_npi,
        "state_license_state": state_license_state.upper() if state_license_state else "",
        "state_license_number": state_license_number,
        "credentials": credentials,
        "email": email,
    }

    def _err(msg: str):
        return render(
            "signup.html",
            _signup_context(request, error=msg, account_type=chosen_type, form=form_state),
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

    # Patient path — fast-track, no clinician checks.
    if chosen_type == "patient":
        user_id = storage.create_user(email, hash_password(password), account_type="patient")
        _begin_session(request, storage, user_id)
        audit.record(
            storage,
            action="user.signup",
            request=request,
            actor_user_id=user_id,
            scope_user_id=user_id,
            metadata={"account_type": "patient"},
        )
        return RedirectResponse("/onboarding", status_code=303)

    # Clinician path — require name + NPI + state + attestation, then verify.
    fn = first_name.strip()
    ln = last_name.strip()
    npi = individual_npi.strip()
    state = state_license_state.strip().upper()
    if not fn or not ln:
        return _err("First and last name are required for clinician signups.")
    if not _NAME_RE.match(fn) or not _NAME_RE.match(ln):
        return _err("Names may only contain letters, spaces, hyphens, and apostrophes.")
    if not npi or len(npi) != 10 or not npi.isdigit():
        return _err("Enter a 10-digit NPI to verify your clinician credentials.")
    if not state or state not in _US_STATE_CODES:
        return _err("Choose your state of licensure.")
    license_num_clean = state_license_number.strip() or None
    if license_num_clean and not _LICENSE_RE.match(license_num_clean):
        return _err("State license number may only contain letters, numbers, and dashes.")
    creds_clean = credentials.strip() or None
    if creds_clean and len(creds_clean) > _CREDENTIALS_MAX:
        return _err("Credentials field is too long.")
    if attestation != "on":
        return _err("You must attest that you are the licensed clinician identified by this NPI.")

    # Run NPPES + OIG verification off the event loop (sync clients).
    oig = get_oig_client()
    loop = asyncio.get_running_loop()
    try:
        verification: ClinicianVerification = await loop.run_in_executor(
            None,
            lambda: verify_clinician(
                npi=npi,
                first_name=fn,
                last_name=ln,
                state_license_state=state,
                nppes=nppes,
                oig=oig,  # type: ignore[arg-type]
            ),
        )
    except Exception:
        logger.exception("Clinician verification raised unexpectedly for npi=%s", npi)
        return _err(_GENERIC_CLINICIAN_REJECT)

    if verification.verdict == "rejected":
        # Audit the rejection without ever creating a user row. Reasons
        # are logged for ops but the response is intentionally generic
        # (esp. for ``oig_excluded`` — never confirm exclusion status to
        # whoever is signing up).
        audit.record(
            storage,
            action="user.signup_rejected_clinician_unverified",
            request=request,
            metadata={
                "npi_last4": npi[-4:],
                "reasons": verification.reasons,
                "method": verification.method,
            },
        )
        return _err(_GENERIC_CLINICIAN_REJECT)

    # verified or pending_review → create the row with the verdict baked in.
    user_id = storage.create_user(
        email,
        hash_password(password),
        account_type="clinician",
        first_name=fn,
        last_name=ln,
        individual_npi=npi,
        credentials=creds_clean,
        state_license_number=license_num_clean,
        state_license_state=state,
        clinician_verification_status=verification.verdict,
        clinician_verified_at=datetime.now(tz=timezone.utc).isoformat(),
        clinician_verified_method=verification.method,
        clinician_verification_reasons=verification.reasons,
    )
    _begin_session(request, storage, user_id)
    audit.record(
        storage,
        action="user.signup",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
        metadata={
            "account_type": "clinician",
            "verification_status": verification.verdict,
            "reasons": verification.reasons,
            "primary_taxonomy": verification.primary_taxonomy,
        },
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
    # New OAuth users land on the audience picker first so we can branch
    # onboarding correctly. Returning OAuth users (no terms yet but
    # account_type already set, e.g. mid-flow refresh) still go to
    # /onboarding directly.
    if user and not user.get("terms_accepted_at") and user.get("account_type") == "patient":
        # ``patient`` is the column default — interpret as "not yet
        # chosen" by checking whether onboarding ever started.
        if not user.get("first_name") and not user.get("date_of_birth"):
            return RedirectResponse("/auth/account-type", status_code=303)
    return RedirectResponse("/onboarding", status_code=303)


@router.get("/account-type", response_class=HTMLResponse)
async def account_type_picker(
    request: Request,
    current_user: dict = Depends(require_user),
):
    """One-time audience picker shown after GitHub OAuth signup.

    Skipped if the user has already completed onboarding (their
    account_type is meaningful at that point) or has clinician fields
    set.
    """
    if current_user.get("terms_accepted_at"):
        return RedirectResponse("/", status_code=303)
    return render(
        "account_type_picker.html",
        {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": current_user,
            "us_states": US_STATES,
            "form": {
                "first_name": current_user.get("first_name") or "",
                "last_name": current_user.get("last_name") or "",
            },
            "error": None,
        },
    )


@router.post("/account-type", response_class=HTMLResponse)
async def account_type_pick(
    request: Request,
    account_type: Annotated[str, Form(max_length=16)] = "patient",
    first_name: Annotated[str, Form(max_length=100)] = "",
    last_name: Annotated[str, Form(max_length=100)] = "",
    individual_npi: Annotated[str, Form(max_length=10)] = "",
    state_license_state: Annotated[str, Form(max_length=2)] = "",
    state_license_number: Annotated[str, Form(max_length=40)] = "",
    credentials: Annotated[str, Form(max_length=_CREDENTIALS_MAX)] = "",
    attestation: Annotated[str, Form(max_length=4)] = "",
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    nppes: NPPESClient = Depends(get_client),
):
    """Persist the audience choice for an OAuth-signup user.

    Patient picks → set ``account_type='patient'`` and continue to
    onboarding. Clinician picks → run the same verify_clinician flow as
    the form-based signup, then persist the verdict.
    """
    chosen = "clinician" if account_type == "clinician" else "patient"
    user_id = current_user["id"]
    form_state = {
        "first_name": first_name,
        "last_name": last_name,
        "individual_npi": individual_npi,
        "state_license_state": state_license_state.upper() if state_license_state else "",
        "state_license_number": state_license_number,
        "credentials": credentials,
    }

    def _err(msg: str):
        return render(
            "account_type_picker.html",
            {
                "request": request,
                "active_page": None,
                "saved_count": 0,
                "user": current_user,
                "us_states": US_STATES,
                "form": form_state,
                "error": msg,
                "account_type": chosen,
            },
        )

    if chosen == "patient":
        storage.update_user_account_type(user_id, account_type="patient")
        audit.record(
            storage,
            action="user.account_type_set",
            request=request,
            actor_user_id=user_id,
            scope_user_id=user_id,
            metadata={"account_type": "patient", "via": "oauth_picker"},
        )
        return RedirectResponse("/onboarding", status_code=303)

    fn = first_name.strip()
    ln = last_name.strip()
    npi = individual_npi.strip()
    state = state_license_state.strip().upper()
    if not fn or not ln:
        return _err("First and last name are required for clinician accounts.")
    if not npi or len(npi) != 10 or not npi.isdigit():
        return _err("Enter a 10-digit NPI to verify your clinician credentials.")
    if not state or state not in _US_STATE_CODES:
        return _err("Choose your state of licensure.")
    if attestation != "on":
        return _err("You must attest that you are the licensed clinician identified by this NPI.")

    oig = get_oig_client()
    loop = asyncio.get_running_loop()
    try:
        verification = await loop.run_in_executor(
            None,
            lambda: verify_clinician(
                npi=npi,
                first_name=fn,
                last_name=ln,
                state_license_state=state,
                nppes=nppes,
                oig=oig,  # type: ignore[arg-type]
            ),
        )
    except Exception:
        logger.exception("Clinician verification raised unexpectedly during account-type picker")
        return _err(_GENERIC_CLINICIAN_REJECT)

    if verification.verdict == "rejected":
        audit.record(
            storage,
            action="user.account_type_clinician_rejected",
            request=request,
            actor_user_id=user_id,
            scope_user_id=user_id,
            metadata={"npi_last4": npi[-4:], "reasons": verification.reasons},
        )
        return _err(_GENERIC_CLINICIAN_REJECT)

    # Persist clinician fields + verdict. Carry over the form names if
    # the user didn't have them set yet.
    storage.update_user_profile(
        user_id,
        first_name=fn,
        last_name=ln,
        display_name=f"{fn} {ln}",
    )
    storage.update_user_signature(
        user_id,
        credentials=credentials.strip() or None,
        individual_npi=npi,
        state_license_number=state_license_number.strip() or None,
        state_license_state=state,
    )
    storage.update_user_account_type(
        user_id,
        account_type="clinician",
        clinician_verification_status=verification.verdict,
        clinician_verified_at=datetime.now(tz=timezone.utc).isoformat(),
        clinician_verified_method=verification.method,
        clinician_verification_reasons=verification.reasons,
    )
    audit.record(
        storage,
        action="user.account_type_set",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
        metadata={
            "account_type": "clinician",
            "verification_status": verification.verdict,
            "reasons": verification.reasons,
            "via": "oauth_picker",
        },
    )
    return RedirectResponse("/onboarding", status_code=303)


@router.get("/upgrade-to-clinician", response_class=HTMLResponse)
async def upgrade_form(
    request: Request,
    current_user: dict = Depends(require_user),
):
    """Render the verification form so a patient can upgrade in place."""
    if current_user.get("account_type") == "clinician":
        return RedirectResponse("/profile", status_code=303)
    return render(
        "account_type_picker.html",
        {
            "request": request,
            "active_page": "profile",
            "saved_count": 0,
            "user": current_user,
            "us_states": US_STATES,
            "form": {
                "first_name": current_user.get("first_name") or "",
                "last_name": current_user.get("last_name") or "",
            },
            "error": None,
            "account_type": "clinician",  # default to the form open
        },
    )


@router.post("/upgrade-to-clinician", response_class=HTMLResponse)
async def upgrade_post(
    request: Request,
    first_name: Annotated[str, Form(max_length=100)] = "",
    last_name: Annotated[str, Form(max_length=100)] = "",
    individual_npi: Annotated[str, Form(max_length=10)] = "",
    state_license_state: Annotated[str, Form(max_length=2)] = "",
    state_license_number: Annotated[str, Form(max_length=40)] = "",
    credentials: Annotated[str, Form(max_length=_CREDENTIALS_MAX)] = "",
    attestation: Annotated[str, Form(max_length=4)] = "",
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    nppes: NPPESClient = Depends(get_client),
):
    """Patient → clinician upgrade. Same verifier as signup."""
    user_id = current_user["id"]
    fn = first_name.strip()
    ln = last_name.strip()
    npi = individual_npi.strip()
    state = state_license_state.strip().upper()
    form_state = {
        "first_name": fn,
        "last_name": ln,
        "individual_npi": npi,
        "state_license_state": state,
        "state_license_number": state_license_number,
        "credentials": credentials,
    }

    def _err(msg: str):
        return render(
            "account_type_picker.html",
            {
                "request": request,
                "active_page": "profile",
                "saved_count": 0,
                "user": current_user,
                "us_states": US_STATES,
                "form": form_state,
                "error": msg,
                "account_type": "clinician",
            },
        )

    if not fn or not ln:
        return _err("First and last name are required.")
    if not npi or len(npi) != 10 or not npi.isdigit():
        return _err("Enter a 10-digit NPI to verify your clinician credentials.")
    if not state or state not in _US_STATE_CODES:
        return _err("Choose your state of licensure.")
    if attestation != "on":
        return _err("You must attest that you are the licensed clinician identified by this NPI.")

    oig = get_oig_client()
    loop = asyncio.get_running_loop()
    try:
        verification = await loop.run_in_executor(
            None,
            lambda: verify_clinician(
                npi=npi,
                first_name=fn,
                last_name=ln,
                state_license_state=state,
                nppes=nppes,
                oig=oig,  # type: ignore[arg-type]
            ),
        )
    except Exception:
        logger.exception("Clinician upgrade verification failed for user_id=%s", user_id)
        return _err(_GENERIC_CLINICIAN_REJECT)

    if verification.verdict == "rejected":
        audit.record(
            storage,
            action="user.account_type_clinician_rejected",
            request=request,
            actor_user_id=user_id,
            scope_user_id=user_id,
            metadata={"npi_last4": npi[-4:], "reasons": verification.reasons, "via": "upgrade"},
        )
        return _err(_GENERIC_CLINICIAN_REJECT)

    storage.update_user_profile(user_id, first_name=fn, last_name=ln, display_name=f"{fn} {ln}")
    storage.update_user_signature(
        user_id,
        credentials=credentials.strip() or None,
        individual_npi=npi,
        state_license_number=state_license_number.strip() or None,
        state_license_state=state,
    )
    storage.update_user_account_type(
        user_id,
        account_type="clinician",
        clinician_verification_status=verification.verdict,
        clinician_verified_at=datetime.now(tz=timezone.utc).isoformat(),
        clinician_verified_method=verification.method,
        clinician_verification_reasons=verification.reasons,
    )
    audit.record(
        storage,
        action="user.account_type_changed",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
        metadata={
            "from": "patient",
            "to": "clinician",
            "verification_status": verification.verdict,
            "via": "upgrade",
        },
    )
    return RedirectResponse("/profile", status_code=303)


@router.post("/downgrade-to-patient", response_class=HTMLResponse)
async def downgrade_post(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    """Clinician → patient downgrade. Resets verification verdict.

    Clinician fields (NPI, license, credentials, signature image) are
    intentionally left in place so a future re-upgrade is fast — only
    the verdict columns reset to ``not_applicable``.
    """
    user_id = current_user["id"]
    if current_user.get("account_type") != "clinician":
        return RedirectResponse("/profile", status_code=303)
    storage.update_user_account_type(
        user_id,
        account_type="patient",
        clinician_verification_status="not_applicable",
    )
    audit.record(
        storage,
        action="user.account_type_changed",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
        metadata={"from": "clinician", "to": "patient", "via": "downgrade"},
    )
    return RedirectResponse("/profile", status_code=303)


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
