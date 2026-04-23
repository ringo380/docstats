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

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request
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


# FastAPI OperationOutcome issue.code vocab (subset we emit). Mirrors FHIR
# R4 ``issue-type`` value set — callers pass the closest match for the
# HTTP status code.
_STATUS_TO_FHIR_CODE: dict[int, str] = {
    400: "invalid",
    401: "security",
    403: "forbidden",
    404: "not-found",
    409: "incomplete",
    413: "too-costly",
    422: "invalid",
    500: "exception",
    503: "transient",
}


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Negotiated error handler for /api/v2/* HTTPExceptions.

    When any dependency (``require_user_api`` / ``require_phi_consent_api``)
    or handler raises ``HTTPException``, FastAPI's default handler returns
    ``{"detail": ...}`` with ``Content-Type: application/json`` — which
    breaks FHIR clients that asked for ``application/fhir+json``. This
    handler rewraps the response through the same content negotiation as
    the success paths.

    Registered on the app via ``add_exception_handler(HTTPException,
    http_exception_handler)`` scoped by path in ``web.py`` — anything
    outside ``/api/v2/*`` routes falls through to the framework default.
    """
    fhir_mode = _wants_fhir(request)
    content_type = FHIR_CONTENT_TYPE if fhir_mode else JSON_CONTENT_TYPE
    detail = exc.detail

    # require_user_api / require_phi_consent_api use dict details shaped
    # like ``{"code": ..., "message": ...}``. Plain-JSON callers expect
    # the same ``{"detail": {...}}`` shape FastAPI would have emitted, so
    # preserve the dict there. FHIR callers get a flattened
    # OperationOutcome with the dict's ``message`` as diagnostics.
    if fhir_mode:
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("detail") or "Error")
        else:
            message = str(detail)
        body: dict[str, Any] = operation_outcome(
            severity="error",
            code=_STATUS_TO_FHIR_CODE.get(exc.status_code, "processing"),
            diagnostics=message,
        )
    else:
        body = {"detail": detail}

    return JSONResponse(
        content=body,
        status_code=exc.status_code,
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


# Both read handlers chain: require_phi_consent_api → require_user_api →
# get_current_user. Unauthenticated → 401 JSON; authenticated-but-not-
# consented → 403 JSON. Neither path redirects, so curl-based consumers
# get actionable errors. The equivalent browser flow (`require_user` /
# `require_phi_consent`) still 303-redirects to /auth/login or the PHI
# consent prompt, which is correct for web UI callers.
_ = require_user_api  # imported for symmetry; not used as a direct dep here


# ---------- /api/v2/webhooks/inbound (Phase 8.C — dead-lettered inbox) ----------

# Maximum accepted webhook body. 256 KiB is comfortably above anything
# current delivery vendors send and well below Railway's platform limit.
WEBHOOK_MAX_BYTES = 256 * 1024

# ±5-minute skew for the signed X-Timestamp to kill replays against the
# inbox (denial-of-wallet would be trivial otherwise — row inserts are
# cheap per request but accumulate).
WEBHOOK_CLOCK_SKEW_SECONDS = 300

# Only these header names are persisted with the row. Everything else is
# dropped before write — raw proxy identifiers, cookies, etc. must not
# leak into the DB.
_HEADER_ALLOWLIST = frozenset(
    {"content-type", "user-agent", "x-signature", "x-timestamp", "x-source"}
)


def _filter_headers(request: Request) -> dict[str, str]:
    """Lowercase-keyed allowlisted subset of the request headers."""
    return {
        name.lower(): value
        for name, value in request.headers.items()
        if name.lower() in _HEADER_ALLOWLIST
    }


def _webhook_error(
    *, status_code: int, code: str, detail: str, audit_note: str | None = None
) -> JSONResponse:
    """Error response shape — not negotiated (webhook callers aren't FHIR).

    ``audit_note`` is ignored here but kept in the signature so the
    handler can uniformly build both stored rows and error responses.
    """
    _ = audit_note
    return JSONResponse(
        content={"code": code, "detail": detail},
        status_code=status_code,
        headers={
            "X-Docstats-Api-Version": API_VERSION,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/webhooks/inbound")
async def webhooks_inbound(
    request: Request,
    storage: StorageBase = Depends(get_storage),
) -> JSONResponse:
    """Dead-lettered inbound webhook endpoint.

    Accepts HMAC-signed JSON posts and persists them to
    ``webhook_inbox``. No handlers yet — Phase 9 (outbound delivery
    vendor callbacks) and beyond consume these rows. Disabled by
    default; set ``WEBHOOK_INBOX_SECRET`` to activate.
    """
    secret = os.environ.get("WEBHOOK_INBOX_SECRET")
    if not secret:
        return _webhook_error(
            status_code=503,
            code="endpoint_disabled",
            detail="Webhook endpoint is administratively disabled (WEBHOOK_INBOX_SECRET unset).",
        )

    # Content-Length cap before reading the body — avoids buffering a
    # 50 MB upload just to reject it. Starlette still parses into a
    # SpooledTemporaryFile, but the cap keeps the memory / disk
    # footprint bounded.
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > WEBHOOK_MAX_BYTES:
        return _webhook_error(
            status_code=413,
            code="payload_too_large",
            detail=f"Body exceeds {WEBHOOK_MAX_BYTES} bytes.",
        )
    raw_body = await request.body()
    if len(raw_body) > WEBHOOK_MAX_BYTES:
        return _webhook_error(
            status_code=413,
            code="payload_too_large",
            detail=f"Body exceeds {WEBHOOK_MAX_BYTES} bytes.",
        )

    timestamp = request.headers.get("x-timestamp")
    signature = request.headers.get("x-signature")
    if not timestamp or not signature:
        return _webhook_error(
            status_code=401,
            code="invalid_signature",
            detail="Missing X-Timestamp or X-Signature header.",
        )

    # Timestamp replay guard. Must parse as int (Unix seconds).
    try:
        ts_int = int(timestamp)
    except ValueError:
        return _webhook_error(
            status_code=401,
            code="invalid_signature",
            detail="X-Timestamp must be Unix seconds.",
        )
    now = int(datetime.now(tz=timezone.utc).timestamp())
    if abs(now - ts_int) > WEBHOOK_CLOCK_SKEW_SECONDS:
        return _webhook_error(
            status_code=401,
            code="invalid_signature",
            detail="X-Timestamp outside acceptable clock skew.",
        )

    # HMAC-SHA-256 over "<timestamp>.<body>". Accept either the raw hex
    # or the "sha256=..." prefixed form that most vendor SDKs emit.
    # Do NOT .strip() the presented value — that would silently trim
    # whitespace differences into length-mismatched digests and confuse
    # integrators debugging legitimate 401s. If a vendor SDK appends a
    # trailing newline they need to fix their SDK, not rely on us.
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, sha256).hexdigest()
    presented = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, presented):
        return _webhook_error(
            status_code=401,
            code="invalid_signature",
            detail="HMAC signature does not match.",
        )

    # Body must be valid JSON. Invalid JSON is dropped with 400 — but we
    # do NOT record it to the inbox (nothing useful to triage without a
    # parseable payload).
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _webhook_error(
            status_code=400,
            code="invalid_payload",
            detail="Request body must be UTF-8 JSON.",
        )
    if not isinstance(payload, dict):
        return _webhook_error(
            status_code=400,
            code="invalid_payload",
            detail="Top-level JSON value must be an object.",
        )

    try:
        inbox_id = storage.record_inbound_webhook(
            source=request.headers.get("x-source"),
            payload_json=payload,
            http_headers_json=_filter_headers(request),
            signature=signature,
            status="received",
        )
    except Exception:
        logger.exception("Failed to persist inbound webhook")
        return _webhook_error(
            status_code=500,
            code="storage_error",
            detail="Failed to record inbound webhook.",
        )

    return JSONResponse(
        content={"id": inbox_id, "status": "received"},
        status_code=202,
        headers={
            "X-Docstats-Api-Version": API_VERSION,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
