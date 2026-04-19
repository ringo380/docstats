"""Referrals routes — workspace (2.B), create (2.C), detail + edit (2.D).

Wizard ships as a single dense form (the plan's "quick-mode" collapse of the
8-step wizard). The stepped wizard is a progressive-enhancement pass.

Detail page is an editable form with a side rail for completeness + the event
timeline. Updates emit one ``field_edited`` ReferralEvent per changed field;
status transitions run through the state-machine in ``domain.referrals`` and
emit ``status_changed`` events. Clinical sub-entity edits (diagnoses,
medications, allergies, attachments) are still a follow-up slice.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, Response

from docstats.domain.audit import record as audit_record
from docstats.domain.referrals import (
    AUTH_STATUS_VALUES,
    STATUS_TRANSITIONS,
    STATUS_VALUES,
    URGENCY_VALUES,
    InvalidTransition,
    baseline_completeness,
    require_transition,
)
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope, render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.validators import validate_npi

router = APIRouter(prefix="/referrals", tags=["referrals"])


def _ctx(request: Request, user: dict, storage: StorageBase, **extra) -> dict:
    return {
        "request": request,
        "active_page": "referrals",
        "user": user,
        "saved_count": saved_count(storage, user["id"]),
        **extra,
    }


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


def _validate_optional_npi(value: str | None, field: str) -> str | None:
    """Accept blank; reject malformed 10-digit pattern."""
    v = _clean(value)
    if v is None:
        return None
    try:
        return validate_npi(v)
    except Exception:
        raise HTTPException(status_code=422, detail=f"{field} must be 10 digits.")


# --- Workspace list (Phase 2.B) ---


@router.get("", response_class=HTMLResponse)
async def referrals_workspace(
    request: Request,
    status: str | None = Query(None, max_length=32),
    urgency: str | None = Query(None, max_length=16),
    patient_id: int | None = Query(None, ge=1),
    assigned_to_user_id: int | None = Query(None, ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    status_filter = status if status in STATUS_VALUES else None
    urgency_filter = urgency if urgency in URGENCY_VALUES else None

    window = 200 if urgency_filter else 50
    referrals = storage.list_referrals(
        scope,
        patient_id=patient_id,
        status=status_filter,
        assigned_to_user_id=assigned_to_user_id,
        limit=window,
    )
    if urgency_filter:
        referrals = [r for r in referrals if r.urgency == urgency_filter]
        referrals = referrals[:50]

    patient_ids = {r.patient_id for r in referrals}
    patients_by_id = {
        pid: p for pid in patient_ids if (p := storage.get_patient(scope, pid)) is not None
    }

    return render(
        "referrals_workspace.html",
        _ctx(
            request,
            current_user,
            storage,
            referrals=referrals,
            patients_by_id=patients_by_id,
            status_values=STATUS_VALUES,
            urgency_values=URGENCY_VALUES,
            filters={
                "status": status_filter or "",
                "urgency": urgency_filter or "",
                "patient_id": patient_id or "",
                "assigned_to_user_id": assigned_to_user_id or "",
            },
        ),
    )


# --- Create (Phase 2.C) ---


@router.get("/new", response_class=HTMLResponse)
async def referral_new_form(
    request: Request,
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    patients = storage.list_patients(scope, limit=200)
    return render(
        "referral_new.html",
        _ctx(
            request,
            current_user,
            storage,
            patients=patients,
            urgency_values=URGENCY_VALUES,
            values={},
            errors=None,
        ),
    )


@router.post("", response_class=HTMLResponse)
async def referral_create(
    request: Request,
    patient_id: int = Form(..., ge=1),
    reason: str = Form(..., max_length=500),
    clinical_question: str | None = Form(None, max_length=2000),
    urgency: str = Form("routine", max_length=16),
    requested_service: str | None = Form(None, max_length=200),
    receiving_provider_npi: str | None = Form(None, max_length=10),
    receiving_organization_name: str | None = Form(None, max_length=200),
    specialty_desc: str | None = Form(None, max_length=200),
    specialty_code: str | None = Form(None, max_length=16),
    referring_provider_name: str | None = Form(None, max_length=200),
    referring_provider_npi: str | None = Form(None, max_length=10),
    referring_organization: str | None = Form(None, max_length=200),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    if urgency not in URGENCY_VALUES:
        raise HTTPException(status_code=422, detail="Unknown urgency value.")

    def _rerender(errors: list[str]) -> Response:
        patients = storage.list_patients(scope, limit=200)
        return render(
            "referral_new.html",
            _ctx(
                request,
                current_user,
                storage,
                patients=patients,
                urgency_values=URGENCY_VALUES,
                values={
                    "patient_id": str(patient_id) if patient_id else "",
                    "reason": reason,
                    "clinical_question": clinical_question,
                    "urgency": urgency,
                    "requested_service": requested_service,
                    "receiving_provider_npi": receiving_provider_npi,
                    "receiving_organization_name": receiving_organization_name,
                    "specialty_desc": specialty_desc,
                    "specialty_code": specialty_code,
                    "referring_provider_name": referring_provider_name,
                    "referring_provider_npi": referring_provider_npi,
                    "referring_organization": referring_organization,
                },
                errors=errors,
            ),
        )

    reason_clean = _clean(reason) or ""
    if not reason_clean:
        return _rerender(["Reason for referral is required."])

    # NPI format checks at the boundary; blank passes through.
    recv_npi = _validate_optional_npi(receiving_provider_npi, "Receiving provider NPI")
    ref_npi = _validate_optional_npi(referring_provider_npi, "Referring provider NPI")

    try:
        referral = storage.create_referral(
            scope,
            patient_id=patient_id,
            reason=reason_clean,
            clinical_question=_clean(clinical_question),
            urgency=urgency,
            requested_service=_clean(requested_service),
            receiving_provider_npi=recv_npi,
            receiving_organization_name=_clean(receiving_organization_name),
            specialty_desc=_clean(specialty_desc),
            specialty_code=_clean(specialty_code),
            referring_provider_name=_clean(referring_provider_name),
            referring_provider_npi=ref_npi,
            referring_organization=_clean(referring_organization),
            created_by_user_id=current_user["id"],
        )
    except ValueError as e:
        # Cross-scope patient_id or unknown enum value.
        return _rerender([str(e)])

    audit_record(
        storage,
        action="referral.create",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral",
        entity_id=str(referral.id),
        metadata={"patient_id": patient_id, "urgency": urgency},
    )
    dest = f"/referrals/{referral.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


# --- Detail (basic; inline-edit lives in Phase 2.D) ---


def _allowed_next_statuses(current: str) -> list[str]:
    """Return status values reachable from ``current`` via the state machine.

    Sorted so the UI is deterministic. ``STATUS_TRANSITIONS`` is a frozenset
    mapping so we convert before sorting.
    """
    return sorted(STATUS_TRANSITIONS.get(current, frozenset()))


def _render_detail(
    request: Request,
    current_user: dict,
    storage: StorageBase,
    scope: Scope,
    referral_id: int,
    *,
    errors: list[str] | None = None,
) -> Response:
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    patient = storage.get_patient(scope, referral.patient_id)
    events = storage.list_referral_events(scope, referral_id, limit=50)
    completeness = baseline_completeness(referral)
    return render(
        "referral_detail.html",
        _ctx(
            request,
            current_user,
            storage,
            referral=referral,
            patient=patient,
            events=events,
            completeness=completeness,
            urgency_values=URGENCY_VALUES,
            auth_status_values=AUTH_STATUS_VALUES,
            allowed_next=_allowed_next_statuses(referral.status),
            errors=errors,
        ),
    )


@router.get("/{referral_id}", response_class=HTMLResponse)
async def referral_detail(
    request: Request,
    referral_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    return _render_detail(request, current_user, storage, scope, referral_id)


@router.get("/{referral_id}/completeness", response_class=HTMLResponse)
async def referral_completeness(
    request: Request,
    referral_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    completeness = baseline_completeness(referral)
    return render(
        "_referral_completeness.html",
        _ctx(
            request,
            current_user,
            storage,
            referral=referral,
            completeness=completeness,
        ),
    )


# Fields writable via the detail-page update form. Editing the primary
# diagnosis denormalized columns directly would break the "referral_diagnoses
# sub-table is source of truth" invariant, so the form exposes them read-only
# here — diagnosis edits will land with the Phase 2.D follow-up that surfaces
# the sub-entity CRUD.
_EDITABLE_FIELDS: tuple[str, ...] = (
    "reason",
    "clinical_question",
    "urgency",
    "requested_service",
    "receiving_provider_npi",
    "receiving_organization_name",
    "specialty_desc",
    "specialty_code",
    "referring_provider_name",
    "referring_provider_npi",
    "referring_organization",
    "authorization_number",
    "authorization_status",
)


@router.post("/{referral_id}", response_class=HTMLResponse)
async def referral_update(
    request: Request,
    referral_id: int = Path(..., ge=1),
    reason: str | None = Form(None, max_length=500),
    clinical_question: str | None = Form(None, max_length=2000),
    urgency: str | None = Form(None, max_length=16),
    requested_service: str | None = Form(None, max_length=200),
    receiving_provider_npi: str | None = Form(None, max_length=10),
    receiving_organization_name: str | None = Form(None, max_length=200),
    specialty_desc: str | None = Form(None, max_length=200),
    specialty_code: str | None = Form(None, max_length=16),
    referring_provider_name: str | None = Form(None, max_length=200),
    referring_provider_npi: str | None = Form(None, max_length=10),
    referring_organization: str | None = Form(None, max_length=200),
    authorization_number: str | None = Form(None, max_length=64),
    authorization_status: str | None = Form(None, max_length=32),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    existing = storage.get_referral(scope, referral_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    # Validate NPI format at the boundary; blank → skip.
    recv_npi = _validate_optional_npi(receiving_provider_npi, "Receiving provider NPI")
    ref_npi = _validate_optional_npi(referring_provider_npi, "Referring provider NPI")

    # Build a "desired" map where only fields the user touched (non-blank,
    # and different from current) are passed. None = storage skips the field.
    desired: dict[str, str | None] = {
        "reason": _clean(reason),
        "clinical_question": _clean(clinical_question),
        "urgency": _clean(urgency),
        "requested_service": _clean(requested_service),
        "receiving_provider_npi": recv_npi,
        "receiving_organization_name": _clean(receiving_organization_name),
        "specialty_desc": _clean(specialty_desc),
        "specialty_code": _clean(specialty_code),
        "referring_provider_name": _clean(referring_provider_name),
        "referring_provider_npi": ref_npi,
        "referring_organization": _clean(referring_organization),
        "authorization_number": _clean(authorization_number),
        "authorization_status": _clean(authorization_status),
    }
    # Only pass fields that changed, so we don't rewrite identical values
    # and can emit one event per actual change. Dict typed Any so **unpacking
    # into update_referral's mixed-type signature (str / int / enum) is OK.
    changed: dict[str, Any] = {}
    for k, v in desired.items():
        if v is None:
            continue
        if getattr(existing, k) != v:
            changed[k] = v

    if not changed:
        return _render_detail(request, current_user, storage, scope, referral_id, errors=None)

    try:
        storage.update_referral(scope, referral_id, **changed)
    except ValueError as e:
        return _render_detail(request, current_user, storage, scope, referral_id, errors=[str(e)])

    # Emit one field_edited event per changed field so the timeline is
    # fine-grained. ``from_value`` / ``to_value`` carry field name + new value;
    # the event log isn't a full diff store, just enough to show "coordinator X
    # updated Reason at 10:42am".
    for k, v in changed.items():
        storage.record_referral_event(
            scope,
            referral_id,
            event_type="field_edited",
            from_value=k,
            to_value=v,
            actor_user_id=current_user["id"],
        )
    audit_record(
        storage,
        action="referral.update",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral",
        entity_id=str(referral_id),
        metadata={"fields": sorted(changed.keys())},
    )
    dest = f"/referrals/{referral_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


@router.post("/{referral_id}/status", response_class=HTMLResponse)
async def referral_set_status(
    request: Request,
    referral_id: int = Path(..., ge=1),
    new_status: str = Form(..., max_length=32),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    existing = storage.get_referral(scope, referral_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    if new_status not in STATUS_VALUES:
        raise HTTPException(status_code=422, detail="Unknown status value.")
    try:
        require_transition(existing.status, new_status)
    except InvalidTransition as e:
        return _render_detail(request, current_user, storage, scope, referral_id, errors=[str(e)])
    old_status = existing.status
    storage.set_referral_status(scope, referral_id, new_status)
    storage.record_referral_event(
        scope,
        referral_id,
        event_type="status_changed",
        from_value=old_status,
        to_value=new_status,
        actor_user_id=current_user["id"],
    )
    audit_record(
        storage,
        action="referral.status",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral",
        entity_id=str(referral_id),
        metadata={"from": old_status, "to": new_status},
    )
    dest = f"/referrals/{referral_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


@router.delete("/{referral_id}")
async def referral_delete(
    request: Request,
    referral_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    existing = storage.get_referral(scope, referral_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    if not storage.soft_delete_referral(scope, referral_id):
        raise HTTPException(status_code=404, detail="Referral not found.")
    audit_record(
        storage,
        action="referral.delete",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral",
        entity_id=str(referral_id),
    )
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/referrals"})
    return Response(status_code=303, headers={"Location": "/referrals"})
