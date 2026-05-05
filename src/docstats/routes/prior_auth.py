"""Prior authorization routes — Phase 11.E.

Endpoints (referral-scoped):

  POST /referrals/{id}/auth-submit            — submit a new auth request to Availity
  POST /referrals/{id}/auth-status/refresh    — poll latest decision (cooldown-gated)
  GET  /referrals/{id}/auth-status            — read-only cached card (no API call)

POST is used for the refresh path (rather than overloading GET) so htmx polling /
browser prefetch can't accidentally hammer the clearinghouse.

Cooldown / submit gating:
  - Submit rejects if a non-terminal submission already exists for the referral
    (caller should poll the existing one).
  - Refresh rejects if last_polled_at is younger than AUTH_POLL_COOLDOWN_SECONDS.
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
from docstats.domain.prior_auth import (
    PA_STATUS_TERMINAL,
    build_idempotency_key,
    parse_authorization_response,
)
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope, render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(tags=["prior-auth"])

_DEFAULT_POLL_COOLDOWN = 60  # seconds


def _poll_cooldown_seconds() -> int:
    raw = os.environ.get("AUTH_POLL_COOLDOWN_SECONDS", "")
    try:
        val = int(raw)
        return max(10, min(val, 3600))
    except (ValueError, TypeError):
        return _DEFAULT_POLL_COOLDOWN


def _ctx(request: Request, user: dict, storage: StorageBase, **extra) -> dict:
    return {
        "request": request,
        "user": user,
        "saved_count": saved_count(storage, user["id"]),
        **extra,
    }


def _split_codes(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [tok.strip().upper() for tok in raw.replace(";", ",").split(",") if tok.strip()]


def _emit_pa_event(
    storage: StorageBase,
    scope: Scope,
    *,
    referral_id: int,
    actor_user_id: int,
    event_type: str,
    note: str,
) -> None:
    """Best-effort referral_event for prior-auth lifecycle. Logs and swallows on failure."""
    try:
        storage.record_referral_event(
            scope,
            referral_id=referral_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            note=note,
        )
    except Exception:
        logger.exception("Failed to record prior-auth referral event")


# ---------------------------------------------------------------------------
# POST /referrals/{id}/auth-submit
# ---------------------------------------------------------------------------


@router.post("/referrals/{referral_id}/auth-submit", response_class=HTMLResponse)
async def submit_prior_auth(
    request: Request,
    referral_id: int = Path(..., ge=1),
    payer_id: str = Form(..., max_length=64),
    payer_name: str | None = Form(None, max_length=200),
    member_id: str = Form(..., max_length=64),
    service_type: str = Form(..., max_length=8),
    diagnosis_codes: str | None = Form(None, max_length=500),
    procedure_codes: str = Form(..., max_length=500),
    service_date: str | None = Form(None, max_length=10),
    place_of_service: str | None = Form(None, max_length=8),
    provider_npi: str | None = Form(None, max_length=10),
    requesting_provider_npi: str | None = Form(None, max_length=10),
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

    # Block double-submit: if there's already a non-terminal row, the caller
    # should poll the existing submission instead of creating another.
    latest = storage.get_latest_prior_auth_submission(scope, referral_id)
    if latest is not None and latest.status not in PA_STATUS_TERMINAL:
        return render(
            "_prior_auth_card.html",
            _ctx(
                request,
                current_user,
                storage,
                submission=latest,
                referral=referral,
                patient=patient,
                error="An auth request is already in flight for this referral. "
                "Refresh status or cancel before submitting again.",
            ),
        )

    proc = _split_codes(procedure_codes)
    if not proc:
        return render(
            "_prior_auth_card.html",
            _ctx(
                request,
                current_user,
                storage,
                submission=latest,
                referral=referral,
                patient=patient,
                error="At least one procedure code is required.",
            ),
        )
    diags = _split_codes(diagnosis_codes)
    if not patient.date_of_birth:
        return render(
            "_prior_auth_card.html",
            _ctx(
                request,
                current_user,
                storage,
                submission=latest,
                referral=referral,
                patient=patient,
                error="Patient date of birth is required for prior auth.",
            ),
        )

    npi = (provider_npi or "").strip() or (referral.referring_provider_npi or "") or "0000000000"
    requesting_npi = (requesting_provider_npi or "").strip() or npi

    payload: dict = {
        "payerId": payer_id,
        "providerNpi": npi,
        "requestingProviderNpi": requesting_npi,
        "memberId": member_id,
        "patientBirthDate": patient.date_of_birth,
        "patientLastName": patient.last_name,
        "patientFirstName": patient.first_name,
        "serviceType": service_type,
        "diagnosisCodes": diags,
        "procedureCodes": proc,
        "serviceDate": service_date,
        "placeOfService": place_of_service,
    }

    idem_key = build_idempotency_key(
        referral_id=referral_id, procedure_codes=proc, service_date=service_date
    )

    submission = storage.create_prior_auth_submission(
        scope,
        referral_id=referral_id,
        availity_payer_id=payer_id,
        payer_name=payer_name,
        member_id=member_id,
        service_type=service_type,
        diagnosis_codes=diags,
        procedure_codes=proc,
        service_date=service_date,
        place_of_service=place_of_service,
        status="pending",
        idempotency_key=idem_key,
        raw_request_json=json.dumps(payload),
    )

    now = datetime.now(tz=timezone.utc)
    try:
        client = get_availity_client()
        raw = await client.async_submit_authorization(payload, idempotency_key=idem_key)
        parsed = parse_authorization_response(raw)
        storage.update_prior_auth_submission(
            submission.id,  # type: ignore[arg-type]
            status=parsed["status"],
            availity_submission_id=parsed.get("availity_submission_id"),
            reference_number=parsed.get("reference_number"),
            decision_date=parsed.get("decision_date"),
            decision_reason=parsed.get("decision_reason"),
            raw_response_json=json.dumps(raw),
            submitted_at=now,
        )
        audit_record(
            storage,
            action="auth.submitted",
            request=request,
            actor_user_id=current_user["id"],
            scope_user_id=scope.user_id if scope.is_solo else None,
            scope_organization_id=scope.organization_id,
            entity_type="referral",
            entity_id=str(referral_id),
            metadata={
                "payer_id": payer_id,
                "service_type": service_type,
                "result_status": parsed["status"],
            },
        )
        _emit_pa_event(
            storage,
            scope,
            referral_id=referral_id,
            actor_user_id=current_user["id"],
            event_type="auth_submitted",
            note=f"Prior auth submitted to {payer_name or payer_id} (status: {parsed['status']})",
        )
    except AvailityDisabledError:
        storage.update_prior_auth_submission(
            submission.id,  # type: ignore[arg-type]
            status="error",
            error_message="Prior authorization is not configured on this server.",
            submitted_at=now,
        )
    except AvailityUnavailableError as e:
        storage.update_prior_auth_submission(
            submission.id,  # type: ignore[arg-type]
            status="unavailable",
            error_message=f"Clearinghouse temporarily unavailable: {e}",
            submitted_at=now,
        )
    except AvailityError as e:
        storage.update_prior_auth_submission(
            submission.id,  # type: ignore[arg-type]
            status="error",
            error_message=str(e),
            submitted_at=now,
        )

    updated = storage.get_latest_prior_auth_submission(scope, referral_id)
    return render(
        "_prior_auth_card.html",
        _ctx(
            request,
            current_user,
            storage,
            submission=updated,
            referral=referral,
            patient=patient,
        ),
    )


# ---------------------------------------------------------------------------
# POST /referrals/{id}/auth-status/refresh
# ---------------------------------------------------------------------------


@router.post("/referrals/{referral_id}/auth-status/refresh", response_class=HTMLResponse)
async def refresh_prior_auth_status(
    request: Request,
    referral_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    patient = storage.get_patient(scope, referral.patient_id)

    submission = storage.get_latest_prior_auth_submission(scope, referral_id)
    if submission is None:
        return render(
            "_prior_auth_card.html",
            _ctx(
                request,
                current_user,
                storage,
                submission=None,
                referral=referral,
                patient=patient,
                error="No prior-auth submission found to refresh.",
            ),
        )

    if submission.status in PA_STATUS_TERMINAL:
        return render(
            "_prior_auth_card.html",
            _ctx(
                request,
                current_user,
                storage,
                submission=submission,
                referral=referral,
                patient=patient,
                error=f"Status already terminal ({submission.status}); refresh has no effect.",
            ),
        )

    if submission.availity_submission_id is None:
        return render(
            "_prior_auth_card.html",
            _ctx(
                request,
                current_user,
                storage,
                submission=submission,
                referral=referral,
                patient=patient,
                error="Availity has not yet acknowledged this submission. Try again shortly.",
            ),
        )

    cooldown = _poll_cooldown_seconds()
    if submission.last_polled_at is not None:
        age = (datetime.now(tz=timezone.utc) - submission.last_polled_at).total_seconds()
        if age < cooldown:
            seconds_left = int(cooldown - age)
            return render(
                "_prior_auth_card.html",
                _ctx(
                    request,
                    current_user,
                    storage,
                    submission=submission,
                    referral=referral,
                    patient=patient,
                    error=f"Polled {int(age)}s ago — wait {seconds_left}s before refreshing.",
                    cooldown_active=True,
                ),
            )

    now = datetime.now(tz=timezone.utc)
    try:
        client = get_availity_client()
        raw = await client.async_get_authorization_status(submission.availity_submission_id)
        parsed = parse_authorization_response(raw)
        storage.update_prior_auth_submission(
            submission.id,  # type: ignore[arg-type]
            status=parsed["status"],
            reference_number=parsed.get("reference_number"),
            decision_date=parsed.get("decision_date"),
            decision_reason=parsed.get("decision_reason"),
            raw_response_json=json.dumps(raw),
            last_polled_at=now,
        )
        audit_record(
            storage,
            action="auth.status_polled",
            request=request,
            actor_user_id=current_user["id"],
            scope_user_id=scope.user_id if scope.is_solo else None,
            scope_organization_id=scope.organization_id,
            entity_type="referral",
            entity_id=str(referral_id),
            metadata={"result_status": parsed["status"]},
        )
        # Decision-reached audit + timeline event
        if parsed["status"] in ("approved", "denied"):
            audit_record(
                storage,
                action=f"auth.{parsed['status']}",
                request=request,
                actor_user_id=current_user["id"],
                scope_user_id=scope.user_id if scope.is_solo else None,
                scope_organization_id=scope.organization_id,
                entity_type="referral",
                entity_id=str(referral_id),
                metadata={"reference_number": parsed.get("reference_number")},
            )
            _emit_pa_event(
                storage,
                scope,
                referral_id=referral_id,
                actor_user_id=current_user["id"],
                event_type=f"auth_{parsed['status']}",
                note=(
                    f"Prior auth {parsed['status']}"
                    + (
                        f" — ref {parsed['reference_number']}"
                        if parsed.get("reference_number")
                        else ""
                    )
                    + (f" ({parsed['decision_reason']})" if parsed.get("decision_reason") else "")
                ),
            )
    except AvailityDisabledError:
        storage.update_prior_auth_submission(
            submission.id,  # type: ignore[arg-type]
            status="error",
            error_message="Prior authorization is not configured on this server.",
            last_polled_at=now,
        )
    except AvailityUnavailableError as e:
        storage.update_prior_auth_submission(
            submission.id,  # type: ignore[arg-type]
            error_message=f"Clearinghouse temporarily unavailable: {e}",
            last_polled_at=now,
        )
    except AvailityError as e:
        storage.update_prior_auth_submission(
            submission.id,  # type: ignore[arg-type]
            status="error",
            error_message=str(e),
            last_polled_at=now,
        )

    updated = storage.get_latest_prior_auth_submission(scope, referral_id)
    return render(
        "_prior_auth_card.html",
        _ctx(
            request,
            current_user,
            storage,
            submission=updated,
            referral=referral,
            patient=patient,
        ),
    )


# ---------------------------------------------------------------------------
# GET /referrals/{id}/auth-status  — read-only cached card
# ---------------------------------------------------------------------------


@router.get("/referrals/{referral_id}/auth-status", response_class=HTMLResponse)
async def get_prior_auth_status(
    request: Request,
    referral_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    patient = storage.get_patient(scope, referral.patient_id)
    submission = storage.get_latest_prior_auth_submission(scope, referral_id)
    return render(
        "_prior_auth_card.html",
        _ctx(
            request,
            current_user,
            storage,
            submission=submission,
            referral=referral,
            patient=patient,
        ),
    )
