"""FHIR Patient parser tests."""

from __future__ import annotations

import pytest

from docstats.ehr.mappers import parse_fhir_patient


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
