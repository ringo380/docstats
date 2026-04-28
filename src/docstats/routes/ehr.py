"""SMART-on-FHIR routes — Phase 12.A/12.B standalone + EHR-launch + Patient import.

Tokens are Fernet-encrypted at rest; plaintext never touches logs / audit
metadata / session storage. PKCE (S256) is used alongside the confidential
client_secret for defense in depth.

PHI is NOT cached in the session cookie. The OAuth callback persists the
encrypted access token + ``patient_fhir_id`` on ``ehr_connections``; the
review and confirm routes then re-fetch the Patient resource from Epic on
demand and discard the parsed dict at the end of the request. The cookie
only carries opaque OAuth state (PKCE verifier + state token).

Feature flagged on ``EHR_EPIC_SANDBOX_ENABLED=1`` — every route returns 404
when the flag is off so accidental Railway misconfig doesn't expose a
half-built integration.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from docstats.auth import require_user
from docstats.domain import audit
from docstats.domain.ehr import EHRConnection, EPIC_SCOPES, EPIC_SCOPES_EHR_LAUNCH, ImportedPatient
from docstats.ehr import epic
from docstats.ehr.crypto import EHRConfigError, decrypt_token, encrypt_token
from docstats.ehr.epic import EpicError
from docstats.ehr.mappers import parse_fhir_patient
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope, render
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ehr", tags=["ehr"])

EHR_VENDOR = "epic_sandbox"
SESSION_STATE_KEY = "ehr_epic_state"
SESSION_VERIFIER_KEY = "ehr_epic_pkce_verifier"
SESSION_EHR_ISS = "ehr_epic_launch_iss"
SESSION_LAUNCH_MODE = "ehr_epic_launch_mode"

_REFRESH_LEAD_SECONDS = 60  # refresh if token expires within this window

# Closed set of error reasons we may echo into ``/profile?ehr_error=...``.
# Never pass an upstream-controlled string through — Epic could return
# attacker-shaped ``error`` query params, and even though the profile
# template auto-escapes, an allowlist is cleaner.
_ALLOWED_ERROR_REASONS: frozenset[str] = frozenset(
    {
        "state_mismatch",
        "token_exchange",
        "no_patient_context",
        "server_config",
        "patient_fetch",
        "no_pending_import",
        "no_active_connection",
        "missing_patient_name",
        "merge_requires_patient_id",
        "patient_not_found",
        "invalid_action",
        "oauth_error",
    }
)


def _enabled() -> bool:
    return os.getenv("EHR_EPIC_SANDBOX_ENABLED", "").strip() == "1"


def _require_enabled() -> None:
    if not _enabled():
        raise HTTPException(status_code=404)


def _err_redirect(reason: str) -> RedirectResponse:
    """Redirect to /profile with a sanitized error reason."""
    safe = reason if reason in _ALLOWED_ERROR_REASONS else "oauth_error"
    return RedirectResponse(f"/profile?ehr_error={safe}", status_code=303)


def _audit_failure(storage: StorageBase, request: Request, user_id: int, reason: str) -> None:
    audit.record(
        storage,
        action="ehr.connect_failed",
        request=request,
        actor_user_id=user_id,
        metadata={"ehr_vendor": EHR_VENDOR, "reason": reason},
    )


def _iss_allowlist() -> frozenset[str]:
    """Return the allowlist of valid EHR-launch iss values from env, or empty."""
    raw = os.getenv("EPIC_EHR_LAUNCH_ISS_ALLOWLIST", "").strip()
    return frozenset(v.strip().rstrip("/") for v in raw.split(",") if v.strip())


def _maybe_refresh(conn: EHRConnection, storage: StorageBase) -> str:
    """Return a fresh plaintext access token, refreshing if needed.

    Refreshes when the token expires within ``_REFRESH_LEAD_SECONDS``. On
    refresh failure, returns the stale access token so callers can proceed
    (Epic will return 401, which the caller catches as EpicError). Never
    raises — refresh is best-effort.
    """
    try:
        access_token = decrypt_token(conn.access_token_enc)
    except (EHRConfigError, InvalidToken):
        logger.exception("Failed to decrypt EHR access token for connection_id=%d", conn.id)
        return ""

    if conn.refresh_token_enc is None:
        return access_token

    now = datetime.now(tz=timezone.utc)
    if (conn.expires_at - now).total_seconds() > _REFRESH_LEAD_SECONDS:
        return access_token

    try:
        refresh_token = decrypt_token(conn.refresh_token_enc)
        token = epic.refresh(refresh_token)
        new_access_enc = encrypt_token(token.access_token)
        new_refresh_enc = encrypt_token(token.refresh_token) if token.refresh_token else None
        new_expires_at = now + timedelta(seconds=token.expires_in)
        storage.update_ehr_connection_tokens(
            conn.id,
            access_token_enc=new_access_enc,
            refresh_token_enc=new_refresh_enc,
            expires_at=new_expires_at,
        )
        logger.info("Refreshed EHR token for connection_id=%d", conn.id)
        return token.access_token
    except Exception:
        logger.exception("EHR token refresh failed for connection_id=%d", conn.id)
        return access_token


async def _load_pending_patient(
    storage: StorageBase, user_id: int
) -> tuple[EHRConnection, ImportedPatient] | None:
    """Re-fetch the FHIR Patient associated with the user's active connection.

    Returns ``(connection, imported)`` on success or ``None`` if there is no
    active connection, the stored token can't be decrypted, or Epic refuses
    the read. Never raises — callers branch on ``None``.
    """
    conn = storage.get_active_ehr_connection(user_id, EHR_VENDOR)
    if conn is None or not conn.patient_fhir_id:
        return None

    loop = asyncio.get_running_loop()
    try:
        access_token = await loop.run_in_executor(None, lambda: _maybe_refresh(conn, storage))
    except Exception:
        logger.exception("Failed to obtain EHR access token for user_id=%d", user_id)
        return None
    if not access_token:
        return None

    fhir_id = conn.patient_fhir_id
    try:
        patient_resource = await loop.run_in_executor(
            None,
            lambda: epic.fetch_patient(access_token=access_token, patient_fhir_id=fhir_id),
        )
        imported = parse_fhir_patient(patient_resource)
    except (EpicError, ValueError):
        logger.exception("Epic Patient fetch/parse failed for user_id=%d", user_id)
        return None
    return conn, imported


@router.get("/launch/epic")
async def launch_epic(
    request: Request,
    iss: str | None = Query(None),
    launch: str | None = Query(None),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """EHR-launch entry point — Epic opens this URL with iss + launch params.

    Validates iss against the allowlist, then builds an Epic authorize URL
    with the EHR-launch scope set and ``launch=<token>`` param. The callback
    route handles both standalone and EHR-launch flows identically.
    """
    _require_enabled()
    if not iss or not launch:
        raise HTTPException(status_code=400, detail="Missing iss or launch parameter")

    allowlist = _iss_allowlist()
    if not allowlist or iss.rstrip("/") not in allowlist:
        raise HTTPException(status_code=400, detail="iss not in EHR launch allowlist")

    user_id = current_user["id"]
    try:
        verifier, challenge = epic.make_pkce_pair()
        state = epic.make_state()
        iss_norm = iss.rstrip("/")
        loop = asyncio.get_running_loop()
        url = await loop.run_in_executor(
            None,
            lambda: epic.build_ehr_launch_authorize_url(
                state=state,
                code_challenge=challenge,
                scope=EPIC_SCOPES_EHR_LAUNCH,
                launch_token=launch,
                iss_override=iss_norm,
            ),
        )
    except EpicError as e:
        logger.exception("Epic EHR-launch URL build failed")
        _audit_failure(storage, request, user_id, f"epic_error:{type(e).__name__}")
        raise HTTPException(status_code=502, detail="Epic sandbox unavailable") from e

    request.session[SESSION_STATE_KEY] = state
    request.session[SESSION_VERIFIER_KEY] = verifier
    request.session[SESSION_EHR_ISS] = iss_norm
    request.session[SESSION_LAUNCH_MODE] = "ehr"
    return RedirectResponse(url, status_code=303)


@router.get("/connect/epic")
async def connect_epic(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """Begin Epic standalone launch — redirect to Epic authorize endpoint."""
    _require_enabled()
    user_id = current_user["id"]

    try:
        verifier, challenge = epic.make_pkce_pair()
        state = epic.make_state()
        # Discovery + URL build do sync httpx on cold cache; wrap in executor
        # so the event loop never blocks on the .well-known fetch.
        loop = asyncio.get_running_loop()
        url = await loop.run_in_executor(
            None,
            lambda: epic.build_authorize_url(
                state=state, code_challenge=challenge, scope=EPIC_SCOPES
            ),
        )
    except EpicError as e:
        logger.exception("Epic discovery / URL build failed")
        _audit_failure(storage, request, user_id, f"epic_error:{type(e).__name__}")
        raise HTTPException(status_code=502, detail="Epic sandbox unavailable") from e

    request.session[SESSION_STATE_KEY] = state
    request.session[SESSION_VERIFIER_KEY] = verifier
    return RedirectResponse(url, status_code=303)


@router.get("/callback/epic")
async def callback_epic(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """Token exchange + redirect to review page.

    The Patient resource itself is NOT fetched here — review and confirm
    re-fetch on demand from the persisted connection so PHI never lives in
    the session cookie.
    """
    _require_enabled()
    user_id = current_user["id"]

    expected_state = request.session.pop(SESSION_STATE_KEY, None)
    verifier = request.session.pop(SESSION_VERIFIER_KEY, None)
    # EHR-launch keys — pop regardless of mode so they don't linger.
    request.session.pop(SESSION_EHR_ISS, None)
    request.session.pop(SESSION_LAUNCH_MODE, None)

    if error:
        _audit_failure(storage, request, user_id, f"oauth_error:{error}")
        return _err_redirect("oauth_error")
    if not code or not state or state != expected_state or not verifier:
        _audit_failure(storage, request, user_id, "state_mismatch")
        return _err_redirect("state_mismatch")

    loop = asyncio.get_running_loop()
    try:
        token = await loop.run_in_executor(
            None, lambda: epic.exchange_code(code=code, code_verifier=verifier)
        )
    except (EpicError, EHRConfigError) as e:
        logger.exception("Epic token exchange failed")
        _audit_failure(storage, request, user_id, f"token_exchange:{type(e).__name__}")
        return _err_redirect("token_exchange")

    if not token.patient_fhir_id:
        # Without a patient context we can't import — Epic should always return
        # one when the launch/patient scope is granted, but fail loud if not.
        _audit_failure(storage, request, user_id, "no_patient_context")
        return _err_redirect("no_patient_context")
    patient_fhir_id: str = token.patient_fhir_id

    try:
        access_enc = encrypt_token(token.access_token)
        refresh_enc = encrypt_token(token.refresh_token) if token.refresh_token else None
    except EHRConfigError:
        logger.exception("EHR_TOKEN_KEY missing or malformed")
        _audit_failure(storage, request, user_id, "ehr_token_key_missing")
        return _err_redirect("server_config")

    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=token.expires_in)
    endpoints = await loop.run_in_executor(None, epic.discover)

    # `iss` is normalised — fetch_patient rstrips the same value, so we
    # store the canonical form to keep audit / lookup paths consistent.
    iss = endpoints.fhir_base.rstrip("/")
    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor=EHR_VENDOR,
        iss=iss,
        access_token_enc=access_enc,
        refresh_token_enc=refresh_enc,
        expires_at=expires_at,
        scope=token.scope or EPIC_SCOPES,
        patient_fhir_id=patient_fhir_id,
    )
    audit.record(
        storage,
        action="ehr.connected",
        request=request,
        actor_user_id=user_id,
        metadata={"ehr_vendor": EHR_VENDOR, "fhir_patient_id": patient_fhir_id},
    )
    return RedirectResponse("/ehr/import/review", status_code=303)


def _candidate_matches(storage: StorageBase, scope: Scope, imported: ImportedPatient) -> list[dict]:
    """Up to 3 candidate patients to merge into.

    MRN exact-match first; then name+DOB matches as fallback. Returns plain
    dicts so the template can render uniformly without juggling Pydantic.
    """
    seen: set[int] = set()
    out: list[dict] = []

    if imported.mrn:
        for p in storage.list_patients(scope, mrn=imported.mrn, limit=3):
            if p.id not in seen:
                seen.add(p.id)
                out.append(p.model_dump())

    if len(out) < 3 and imported.last_name:
        search = " ".join(filter(None, [imported.first_name, imported.last_name]))
        for p in storage.list_patients(scope, search=search, limit=10):
            if p.id in seen:
                continue
            # If we have a DOB on both sides, require it to match before we
            # surface the candidate — name+DOB is the realistic minimum match.
            if (
                imported.date_of_birth
                and getattr(p, "date_of_birth", None)
                and str(p.date_of_birth) != imported.date_of_birth
            ):
                continue
            seen.add(p.id)
            out.append(p.model_dump())
            if len(out) >= 3:
                break

    return out


@router.get("/import/review", response_class=HTMLResponse)
async def import_review(
    request: Request,
    current_user: dict = Depends(require_phi_consent),
    storage: StorageBase = Depends(get_storage),
    scope: Scope = Depends(get_scope),
) -> Response:
    _require_enabled()
    loaded = await _load_pending_patient(storage, current_user["id"])
    if loaded is None:
        return _err_redirect("no_active_connection")
    _, imported = loaded
    candidates = _candidate_matches(storage, scope, imported)
    return render(
        "ehr_review.html",
        {
            "request": request,
            "active_page": "profile",
            "user": current_user,
            "imported": imported,
            "candidates": candidates,
        },
    )


@router.post("/import/confirm")
async def import_confirm(
    request: Request,
    action: str = Form(..., max_length=20),
    patient_id: int | None = Form(None),
    current_user: dict = Depends(require_phi_consent),
    storage: StorageBase = Depends(get_storage),
    scope: Scope = Depends(get_scope),
) -> Response:
    _require_enabled()
    user_id = current_user["id"]
    loaded = await _load_pending_patient(storage, user_id)
    if loaded is None:
        return _err_redirect("no_active_connection")
    _, imported = loaded

    if action == "create_new":
        if not imported.first_name or not imported.last_name:
            # FHIR Patient lacked a usable name; we don't auto-fabricate one.
            return _err_redirect("missing_patient_name")
        patient = storage.create_patient(
            scope,
            first_name=imported.first_name,
            last_name=imported.last_name,
            middle_name=imported.middle_name,
            date_of_birth=imported.date_of_birth,
            mrn=imported.mrn,
            phone=imported.phone,
            email=imported.email,
            address_line1=imported.address_line1,
            address_line2=imported.address_line2,
            address_city=imported.address_city,
            address_state=imported.address_state,
            address_zip=imported.address_zip,
            ehr_fhir_id=imported.fhir_id,
            created_by_user_id=user_id,
        )
        new_id = patient.id
    elif action == "merge":
        if patient_id is None:
            return _err_redirect("merge_requires_patient_id")
        existing = storage.get_patient(scope, patient_id)
        if existing is None:
            return _err_redirect("patient_not_found")
        # None-means-leave-alone semantics: only fill blank target fields so we
        # never silently overwrite user-curated data.
        update_kwargs = {}
        for field in (
            "middle_name",
            "date_of_birth",
            "mrn",
            "phone",
            "email",
            "address_line1",
            "address_line2",
            "address_city",
            "address_state",
            "address_zip",
        ):
            existing_val = getattr(existing, field, None)
            imported_val = getattr(imported, field, None)
            if not existing_val and imported_val:
                update_kwargs[field] = imported_val
        if not existing.ehr_fhir_id and imported.fhir_id:
            update_kwargs["ehr_fhir_id"] = imported.fhir_id
        if update_kwargs:
            storage.update_patient(scope, patient_id, **update_kwargs)
        new_id = patient_id
    else:
        return _err_redirect("invalid_action")

    audit.record(
        storage,
        action="patient.imported_from_ehr",
        request=request,
        actor_user_id=user_id,
        scope_user_id=scope.user_id,
        scope_organization_id=scope.organization_id,
        entity_type="patient",
        entity_id=str(new_id),
        metadata={
            "ehr_vendor": EHR_VENDOR,
            "fhir_patient_id": imported.fhir_id,
            "action": action,
        },
    )
    return RedirectResponse(f"/patients/{new_id}", status_code=303)


@router.post("/disconnect/epic")
async def disconnect_epic(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    _require_enabled()
    user_id = current_user["id"]
    revoked = storage.revoke_ehr_connection(user_id, EHR_VENDOR)
    if revoked:
        audit.record(
            storage,
            action="ehr.disconnected",
            request=request,
            actor_user_id=user_id,
            metadata={"ehr_vendor": EHR_VENDOR},
        )
    # htmx callers swap the partial inline; non-htmx (curl, missing JS) gets
    # a normal redirect back to /profile so the page is sensibly re-rendered.
    if request.headers.get("HX-Request"):
        return render(
            "_connected_ehrs.html",
            {
                "request": request,
                "epic_connection": None,
                "ehr_enabled": True,
            },
        )
    return RedirectResponse("/profile", status_code=303)
