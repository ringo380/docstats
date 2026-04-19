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

import logging
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
    require_transition,
)
from docstats.domain.rules import (
    detect_red_flags_in_text,
    resolve_specialty_rule,
    rules_based_completeness,
)
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope, render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.validators import validate_npi

logger = logging.getLogger(__name__)

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

    referrals = storage.list_referrals(
        scope,
        patient_id=patient_id,
        status=status_filter,
        urgency=urgency_filter,
        assigned_to_user_id=assigned_to_user_id,
        limit=50,
    )

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


@router.get("/intake-questions", response_class=HTMLResponse)
async def referral_intake_questions(
    request: Request,
    specialty_code: str = Query("", max_length=16),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    """htmx partial: specialty-aware intake prompts for the create wizard.

    Empty `specialty_code` returns an empty fragment (the caller hides the
    panel). Unknown code also returns empty.
    """
    rule = resolve_specialty_rule(storage, scope.organization_id, _clean(specialty_code))
    prompts: list[str] = []
    rejection_hints: list[str] = []
    if rule is not None:
        raw = rule.intake_questions.get("prompts", []) if rule.intake_questions else []
        if isinstance(raw, list):
            prompts = [str(p) for p in raw if isinstance(p, str)]
        raw_reasons = (
            rule.common_rejection_reasons.get("reasons", [])
            if rule.common_rejection_reasons
            else []
        )
        if isinstance(raw_reasons, list):
            rejection_hints = [str(x) for x in raw_reasons if isinstance(x, str)]
    return render(
        "_referral_intake_questions.html",
        _ctx(
            request,
            current_user,
            storage,
            specialty_rule=rule,
            prompts=prompts,
            rejection_hints=rejection_hints,
        ),
    )


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

    # Red-flag auto-escalation: scan reason + clinical_question against the
    # picked specialty's urgency_red_flags keywords. If the user left urgency
    # at "routine" and we hit a flag, bump to "urgent". Higher user-set
    # urgency (priority/stat) is preserved — we never downgrade or override
    # an explicit coordinator judgment.
    final_urgency = urgency
    red_flag_hits: list[str] = []
    specialty_code_clean = _clean(specialty_code)
    if specialty_code_clean:
        rule = resolve_specialty_rule(storage, scope.organization_id, specialty_code_clean)
        red_flag_hits = detect_red_flags_in_text(reason_clean, _clean(clinical_question), rule)
        if red_flag_hits and urgency == "routine":
            final_urgency = "urgent"

    try:
        referral = storage.create_referral(
            scope,
            patient_id=patient_id,
            reason=reason_clean,
            clinical_question=_clean(clinical_question),
            urgency=final_urgency,
            requested_service=_clean(requested_service),
            receiving_provider_npi=recv_npi,
            receiving_organization_name=_clean(receiving_organization_name),
            specialty_desc=_clean(specialty_desc),
            specialty_code=specialty_code_clean,
            referring_provider_name=_clean(referring_provider_name),
            referring_provider_npi=ref_npi,
            referring_organization=_clean(referring_organization),
            created_by_user_id=current_user["id"],
        )
    except ValueError as e:
        # Cross-scope patient_id or unknown enum value.
        return _rerender([str(e)])

    # If we escalated, record a field_edited event so the timeline shows why.
    if final_urgency != urgency:
        try:
            storage.record_referral_event(
                scope,
                referral.id,
                event_type="field_edited",
                from_value=urgency,
                to_value=final_urgency,
                actor_user_id=current_user["id"],
                note=f"urgency (auto-escalated: red flags {', '.join(red_flag_hits)})",
            )
        except Exception:
            logger.exception("Failed to record auto-urgency event for referral %s", referral.id)

    audit_record(
        storage,
        action="referral.create",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral",
        entity_id=str(referral.id),
        metadata={
            "patient_id": patient_id,
            "urgency": final_urgency,
            "urgency_escalated": final_urgency != urgency,
            "red_flags": red_flag_hits,
        },
    )
    dest = f"/referrals/{referral.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


# --- Detail + inline edit (Phase 2.D) ---


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
    completeness = rules_based_completeness(storage, scope, referral)
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
    completeness = rules_based_completeness(storage, scope, referral)
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


# Fields the POST /referrals/{id}/clear/{field} endpoint will set to NULL.
# Matches ``StorageBase.clear_referral_field``'s allow-list — keep in sync.
_CLEARABLE_FIELDS: frozenset[str] = frozenset(
    {"assigned_to_user_id", "authorization_number", "payer_plan_id", "external_reference_id"}
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

    # Route-boundary enum validation — match referral_create's 422 behavior so
    # both surfaces reject bad enum values consistently instead of letting
    # storage's ValueError surface as a generic form error.
    urgency_clean = _clean(urgency)
    if urgency_clean is not None and urgency_clean not in URGENCY_VALUES:
        raise HTTPException(status_code=422, detail="Unknown urgency value.")
    auth_status_clean = _clean(authorization_status)
    if auth_status_clean is not None and auth_status_clean not in AUTH_STATUS_VALUES:
        raise HTTPException(status_code=422, detail="Unknown authorization_status value.")

    # Validate NPI format at the boundary; blank → skip.
    recv_npi = _validate_optional_npi(receiving_provider_npi, "Receiving provider NPI")
    ref_npi = _validate_optional_npi(referring_provider_npi, "Referring provider NPI")

    # Build a "desired" map where only fields the user touched (non-blank,
    # and different from current) are passed. None = storage skips the field.
    desired: dict[str, str | None] = {
        "reason": _clean(reason),
        "clinical_question": _clean(clinical_question),
        "urgency": urgency_clean,
        "requested_service": _clean(requested_service),
        "receiving_provider_npi": recv_npi,
        "receiving_organization_name": _clean(receiving_organization_name),
        "specialty_desc": _clean(specialty_desc),
        "specialty_code": _clean(specialty_code),
        "referring_provider_name": _clean(referring_provider_name),
        "referring_provider_npi": ref_npi,
        "referring_organization": _clean(referring_organization),
        "authorization_number": _clean(authorization_number),
        "authorization_status": auth_status_clean,
    }
    # Only pass fields that changed, so we don't rewrite identical values
    # and can emit one event per actual change. Dict typed Any so **unpacking
    # into update_referral's mixed-type signature (str / int / enum) is OK.
    # We capture old values here (before mutation) so the field_edited event
    # can log from_value=<old>, to_value=<new> — matching the status_changed
    # event's semantics instead of overloading from_value with a field name.
    changed: dict[str, Any] = {}
    old_values: dict[str, Any] = {}
    for k, v in desired.items():
        if v is None:
            continue
        current = getattr(existing, k)
        if current != v:
            changed[k] = v
            old_values[k] = current

    if not changed:
        return _render_detail(request, current_user, storage, scope, referral_id, errors=None)

    try:
        updated = storage.update_referral(scope, referral_id, **changed)
    except ValueError as e:
        return _render_detail(request, current_user, storage, scope, referral_id, errors=[str(e)])
    if updated is None:
        # Row soft-deleted between the read and the write (TOCTOU). Treat as
        # 404 rather than emit events against a vanished referral.
        raise HTTPException(status_code=404, detail="Referral not found.")

    # Emit one field_edited event per changed field so the timeline is
    # fine-grained. ``from_value`` = previous value, ``to_value`` = new value,
    # ``note`` = field name — same shape the timeline template expects, and
    # consistent with the status_changed event's (old, new) semantics.
    #
    # Event inserts can fail independently per row (e.g. DB blip between
    # writes). We catch+log rather than surface a 500 — the update already
    # landed and the audit_events table still captures the fact of the
    # update below, so losing a single field_edited row is graceful
    # degradation, not data loss.
    for k, v in changed.items():
        try:
            storage.record_referral_event(
                scope,
                referral_id,
                event_type="field_edited",
                from_value=None if old_values[k] is None else str(old_values[k]),
                to_value=None if v is None else str(v),
                actor_user_id=current_user["id"],
                note=k,
            )
        except Exception:
            logger.exception(
                "Failed to record field_edited event for referral %s field %s",
                referral_id,
                k,
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
    # TOCTOU guard: the referral could have been soft-deleted between our
    # read above and this write — set_referral_status returns None in that
    # case. Re-raise as 404 rather than emit a status_changed event for a
    # vanished row.
    if storage.set_referral_status(scope, referral_id, new_status) is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    try:
        storage.record_referral_event(
            scope,
            referral_id,
            event_type="status_changed",
            from_value=old_status,
            to_value=new_status,
            actor_user_id=current_user["id"],
        )
    except Exception:
        # Same degradation posture as referral_update — the status write
        # landed and the audit_events row below captures the fact of the
        # transition; losing the referral_events row is non-fatal.
        logger.exception(
            "Failed to record status_changed event for referral %s (%s → %s)",
            referral_id,
            old_status,
            new_status,
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


@router.post("/{referral_id}/clear/{field}", response_class=HTMLResponse)
async def referral_clear_field(
    request: Request,
    referral_id: int = Path(..., ge=1),
    field: str = Path(..., max_length=64),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Null-out one of the four clearable nullable referral fields.

    Companion to the POST /referrals/{id} update route's "None means skip"
    semantics — the update form can't distinguish "leave alone" from "set to
    NULL" for a blank input, so explicit clearing lives here. The allow-list
    matches ``StorageBase.clear_referral_field``; unknown fields return 422.
    """
    if field not in _CLEARABLE_FIELDS:
        raise HTTPException(status_code=422, detail=f"Field {field!r} is not clearable.")
    existing = storage.get_referral(scope, referral_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    old_value = getattr(existing, field)
    try:
        updated = storage.clear_referral_field(scope, referral_id, field)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    try:
        storage.record_referral_event(
            scope,
            referral_id,
            event_type="field_edited",
            from_value=None if old_value is None else str(old_value),
            to_value=None,
            actor_user_id=current_user["id"],
            note=field,
        )
    except Exception:
        logger.exception(
            "Failed to record field_edited (clear) event for referral %s field %s",
            referral_id,
            field,
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
        metadata={"cleared": field},
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
