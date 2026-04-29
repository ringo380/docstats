"""FHIR resource mapper tests — Patient and clinical resources."""

from __future__ import annotations

import pytest

from docstats.ehr.mappers import (
    parse_fhir_allergies,
    parse_fhir_conditions,
    parse_fhir_document_references,
    parse_fhir_medications,
    parse_fhir_patient,
)


def _patient(**overrides):
    base = {
        "resourceType": "Patient",
        "id": "ePXxa1234abc",
        "identifier": [
            {
                "type": {"coding": [{"code": "MR"}]},
                "value": "MRN-99",
            }
        ],
        "name": [
            {
                "use": "official",
                "given": ["Jane", "Quinn"],
                "family": "Doe",
            }
        ],
        "birthDate": "1980-04-15",
        "gender": "female",
        "telecom": [
            {"system": "phone", "value": "415-555-0100"},
            {"system": "email", "value": "jane@example.com"},
        ],
        "address": [
            {
                "use": "home",
                "line": ["123 Main St", "Apt 4B"],
                "city": "San Francisco",
                "state": "CA",
                "postalCode": "94105",
            }
        ],
    }
    base.update(overrides)
    return base


def test_full_patient_round_trip():
    p = parse_fhir_patient(_patient())
    assert p.fhir_id == "ePXxa1234abc"
    assert p.first_name == "Jane"
    assert p.middle_name == "Quinn"
    assert p.last_name == "Doe"
    assert p.date_of_birth == "1980-04-15"
    assert p.gender == "female"
    assert p.mrn == "MRN-99"
    assert p.phone == "415-555-0100"
    assert p.email == "jane@example.com"
    assert p.address_line1 == "123 Main St"
    assert p.address_line2 == "Apt 4B"
    assert p.address_city == "San Francisco"
    assert p.address_state == "CA"
    assert p.address_zip == "94105"


def test_missing_fields_tolerated():
    p = parse_fhir_patient({"resourceType": "Patient", "id": "x"})
    assert p.fhir_id == "x"
    assert p.first_name is None
    assert p.last_name is None
    assert p.mrn is None
    assert p.phone is None
    assert p.address_line1 is None


def test_mrn_filtered_by_type_code():
    """Identifiers without MR coding should not be picked over the MR one."""
    res = _patient(
        identifier=[
            {"type": {"coding": [{"code": "SSN"}]}, "value": "SSN-1"},
            {"type": {"coding": [{"code": "MR"}]}, "value": "MRN-CORRECT"},
        ]
    )
    assert parse_fhir_patient(res).mrn == "MRN-CORRECT"


def test_mrn_fallback_when_untagged():
    """Some sandboxes omit type.coding — fall back to first identifier value."""
    res = _patient(identifier=[{"value": "any-id"}])
    assert parse_fhir_patient(res).mrn == "any-id"


def test_non_patient_resource_raises():
    with pytest.raises(ValueError):
        parse_fhir_patient({"resourceType": "Observation", "id": "x"})


def test_missing_id_raises():
    with pytest.raises(ValueError):
        parse_fhir_patient({"resourceType": "Patient"})


def test_official_name_preferred_over_other_uses():
    res = _patient(
        name=[
            {"use": "nickname", "given": ["Janie"], "family": "D"},
            {"use": "official", "given": ["Jane"], "family": "Doe"},
        ]
    )
    p = parse_fhir_patient(res)
    assert p.first_name == "Jane"
    assert p.last_name == "Doe"


def test_first_address_when_no_home():
    res = _patient(
        address=[
            {"use": "work", "line": ["1 Office"], "city": "SF", "state": "CA", "postalCode": "1"}
        ]
    )
    p = parse_fhir_patient(res)
    assert p.address_line1 == "1 Office"


# ---------------------------------------------------------------------------
# parse_fhir_conditions
# ---------------------------------------------------------------------------


def _condition(code_system: str, code_value: str, display: str) -> dict:
    return {
        "resourceType": "Condition",
        "code": {
            "coding": [{"system": code_system, "code": code_value, "display": display}],
            "text": display,
        },
    }


def test_conditions_icd10_preferred_over_snomed():
    resources = [
        {
            "resourceType": "Condition",
            "code": {
                "coding": [
                    {"system": "http://snomed.info/sct", "code": "44054006", "display": "Diabetes"},
                    {
                        "system": "http://hl7.org/fhir/sid/icd-10-cm",
                        "code": "E11.9",
                        "display": "Type 2 diabetes",
                    },
                ]
            },
        }
    ]
    out = parse_fhir_conditions(resources)
    assert len(out) == 1
    assert out[0]["icd10_code"] == "E11.9"
    assert out[0]["icd10_desc"] == "Type 2 diabetes"


def test_conditions_first_entry_is_primary():
    resources = [
        _condition("http://hl7.org/fhir/sid/icd-10-cm", "E11.9", "DM2"),
        _condition("http://hl7.org/fhir/sid/icd-10-cm", "I10", "HTN"),
    ]
    out = parse_fhir_conditions(resources)
    assert out[0]["is_primary"] is True
    assert out[1]["is_primary"] is False


def test_conditions_wrong_resource_type_skipped():
    resources = [
        {"resourceType": "Observation", "code": {"coding": [{"code": "E11.9"}]}},
        _condition("http://hl7.org/fhir/sid/icd-10-cm", "I10", "HTN"),
    ]
    out = parse_fhir_conditions(resources)
    assert len(out) == 1
    assert out[0]["icd10_code"] == "I10"


def test_conditions_no_code_skipped():
    resources = [{"resourceType": "Condition", "code": {"coding": []}}]
    out = parse_fhir_conditions(resources)
    assert out == []


# ---------------------------------------------------------------------------
# parse_fhir_medications
# ---------------------------------------------------------------------------


def test_medications_name_from_text():
    resources = [
        {
            "resourceType": "MedicationStatement",
            "medicationCodeableConcept": {"text": "Metformin 500mg"},
        }
    ]
    out = parse_fhir_medications(resources)
    assert len(out) == 1
    assert out[0]["name"] == "Metformin 500mg"


def test_medications_name_from_coding_display():
    resources = [
        {
            "resourceType": "MedicationStatement",
            "medicationCodeableConcept": {
                "coding": [{"display": "Lisinopril 10mg"}],
            },
        }
    ]
    out = parse_fhir_medications(resources)
    assert out[0]["name"] == "Lisinopril 10mg"


def test_medications_missing_dosage_returns_none_fields():
    resources = [
        {
            "resourceType": "MedicationStatement",
            "medicationCodeableConcept": {"text": "Aspirin"},
        }
    ]
    out = parse_fhir_medications(resources)
    assert out[0]["dose"] is None
    assert out[0]["route"] is None
    assert out[0]["frequency"] is None


def test_medications_wrong_resource_type_skipped():
    resources = [
        {"resourceType": "MedicationRequest", "medicationCodeableConcept": {"text": "X"}},
        {
            "resourceType": "MedicationStatement",
            "medicationCodeableConcept": {"text": "Aspirin"},
        },
    ]
    out = parse_fhir_medications(resources)
    assert len(out) == 1


def test_medications_no_name_skipped():
    resources = [{"resourceType": "MedicationStatement", "medicationCodeableConcept": {}}]
    out = parse_fhir_medications(resources)
    assert out == []


# ---------------------------------------------------------------------------
# parse_fhir_allergies
# ---------------------------------------------------------------------------


def test_allergies_substance_reaction_severity():
    resources = [
        {
            "resourceType": "AllergyIntolerance",
            "code": {"text": "Penicillin"},
            "reaction": [
                {
                    "manifestation": [{"text": "Hives"}],
                    "severity": "moderate",
                }
            ],
        }
    ]
    out = parse_fhir_allergies(resources)
    assert len(out) == 1
    assert out[0]["substance"] == "Penicillin"
    assert out[0]["reaction"] == "Hives"
    assert out[0]["severity"] == "moderate"


def test_allergies_missing_reaction_returns_none():
    resources = [
        {
            "resourceType": "AllergyIntolerance",
            "code": {"text": "Latex"},
        }
    ]
    out = parse_fhir_allergies(resources)
    assert out[0]["reaction"] is None
    assert out[0]["severity"] is None


def test_allergies_no_substance_skipped():
    resources = [{"resourceType": "AllergyIntolerance", "code": {}}]
    out = parse_fhir_allergies(resources)
    assert out == []


def test_allergies_wrong_resource_type_skipped():
    resources = [
        {"resourceType": "Observation"},
        {"resourceType": "AllergyIntolerance", "code": {"text": "Peanuts"}},
    ]
    out = parse_fhir_allergies(resources)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# parse_fhir_document_references
# ---------------------------------------------------------------------------


def _doc_ref(label: str = "Progress Note", content_url: str | None = None) -> dict:
    attachment: dict = {}
    if content_url:
        attachment["url"] = content_url
        attachment["contentType"] = "application/pdf"
    return {
        "resourceType": "DocumentReference",
        "type": {"text": label},
        "date": "2024-03-15T10:00:00Z",
        "content": [{"attachment": attachment}] if attachment else [],
    }


def test_doc_ref_label_and_date_extracted():
    out = parse_fhir_document_references([_doc_ref("Discharge Summary")])
    assert out[0]["label"] == "Discharge Summary"
    assert out[0]["date_of_service"] == "2024-03-15"


def test_doc_ref_date_truncated_to_date():
    resources = [
        {
            "resourceType": "DocumentReference",
            "type": {"text": "Note"},
            "date": "2024-06-01T14:30:00+05:00",
            "content": [],
        }
    ]
    out = parse_fhir_document_references(resources)
    assert out[0]["date_of_service"] == "2024-06-01"


def test_doc_ref_content_url_extracted():
    out = parse_fhir_document_references([_doc_ref(content_url="Binary/abc123")])
    assert out[0]["content_url"] == "Binary/abc123"
    assert out[0]["content_type"] == "application/pdf"


def test_doc_ref_absolute_url_preserved():
    out = parse_fhir_document_references(
        [_doc_ref(content_url="https://fhir.example.com/Binary/xyz")]
    )
    assert out[0]["content_url"] == "https://fhir.example.com/Binary/xyz"


def test_doc_ref_inline_data_extracted():
    import base64

    payload = base64.b64encode(b"PDF content").decode()
    resources = [
        {
            "resourceType": "DocumentReference",
            "type": {"text": "Lab"},
            "content": [{"attachment": {"data": payload, "contentType": "application/pdf"}}],
        }
    ]
    out = parse_fhir_document_references(resources)
    assert out[0]["inline_data"] == payload
    assert out[0]["content_url"] is None


def test_doc_ref_no_content_returns_none_fields():
    resources = [
        {
            "resourceType": "DocumentReference",
            "type": {"text": "Note"},
            "content": [],
        }
    ]
    out = parse_fhir_document_references(resources)
    assert out[0]["content_url"] is None
    assert out[0]["inline_data"] is None


def test_doc_ref_wrong_resource_type_skipped():
    resources = [
        {"resourceType": "Binary"},
        {"resourceType": "DocumentReference", "type": {"text": "Lab"}, "content": []},
    ]
    out = parse_fhir_document_references(resources)
    assert len(out) == 1


def test_doc_ref_default_label_when_type_missing():
    resources = [{"resourceType": "DocumentReference", "content": []}]
    out = parse_fhir_document_references(resources)
    assert out[0]["label"] == "Imported document"
