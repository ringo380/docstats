"""API v2 — stable read endpoints for machine consumers (Phase 8.B).

- ``GET /api/v2/referrals/{id}`` — plain JSON by default, FHIR Bundle when
  ``Accept: application/fhir+json``.
- ``GET /api/v2/patients/{id}`` — same negotiation; fhir+json mode returns a
  bare Patient resource (not a ``$everything`` bundle — out of scope for 8.B).

Auth = session cookie via :func:`docstats.auth.require_user_api`, which
returns 401 JSON instead of redirecting to ``/auth/login`` (the plain
``require_user`` dependency does the latter, which is correct for browsers
but fatal for API clients). Real OAuth2 client_credentials lands in
Phase 12 (SMART-on-FHIR).

Content negotiation is intentionally simple: we check whether
``"fhir+json"`` appears as a substring in the Accept header. RFC 7231
q-value parsing is out of scope — single-q and multi-type Accept headers
work in practice. The explicit ``*/*`` test pins this so a future
contributor can't add a helpful-but-breaking real parser.

Every successful request audits ``referral.api_v2.read`` /
``patient.api_v2.read`` with the chosen content type in the metadata so
operators can grep the audit log for consumer behavior (who's asking for
which format).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse

from docstats.auth import require_user_api
from docstats.domain.audit import record as audit_record
from docstats.enrichment import fetch_receiving_direct_endpoints
from docstats.exports import build_patient_resource, build_referral_bundle, operation_outcome
from docstats.phi import require_phi_consent_api
from docstats.routes._common import get_client, get_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["api-v2"])

API_VERSION = "2"
FHIR_CONTENT_TYPE = "application/fhir+json"
JSON_CONTENT_TYPE = "application/json"


def _wants_fhir(request: Request) -> bool:
    """Return True if the Accept header asks for FHIR JSON.

    Substring check — no RFC 7231 q-value parsing. Pinned by
    ``test_wildcard_accept_returns_plain_json`` so any future rewrite
    that changes the tie-breaker surfaces in CI.
    """
    accept = (request.headers.get("accept") or "").lower()
    return "fhir+json" in accept


def _negotiate_content_type(request: Request) -> str:
    return FHIR_CONTENT_TYPE if _wants_fhir(request) else JSON_CONTENT_TYPE


def _base_response_headers(content_type: str) -> dict[str, str]:
    return {
        "X-Docstats-Api-Version": API_VERSION,
        "Content-Type": content_type,
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }


def _error_response(
    *,
    status_code: int,
    fhir_mode: bool,
    code: str,
    detail: str,
) -> JSONResponse:
    """Return a negotiated error response.

    fhir+json mode → FHIR OperationOutcome; plain JSON → ``{detail, code}``.
    FHIR severity is always ``error`` for 4xx/5xx here; OperationOutcome
    codes follow the FHIR closed vocab (``not-found`` / ``forbidden`` / …).
    """
    content_type = FHIR_CONTENT_TYPE if fhir_mode else JSON_CONTENT_TYPE
    if fhir_mode:
        body: dict[str, Any] = operation_outcome(severity="error", code=code, diagnostics=detail)
    else:
        body = {"code": code, "detail": detail}
    return JSONResponse(
        content=body,
        status_code=status_code,
        headers=_base_response_headers(content_type),
    )


# ---------- /api/v2/referrals/{id} ----------


@router.get("/referrals/{referral_id}")
async def get_referral_v2(
    request: Request,
    referral_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent_api),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
) -> JSONResponse:
    fhir_mode = _wants_fhir(request)

    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        return _error_response(
            status_code=404,
            fhir_mode=fhir_mode,
            code="not-found",
            detail=f"Referral {referral_id} not found or not visible to this scope.",
        )

    patient = storage.get_patient(scope, referral.patient_id)
    if patient is None:
        return _error_response(
            status_code=409,
            fhir_mode=fhir_mode,
            code="incomplete",
            detail="Patient record unavailable for this referral.",
        )

    if fhir_mode:
        diagnoses = storage.list_referral_diagnoses(scope, referral_id)
        medications = storage.list_referral_medications(scope, referral_id)
        allergies = storage.list_referral_allergies(scope, referral_id)
        attachments = storage.list_referral_attachments(scope, referral_id)
        responses = storage.list_referral_responses(scope, referral_id)
        receiving_endpoints = await fetch_receiving_direct_endpoints(
            referral.receiving_provider_npi, get_client()
        )

        # TOCTOU re-check — mirrors routes/exports.py.
        if storage.get_referral(scope, referral_id) is None:
            return _error_response(
                status_code=404,
                fhir_mode=fhir_mode,
                code="not-found",
                detail=f"Referral {referral_id} not found or not visible to this scope.",
            )

        body: dict[str, Any] = build_referral_bundle(
            referral=referral,
            patient=patient,
            diagnoses=diagnoses,
            medications=medications,
            allergies=allergies,
            attachments=attachments,
            responses=responses,
            receiving_endpoints=receiving_endpoints,
            generated_at=datetime.now(tz=timezone.utc),
        )
        entry_count = len(body.get("entry", []))
    else:
        body = referral.model_dump(mode="json")
        entry_count = 0

    content_type = FHIR_CONTENT_TYPE if fhir_mode else JSON_CONTENT_TYPE

    audit_record(
        storage,
        action="referral.api_v2.read",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral",
        entity_id=str(referral_id),
        metadata={
            "accept_header": request.headers.get("accept") or "",
            "content_type": content_type,
            "bundle_entries": entry_count,
        },
    )

    return JSONResponse(content=body, status_code=200, headers=_base_response_headers(content_type))


# ---------- /api/v2/patients/{id} ----------


@router.get("/patients/{patient_id}")
async def get_patient_v2(
    request: Request,
    patient_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent_api),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
) -> JSONResponse:
    fhir_mode = _wants_fhir(request)

    patient = storage.get_patient(scope, patient_id)
    if patient is None:
        return _error_response(
            status_code=404,
            fhir_mode=fhir_mode,
            code="not-found",
            detail=f"Patient {patient_id} not found or not visible to this scope.",
        )

    if fhir_mode:
        # Bare Patient resource, not a Bundle. $everything-style patient-
        # centric bundles are a Phase 12 concern — response size unbounded.
        body: dict[str, Any] = build_patient_resource(patient)
    else:
        body = patient.model_dump(mode="json")

    content_type = FHIR_CONTENT_TYPE if fhir_mode else JSON_CONTENT_TYPE

    audit_record(
        storage,
        action="patient.api_v2.read",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="patient",
        entity_id=str(patient_id),
        metadata={
            "accept_header": request.headers.get("accept") or "",
            "content_type": content_type,
        },
    )

    return JSONResponse(content=body, status_code=200, headers=_base_response_headers(content_type))


# Both handlers chain: require_phi_consent_api → require_user_api →
# get_current_user. Unauthenticated → 401 JSON; authenticated-but-not-
# consented → 403 JSON. Neither path redirects, so curl-based consumers
# get actionable errors. The equivalent browser flow (`require_user` /
# `require_phi_consent`) still 303-redirects to /auth/login or the PHI
# consent prompt, which is correct for web UI callers.
_ = require_user_api  # imported for symmetry; not used as a direct dep here
