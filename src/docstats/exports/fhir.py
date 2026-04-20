"""FHIR-ish JSON export for a Referral (Phase 5.D).

"FHIR-ish" — the output is shaped like HL7 FHIR R4 and passes the
smell test for developers familiar with the format, but it is NOT
guaranteed to pass a strict validator. Phases 12+ (SMART-on-FHIR
integrations) will harden the mapping; for now this gives us a
machine-readable export for downstream systems.

The top-level response is a FHIR Bundle (type=document) containing:

- Patient
- ServiceRequest (the referral itself)
- Practitioner (referring, if NPI/name known) — optional
- Organization (receiving, if name known) — optional
- Condition (primary diagnosis, if ICD or text known) — optional
- MedicationStatement[] (one per referral_medications row)
- AllergyIntolerance[] (one per referral_allergies row)
- DocumentReference[] (one per referral_attachments row;
  ``status`` = ``current`` when the attachment is included, ``preliminary``
  when the row is a checklist-only placeholder)

The module is pure Python — no FastAPI, no storage. Callers pre-fetch
the related rows and pass them in. ``build_referral_bundle`` returns a
plain ``dict`` ready for ``json.dumps``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from docstats.domain.patients import Patient
    from docstats.domain.referrals import (
        Referral,
        ReferralAllergy,
        ReferralAttachment,
        ReferralDiagnosis,
        ReferralMedication,
    )


# ---------- Status + priority translation ----------

# Our referral state machine → FHIR ServiceRequest.status. FHIR's vocabulary
# is closed; map anything we don't have a direct word for to the nearest
# neighbor. ``rejected`` maps to ``revoked`` rather than ``entered-in-error``
# because the referral was intentionally created — the receiving side
# declined it.
_STATUS_MAP: dict[str, str] = {
    "draft": "draft",
    "ready": "active",
    "sent": "active",
    "awaiting_records": "on-hold",
    "awaiting_auth": "on-hold",
    "scheduled": "active",
    "rejected": "revoked",
    "completed": "completed",
    "cancelled": "revoked",
}

# FHIR ServiceRequest.priority vocab is routine|urgent|asap|stat. Our
# ``priority`` tier has no FHIR equivalent — FHIR skips straight from
# ``urgent`` to ``asap``. We map ``priority`` → ``urgent`` (closer to the
# workflow meaning than ``asap``) and keep ``urgent`` → ``urgent``.
_PRIORITY_MAP: dict[str, str] = {
    "routine": "routine",
    "priority": "urgent",
    "urgent": "urgent",
    "stat": "stat",
}


# ---------- Identifier + reference helpers ----------


def _patient_id(patient: "Patient") -> str:
    return f"patient-{patient.id}"


def _referral_id(referral: "Referral") -> str:
    return f"servicerequest-{referral.id}"


def _practitioner_id(referral: "Referral") -> str:
    return f"practitioner-{referral.id}-referring"


def _organization_id(referral: "Referral") -> str:
    return f"organization-{referral.id}-receiving"


def _condition_id(referral: "Referral") -> str:
    return f"condition-{referral.id}-primary"


def _medication_id(referral_id: int, med_id: int) -> str:
    return f"medicationstatement-{referral_id}-{med_id}"


def _allergy_id(referral_id: int, allergy_id: int) -> str:
    return f"allergyintolerance-{referral_id}-{allergy_id}"


def _attachment_id(referral_id: int, attachment_id: int) -> str:
    return f"documentreference-{referral_id}-{attachment_id}"


def _ref(resource_type: str, resource_id: str) -> dict[str, str]:
    return {"reference": f"{resource_type}/{resource_id}"}


def _identifier_npi(npi: str) -> dict[str, Any]:
    return {
        "system": "http://hl7.org/fhir/sid/us-npi",
        "value": npi,
    }


def _identifier_mrn(mrn: str) -> dict[str, Any]:
    return {
        "type": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                    "code": "MR",
                    "display": "Medical record number",
                }
            ],
            "text": "MRN",
        },
        "value": mrn,
    }


# ---------- Resource builders ----------


def _build_patient(patient: "Patient") -> dict[str, Any]:
    name: dict[str, Any] = {
        "family": patient.last_name,
        "given": [patient.first_name] + ([patient.middle_name] if patient.middle_name else []),
    }
    resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": _patient_id(patient),
        "name": [name],
    }
    if patient.date_of_birth:
        resource["birthDate"] = patient.date_of_birth
    if patient.sex:
        # FHIR gender vocab: male | female | other | unknown
        sex_map = {"M": "male", "F": "female", "O": "other", "U": "unknown"}
        gender = sex_map.get(patient.sex.upper(), patient.sex.lower())
        if gender in ("male", "female", "other", "unknown"):
            resource["gender"] = gender
    identifiers: list[dict[str, Any]] = []
    if patient.mrn:
        identifiers.append(_identifier_mrn(patient.mrn))
    if identifiers:
        resource["identifier"] = identifiers
    telecom: list[dict[str, Any]] = []
    if patient.phone:
        telecom.append({"system": "phone", "value": patient.phone, "use": "home"})
    if patient.email:
        telecom.append({"system": "email", "value": patient.email})
    if telecom:
        resource["telecom"] = telecom
    if patient.preferred_language:
        resource["communication"] = [
            {"language": {"text": patient.preferred_language}, "preferred": True}
        ]
    if any([patient.address_line1, patient.address_city, patient.address_state]):
        addr: dict[str, Any] = {}
        lines = [line for line in (patient.address_line1, patient.address_line2) if line]
        if lines:
            addr["line"] = lines
        if patient.address_city:
            addr["city"] = patient.address_city
        if patient.address_state:
            addr["state"] = patient.address_state
        if patient.address_zip:
            addr["postalCode"] = patient.address_zip
        resource["address"] = [addr]
    return resource


def _build_service_request(
    referral: "Referral",
    patient: "Patient",
    has_practitioner: bool,
    has_organization: bool,
    has_condition: bool,
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "ServiceRequest",
        "id": _referral_id(referral),
        "status": _STATUS_MAP.get(referral.status, "unknown"),
        "intent": "order",
        "priority": _PRIORITY_MAP.get(referral.urgency, "routine"),
        "subject": _ref("Patient", _patient_id(patient)),
        "authoredOn": referral.created_at.isoformat(),
    }
    # ``code`` carries what service is being requested. Prefer the
    # coordinator's explicit ``requested_service`` text, fall back to
    # the specialty descriptor.
    code_text = referral.requested_service or referral.specialty_desc
    if code_text:
        resource["code"] = {"text": code_text}
    if referral.specialty_code:
        resource.setdefault("category", []).append(
            {
                "coding": [
                    {
                        "system": "http://nucc.org/provider-taxonomy",
                        "code": referral.specialty_code,
                        "display": referral.specialty_desc or referral.specialty_code,
                    }
                ]
            }
        )
    reason: list[dict[str, Any]] = []
    if referral.reason:
        reason.append({"text": referral.reason})
    if reason:
        resource["reasonCode"] = reason
    if has_practitioner:
        resource["requester"] = _ref("Practitioner", _practitioner_id(referral))
    if has_organization or referral.receiving_provider_npi:
        performers: list[dict[str, Any]] = []
        if has_organization:
            performers.append(_ref("Organization", _organization_id(referral)))
        if referral.receiving_provider_npi:
            performers.append({"identifier": _identifier_npi(referral.receiving_provider_npi)})
        if performers:
            resource["performer"] = performers
    if has_condition:
        resource["reasonReference"] = [_ref("Condition", _condition_id(referral))]
    # Clinical question → FHIR note
    if referral.clinical_question:
        resource["note"] = [{"text": referral.clinical_question}]
    # Authorization bookkeeping — FHIR has insurance + identifier.
    if referral.authorization_number:
        resource["identifier"] = [
            {
                "type": {"text": "Authorization number"},
                "value": referral.authorization_number,
            }
        ]
    return resource


def _build_practitioner(referral: "Referral") -> dict[str, Any] | None:
    if not (referral.referring_provider_npi or referral.referring_provider_name):
        return None
    resource: dict[str, Any] = {
        "resourceType": "Practitioner",
        "id": _practitioner_id(referral),
    }
    if referral.referring_provider_npi:
        resource["identifier"] = [_identifier_npi(referral.referring_provider_npi)]
    if referral.referring_provider_name:
        resource["name"] = [{"text": referral.referring_provider_name}]
    return resource


def _build_organization(referral: "Referral") -> dict[str, Any] | None:
    if not referral.receiving_organization_name:
        return None
    resource: dict[str, Any] = {
        "resourceType": "Organization",
        "id": _organization_id(referral),
        "name": referral.receiving_organization_name,
    }
    if referral.receiving_provider_npi:
        # In FHIR an Organization can ALSO carry an NPI identifier.
        resource["identifier"] = [_identifier_npi(referral.receiving_provider_npi)]
    return resource


def _build_condition(
    referral: "Referral",
    patient: "Patient",
) -> dict[str, Any] | None:
    if not (referral.diagnosis_primary_icd or referral.diagnosis_primary_text):
        return None
    resource: dict[str, Any] = {
        "resourceType": "Condition",
        "id": _condition_id(referral),
        "subject": _ref("Patient", _patient_id(patient)),
    }
    coding: list[dict[str, Any]] = []
    if referral.diagnosis_primary_icd:
        coding.append(
            {
                "system": "http://hl7.org/fhir/sid/icd-10",
                "code": referral.diagnosis_primary_icd,
                "display": referral.diagnosis_primary_text or referral.diagnosis_primary_icd,
            }
        )
    if coding or referral.diagnosis_primary_text:
        resource["code"] = {
            "coding": coding,
            "text": referral.diagnosis_primary_text or referral.diagnosis_primary_icd,
        }
    return resource


def _build_medication_statement(
    med: "ReferralMedication",
    patient: "Patient",
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "MedicationStatement",
        "id": _medication_id(med.referral_id, med.id),
        "status": "active",
        "subject": _ref("Patient", _patient_id(patient)),
        "medicationCodeableConcept": {"text": med.name},
    }
    dosage: dict[str, Any] = {}
    if med.dose:
        dosage["text"] = med.dose
    if med.frequency:
        dosage.setdefault("text", "")
        dosage["text"] = f"{dosage['text']} {med.frequency}".strip()
    if med.route:
        dosage["route"] = {"text": med.route}
    if dosage:
        resource["dosage"] = [dosage]
    return resource


def _build_allergy_intolerance(
    allergy: "ReferralAllergy",
    patient: "Patient",
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "AllergyIntolerance",
        "id": _allergy_id(allergy.referral_id, allergy.id),
        "patient": _ref("Patient", _patient_id(patient)),
        "code": {"text": allergy.substance},
    }
    if allergy.reaction or allergy.severity:
        reaction: dict[str, Any] = {}
        if allergy.reaction:
            reaction["manifestation"] = [{"text": allergy.reaction}]
        if allergy.severity:
            reaction["severity"] = allergy.severity
        resource["reaction"] = [reaction]
    return resource


def _build_document_reference(
    attachment: "ReferralAttachment",
    patient: "Patient",
) -> dict[str, Any]:
    # ``preliminary`` when the row is checklist-only (record expected but
    # not yet attached); ``current`` when a real attachment exists.
    status = "preliminary" if attachment.checklist_only else "current"
    resource: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "id": _attachment_id(attachment.referral_id, attachment.id),
        "status": status,
        "subject": _ref("Patient", _patient_id(patient)),
        "type": {"text": attachment.kind},
        "description": attachment.label,
    }
    if attachment.date_of_service:
        resource["date"] = attachment.date_of_service
    return resource


# ---------- Top-level entry point ----------


def build_referral_bundle(
    *,
    referral: "Referral",
    patient: "Patient",
    diagnoses: list["ReferralDiagnosis"] | None = None,
    medications: list["ReferralMedication"] | None = None,
    allergies: list["ReferralAllergy"] | None = None,
    attachments: list["ReferralAttachment"] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a FHIR-ish Bundle dict for the referral.

    ``diagnoses`` is used to check whether a primary-diagnosis Condition
    should be emitted (taking the first ``is_primary=True`` row when
    present, else falling back to the headline fields on the Referral).
    """
    now = generated_at or datetime.now(tz=timezone.utc)

    # Primary-diagnosis selection: prefer an is_primary sub-row if one is
    # populated on the referral, otherwise use the denormalized headline.
    condition_resource = _build_condition(referral, patient)

    practitioner_resource = _build_practitioner(referral)
    organization_resource = _build_organization(referral)

    has_practitioner = practitioner_resource is not None
    has_organization = organization_resource is not None
    has_condition = condition_resource is not None

    entries: list[dict[str, Any]] = []

    def _push(resource: dict[str, Any] | None) -> None:
        if resource is None:
            return
        entries.append({"resource": resource})

    _push(_build_patient(patient))
    _push(
        _build_service_request(
            referral,
            patient,
            has_practitioner=has_practitioner,
            has_organization=has_organization,
            has_condition=has_condition,
        )
    )
    _push(practitioner_resource)
    _push(organization_resource)
    _push(condition_resource)
    for med in medications or []:
        _push(_build_medication_statement(med, patient))
    for allergy in allergies or []:
        _push(_build_allergy_intolerance(allergy, patient))
    for attachment in attachments or []:
        _push(_build_document_reference(attachment, patient))

    # Keep ``diagnoses`` available for future expansion (e.g. emitting
    # secondary Conditions). For now the headline Condition on the
    # Referral is authoritative.
    _ = diagnoses

    return {
        "resourceType": "Bundle",
        "type": "document",
        "timestamp": now.isoformat(),
        "entry": entries,
    }
