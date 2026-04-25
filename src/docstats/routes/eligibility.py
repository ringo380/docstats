"""Eligibility check routes — Phase 11.B/C.

Phase 11.B endpoints (patient context):
  POST /patients/{id}/eligibility          — trigger a new eligibility check
  GET  /patients/{id}/eligibility/latest   — fetch the most recent result (htmx partial)

Phase 11.C endpoints (referral context):
  POST /referrals/{id}/eligibility         — trigger from referral; pre-fills payer from
                                             referral.payer_plan_id if available
  GET  /referrals/{id}/eligibility/latest  — side-rail card partial

Two endpoints per resource type:

  POST /patients/{id}/eligibility          — trigger a new eligibility check
  GET  /patients/{id}/eligibility/latest   — fetch the most recent result (htmx partial)

Cooldown: rejects if the last check is less than MIN_CHECK_INTERVAL_SECONDS old
(configurable via ELIGIBILITY_COOLDOWN_SECONDS, default 60s).  This prevents
double-clicks and accidental hammering of the Availity sandbox quota.

Rate-limiting and async wrapping live in AvailityClient; this layer handles
request validation, DB persistence, and audit logging.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Request
from fastapi.responses import HTMLResponse

from docstats.availity_client import (
    AvailityDisabledError,
    AvailityError,
    AvailityUnavailableError,
    get_availity_client,
)
from docstats.domain.audit import record as audit_record
from docstats.domain.eligibility import parse_coverage_response
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope, render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(tags=["eligibility"])

_DEFAULT_COOLDOWN = 60  # seconds
_DEFAULT_SERVICE_TYPE = "30"  # Health Benefit Plan Coverage


def _cooldown_seconds() -> int:
    raw = os.environ.get("ELIGIBILITY_COOLDOWN_SECONDS", "")
    try:
        val = int(raw)
        return max(10, min(val, 3600))
    except (ValueError, TypeError):
        return _DEFAULT_COOLDOWN


def _require_patient(scope: Scope, storage: StorageBase, patient_id: int):
    patient = storage.get_patient(scope, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    return patient


def _ctx(request: Request, user: dict, storage: StorageBase, **extra) -> dict:
    return {
        "request": request,
        "user": user,
        "saved_count": saved_count(storage, user["id"]),
        **extra,
    }


# ---------------------------------------------------------------------------
# POST /patients/{id}/eligibility  — trigger new check
# ---------------------------------------------------------------------------

@router.post("/patients/{patient_id}/eligibility", response_class=HTMLResponse)
async def trigger_eligibility_check(
    request: Request,
    patient_id: int = Path(..., ge=1),
    payer_id: str = Form(..., max_length=64),
    payer_name: str | None = Form(None, max_length=200),
    member_id: str = Form(..., max_length=64),
    service_type: str | None = Form(None, max_length=8),
    provider_npi: str | None = Form(None, max_length=10),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    patient = _require_patient(scope, storage, patient_id)

    svc_type = (service_type or "").strip() or _DEFAULT_SERVICE_TYPE

    # Cooldown guard: don't burn quota on double-clicks
    latest = storage.get_latest_eligibility_check(
        scope, patient_id, availity_payer_id=payer_id, service_type=svc_type
    )
    cooldown = _cooldown_seconds()
    if latest and latest.checked_at:
        age = (datetime.now(tz=timezone.utc) - latest.checked_at).total_seconds()
        if age < cooldown:
            seconds_left = int(cooldown - age)
            return render(
                "_eligibility_result.html",
                _ctx(
                    request,
                    current_user,
                    storage,
                    check=latest,
                    patient=patient,
                    error=f"Checked {int(age)}s ago — please wait {seconds_left}s before checking again.",
                    cooldown_active=True,
                ),
            )

    # Create a pending row first so the UI can show a spinner immediately
    check = storage.create_eligibility_check(
        scope,
        patient_id=patient_id,
        availity_payer_id=payer_id,
        payer_name=payer_name,
        service_type=svc_type,
        status="pending",
    )

    # Build the eligibility payload
    if not patient.date_of_birth:
        storage.update_eligibility_check(
            check.id,  # type: ignore[arg-type]
            status="error",
            error_message="Patient date of birth is required for eligibility checks.",
        )
        return render(
            "_eligibility_result.html",
            _ctx(
                request,
                current_user,
                storage,
                check=storage.get_latest_eligibility_check(
                    scope, patient_id, availity_payer_id=payer_id, service_type=svc_type
                ),
                patient=patient,
                error="Patient date of birth is required for eligibility checks.",
            ),
        )

    npi = (provider_npi or "").strip() or current_user.get("npi", "") or "0000000000"

    payload: dict = {
        "payerId": payer_id,
        "providerNpi": npi,
        "memberId": member_id,
        "patientBirthDate": patient.date_of_birth,
        "patientLastName": patient.last_name,
        "patientFirstName": patient.first_name,
        "serviceType": svc_type,
    }

    # Call Availity
    now = datetime.now(tz=timezone.utc)
    try:
        client = get_availity_client()
        raw = await client.async_check_eligibility(payload)
        result = parse_coverage_response(raw)
        storage.update_eligibility_check(
            check.id,  # type: ignore[arg-type]
            status="complete",
            result_json=result.model_dump_json(),
            raw_response_json=json.dumps(raw),
            checked_at=now,
        )
        audit_record(
            storage,
            action="eligibility.check",
            request=request,
            actor_user_id=current_user["id"],
            scope_user_id=scope.user_id if scope.is_solo else None,
            scope_organization_id=scope.organization_id,
            entity_type="patient",
            entity_id=str(patient_id),
            metadata={"payer_id": payer_id, "service_type": svc_type, "status": "complete"},
        )
    except AvailityDisabledError:
        storage.update_eligibility_check(
            check.id,  # type: ignore[arg-type]
            status="error",
            error_message="Eligibility checking is not configured on this server.",
            checked_at=now,
        )
    except AvailityUnavailableError as e:
        storage.update_eligibility_check(
            check.id,  # type: ignore[arg-type]
            status="error",
            error_message=f"Clearinghouse temporarily unavailable: {e}",
            checked_at=now,
        )
    except AvailityError as e:
        storage.update_eligibility_check(
            check.id,  # type: ignore[arg-type]
            status="error",
            error_message=str(e),
            checked_at=now,
        )

    updated_check = storage.get_latest_eligibility_check(
        scope, patient_id, availity_payer_id=payer_id, service_type=svc_type
    )
    return render(
        "_eligibility_result.html",
        _ctx(request, current_user, storage, check=updated_check, patient=patient),
    )


# ---------------------------------------------------------------------------
# GET /patients/{id}/eligibility/latest  — fetch latest result (htmx)
# ---------------------------------------------------------------------------

@router.get("/patients/{patient_id}/eligibility/latest", response_class=HTMLResponse)
async def get_latest_eligibility(
    request: Request,
    patient_id: int = Path(..., ge=1),
    payer_id: str | None = None,
    service_type: str | None = None,
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    patient = _require_patient(scope, storage, patient_id)
    check = storage.get_latest_eligibility_check(
        scope,
        patient_id,
        availity_payer_id=payer_id or None,
        service_type=service_type or None,
    )
    return render(
        "_eligibility_result.html",
        _ctx(request, current_user, storage, check=check, patient=patient),
    )


# ---------------------------------------------------------------------------
# POST /referrals/{id}/eligibility  — trigger from referral context (11.C)
# ---------------------------------------------------------------------------

@router.post("/referrals/{referral_id}/eligibility", response_class=HTMLResponse)
async def trigger_eligibility_from_referral(
    request: Request,
    referral_id: int = Path(..., ge=1),
    payer_id: str = Form(..., max_length=64),
    payer_name: str | None = Form(None, max_length=200),
    member_id: str = Form(..., max_length=64),
    service_type: str | None = Form(None, max_length=8),
    provider_npi: str | None = Form(None, max_length=10),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    patient = storage.get_patient(scope, referral.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")

    svc_type = (service_type or "").strip() or _DEFAULT_SERVICE_TYPE

    # Cooldown guard
    latest = storage.get_latest_eligibility_check(
        scope, referral.patient_id, availity_payer_id=payer_id, service_type=svc_type
    )
    cooldown = _cooldown_seconds()
    if latest and latest.checked_at:
        age = (datetime.now(tz=timezone.utc) - latest.checked_at).total_seconds()
        if age < cooldown:
            seconds_left = int(cooldown - age)
            return render(
                "_referral_eligibility_card.html",
                _ctx(
                    request,
                    current_user,
                    storage,
                    check=latest,
                    referral=referral,
                    patient=patient,
                    error=f"Checked {int(age)}s ago — wait {seconds_left}s.",
                    cooldown_active=True,
                ),
            )

    check = storage.create_eligibility_check(
        scope,
        patient_id=referral.patient_id,
        availity_payer_id=payer_id,
        payer_name=payer_name,
        service_type=svc_type,
        status="pending",
    )

    if not patient.date_of_birth:
        storage.update_eligibility_check(
            check.id,  # type: ignore[arg-type]
            status="error",
            error_message="Patient date of birth is required.",
        )
        latest_err = storage.get_latest_eligibility_check(
            scope, referral.patient_id, availity_payer_id=payer_id, service_type=svc_type
        )
        return render(
            "_referral_eligibility_card.html",
            _ctx(
                request,
                current_user,
                storage,
                check=latest_err,
                referral=referral,
                patient=patient,
                error="Patient date of birth is required.",
            ),
        )

    npi = (provider_npi or "").strip() or (referral.referring_provider_npi or "") or "0000000000"
    payload: dict = {
        "payerId": payer_id,
        "providerNpi": npi,
        "memberId": member_id,
        "patientBirthDate": patient.date_of_birth,
        "patientLastName": patient.last_name,
        "patientFirstName": patient.first_name,
        "serviceType": svc_type,
    }

    now = datetime.now(tz=timezone.utc)
    try:
        client = get_availity_client()
        raw = await client.async_check_eligibility(payload)
        result = parse_coverage_response(raw)
        storage.update_eligibility_check(
            check.id,  # type: ignore[arg-type]
            status="complete",
            result_json=result.model_dump_json(),
            raw_response_json=json.dumps(raw),
            checked_at=now,
        )
        audit_record(
            storage,
            action="eligibility.check",
            request=request,
            actor_user_id=current_user["id"],
            scope_user_id=scope.user_id if scope.is_solo else None,
            scope_organization_id=scope.organization_id,
            entity_type="referral",
            entity_id=str(referral_id),
            metadata={"payer_id": payer_id, "service_type": svc_type, "status": "complete"},
        )
    except (AvailityDisabledError, AvailityUnavailableError, AvailityError) as e:
        msg = (
            "Eligibility checking is not configured on this server."
            if isinstance(e, AvailityDisabledError)
            else str(e)
        )
        storage.update_eligibility_check(
            check.id,  # type: ignore[arg-type]
            status="error",
            error_message=msg,
            checked_at=now,
        )

    updated_check = storage.get_latest_eligibility_check(
        scope, referral.patient_id, availity_payer_id=payer_id, service_type=svc_type
    )
    return render(
        "_referral_eligibility_card.html",
        _ctx(request, current_user, storage, check=updated_check,
             referral=referral, patient=patient),
    )


# ---------------------------------------------------------------------------
# GET /referrals/{id}/eligibility/latest  — side-rail card (htmx, 11.C)
# ---------------------------------------------------------------------------

@router.get("/referrals/{referral_id}/eligibility/latest", response_class=HTMLResponse)
async def get_referral_eligibility_latest(
    request: Request,
    referral_id: int = Path(..., ge=1),
    payer_id: str | None = None,
    service_type: str | None = None,
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    patient = storage.get_patient(scope, referral.patient_id)
    check = storage.get_latest_eligibility_check(
        scope,
        referral.patient_id,
        availity_payer_id=payer_id or None,
        service_type=service_type or None,
    )
    return render(
        "_referral_eligibility_card.html",
        _ctx(request, current_user, storage, check=check,
             referral=referral, patient=patient),
    )
