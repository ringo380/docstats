"""Patient CRUD routes — Phase 2.A.

Scope-enforced via ``get_scope`` and PHI-gated via ``require_phi_consent``.
Anonymous callers never reach these — ``require_phi_consent`` depends on
``require_user`` which raises ``AuthRequiredException``.

Solo mode still gets the list + detail views; the Phase 2.C referral wizard
auto-creates a "self" patient for solo users from their profile, but that's a
wizard convenience, not a scope concern. The storage layer treats every Patient
row the same.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, Response

from docstats.domain.audit import record as audit_record
from docstats.phi import require_phi_consent
from docstats.routes._common import US_STATES, get_scope, render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

router = APIRouter(prefix="/patients", tags=["patients"])


def _ctx(request: Request, user: dict, storage: StorageBase, **extra) -> dict:
    """Common template context — nav needs user + saved_count."""
    return {
        "request": request,
        "active_page": "patients",
        "user": user,
        "saved_count": saved_count(storage, user["id"]),
        "states": US_STATES,
        **extra,
    }


def _clean(value: str | None) -> str | None:
    """Trim whitespace; collapse empty strings to None for optional fields."""
    if value is None:
        return None
    v = value.strip()
    return v or None


def _validate_dob(dob: str | None) -> str | None:
    """Parse ISO YYYY-MM-DD; raise 422 on malformed input. None passes through.

    Rejects future dates to match onboarding's DOB validation — a DOB in the
    future is always a typo (no "date of birth" can be after today).
    """
    dob = _clean(dob)
    if dob is None:
        return None
    try:
        parsed = date.fromisoformat(dob)
    except ValueError:
        raise HTTPException(status_code=422, detail="Date of birth must be YYYY-MM-DD.")
    if parsed > date.today():
        raise HTTPException(status_code=422, detail="Date of birth cannot be in the future.")
    return dob


def _require_patient(scope: Scope, storage: StorageBase, patient_id: int):
    patient = storage.get_patient(scope, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    return patient


@router.get("", response_class=HTMLResponse)
async def patients_list(
    request: Request,
    search: str | None = Query(None, max_length=100),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    patients = storage.list_patients(scope, search=search, limit=200)
    return render(
        "patients_list.html",
        _ctx(
            request,
            current_user,
            storage,
            patients=patients,
            search=search or "",
        ),
    )


@router.get("/new", response_class=HTMLResponse)
async def patient_new_form(
    request: Request,
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),  # noqa: ARG001 — validates access
    storage: StorageBase = Depends(get_storage),
):
    return render(
        "patient_new.html",
        _ctx(request, current_user, storage, errors=None, values={}),
    )


@router.post("", response_class=HTMLResponse)
async def patient_create(
    request: Request,
    first_name: str = Form(..., max_length=100),
    last_name: str = Form(..., max_length=100),
    middle_name: str | None = Form(None, max_length=100),
    date_of_birth: str | None = Form(None, max_length=10),
    sex: str | None = Form(None, max_length=32),
    mrn: str | None = Form(None, max_length=64),
    preferred_language: str | None = Form(None, max_length=64),
    pronouns: str | None = Form(None, max_length=32),
    phone: str | None = Form(None, max_length=40),
    email: str | None = Form(None, max_length=254),
    address_line1: str | None = Form(None, max_length=200),
    address_line2: str | None = Form(None, max_length=200),
    address_city: str | None = Form(None, max_length=100),
    address_state: str | None = Form(None, max_length=2),
    address_zip: str | None = Form(None, max_length=10),
    emergency_contact_name: str | None = Form(None, max_length=100),
    emergency_contact_phone: str | None = Form(None, max_length=40),
    notes: str | None = Form(None, max_length=2000),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    first = _clean(first_name) or ""
    last = _clean(last_name) or ""
    if not first or not last:
        return render(
            "patient_new.html",
            _ctx(
                request,
                current_user,
                storage,
                errors=["First name and last name are required."],
                values={
                    "first_name": first,
                    "last_name": last,
                    "middle_name": middle_name or "",
                    "date_of_birth": date_of_birth or "",
                    "sex": sex or "",
                    "mrn": mrn or "",
                    "phone": phone or "",
                    "email": email or "",
                },
            ),
        )
    dob = _validate_dob(date_of_birth)
    try:
        patient = storage.create_patient(
            scope,
            first_name=first,
            last_name=last,
            middle_name=_clean(middle_name),
            date_of_birth=dob,
            sex=_clean(sex),
            mrn=_clean(mrn),
            preferred_language=_clean(preferred_language),
            pronouns=_clean(pronouns),
            phone=_clean(phone),
            email=_clean(email),
            address_line1=_clean(address_line1),
            address_line2=_clean(address_line2),
            address_city=_clean(address_city),
            address_state=_clean(address_state),
            address_zip=_clean(address_zip),
            emergency_contact_name=_clean(emergency_contact_name),
            emergency_contact_phone=_clean(emergency_contact_phone),
            notes=_clean(notes),
            created_by_user_id=current_user["id"],
        )
    except ValueError as e:
        return render(
            "patient_new.html",
            _ctx(
                request,
                current_user,
                storage,
                errors=[str(e)],
                values={"first_name": first, "last_name": last},
            ),
        )
    audit_record(
        storage,
        action="patient.create",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="patient",
        entity_id=str(patient.id),
    )
    dest = f"/patients/{patient.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


@router.get("/{patient_id}", response_class=HTMLResponse)
async def patient_detail(
    request: Request,
    patient_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    patient = _require_patient(scope, storage, patient_id)
    latest_check = storage.get_latest_eligibility_check(scope, patient_id)
    return render(
        "patient_detail.html",
        _ctx(
            request, current_user, storage, patient=patient, errors=None, latest_check=latest_check
        ),
    )


@router.post("/{patient_id}", response_class=HTMLResponse)
async def patient_update(
    request: Request,
    patient_id: int = Path(..., ge=1),
    first_name: str = Form(..., max_length=100),
    last_name: str = Form(..., max_length=100),
    middle_name: str | None = Form(None, max_length=100),
    date_of_birth: str | None = Form(None, max_length=10),
    sex: str | None = Form(None, max_length=32),
    mrn: str | None = Form(None, max_length=64),
    preferred_language: str | None = Form(None, max_length=64),
    pronouns: str | None = Form(None, max_length=32),
    phone: str | None = Form(None, max_length=40),
    email: str | None = Form(None, max_length=254),
    address_line1: str | None = Form(None, max_length=200),
    address_line2: str | None = Form(None, max_length=200),
    address_city: str | None = Form(None, max_length=100),
    address_state: str | None = Form(None, max_length=2),
    address_zip: str | None = Form(None, max_length=10),
    emergency_contact_name: str | None = Form(None, max_length=100),
    emergency_contact_phone: str | None = Form(None, max_length=40),
    notes: str | None = Form(None, max_length=2000),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    _require_patient(scope, storage, patient_id)  # 404 if missing/cross-tenant
    first = _clean(first_name) or ""
    last = _clean(last_name) or ""
    if not first or not last:
        patient = storage.get_patient(scope, patient_id)
        latest_check = storage.get_latest_eligibility_check(scope, patient_id)
        return render(
            "patient_detail.html",
            _ctx(
                request,
                current_user,
                storage,
                patient=patient,
                latest_check=latest_check,
                errors=["First name and last name are required."],
            ),
        )
    dob = _validate_dob(date_of_birth)
    try:
        updated = storage.update_patient(
            scope,
            patient_id,
            first_name=first,
            last_name=last,
            middle_name=_clean(middle_name),
            date_of_birth=dob,
            sex=_clean(sex),
            mrn=_clean(mrn),
            preferred_language=_clean(preferred_language),
            pronouns=_clean(pronouns),
            phone=_clean(phone),
            email=_clean(email),
            address_line1=_clean(address_line1),
            address_line2=_clean(address_line2),
            address_city=_clean(address_city),
            address_state=_clean(address_state),
            address_zip=_clean(address_zip),
            emergency_contact_name=_clean(emergency_contact_name),
            emergency_contact_phone=_clean(emergency_contact_phone),
            notes=_clean(notes),
        )
    except ValueError as e:
        patient = storage.get_patient(scope, patient_id)
        latest_check = storage.get_latest_eligibility_check(scope, patient_id)
        return render(
            "patient_detail.html",
            _ctx(
                request,
                current_user,
                storage,
                patient=patient,
                latest_check=latest_check,
                errors=[str(e)],
            ),
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    audit_record(
        storage,
        action="patient.update",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="patient",
        entity_id=str(patient_id),
    )
    dest = f"/patients/{patient_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


@router.delete("/{patient_id}")
async def patient_delete(
    request: Request,
    patient_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    _require_patient(scope, storage, patient_id)
    ok = storage.soft_delete_patient(scope, patient_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Patient not found.")
    audit_record(
        storage,
        action="patient.delete",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="patient",
        entity_id=str(patient_id),
    )
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/patients"})
    return Response(status_code=303, headers={"Location": "/patients"})
