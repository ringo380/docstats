"""SMART-on-FHIR routes — Phase 12.A standalone launch + Patient import.

Standalone launch only (user clicks "Connect Epic Sandbox" on /profile).
Tokens are Fernet-encrypted at rest; plaintext never touches logs / audit
metadata / session storage. PKCE (S256) is used alongside the confidential
client_secret for defense in depth.

Feature flagged on ``EHR_EPIC_SANDBOX_ENABLED=1`` — every route returns 404
when the flag is off so accidental Railway misconfig doesn't expose a
half-built integration.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from docstats.auth import require_user
from docstats.domain import audit
from docstats.domain.ehr import EPIC_SCOPES, ImportedPatient
from docstats.ehr import epic
from docstats.ehr.crypto import EHRConfigError, encrypt_token
from docstats.ehr.epic import EpicError
from docstats.ehr.mappers import parse_fhir_patient
from docstats.routes._common import render
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ehr", tags=["ehr"])

EHR_VENDOR = "epic_sandbox"
SESSION_STATE_KEY = "ehr_epic_state"
SESSION_VERIFIER_KEY = "ehr_epic_pkce_verifier"
SESSION_PENDING_KEY = "ehr_pending_patient"


def _enabled() -> bool:
    return os.getenv("EHR_EPIC_SANDBOX_ENABLED", "").strip() == "1"


def _require_enabled() -> None:
    if not _enabled():
        raise HTTPException(status_code=404)


def _audit_failure(storage: StorageBase, request: Request, user_id: int, reason: str) -> None:
    audit.record(
        storage,
        action="ehr.connect_failed",
        request=request,
        actor_user_id=user_id,
        metadata={"ehr_vendor": EHR_VENDOR, "reason": reason},
    )


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
        # Discovery + URL build are sync httpx; wrap in executor to avoid
        # blocking the event loop on cold cache.
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
    """Token exchange + Patient fetch + redirect to review page."""
    _require_enabled()
    user_id = current_user["id"]

    expected_state = request.session.pop(SESSION_STATE_KEY, None)
    verifier = request.session.pop(SESSION_VERIFIER_KEY, None)

    if error:
        _audit_failure(storage, request, user_id, f"oauth_error:{error}")
        return RedirectResponse(f"/profile?ehr_error={error}", status_code=303)
    if not code or not state or state != expected_state or not verifier:
        _audit_failure(storage, request, user_id, "state_mismatch")
        return RedirectResponse("/profile?ehr_error=state_mismatch", status_code=303)

    loop = asyncio.get_running_loop()
    try:
        token = await loop.run_in_executor(
            None, lambda: epic.exchange_code(code=code, code_verifier=verifier)
        )
    except (EpicError, EHRConfigError) as e:
        logger.exception("Epic token exchange failed")
        _audit_failure(storage, request, user_id, f"token_exchange:{type(e).__name__}")
        return RedirectResponse("/profile?ehr_error=token_exchange", status_code=303)

    if not token.patient_fhir_id:
        # Without a patient context we can't import — Epic should always return
        # one when the launch/patient scope is granted, but fail loud if not.
        _audit_failure(storage, request, user_id, "no_patient_context")
        return RedirectResponse("/profile?ehr_error=no_patient_context", status_code=303)
    patient_fhir_id: str = token.patient_fhir_id

    try:
        access_enc = encrypt_token(token.access_token)
        refresh_enc = encrypt_token(token.refresh_token) if token.refresh_token else None
    except EHRConfigError:
        logger.exception("EHR_TOKEN_KEY missing or malformed")
        _audit_failure(storage, request, user_id, "ehr_token_key_missing")
        return RedirectResponse("/profile?ehr_error=server_config", status_code=303)

    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=token.expires_in)
    endpoints = epic.discover()  # cached

    storage.create_ehr_connection(
        user_id=user_id,
        ehr_vendor=EHR_VENDOR,
        iss=endpoints.fhir_base,
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

    # Fetch + parse the Patient, stash parsed dict (NOT tokens) in session.
    try:
        patient_resource = await loop.run_in_executor(
            None,
            lambda: epic.fetch_patient(
                access_token=token.access_token, patient_fhir_id=patient_fhir_id
            ),
        )
        imported = parse_fhir_patient(patient_resource)
    except (EpicError, ValueError):
        logger.exception("Epic Patient fetch/parse failed")
        return RedirectResponse("/profile?ehr_error=patient_fetch", status_code=303)

    request.session[SESSION_PENDING_KEY] = imported.model_dump()
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
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    _require_enabled()
    pending = request.session.get(SESSION_PENDING_KEY)
    if not pending:
        return RedirectResponse("/profile?ehr_error=no_pending_import", status_code=303)
    imported = ImportedPatient(**pending)
    scope = Scope(user_id=current_user["id"])
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
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    _require_enabled()
    user_id = current_user["id"]
    pending = request.session.get(SESSION_PENDING_KEY)
    if not pending:
        return RedirectResponse("/profile?ehr_error=no_pending_import", status_code=303)
    imported = ImportedPatient(**pending)
    scope = Scope(user_id=user_id)

    if action == "create_new":
        if not imported.first_name or not imported.last_name:
            # FHIR Patient lacked a usable name; we don't auto-fabricate one.
            return RedirectResponse("/profile?ehr_error=missing_patient_name", status_code=303)
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
            created_by_user_id=user_id,
        )
        new_id = patient.id
    elif action == "merge":
        if patient_id is None:
            return RedirectResponse("/profile?ehr_error=merge_requires_patient_id", status_code=303)
        existing = storage.get_patient(scope, patient_id)
        if existing is None:
            return RedirectResponse("/profile?ehr_error=patient_not_found", status_code=303)
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
        if update_kwargs:
            storage.update_patient(scope, patient_id, **update_kwargs)
        new_id = patient_id
    else:
        return RedirectResponse("/profile?ehr_error=invalid_action", status_code=303)

    audit.record(
        storage,
        action="patient.imported_from_ehr",
        request=request,
        actor_user_id=user_id,
        scope_user_id=user_id,
        entity_type="patient",
        entity_id=str(new_id),
        metadata={
            "ehr_vendor": EHR_VENDOR,
            "fhir_patient_id": imported.fhir_id,
            "action": action,
        },
    )
    request.session.pop(SESSION_PENDING_KEY, None)
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
    return render(
        "_connected_ehrs.html",
        {
            "request": request,
            "epic_connection": None,
            "ehr_enabled": True,
        },
    )
