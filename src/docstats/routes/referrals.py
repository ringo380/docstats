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

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, Response

from docstats.domain.audit import record as audit_record
from docstats.domain.referrals import (
    ATTACHMENT_KIND_VALUES,
    AUTH_STATUS_VALUES,
    RECEIVED_VIA_VALUES,
    STATUS_TRANSITIONS,
    STATUS_VALUES,
    URGENCY_VALUES,
    InvalidTransition,
    TransitionRoleDenied,
    require_transition_for_role,
    role_can_transition_status,
    transition_allowed_for_role,
)
from docstats.domain.orgs import DEFAULT_STALE_THRESHOLD_DAYS
from docstats.enrichment import fetch_receiving_direct_endpoints
from docstats.domain.eligibility import overlay_eligibility
from docstats.domain.rules import (
    detect_red_flags_in_text,
    resolve_specialty_rule,
    rules_based_completeness,
)
from docstats.phi import require_phi_consent
from docstats.routes._common import (
    assigned_open_count,
    get_client,
    get_scope,
    render,
    resolve_assignee_filter,
    saved_count,
)
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.validators import validate_npi

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/referrals", tags=["referrals"])

STALE_REFERRAL_STATUSES = ("awaiting_records", "awaiting_auth")


def _ctx(
    request: Request,
    user: dict,
    storage: StorageBase,
    scope: Scope | None = None,
    **extra,
) -> dict:
    uid = user["id"]
    return {
        "request": request,
        "active_page": "referrals",
        "user": user,
        "saved_count": saved_count(storage, uid),
        "assigned_open_count": assigned_open_count(storage, scope, uid) if scope else 0,
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


def _stale_threshold_days(storage: StorageBase, scope: Scope) -> int:
    if scope.is_org and scope.organization_id is not None:
        org = storage.get_organization(scope.organization_id)
        if org is not None:
            return org.stale_threshold_days
    return DEFAULT_STALE_THRESHOLD_DAYS


# --- Workspace list (Phase 2.B) ---


@router.get("", response_class=HTMLResponse)
async def referrals_workspace(
    request: Request,
    status: str | None = Query(None, max_length=32),
    urgency: str | None = Query(None, max_length=16),
    patient_id: int | None = Query(None, ge=1),
    assigned_to_user_id: int | None = Query(None, ge=1),
    # Phase 7.C: shorthand alias for assigned_to_user_id. ``me`` resolves
    # to the caller; a numeric string resolves to that user id (same as
    # ``assigned_to_user_id``, but accessible from a bookmarkable URL
    # without the caller needing to know their own id).
    assignee: str | None = Query(None, max_length=16),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    status_filter = status if status in STATUS_VALUES else None
    urgency_filter = urgency if urgency in URGENCY_VALUES else None

    # Resolve assignee shorthand. ``assignee`` takes precedence over the
    # legacy numeric param when both are supplied.
    effective_assigned, assignee_clean = resolve_assignee_filter(
        assignee,
        assigned_to_user_id,
        current_user["id"],
    )

    referrals = storage.list_referrals(
        scope,
        patient_id=patient_id,
        status=status_filter,
        urgency=urgency_filter,
        assigned_to_user_id=effective_assigned,
        limit=50,
    )
    stale_threshold_days = _stale_threshold_days(storage, scope)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_threshold_days)
    stale_referral_count = storage.count_referrals(
        scope,
        statuses=STALE_REFERRAL_STATUSES,
        updated_before=stale_cutoff,
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
            scope,
            referrals=referrals,
            patients_by_id=patients_by_id,
            status_values=STATUS_VALUES,
            urgency_values=URGENCY_VALUES,
            stale_referral_count=stale_referral_count,
            stale_threshold_days=stale_threshold_days,
            filters={
                "status": status_filter or "",
                "urgency": urgency_filter or "",
                "patient_id": patient_id or "",
                "assigned_to_user_id": effective_assigned or "",
                "assignee": assignee_clean or "",
                "assignee_is_me": assignee_clean == "me",
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
            scope,
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
            scope,
            patients=patients,
            urgency_values=URGENCY_VALUES,
            values={},
            errors=None,
        ),
    )


async def _ehr_post_create_hook(
    *,
    referral: Any,
    patient_id: int,
    user_id: int,
    scope: Scope,
    storage: StorageBase,
    request: Request,
) -> None:
    """Fetch clinical resources from Epic + write ServiceRequest after referral creation.

    Entirely soft-fail — any exception is logged and swallowed so it can
    never break referral creation. Only runs when:
    - The patient has an ehr_fhir_id (set during EHR Patient import)
    - The user has an active EHR connection
    """
    import base64 as _base64
    import os as _os

    from docstats.domain.audit import record as _audit
    from docstats.ehr import epic as _epic_mod, cerner as _cerner_mod  # noqa: F401 — registers vendors
    from docstats.ehr import registry as _reg
    from docstats.ehr.registry import EHRError as _EHRError
    from docstats.ehr.mappers import (
        parse_fhir_conditions as _conditions,
        parse_fhir_medications as _medications,
        parse_fhir_allergies as _allergies,
        parse_fhir_document_references as _doc_refs,
    )
    from docstats.routes.ehr import _maybe_refresh
    from docstats.storage_files import (
        ALLOWED_MIME_TYPES,
        MAX_UPLOAD_BYTES,
        build_object_path,
        get_file_backend,
        sniff_mime,
    )

    def _upload_enabled() -> bool:
        return _os.environ.get("ATTACHMENT_UPLOAD_ENABLED", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )

    def _kind_for_mime(mime: str) -> str:
        if mime.startswith("image/"):
            return "imaging"
        return "note"

    try:
        patient = storage.get_patient(scope, patient_id)
        if patient is None or not patient.ehr_fhir_id:
            return

        # Pick the most recently created active connection that has a
        # patient_fhir_id. Iterating `_reg.list_vendors()` and stopping at
        # the first match picks an arbitrary vendor in multi-vendor accounts,
        # which can route Patient PHI to the wrong EHR.
        conn = next(
            (c for c in storage.list_active_ehr_connections(user_id) if c.patient_fhir_id),
            None,
        )
        if conn is None:
            return

        _vendor = _reg.get(conn.ehr_vendor)
        ehr_vendor: str = conn.ehr_vendor

        loop = asyncio.get_running_loop()
        access_token: str = await loop.run_in_executor(None, lambda: _maybe_refresh(conn, storage))
        if not access_token:
            return

        fhir_id: str = patient.ehr_fhir_id
        # Use the iss stored on the connection so EHR-launch flows hit the
        # correct FHIR base, not the env-configured sandbox default.
        conn_iss: str | None = conn.iss or None

        # Resolve the FHIR base once — needed for relative document content URLs.
        endpoints = await loop.run_in_executor(
            None, lambda: _vendor.discover(base_url_override=conn_iss)
        )

        async def _fetch(fn, **kwargs):
            try:
                return await loop.run_in_executor(None, lambda: fn(**kwargs))
            except _EHRError:
                logger.exception(
                    "EHR clinical fetch failed (%s) for referral %s", fn.__name__, referral.id
                )
                return []

        conds, meds, allergies, docs = await asyncio.gather(
            _fetch(
                _vendor.fetch_conditions,
                access_token=access_token,
                patient_fhir_id=fhir_id,
                iss_override=conn_iss,
            ),
            _fetch(
                _vendor.fetch_medications,
                access_token=access_token,
                patient_fhir_id=fhir_id,
                iss_override=conn_iss,
            ),
            _fetch(
                _vendor.fetch_allergies,
                access_token=access_token,
                patient_fhir_id=fhir_id,
                iss_override=conn_iss,
            ),
            _fetch(
                _vendor.fetch_document_references,
                access_token=access_token,
                patient_fhir_id=fhir_id,
                iss_override=conn_iss,
            ),
        )

        diag_count = med_count = allergy_count = doc_count = 0

        for entry in _conditions(conds):
            try:
                icd = entry.get("icd10_code") or ""
                desc = entry.get("icd10_desc") or ""
                if not icd and not desc:
                    continue
                storage.add_referral_diagnosis(
                    scope,
                    referral.id,
                    icd10_code=icd or "UNKNOWN",
                    icd10_desc=desc,
                    is_primary=entry.get("is_primary", False),
                    source="ehr_import",
                )
                diag_count += 1
            except Exception:
                logger.exception("Failed to insert EHR diagnosis for referral %s", referral.id)

        for entry in _medications(meds):
            try:
                storage.add_referral_medication(
                    scope,
                    referral.id,
                    name=entry["name"],
                    dose=entry.get("dose"),
                    route=entry.get("route"),
                    frequency=entry.get("frequency"),
                    source="ehr_import",
                )
                med_count += 1
            except Exception:
                logger.exception("Failed to insert EHR medication for referral %s", referral.id)

        for entry in _allergies(allergies):
            try:
                storage.add_referral_allergy(
                    scope,
                    referral.id,
                    substance=entry["substance"],
                    reaction=entry.get("reaction"),
                    severity=entry.get("severity"),
                    source="ehr_import",
                )
                allergy_count += 1
            except Exception:
                logger.exception("Failed to insert EHR allergy for referral %s", referral.id)

        for entry in _doc_refs(docs):
            inserted = False
            if _upload_enabled():
                try:
                    content_bytes: bytes | None = None
                    if entry.get("inline_data"):
                        content_bytes = _base64.b64decode(entry["inline_data"])
                    elif entry.get("content_url"):
                        content_url = entry["content_url"]

                        def _fetch_content(url: str) -> tuple[bytes, str]:
                            result = _vendor.fetch_document_content(
                                url,
                                access_token=access_token,
                                fhir_base=endpoints.fhir_base,
                            )
                            return (bytes(result[0]), str(result[1]))

                        content_bytes, _claimed_mime = await loop.run_in_executor(
                            None, lambda: _fetch_content(content_url)
                        )

                    if content_bytes is not None:
                        if len(content_bytes) > MAX_UPLOAD_BYTES:
                            raise ValueError("EHR document exceeds 50 MB size limit")
                        actual_mime = sniff_mime(content_bytes)
                        if actual_mime not in ALLOWED_MIME_TYPES:
                            raise ValueError(f"EHR document MIME {actual_mime!r} not in allow-list")
                        # No virus scanning — EHR is a trusted clinical system.
                        file_backend = get_file_backend()
                        placeholder = storage.add_referral_attachment(
                            scope,
                            referral.id,
                            kind=_kind_for_mime(actual_mime),
                            label=entry.get("label", "Imported document"),
                            date_of_service=entry.get("date_of_service"),
                            checklist_only=True,
                            storage_ref=None,
                            source="ehr_import",
                        )
                        if placeholder is not None:
                            obj_path = build_object_path(
                                scope=scope,
                                referral_id=referral.id,
                                attachment_id=placeholder.id,
                                mime_type=actual_mime,
                            )
                            file_ref = await file_backend.put(
                                path=obj_path, data=content_bytes, mime_type=actual_mime
                            )
                            storage.update_referral_attachment(
                                scope,
                                referral.id,
                                placeholder.id,
                                storage_ref=file_ref.storage_ref,
                                checklist_only=False,
                            )
                            doc_count += 1
                            inserted = True
                except Exception:
                    logger.exception(
                        "EHR doc content download failed for referral %s; "
                        "falling back to checklist-only",
                        referral.id,
                    )

            if not inserted:
                try:
                    storage.add_referral_attachment(
                        scope,
                        referral.id,
                        kind="note",
                        label=entry.get("label", "Imported document"),
                        date_of_service=entry.get("date_of_service"),
                        storage_ref=None,
                        checklist_only=True,
                        source="ehr_import",
                    )
                    doc_count += 1
                except Exception:
                    logger.exception(
                        "Failed to insert EHR document ref for referral %s", referral.id
                    )

        _audit(
            storage,
            action="ehr.clinical_import",
            request=request,
            actor_user_id=user_id,
            entity_type="referral",
            entity_id=str(referral.id),
            metadata={
                "ehr_vendor": ehr_vendor,
                "fhir_patient_id": fhir_id,
                "diagnoses": diag_count,
                "medications": med_count,
                "allergies": allergy_count,
                "documents": doc_count,
            },
        )

        # ServiceRequest write-back
        try:
            sr_id = await loop.run_in_executor(
                None,
                lambda: _vendor.write_service_request(
                    access_token=access_token,
                    patient_fhir_id=fhir_id,
                    referral_id=referral.id,
                    specialty_desc=getattr(referral, "specialty_desc", None),
                    reason=getattr(referral, "reason", None),
                    requesting_provider_name=getattr(referral, "referring_provider_name", None),
                    iss_override=conn_iss,
                ),
            )
            storage.update_referral_ehr_service_request_id(referral.id, sr_id)
            _audit(
                storage,
                action="ehr.service_request_written",
                request=request,
                actor_user_id=user_id,
                entity_type="referral",
                entity_id=str(referral.id),
                metadata={"ehr_vendor": ehr_vendor, "service_request_id": sr_id},
            )
        except _EHRError as sr_err:
            logger.exception("EHR ServiceRequest write failed for referral %s", referral.id)
            _audit(
                storage,
                action="ehr.service_request_write_failed",
                request=request,
                actor_user_id=user_id,
                entity_type="referral",
                entity_id=str(referral.id),
                metadata={"ehr_vendor": ehr_vendor, "reason": str(sr_err)},
            )

    except Exception:
        logger.exception("EHR post-create hook failed for referral %s", referral.id)


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
                scope,
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

    # EHR clinical import + ServiceRequest write-back (soft-fail — never
    # break referral creation on EHR errors).
    await _ehr_post_create_hook(
        referral=referral,
        patient_id=patient_id,
        user_id=current_user["id"],
        scope=scope,
        storage=storage,
        request=request,
    )

    dest = f"/referrals/{referral.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


# --- Detail + inline edit (Phase 2.D) ---


def _allowed_next_statuses(current: str, scope: Scope) -> list[str]:
    """Return status values reachable from ``current`` via the state machine.

    Sorted so the UI is deterministic. ``STATUS_TRANSITIONS`` is a frozenset
    mapping so we convert before sorting.
    """
    return sorted(
        status
        for status in STATUS_TRANSITIONS.get(current, frozenset())
        if transition_allowed_for_role(
            current,
            status,
            scope.membership_role,
            is_org=scope.is_org,
        )
    )


def _status_transition_locked_reason(scope: Scope) -> str | None:
    if role_can_transition_status(scope.membership_role, is_org=scope.is_org):
        return None
    return "Your role can view referrals but cannot change status."


def _format_actor(user_row: dict | None) -> str:
    """Mirror the nav-bar display-name formula from base.html.

    Prefer ``first_name last_name`` when both are set; fall back to
    ``display_name``; then the bare email. Returns ``"—"`` when the row
    is None (actor hard-deleted — audit FK is SET NULL on user delete).
    """
    if user_row is None:
        return "—"
    first = (user_row.get("first_name") or "").strip()
    last = (user_row.get("last_name") or "").strip()
    if first and last:
        return f"{first} {last}"
    display = (user_row.get("display_name") or "").strip()
    if display:
        return display
    email = (user_row.get("email") or "").strip()
    return email or "—"


def _build_actor_map(storage: StorageBase, events: list) -> dict[int, str]:
    """Fetch display names for every distinct ``actor_user_id`` on the
    event list. Per-request cache: ~50 events × small actor cardinality
    means this is at most a handful of ``get_user_by_id`` calls.
    """
    ids: set[int] = {e.actor_user_id for e in events if e.actor_user_id is not None}
    return {uid: _format_actor(storage.get_user_by_id(uid)) for uid in ids}


async def _render_detail(
    request: Request,
    current_user: dict,
    storage: StorageBase,
    scope: Scope,
    referral_id: int,
    *,
    errors: list[str] | None = None,
    response_errors: list[str] | None = None,
    response_values: dict | None = None,
    note_error: str | None = None,
    note_value: str | None = None,
) -> Response:
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    patient = storage.get_patient(scope, referral.patient_id)
    events = storage.list_referral_events(scope, referral_id, limit=50)
    responses = storage.list_referral_responses(scope, referral_id)
    completeness = rules_based_completeness(storage, scope, referral)
    # Phase 11.C: overlay live eligibility data into the completeness report when available.
    latest_eligibility = storage.get_latest_eligibility_check(scope, referral.patient_id)
    if latest_eligibility:
        completeness = overlay_eligibility(completeness, latest_eligibility)
    actors_by_id = _build_actor_map(storage, events)
    # Phase 8.C: surface Direct Trust endpoints from NPPES when a receiving
    # NPI is set. Best-effort — NPPES failures degrade to an empty list so
    # the detail page still renders.
    direct_endpoints = await fetch_receiving_direct_endpoints(
        referral.receiving_provider_npi, get_client()
    )
    # Phase 9.A: delivery log + Send-card channel options.
    from docstats.delivery.registry import enabled_channels as _enabled_channels

    deliveries = storage.list_deliveries_for_referral(scope, referral_id)
    delivery_enabled_channels = _enabled_channels()

    # Phase 10.A: attachments list + upload feature flag.
    import os as _os

    attachments = storage.list_referral_attachments(scope, referral_id)
    attachment_uploads_enabled = _os.environ.get(
        "ATTACHMENT_UPLOAD_ENABLED", ""
    ).strip().lower() in ("1", "true", "yes")
    # Assignable users for the Assign dropdown (Phase 7.C). Solo scope → just
    # self; org scope → every live member. Include the currently-assigned
    # user even if they've since left the org so the dropdown still shows
    # them (displayed with an "(off-team)" suffix in the template).
    assignable: list[tuple[int, str]] = []
    if scope.is_org and scope.organization_id is not None:
        for m in storage.list_memberships_for_org(scope.organization_id):
            if m.deleted_at is None:
                member_user = storage.get_user_by_id(m.user_id)
                assignable.append((m.user_id, _format_actor(member_user)))
        assignable.sort(key=lambda pair: pair[1].lower())
    else:
        assignable.append((current_user["id"], _format_actor(current_user)))

    assigned_display = (
        _format_actor(storage.get_user_by_id(referral.assigned_to_user_id))
        if referral.assigned_to_user_id is not None
        else None
    )
    if referral.assigned_to_user_id is not None and referral.assigned_to_user_id not in {
        uid for uid, _ in assignable
    }:
        assignable.append((referral.assigned_to_user_id, f"{assigned_display} (off-team)"))

    return render(
        "referral_detail.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            referral=referral,
            patient=patient,
            events=events,
            actors_by_id=actors_by_id,
            responses=responses,
            completeness=completeness,
            urgency_values=URGENCY_VALUES,
            auth_status_values=AUTH_STATUS_VALUES,
            received_via_values=RECEIVED_VIA_VALUES,
            allowed_next=_allowed_next_statuses(referral.status, scope),
            status_transition_locked_reason=_status_transition_locked_reason(scope),
            assignable_users=assignable,
            assigned_display=assigned_display,
            direct_endpoints=direct_endpoints,
            deliveries=deliveries,
            delivery_enabled_channels=delivery_enabled_channels,
            attachments=attachments,
            attachment_uploads_enabled=attachment_uploads_enabled,
            attachment_kinds=ATTACHMENT_KIND_VALUES,
            check=latest_eligibility,
            errors=errors,
            response_errors=response_errors,
            response_values=response_values or {},
            note_error=note_error,
            note_value=note_value or "",
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
    return await _render_detail(request, current_user, storage, scope, referral_id)


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
            scope,
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
        return await _render_detail(request, current_user, storage, scope, referral_id, errors=None)

    try:
        updated = storage.update_referral(scope, referral_id, **changed)
    except ValueError as e:
        return await _render_detail(
            request, current_user, storage, scope, referral_id, errors=[str(e)]
        )
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
        require_transition_for_role(
            existing.status,
            new_status,
            scope.membership_role,
            is_org=scope.is_org,
        )
    except TransitionRoleDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except InvalidTransition as e:
        return await _render_detail(
            request, current_user, storage, scope, referral_id, errors=[str(e)]
        )
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


def _assignable_user_ids(storage: StorageBase, scope: Scope, current_user_id: int) -> set[int]:
    """User IDs the current scope can legitimately assign a referral to.

    * **Solo mode**: ``{current_user_id}`` — nobody else exists in this scope.
    * **Org mode**: every live member of the active org. Soft-deleted
      memberships are excluded.

    Used by :func:`referral_assign` to gate the write so a cross-scope
    ``user_id`` can't be forged through the form. The storage layer's own
    ``assigned_to_user_id`` column has no FK to scope (it's a plain
    ``users.id`` FK), so this is the guard.
    """
    if scope.is_solo:
        return {current_user_id}
    if scope.is_org and scope.organization_id is not None:
        return {
            m.user_id
            for m in storage.list_memberships_for_org(scope.organization_id)
            if m.deleted_at is None
        }
    return set()


@router.post("/{referral_id}/assign", response_class=HTMLResponse)
async def referral_assign(
    request: Request,
    referral_id: int = Path(..., ge=1),
    # Blank / missing / "unassign" = clear. "me" or numeric = assign.
    user_id: str | None = Form(None, max_length=16),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Assign a referral to a user, or clear the assignment.

    Emits an ``assigned`` ReferralEvent on assign (with the target user id
    in ``to_value``) or ``unassigned`` on clear. Audit action
    ``referral.assign`` / ``referral.unassign``.
    """
    existing = storage.get_referral(scope, referral_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    raw = _clean(user_id) or ""
    # Shorthand: "me" always resolves to the caller.
    target: int | None
    if raw in ("", "unassign"):
        target = None
    elif raw == "me":
        target = current_user["id"]
    else:
        try:
            target = int(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail="user_id must be an integer or 'me'.")
        if target < 1:
            raise HTTPException(status_code=422, detail="user_id must be positive.")

    # No-op? Return 303 without touching the row or emitting an event.
    if target == existing.assigned_to_user_id:
        dest = f"/referrals/{referral_id}"
        if request.headers.get("HX-Request"):
            return Response(status_code=200, headers={"HX-Redirect": dest})
        return Response(status_code=303, headers={"Location": dest})

    if target is not None:
        allowed = _assignable_user_ids(storage, scope, current_user["id"])
        if target not in allowed:
            raise HTTPException(
                status_code=422,
                detail="Target user is not assignable from this scope.",
            )
        updated = storage.update_referral(scope, referral_id, assigned_to_user_id=target)
    else:
        updated = storage.clear_referral_field(scope, referral_id, "assigned_to_user_id")

    if updated is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    prev = existing.assigned_to_user_id
    event_type = "assigned" if target is not None else "unassigned"
    try:
        storage.record_referral_event(
            scope,
            referral_id,
            event_type=event_type,
            from_value=None if prev is None else str(prev),
            to_value=None if target is None else str(target),
            actor_user_id=current_user["id"],
        )
    except Exception:
        logger.exception("Failed to record %s event for referral %s", event_type, referral_id)

    audit_record(
        storage,
        action=f"referral.{event_type}",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral",
        entity_id=str(referral_id),
        metadata={"from": prev, "to": target},
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


# --- Closed-loop response capture (Phase 7.A) ---
#
# Responses are scope-transitive via the parent referral (storage methods gate
# on ``get_referral(scope, ...)``). The route layer adds:
#   * form parsing + enum validation at the boundary,
#   * a ``response_received`` ReferralEvent on every successful create,
#   * auto-transition to ``completed`` when ``consult_completed=True`` AND the
#     state machine allows ``current_status → completed`` (today: only from
#     ``scheduled``). Out-of-machine states silently skip the transition — the
#     response is still recorded, and the coordinator can transition manually.


def _clean_appointment_date(value: str | None) -> str | None:
    """Accept blank; validate ISO YYYY-MM-DD; raise 422 on malformed.

    HTML ``<input type="date">`` produces ISO strings, so the route-boundary
    check catches only hand-crafted payloads — but we still guard since the
    DB column is TEXT (no format enforcement in SQLite).
    """
    v = _clean(value)
    if v is None:
        return None
    from datetime import date

    try:
        date.fromisoformat(v)
    except ValueError:
        raise HTTPException(status_code=422, detail="appointment_date must be YYYY-MM-DD.")
    return v


def _maybe_auto_complete(
    storage: StorageBase,
    scope: Scope,
    referral_id: int,
    actor_user_id: int,
) -> str | None:
    """Attempt the ``current_status → completed`` auto-transition.

    Re-reads the referral status just before the write to close the window
    where a concurrent request may have transitioned the row. ``storage
    .set_referral_status`` does NOT validate the state machine (by design —
    it's dumb storage), so if we rely on a stale snapshot from the request
    handler we can force-write ``completed`` over a racing ``cancelled``.
    Fetching inside the helper shrinks the window to microseconds and reads
    the actual status used for the transition check + emitted event.

    Returns the new status if the transition landed, else None. Event-insert
    failures are logged but not raised — matches the degradation posture of
    ``referral_update``.
    """
    fresh = storage.get_referral(scope, referral_id)
    if fresh is None:
        return None
    from_status = fresh.status
    if not transition_allowed_for_role(
        from_status,
        "completed",
        scope.membership_role,
        is_org=scope.is_org,
    ):
        return None
    updated = storage.set_referral_status(scope, referral_id, "completed")
    if updated is None:
        return None
    try:
        storage.record_referral_event(
            scope,
            referral_id,
            event_type="status_changed",
            from_value=from_status,
            to_value="completed",
            actor_user_id=actor_user_id,
            note="auto: consult completed",
        )
    except Exception:
        logger.exception("Failed to record auto status_changed event for referral %s", referral_id)
    return "completed"


async def _render_detail_with_response_error(
    request: Request,
    current_user: dict,
    storage: StorageBase,
    scope: Scope,
    referral_id: int,
    errors: list[str],
    values: dict,
) -> Response:
    return await _render_detail(
        request,
        current_user,
        storage,
        scope,
        referral_id,
        response_errors=errors,
        response_values=values,
    )


@router.post("/{referral_id}/response", response_class=HTMLResponse)
async def referral_response_create(
    request: Request,
    referral_id: int = Path(..., ge=1),
    appointment_date: str | None = Form(None, max_length=10),
    consult_completed: str | None = Form(None, max_length=8),
    recommendations_text: str | None = Form(None, max_length=4000),
    received_via: str = Form("manual", max_length=16),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    existing = storage.get_referral(scope, referral_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    if received_via not in RECEIVED_VIA_VALUES:
        raise HTTPException(status_code=422, detail="Unknown received_via value.")

    appt = _clean_appointment_date(appointment_date)
    completed = consult_completed in ("on", "true", "1", "yes")
    recs = _clean(recommendations_text)

    if appt is None and not completed and recs is None:
        return await _render_detail_with_response_error(
            request,
            current_user,
            storage,
            scope,
            referral_id,
            ["Add an appointment date, recommendations, or mark the consult as completed."],
            {
                "appointment_date": appointment_date or "",
                "consult_completed": completed,
                "recommendations_text": recommendations_text or "",
                "received_via": received_via,
            },
        )

    created = storage.record_referral_response(
        scope,
        referral_id,
        appointment_date=appt,
        consult_completed=completed,
        recommendations_text=recs,
        received_via=received_via,
        recorded_by_user_id=current_user["id"],
    )
    if created is None:
        # Soft-deleted between the read and write — surface as 404.
        raise HTTPException(status_code=404, detail="Referral not found.")

    # Event log: one response_received per create. Use ``to_value`` to
    # encode the terminal-ness of the response ("scheduled" vs "completed")
    # so the timeline renders something useful without looking up the row.
    summary = "completed" if completed else ("scheduled" if appt else "recorded")
    try:
        storage.record_referral_event(
            scope,
            referral_id,
            event_type="response_received",
            to_value=summary,
            actor_user_id=current_user["id"],
            note=f"via {received_via}",
        )
    except Exception:
        logger.exception("Failed to record response_received event for referral %s", referral_id)

    # Auto-transition to completed when the closed loop is complete AND the
    # state machine permits it from the current status. Helper re-reads the
    # row just before the write so a racing status change isn't clobbered.
    auto_transitioned = False
    if completed:
        new_status = _maybe_auto_complete(storage, scope, referral_id, current_user["id"])
        auto_transitioned = new_status is not None

    audit_record(
        storage,
        action="referral.response.create",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral_response",
        entity_id=str(created.id),
        metadata={
            "referral_id": referral_id,
            "consult_completed": completed,
            "received_via": received_via,
            "auto_transitioned": auto_transitioned,
        },
    )
    dest = f"/referrals/{referral_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


@router.post("/{referral_id}/response/{response_id}", response_class=HTMLResponse)
async def referral_response_update(
    request: Request,
    referral_id: int = Path(..., ge=1),
    response_id: int = Path(..., ge=1),
    appointment_date: str | None = Form(None, max_length=10),
    consult_completed: str | None = Form(None, max_length=8),
    recommendations_text: str | None = Form(None, max_length=4000),
    received_via: str | None = Form(None, max_length=16),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    existing = storage.get_referral(scope, referral_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    via_clean = _clean(received_via)
    if via_clean is not None and via_clean not in RECEIVED_VIA_VALUES:
        raise HTTPException(status_code=422, detail="Unknown received_via value.")

    # Track whether the update is flipping consult_completed from False → True.
    prior_responses = {r.id: r for r in storage.list_referral_responses(scope, referral_id)}
    prior = prior_responses.get(response_id)
    if prior is None:
        raise HTTPException(status_code=404, detail="Response not found.")

    appt = _clean_appointment_date(appointment_date)
    # ``consult_completed`` is a checkbox — absent on unchecked, present on
    # checked. On update, we always interpret it as the user's desired state
    # (since the edit form always posts the checkbox state).
    completed_flag = consult_completed in ("on", "true", "1", "yes")
    recs = _clean(recommendations_text)

    try:
        updated = storage.update_referral_response(
            scope,
            referral_id,
            response_id,
            appointment_date=appt,
            consult_completed=completed_flag,
            recommendations_text=recs,
            received_via=via_clean,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Response not found.")

    newly_completed = completed_flag and not prior.consult_completed
    auto_transitioned = False
    if newly_completed:
        new_status = _maybe_auto_complete(storage, scope, referral_id, current_user["id"])
        auto_transitioned = new_status is not None

    audit_record(
        storage,
        action="referral.response.update",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral_response",
        entity_id=str(response_id),
        metadata={
            "referral_id": referral_id,
            "consult_completed": completed_flag,
            "auto_transitioned": auto_transitioned,
        },
    )
    dest = f"/referrals/{referral_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


@router.delete("/{referral_id}/response/{response_id}")
async def referral_response_delete(
    request: Request,
    referral_id: int = Path(..., ge=1),
    response_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    existing = storage.get_referral(scope, referral_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    if not storage.delete_referral_response(scope, referral_id, response_id):
        raise HTTPException(status_code=404, detail="Response not found.")
    audit_record(
        storage,
        action="referral.response.delete",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral_response",
        entity_id=str(response_id),
        metadata={"referral_id": referral_id},
    )
    dest = f"/referrals/{referral_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


# Fields the POST .../clear/{field} endpoint will set to NULL. Matches
# ``StorageBase.clear_referral_response_field``'s allow-list — keep in sync.
_RESPONSE_CLEARABLE_FIELDS: frozenset[str] = frozenset(
    {"appointment_date", "recommendations_text", "attached_consult_note_ref"}
)


@router.post("/{referral_id}/response/{response_id}/clear/{field}", response_class=HTMLResponse)
async def referral_response_clear_field(
    request: Request,
    referral_id: int = Path(..., ge=1),
    response_id: int = Path(..., ge=1),
    field: str = Path(..., max_length=64),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Explicitly NULL one of the nullable text fields on a response.

    Companion to :func:`referral_response_update`'s None-means-skip semantics
    — the edit form can't distinguish "leave alone" from "set to NULL" for a
    blank text input, so explicit clearing lives here. The allow-list
    matches ``StorageBase.clear_referral_response_field``; unknown fields
    return 422.
    """
    if field not in _RESPONSE_CLEARABLE_FIELDS:
        raise HTTPException(status_code=422, detail=f"Field {field!r} is not clearable.")
    existing_referral = storage.get_referral(scope, referral_id)
    if existing_referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    try:
        updated = storage.clear_referral_response_field(scope, referral_id, response_id, field)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Response not found.")
    audit_record(
        storage,
        action="referral.response.update",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral_response",
        entity_id=str(response_id),
        metadata={"referral_id": referral_id, "cleared": field},
    )
    dest = f"/referrals/{referral_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


# --- Inline comments (Phase 7.B) ---
#
# Coordinators can drop free-text notes on a referral via the detail page.
# Each note lands as a ``note_added`` ReferralEvent — the storage method
# pre-existed, we just wire it to a dedicated route so the audit trail and
# timeline stay consistent with the rest of the event vocabulary.
#
# Note text lives in ``referral_events.note``. ``from_value`` / ``to_value``
# are intentionally empty for note_added events — the timeline template has
# a dedicated branch that renders ``e.note`` as the comment body.

_NOTE_MAX_LENGTH = 4000


@router.post("/{referral_id}/notes", response_class=HTMLResponse)
async def referral_note_create(
    request: Request,
    referral_id: int = Path(..., ge=1),
    note: str = Form(..., max_length=_NOTE_MAX_LENGTH),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    existing = storage.get_referral(scope, referral_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    body = _clean(note)
    if body is None:
        return await _render_detail(
            request,
            current_user,
            storage,
            scope,
            referral_id,
            note_error="Comment cannot be empty.",
            note_value=note,
        )
    # Storage returns None on scope-miss or soft-deleted parent referral.
    # We already verified both above; treat a surprising None as 404 (same
    # TOCTOU posture as referral_update / referral_set_status).
    event = storage.record_referral_event(
        scope,
        referral_id,
        event_type="note_added",
        actor_user_id=current_user["id"],
        note=body,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Referral not found.")
    audit_record(
        storage,
        action="referral.note.create",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="referral",
        entity_id=str(referral_id),
        metadata={"event_id": event.id, "length": len(body)},
    )
    dest = f"/referrals/{referral_id}#timeline"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})
