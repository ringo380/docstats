"""SMART-on-FHIR routes — vendor-agnostic dispatch layer (Phase 12.C).

Supports Epic (``epic_sandbox``) and Cerner/Oracle Health (``cerner_oauth``).
Each vendor is a plain module registered in ``ehr.registry``; routes dispatch
via the registry rather than calling vendor modules directly.

PHI is NOT cached in the session cookie. The OAuth callback persists the
encrypted access token + ``patient_fhir_id`` on ``ehr_connections``; review
and confirm routes re-fetch the Patient resource on demand. The session only
carries opaque OAuth state (PKCE verifier + state token + vendor name).

Feature flags:
  ``EHR_EPIC_SANDBOX_ENABLED=1``  — Epic routes return 404 when unset.
  ``EHR_CERNER_OAUTH_ENABLED=1``  — Cerner routes return 404 when unset.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from types import ModuleType

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from docstats.auth import require_user
from docstats.domain import audit
from docstats.domain.ehr import (
    EHRConnection,
    EPIC_SCOPES,
    EPIC_SCOPES_EHR_LAUNCH,
    CERNER_SCOPES,
    CERNER_SCOPES_EHR_LAUNCH,
    ImportedPatient,
)
from docstats.ehr import epic, cerner  # noqa: F401 — side-effect: registers both vendors
from docstats.ehr import registry as _registry
from docstats.ehr.crypto import EHRConfigError, decrypt_token, encrypt_token
from docstats.ehr.registry import EHRError
from docstats.ehr.mappers import parse_fhir_patient
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope, render
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ehr", tags=["ehr"])

_REFRESH_LEAD_SECONDS = 60

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

# Per-vendor feature-flag env vars.
_VENDOR_FLAG_ENV: dict[str, str] = {
    "epic_sandbox": "EHR_EPIC_SANDBOX_ENABLED",
    "cerner_oauth": "EHR_CERNER_OAUTH_ENABLED",
}

# Per-vendor EHR-launch ISS allowlist env vars.
_VENDOR_ISS_ALLOWLIST_ENV: dict[str, str] = {
    "epic_sandbox": "EPIC_EHR_LAUNCH_ISS_ALLOWLIST",
    "cerner_oauth": "CERNER_EHR_LAUNCH_ISS_ALLOWLIST",
}

# UI metadata for each vendor (label + route paths).
_VENDOR_META: dict[str, dict[str, str]] = {
    "epic_sandbox": {
        "label": "Epic Sandbox",
        "connect_path": "/ehr/connect/epic",
        "disconnect_path": "/ehr/disconnect/epic",
    },
    "cerner_oauth": {
        "label": "Cerner (Oracle Health)",
        "connect_path": "/ehr/connect/cerner",
        "disconnect_path": "/ehr/disconnect/cerner",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vendor_enabled(vendor: str) -> bool:
    env_var = _VENDOR_FLAG_ENV.get(vendor, "")
    return bool(env_var) and os.getenv(env_var, "").strip() == "1"


def _require_vendor_enabled(vendor: str) -> None:
    if not _vendor_enabled(vendor):
        raise HTTPException(status_code=404)


def _err_redirect(reason: str) -> RedirectResponse:
    safe = reason if reason in _ALLOWED_ERROR_REASONS else "oauth_error"
    return RedirectResponse(f"/profile?ehr_error={safe}", status_code=303)


def _ehr_vendor_ui_list(user_id: int, storage: StorageBase) -> list[dict]:
    """Return UI metadata + active connection for every enabled EHR vendor."""
    return [
        {
            **_VENDOR_META[v],
            "key": v,
            "connection": storage.get_active_ehr_connection(user_id, v),
        }
        for v in _registry.list_vendors()
        if _vendor_enabled(v) and v in _VENDOR_META
    ]


def _audit_failure(
    storage: StorageBase, request: Request, user_id: int, vendor: str, reason: str
) -> None:
    audit.record(
        storage,
        action="ehr.connect_failed",
        request=request,
        actor_user_id=user_id,
        metadata={"ehr_vendor": vendor, "reason": reason},
    )


def _iss_allowlist(vendor: str) -> frozenset[str]:
    env_var = _VENDOR_ISS_ALLOWLIST_ENV.get(vendor, "")
    raw = os.getenv(env_var, "").strip() if env_var else ""
    return frozenset(v.strip().rstrip("/") for v in raw.split(",") if v.strip())


# Session key helpers — namespaced by vendor to avoid cross-vendor pollution.
def _session_state_key(vendor: str) -> str:
    return f"ehr_{vendor}_state"


def _session_verifier_key(vendor: str) -> str:
    return f"ehr_{vendor}_pkce_verifier"


def _session_iss_key(vendor: str) -> str:
    return f"ehr_{vendor}_launch_iss"


def _session_launch_mode_key(vendor: str) -> str:
    return f"ehr_{vendor}_launch_mode"


def _maybe_refresh(conn: EHRConnection, storage: StorageBase) -> str:
    """Return a fresh plaintext access token, refreshing if needed.

    Dispatches to the correct vendor module via the registry. On refresh
    failure, returns the stale token so callers can proceed (the EHR will
    return 401). Never raises.
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

    vendor = conn.ehr_vendor
    try:
        vendor_mod = _registry.get(vendor)
    except ValueError:
        logger.error("Unknown EHR vendor %r for connection_id=%d", vendor, conn.id)
        return access_token

    try:
        refresh_token = decrypt_token(conn.refresh_token_enc)
        token = vendor_mod.refresh(refresh_token)
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
        audit.record(
            storage,
            action="ehr.token_refreshed",
            request=None,
            actor_user_id=conn.user_id,
            metadata={"ehr_vendor": vendor},
        )
        return str(token.access_token)
    except Exception:
        logger.exception("EHR token refresh failed for connection_id=%d", conn.id)
        audit.record(
            storage,
            action="ehr.token_refresh_failed",
            request=None,
            actor_user_id=conn.user_id,
            metadata={"ehr_vendor": vendor},
        )
        return access_token


async def _load_pending_patient(
    storage: StorageBase, user_id: int
) -> tuple[EHRConnection, ImportedPatient] | None:
    """Re-fetch the FHIR Patient associated with the user's most recent active connection.

    Iterates all registered vendors to find an active connection with a
    patient_fhir_id. Returns ``(connection, imported)`` on success or None.
    Never raises.
    """
    conn: EHRConnection | None = None
    for vendor in _registry.list_vendors():
        c = storage.get_active_ehr_connection(user_id, vendor)
        if c and c.patient_fhir_id:
            conn = c
            break

    if conn is None:
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
        vendor_mod = _registry.get(conn.ehr_vendor)
    except ValueError:
        logger.exception("Unknown EHR vendor %r for user_id=%d", conn.ehr_vendor, user_id)
        return None
    try:
        patient_resource = await loop.run_in_executor(
            None,
            lambda: vendor_mod.fetch_patient(access_token=access_token, patient_fhir_id=fhir_id),
        )
        imported = parse_fhir_patient(patient_resource)
    except (EHRError, ValueError):
        logger.exception("EHR Patient fetch/parse failed for user_id=%d", user_id)
        return None
    return conn, imported


# ---------------------------------------------------------------------------
# Shared flow helpers (accept a vendor module to keep routes thin)
# ---------------------------------------------------------------------------


async def _connect_flow(
    vendor: str,
    scopes: str,
    request: Request,
    current_user: dict,
    storage: StorageBase,
) -> Response:
    """Begin standalone SMART launch — redirect to vendor authorize endpoint."""
    _require_vendor_enabled(vendor)
    user_id = current_user["id"]
    vendor_mod: ModuleType = _registry.get(vendor)

    try:
        verifier, challenge = vendor_mod.make_pkce_pair()
        state = vendor_mod.make_state()
        loop = asyncio.get_running_loop()
        url = await loop.run_in_executor(
            None,
            lambda: vendor_mod.build_authorize_url(
                state=state, code_challenge=challenge, scope=scopes
            ),
        )
    except EHRError as e:
        logger.exception("EHR discovery / URL build failed for vendor %s", vendor)
        _audit_failure(storage, request, user_id, vendor, f"error:{type(e).__name__}")
        raise HTTPException(status_code=502, detail="EHR sandbox unavailable") from e

    # Clear any stale EHR-launch keys left by an abandoned prior flow so the
    # callback doesn't pick up the wrong iss or launch_mode.
    request.session.pop(_session_iss_key(vendor), None)
    request.session.pop(_session_launch_mode_key(vendor), None)
    request.session[_session_state_key(vendor)] = state
    request.session[_session_verifier_key(vendor)] = verifier
    return RedirectResponse(url, status_code=303)


async def _launch_flow(
    vendor: str,
    scopes: str,
    request: Request,
    current_user: dict,
    storage: StorageBase,
    iss: str | None,
    launch: str | None,
) -> Response:
    """EHR-launch entry point — validate iss, build authorize URL with launch token."""
    _require_vendor_enabled(vendor)
    if not iss or not launch:
        raise HTTPException(status_code=400, detail="Missing iss or launch parameter")

    allowlist = _iss_allowlist(vendor)
    if not allowlist or iss.rstrip("/") not in allowlist:
        raise HTTPException(status_code=400, detail="iss not in EHR launch allowlist")

    user_id = current_user["id"]
    vendor_mod: ModuleType = _registry.get(vendor)
    try:
        verifier, challenge = vendor_mod.make_pkce_pair()
        state = vendor_mod.make_state()
        iss_norm = iss.rstrip("/")
        loop = asyncio.get_running_loop()
        url = await loop.run_in_executor(
            None,
            lambda: vendor_mod.build_ehr_launch_authorize_url(
                state=state,
                code_challenge=challenge,
                scope=scopes,
                launch_token=launch,
                iss_override=iss_norm,
            ),
        )
    except EHRError as e:
        logger.exception("EHR EHR-launch URL build failed for vendor %s", vendor)
        _audit_failure(storage, request, user_id, vendor, f"error:{type(e).__name__}")
        raise HTTPException(status_code=502, detail="EHR sandbox unavailable") from e

    request.session[_session_state_key(vendor)] = state
    request.session[_session_verifier_key(vendor)] = verifier
    request.session[_session_iss_key(vendor)] = iss_norm
    request.session[_session_launch_mode_key(vendor)] = "ehr"
    return RedirectResponse(url, status_code=303)


async def _callback_flow(
    vendor: str,
    scopes_standalone: str,
    scopes_ehr: str,
    request: Request,
    current_user: dict,
    storage: StorageBase,
    code: str | None,
    state: str | None,
    error: str | None,
) -> Response:
    """Token exchange + create ehr_connections row + redirect to review page."""
    _require_vendor_enabled(vendor)
    user_id = current_user["id"]

    expected_state = request.session.pop(_session_state_key(vendor), None)
    verifier = request.session.pop(_session_verifier_key(vendor), None)
    launch_iss: str | None = request.session.pop(_session_iss_key(vendor), None)
    launch_mode: str | None = request.session.pop(_session_launch_mode_key(vendor), None)

    if error:
        _audit_failure(storage, request, user_id, vendor, f"oauth_error:{error}")
        return _err_redirect("oauth_error")
    if not code or not state or state != expected_state or not verifier:
        _audit_failure(storage, request, user_id, vendor, "state_mismatch")
        return _err_redirect("state_mismatch")

    vendor_mod: ModuleType = _registry.get(vendor)
    loop = asyncio.get_running_loop()
    try:
        token = await loop.run_in_executor(
            None,
            lambda: vendor_mod.exchange_code(
                code=code, code_verifier=verifier, iss_override=launch_iss
            ),
        )
    except (EHRError, EHRConfigError) as e:
        logger.exception("EHR token exchange failed for vendor %s", vendor)
        _audit_failure(storage, request, user_id, vendor, f"token_exchange:{type(e).__name__}")
        return _err_redirect("token_exchange")

    if not token.patient_fhir_id:
        _audit_failure(storage, request, user_id, vendor, "no_patient_context")
        return _err_redirect("no_patient_context")
    patient_fhir_id: str = token.patient_fhir_id

    try:
        access_enc = encrypt_token(token.access_token)
        refresh_enc = encrypt_token(token.refresh_token) if token.refresh_token else None
    except EHRConfigError:
        logger.exception("EHR_TOKEN_KEY missing or malformed")
        _audit_failure(storage, request, user_id, vendor, "ehr_token_key_missing")
        return _err_redirect("server_config")

    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=token.expires_in)
    endpoints = await loop.run_in_executor(
        None, lambda: vendor_mod.discover(base_url_override=launch_iss)
    )

    iss_stored = endpoints.fhir_base.rstrip("/")
    scope_fallback = scopes_ehr if launch_mode == "ehr" else scopes_standalone
    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor=vendor,
        iss=iss_stored,
        access_token_enc=access_enc,
        refresh_token_enc=refresh_enc,
        expires_at=expires_at,
        scope=token.scope or scope_fallback,
        patient_fhir_id=patient_fhir_id,
    )
    audit.record(
        storage,
        action="ehr.connected",
        request=request,
        actor_user_id=user_id,
        metadata={"ehr_vendor": vendor, "fhir_patient_id": patient_fhir_id},
    )
    return RedirectResponse("/ehr/import/review", status_code=303)


async def _disconnect_flow(
    vendor: str,
    request: Request,
    current_user: dict,
    storage: StorageBase,
) -> Response:
    """Revoke the user's active connection for *vendor*."""
    _require_vendor_enabled(vendor)
    user_id = current_user["id"]
    revoked = storage.revoke_ehr_connection(user_id, vendor)
    if revoked:
        audit.record(
            storage,
            action="ehr.disconnected",
            request=request,
            actor_user_id=user_id,
            metadata={"ehr_vendor": vendor},
        )
    ehr_vendors = _ehr_vendor_ui_list(user_id, storage)
    if request.headers.get("HX-Request"):
        return render(
            "_connected_ehrs.html",
            {
                "request": request,
                "ehr_vendors": ehr_vendors,
            },
        )
    return RedirectResponse("/profile", status_code=303)


# ---------------------------------------------------------------------------
# Epic routes
# ---------------------------------------------------------------------------


@router.get("/launch/epic")
async def launch_epic(
    request: Request,
    iss: str | None = Query(None),
    launch: str | None = Query(None),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    return await _launch_flow(
        "epic_sandbox", EPIC_SCOPES_EHR_LAUNCH, request, current_user, storage, iss, launch
    )


@router.get("/connect/epic")
async def connect_epic(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    return await _connect_flow("epic_sandbox", EPIC_SCOPES, request, current_user, storage)


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
    return await _callback_flow(
        "epic_sandbox",
        EPIC_SCOPES,
        EPIC_SCOPES_EHR_LAUNCH,
        request,
        current_user,
        storage,
        code,
        state,
        error,
    )


@router.post("/disconnect/epic")
async def disconnect_epic(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    return await _disconnect_flow("epic_sandbox", request, current_user, storage)


# ---------------------------------------------------------------------------
# Cerner routes
# ---------------------------------------------------------------------------


@router.get("/launch/cerner")
async def launch_cerner(
    request: Request,
    iss: str | None = Query(None),
    launch: str | None = Query(None),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    return await _launch_flow(
        "cerner_oauth", CERNER_SCOPES_EHR_LAUNCH, request, current_user, storage, iss, launch
    )


@router.get("/connect/cerner")
async def connect_cerner(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    return await _connect_flow("cerner_oauth", CERNER_SCOPES, request, current_user, storage)


@router.get("/callback/cerner")
async def callback_cerner(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    return await _callback_flow(
        "cerner_oauth",
        CERNER_SCOPES,
        CERNER_SCOPES_EHR_LAUNCH,
        request,
        current_user,
        storage,
        code,
        state,
        error,
    )


@router.post("/disconnect/cerner")
async def disconnect_cerner(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    return await _disconnect_flow("cerner_oauth", request, current_user, storage)


# ---------------------------------------------------------------------------
# Shared import routes (vendor-agnostic)
# ---------------------------------------------------------------------------


def _candidate_matches(storage: StorageBase, scope: Scope, imported: ImportedPatient) -> list[dict]:
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
    # Require at least one vendor enabled.
    if not any(_vendor_enabled(v) for v in _registry.list_vendors()):
        raise HTTPException(status_code=404)
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
    if not any(_vendor_enabled(v) for v in _registry.list_vendors()):
        raise HTTPException(status_code=404)
    user_id = current_user["id"]
    loaded = await _load_pending_patient(storage, user_id)
    if loaded is None:
        return _err_redirect("no_active_connection")
    conn, imported = loaded
    ehr_vendor = conn.ehr_vendor

    if action == "create_new":
        if not imported.first_name or not imported.last_name:
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
            "ehr_vendor": ehr_vendor,
            "fhir_patient_id": imported.fhir_id,
            "action": action,
        },
    )
    return RedirectResponse(f"/patients/{new_id}", status_code=303)
