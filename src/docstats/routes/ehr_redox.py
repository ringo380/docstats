"""Redox aggregator routes (Phase 12.E) — org-scoped admin + import.

Distinct from ``routes/ehr.py`` because Redox is fundamentally different from
SMART-on-FHIR vendors:

- No per-user OAuth dance (no ``authorize`` redirect, no PKCE, no callback)
- Connection is owned by an organization, not a user
- Auth is JWT-bearer assertion (RFC 7523) signed with an RSA keypair
- Tokens are NOT persisted — re-minted in-process via short-lived cache

Handlers in two groups:

Admin (require_admin_scope):
- ``GET  /ehr/redox/connect``    — admin form for destination path
- ``POST /ehr/redox/connect``    — validates by minting a token + persists row
- ``POST /ehr/redox/disconnect`` — admin revokes the org connection

Import (any org member with PHI consent + active org Redox connection):
- ``GET  /ehr/redox/import``         — MRN entry form
- ``POST /ehr/redox/import/lookup``  — find Patient by MRN identifier
- ``GET  /ehr/redox/import/review``  — re-fetch Patient + show match candidates
- ``POST /ehr/redox/import/confirm`` — create_new or merge

Feature flag: ``EHR_REDOX_ENABLED=1`` gates every handler; off → 404.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from docstats.auth import require_user
from docstats.domain import audit
from docstats.domain.ehr import REDOX_SCOPES, EHRConnection
from docstats.ehr import redox  # noqa: F401 — side-effect: registers vendor
from docstats.ehr.mappers import parse_fhir_patient
from docstats.ehr.registry import EHRError
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope, render
from docstats.routes.admin import require_admin_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ehr/redox", tags=["ehr", "redox"])

_VENDOR_KEY = "redox"
_DEFAULT_DESTINATION = "redox-fhir-sandbox/Development"


def _flag_enabled() -> bool:
    return os.environ.get("EHR_REDOX_ENABLED", "").strip() == "1"


def _require_enabled() -> None:
    if not _flag_enabled():
        raise HTTPException(status_code=404)


@router.get("/connect", response_class=HTMLResponse)
def redox_connect_form(
    request: Request,
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """Render the admin connect form."""
    _require_enabled()
    assert scope.organization_id is not None  # require_admin_scope guarantees
    existing = storage.get_active_org_ehr_connection(scope.organization_id, _VENDOR_KEY)
    return render(
        "ehr_redox_connect.html",
        {
            "request": request,
            "active_connection": existing,
            "default_destination": _DEFAULT_DESTINATION,
            "scopes": REDOX_SCOPES,
        },
    )


@router.post("/connect")
def redox_connect_submit(
    request: Request,
    destination_path: str = Form(..., max_length=255),
    scope: Scope = Depends(require_admin_scope),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """Validate creds + persist an org-scoped Redox connection.

    Validation strategy: try to mint a token via the vendor module. If the
    token endpoint returns 200, creds are valid; we don't need to make a FHIR
    call at this point (destination path is just stored — its first FHIR
    request will validate it during patient lookup).
    """
    _require_enabled()
    assert scope.organization_id is not None
    org_id = scope.organization_id

    cleaned_dest = destination_path.strip().strip("/")
    if not cleaned_dest:
        return _redirect_with_error("missing_destination")

    # Validate auth by minting a token. Fails closed if env config is missing
    # or the dashboard hasn't activated the keypair.
    try:
        redox.request_access_token(scope=REDOX_SCOPES, force_refresh=True)
    except redox.RedoxConfigError as exc:
        logger.warning("redox connect config error: %s", exc)
        return _redirect_with_error("server_config")
    except EHRError as exc:
        logger.warning("redox connect token mint failed: %s", exc)
        return _redirect_with_error("token_exchange")

    # Persist (revokes any prior active row for this org+vendor in the same tx).
    storage.create_org_ehr_connection(
        organization_id=org_id,
        ehr_vendor=_VENDOR_KEY,
        iss=cleaned_dest,
        scope=REDOX_SCOPES,
    )

    audit.record(
        storage,
        action="ehr.connected",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=org_id,
        metadata={"ehr_vendor": _VENDOR_KEY, "iss": cleaned_dest},
    )

    return RedirectResponse("/admin/org?ehr_connected=redox", status_code=303)


@router.post("/disconnect")
def redox_disconnect(
    request: Request,
    scope: Scope = Depends(require_admin_scope),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """Revoke the active org Redox connection (idempotent)."""
    _require_enabled()
    assert scope.organization_id is not None
    org_id = scope.organization_id

    revoked = storage.revoke_org_ehr_connection(org_id, _VENDOR_KEY)
    if revoked:
        audit.record(
            storage,
            action="ehr.disconnected",
            request=request,
            actor_user_id=current_user["id"],
            scope_organization_id=org_id,
            metadata={"ehr_vendor": _VENDOR_KEY, "revoked_count": revoked},
        )
    return RedirectResponse("/admin/org?ehr_disconnected=redox", status_code=303)


def _redirect_with_error(reason: str) -> RedirectResponse:
    return RedirectResponse(f"/ehr/redox/connect?error={reason}", status_code=303)


# ---------------------------------------------------------------------------
# Patient import flow
# ---------------------------------------------------------------------------


_DEFAULT_MRN_SYSTEM_HINT = "http://hospital.smarthealthit.org"
_IMPORT_ALLOWED_ERRORS = frozenset(
    {
        "no_org",
        "no_org_connection",
        "missing_mrn",
        "patient_not_found",
        "ambiguous_mrn",
        "lookup_failed",
        "server_config",
        "missing_patient_name",
        "merge_requires_patient_id",
        "patient_not_in_workspace",
        "fetch_failed",
    }
)


def _require_org_redox_connection(storage: StorageBase, scope: Scope) -> EHRConnection | None:
    """Return the active org-scoped Redox connection, or ``None`` if missing.

    Caller must already have asserted ``scope.is_org``. Patient import requires
    an active org-level Redox connection — solo users never see these routes
    even when EHR_REDOX_ENABLED is on.
    """
    if scope.organization_id is None:
        return None
    return storage.get_active_org_ehr_connection(scope.organization_id, _VENDOR_KEY)


def _import_redirect_with_error(reason: str) -> RedirectResponse:
    safe = reason if reason in _IMPORT_ALLOWED_ERRORS else "lookup_failed"
    return RedirectResponse(f"/ehr/redox/import?error={safe}", status_code=303)


@router.get("/import", response_class=HTMLResponse)
def redox_import_form(
    request: Request,
    current_user: dict = Depends(require_phi_consent),
    storage: StorageBase = Depends(get_storage),
    scope: Scope = Depends(get_scope),
) -> Response:
    """Render the MRN-entry form for importing a patient from Redox."""
    _require_enabled()
    if not scope.is_org:
        raise HTTPException(
            status_code=403,
            detail="Redox import requires an active organization.",
        )
    conn = _require_org_redox_connection(storage, scope)
    if conn is None:
        raise HTTPException(
            status_code=403,
            detail="Your organization has no active Redox connection. Ask an admin to connect Redox first.",
        )
    return render(
        "ehr_redox_import.html",
        {
            "request": request,
            "user": current_user,
            "default_mrn_system": _DEFAULT_MRN_SYSTEM_HINT,
            "destination_path": conn.iss,
        },
    )


@router.post("/import/lookup")
async def redox_import_lookup(
    request: Request,
    mrn: str = Form(..., max_length=255),
    mrn_system: str = Form("", max_length=255),
    current_user: dict = Depends(require_phi_consent),
    storage: StorageBase = Depends(get_storage),
    scope: Scope = Depends(get_scope),
) -> Response:
    """Look up a Patient by MRN identifier; redirect to review on hit.

    On miss: 303 back to the form with ``?error=patient_not_found``.
    On ambiguous match (multiple results): 303 with ``ambiguous_mrn``.
    On vendor / config error: soft-fail with the appropriate error code so
    the form re-renders with a useful message.
    """
    _require_enabled()
    if not scope.is_org:
        return _import_redirect_with_error("no_org")
    conn = _require_org_redox_connection(storage, scope)
    if conn is None:
        return _import_redirect_with_error("no_org_connection")

    cleaned_mrn = mrn.strip()
    cleaned_system = mrn_system.strip() or None
    if not cleaned_mrn:
        return _import_redirect_with_error("missing_mrn")

    loop = asyncio.get_running_loop()
    try:
        access_token = await loop.run_in_executor(
            None,
            lambda: redox.request_access_token(scope=REDOX_SCOPES),
        )
    except redox.RedoxConfigError:
        logger.warning("redox import: server config missing")
        return _import_redirect_with_error("server_config")
    except EHRError:
        logger.exception("redox import: token mint failed")
        return _import_redirect_with_error("lookup_failed")

    try:
        fhir_id = await loop.run_in_executor(
            None,
            lambda: redox.find_patient_by_mrn(
                access_token=access_token,
                mrn=cleaned_mrn,
                mrn_system=cleaned_system,
                destination_path=conn.iss,
            ),
        )
    except redox.RedoxError as exc:
        msg = str(exc).lower()
        if "matches" in msg or "ambiguous" in msg:
            return _import_redirect_with_error("ambiguous_mrn")
        logger.exception("redox import: patient lookup failed")
        return _import_redirect_with_error("lookup_failed")

    if not fhir_id:
        return _import_redirect_with_error("patient_not_found")

    audit.record(
        storage,
        action="ehr.import_lookup",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        metadata={"ehr_vendor": _VENDOR_KEY, "fhir_id": fhir_id},
    )
    return RedirectResponse(f"/ehr/redox/import/review?fhir_id={fhir_id}", status_code=303)


def _import_review_redirect(reason: str) -> RedirectResponse:
    return _import_redirect_with_error(reason)


def _candidate_matches_for_redox(storage: StorageBase, scope: Scope, imported) -> list[dict]:
    """Find existing patients in the org's workspace that look like a match.

    Mirrors the SMART-vendor candidate-match shape but lives here to avoid
    cross-importing private helpers from ``routes/ehr.py``.
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
async def redox_import_review(
    request: Request,
    fhir_id: str = Query(..., max_length=255),
    current_user: dict = Depends(require_phi_consent),
    storage: StorageBase = Depends(get_storage),
    scope: Scope = Depends(get_scope),
) -> Response:
    """Re-fetch Patient by id and render the review screen."""
    _require_enabled()
    if not scope.is_org:
        return _import_review_redirect("no_org")
    conn = _require_org_redox_connection(storage, scope)
    if conn is None:
        return _import_review_redirect("no_org_connection")

    cleaned_fhir_id = fhir_id.strip()
    if not cleaned_fhir_id:
        return _import_review_redirect("patient_not_found")

    loop = asyncio.get_running_loop()
    try:
        access_token = await loop.run_in_executor(
            None,
            lambda: redox.request_access_token(scope=REDOX_SCOPES),
        )
        patient_resource = await loop.run_in_executor(
            None,
            lambda: redox.fetch_patient(
                access_token=access_token,
                patient_fhir_id=cleaned_fhir_id,
                destination_path=conn.iss,
            ),
        )
    except redox.RedoxConfigError:
        return _import_review_redirect("server_config")
    except EHRError:
        logger.exception("redox import review: fetch failed for fhir_id=%s", cleaned_fhir_id)
        return _import_review_redirect("fetch_failed")

    try:
        imported = parse_fhir_patient(patient_resource)
    except (ValueError, KeyError):
        logger.exception("redox import review: parse failed")
        return _import_review_redirect("fetch_failed")

    candidates = _candidate_matches_for_redox(storage, scope, imported)
    return render(
        "ehr_redox_review.html",
        {
            "request": request,
            "user": current_user,
            "imported": imported,
            "candidates": candidates,
            "fhir_id": cleaned_fhir_id,
        },
    )


@router.post("/import/confirm")
async def redox_import_confirm(
    request: Request,
    action: str = Form(..., max_length=20),
    fhir_id: str = Form(..., max_length=255),
    patient_id: int | None = Form(None),
    current_user: dict = Depends(require_phi_consent),
    storage: StorageBase = Depends(get_storage),
    scope: Scope = Depends(get_scope),
) -> Response:
    """Create or merge the imported patient into the workspace."""
    _require_enabled()
    if not scope.is_org:
        return _import_redirect_with_error("no_org")
    conn = _require_org_redox_connection(storage, scope)
    if conn is None:
        return _import_redirect_with_error("no_org_connection")

    cleaned_fhir_id = fhir_id.strip()
    if not cleaned_fhir_id:
        return _import_redirect_with_error("patient_not_found")

    user_id = current_user["id"]

    # Re-fetch the Patient resource — same defensive pattern as the SMART path.
    # Never trust a session-cached imported PHI snapshot.
    loop = asyncio.get_running_loop()
    try:
        access_token = await loop.run_in_executor(
            None,
            lambda: redox.request_access_token(scope=REDOX_SCOPES),
        )
        patient_resource = await loop.run_in_executor(
            None,
            lambda: redox.fetch_patient(
                access_token=access_token,
                patient_fhir_id=cleaned_fhir_id,
                destination_path=conn.iss,
            ),
        )
    except redox.RedoxConfigError:
        return _import_redirect_with_error("server_config")
    except EHRError:
        logger.exception("redox import confirm: fetch failed for fhir_id=%s", cleaned_fhir_id)
        return _import_redirect_with_error("fetch_failed")

    try:
        imported = parse_fhir_patient(patient_resource)
    except (ValueError, KeyError):
        return _import_redirect_with_error("fetch_failed")

    if action == "create_new":
        if not imported.first_name or not imported.last_name:
            return _import_redirect_with_error("missing_patient_name")
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
        action_taken = "create_new"
    elif action == "merge":
        if patient_id is None:
            return _import_redirect_with_error("merge_requires_patient_id")
        existing = storage.get_patient(scope, patient_id)
        if existing is None:
            return _import_redirect_with_error("patient_not_in_workspace")
        update_kwargs: dict = {}
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
        action_taken = "merge"
    else:
        return _import_redirect_with_error("missing_patient_name")

    audit.record(
        storage,
        action="patient.imported_from_ehr",
        request=request,
        actor_user_id=user_id,
        scope_organization_id=scope.organization_id,
        entity_type="patient",
        entity_id=str(new_id),
        metadata={
            "ehr_vendor": _VENDOR_KEY,
            "destination_path": conn.iss,
            "fhir_id": cleaned_fhir_id,
            "action": action_taken,
        },
    )
    return RedirectResponse(f"/patients/{new_id}", status_code=303)
