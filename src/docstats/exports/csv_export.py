"""Flat CSV export for referrals (Phase 5.E).

One row per referral, flat column list suitable for spreadsheet analysis.
Headings match the admin-facing field names, not the FHIR-ish mapping
used by the JSON export — CSV is meant for spreadsheet pivots, not
interop pipelines.

The module is FastAPI-free. ``referral_to_csv_row`` takes a Referral +
optional Patient (so the route can batch-fetch patients once by id) and
returns a dict keyed by :data:`CSV_FIELDNAMES`. The route writes the
rows to ``csv.DictWriter`` with that same fieldname list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from docstats.domain.patients import Patient
    from docstats.domain.referrals import Referral


CSV_FIELDNAMES: tuple[str, ...] = (
    "referral_id",
    "status",
    "urgency",
    "patient_id",
    "patient_first_name",
    "patient_last_name",
    "patient_middle_name",
    "patient_date_of_birth",
    "patient_mrn",
    "patient_sex",
    "reason",
    "clinical_question",
    "requested_service",
    "specialty_code",
    "specialty_desc",
    "receiving_organization_name",
    "receiving_provider_npi",
    "referring_organization",
    "referring_provider_name",
    "referring_provider_npi",
    "diagnosis_primary_icd",
    "diagnosis_primary_text",
    "authorization_status",
    "authorization_number",
    "external_source",
    "external_reference_id",
    "assigned_to_user_id",
    "created_at",
    "updated_at",
)


def referral_to_csv_row(
    referral: "Referral",
    patient: "Patient | None",
) -> dict[str, Any]:
    """Flatten a Referral + its Patient into a CSV-ready dict.

    Missing patient is acceptable — it shows up as blank fields rather
    than crashing the export. That keeps a batch export from dying on
    one weird row; the audit log still captures what happened.
    """
    return {
        "referral_id": referral.id,
        "status": referral.status,
        "urgency": referral.urgency,
        "patient_id": referral.patient_id,
        "patient_first_name": patient.first_name if patient else "",
        "patient_last_name": patient.last_name if patient else "",
        "patient_middle_name": (patient.middle_name if patient else None) or "",
        "patient_date_of_birth": (patient.date_of_birth if patient else None) or "",
        "patient_mrn": (patient.mrn if patient else None) or "",
        "patient_sex": (patient.sex if patient else None) or "",
        "reason": referral.reason or "",
        "clinical_question": referral.clinical_question or "",
        "requested_service": referral.requested_service or "",
        "specialty_code": referral.specialty_code or "",
        "specialty_desc": referral.specialty_desc or "",
        "receiving_organization_name": referral.receiving_organization_name or "",
        "receiving_provider_npi": referral.receiving_provider_npi or "",
        "referring_organization": referral.referring_organization or "",
        "referring_provider_name": referral.referring_provider_name or "",
        "referring_provider_npi": referral.referring_provider_npi or "",
        "diagnosis_primary_icd": referral.diagnosis_primary_icd or "",
        "diagnosis_primary_text": referral.diagnosis_primary_text or "",
        "authorization_status": referral.authorization_status,
        "authorization_number": referral.authorization_number or "",
        "external_source": referral.external_source,
        "external_reference_id": referral.external_reference_id or "",
        "assigned_to_user_id": referral.assigned_to_user_id or "",
        "created_at": referral.created_at.isoformat(),
        "updated_at": referral.updated_at.isoformat(),
    }
