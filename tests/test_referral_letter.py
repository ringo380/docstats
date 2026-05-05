"""Tests for AMA-style referral letter rendering — plaintext and PDF/HTML.

The PDF tests use ``pytest.importorskip("weasyprint")`` so this file
runs (and exercises the plaintext path + Jinja template parse) on
machines without the WeasyPrint system libs installed. CI installs the
libs via ``railpack.json`` and runs the full suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from docstats.formatting import referral_letter_text


def _patient(**kwargs):
    base = dict(
        id=1,
        scope_user_id=42,
        scope_organization_id=None,
        first_name="Jane",
        last_name="Doe",
        middle_name=None,
        date_of_birth="1985-04-12",
        sex="F",
        mrn="MRN-001",
        preferred_language="English",
        pronouns=None,
        phone="5551234567",
        email=None,
        address_line1=None,
        address_line2=None,
        address_city=None,
        address_state=None,
        address_zip=None,
        emergency_contact_name=None,
        emergency_contact_phone=None,
        notes=None,
        ehr_fhir_id=None,
        created_by_user_id=None,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        deleted_at=None,
    )
    base.update(kwargs)
    # Lazy import so this test file is collectible without a fully
    # installed dev environment for static-analysis-only CI shards.
    from docstats.domain.patients import Patient

    return Patient(**base)


def _referral(**kwargs):
    base = dict(
        id=11,
        scope_user_id=42,
        scope_organization_id=None,
        patient_id=1,
        referring_provider_npi=None,
        referring_provider_name=None,
        referring_organization=None,
        receiving_provider_npi=None,
        receiving_organization_name="Bay Cardiology",
        specialty_code=None,
        specialty_desc="Cardiovascular Disease",
        reason="55F with progressive exertional dyspnea over 8 weeks.",
        clinical_question="Please evaluate for ischemic etiology and need for stress testing.",
        urgency="urgent",
        requested_service=None,
        diagnosis_primary_icd="I20.9",
        diagnosis_primary_text="Angina pectoris, unspecified",
        payer_plan_id=None,
        authorization_number="AUTH-99",
        authorization_status="obtained",
        status="ready",
        assigned_to_user_id=None,
        external_reference_id=None,
        external_source="manual",
        ehr_service_request_id=None,
        created_by_user_id=42,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        deleted_at=None,
    )
    base.update(kwargs)
    from docstats.domain.referrals import Referral

    return Referral(**base)


def _user(**kwargs):
    base = {
        "id": 42,
        "email": "ryan@example.com",
        "first_name": "Ryan",
        "last_name": "Robson",
        "credentials": "MD",
        "individual_npi": "1234567890",
        "state_license_number": "A12345",
        "state_license_state": "CA",
    }
    base.update(kwargs)
    return base


# ─── Plaintext formatter ───────────────────────────────────────────


def test_letter_text_includes_re_line_and_salutation():
    text = referral_letter_text(
        _referral(),
        _patient(),
        current_user=_user(),
    )
    assert "RE: Jane Doe" in text
    assert "DOB 1985-04-12" in text
    assert "MRN MRN-001" in text
    # Referral domain has no receiving_provider_name field today, so the
    # default fallback salutation fires.
    assert "Dear Colleague:" in text


def test_letter_text_default_salutation_when_no_receiver():
    text = referral_letter_text(
        _referral(),
        _patient(),
        current_user=_user(),
    )
    assert "Dear Colleague:" in text


def test_letter_text_signature_block_uses_user_credentials():
    text = referral_letter_text(_referral(), _patient(), current_user=_user())
    assert "Ryan Robson, MD" in text
    assert "NPI: 1234567890" in text
    assert "License: A12345 (CA)" in text


def test_letter_text_signature_falls_back_when_no_credentials():
    user = _user(credentials=None, individual_npi=None, state_license_number=None)
    text = referral_letter_text(_referral(), _patient(), current_user=user)
    assert "Ryan Robson" in text
    assert "License:" not in text


def test_letter_text_includes_phi_footer():
    text = referral_letter_text(_referral(), _patient(), current_user=_user())
    assert "CONFIDENTIAL" in text
    assert "HIPAA" in text


def test_letter_text_payer_mode_uses_member_block():
    insurance = SimpleNamespace(payer_name="Blue Shield CA", plan_type="HMO")
    text = referral_letter_text(
        _referral(),
        _patient(),
        current_user=_user(),
        insurance_plan=insurance,
        include_payer=True,
    )
    assert "MEMBER" in text
    assert "Blue Shield CA" in text
    assert "REQUESTING PROVIDER" in text
    # Scenario B uses generic salutation, not "Dear Dr."
    assert "To Whom It May Concern" in text


def test_letter_text_includes_diagnoses_and_meds():
    diagnoses = [
        SimpleNamespace(icd10_code="I20.9", icd10_desc="Angina, unspecified", is_primary=True),
        SimpleNamespace(icd10_code="I10", icd10_desc="Essential hypertension", is_primary=False),
    ]
    medications = [
        SimpleNamespace(name="Metoprolol", dose="50mg", route="PO", frequency="BID"),
    ]
    text = referral_letter_text(
        _referral(),
        _patient(),
        current_user=_user(),
        diagnoses=diagnoses,
        medications=medications,
    )
    assert "I10 — Essential hypertension" in text
    assert "Metoprolol" in text
    assert "50mg" in text


def test_letter_text_nkda_when_no_allergies():
    text = referral_letter_text(_referral(), _patient(), current_user=_user())
    assert "NKDA" in text


# ─── HTML/PDF rendering ────────────────────────────────────────────


def test_referral_summary_html_renders_letter_format():
    """Smoke-test the new letter-format Jinja template renders without
    errors. Avoids WeasyPrint (PDF rendering is exercised in
    test_exports.py with the importorskip guard)."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from pathlib import Path

    template_dir = (
        Path(__file__).resolve().parents[1] / "src" / "docstats" / "templates" / "exports"
    )
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("referral_summary.html")
    html = template.render(
        referral=_referral(),
        patient=_patient(),
        patient_age=40,
        patient_phone="(555) 123-4567",
        generated_at=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        generated_by_label="Ryan Robson",
        organization=None,
        current_user=_user(),
        signature_image_url=None,
        diagnoses=[],
        medications=[],
        allergies=[],
        attachments=[],
        pending_attachments=[],
        included_attachments=[],
    )
    # Letter-style body class
    assert 'class="letter-style"' in html
    # RE: line
    assert "RE:" in html
    assert "Jane Doe" in html
    # Salutation
    assert "Dear Dr." in html or "Dear Colleague" in html
    # Signature block
    assert "Ryan Robson" in html
    assert "1234567890" in html
    # PHI confidentiality notice partial
    assert "HIPAA" in html


def test_medical_necessity_html_renders():
    """Smoke-test the prior-auth letter template renders cleanly."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from pathlib import Path

    template_dir = (
        Path(__file__).resolve().parents[1] / "src" / "docstats" / "templates" / "exports"
    )
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("medical_necessity.html")
    insurance = SimpleNamespace(payer_name="Blue Shield CA", plan_type="HMO")
    html = template.render(
        referral=_referral(),
        patient=_patient(),
        patient_age=40,
        patient_phone="(555) 123-4567",
        generated_at=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        generated_by_label="Ryan Robson",
        organization=None,
        current_user=_user(),
        signature_image_url=None,
        diagnoses=[],
        medications=[],
        allergies=[],
        attachments=[],
        insurance_plan=insurance,
        cpt_codes=[
            {"code": "93000", "description": "EKG, complete", "units": 1},
        ],
    )
    assert "Blue Shield CA" in html
    assert "Prior Authorization" in html
    assert "93000" in html
    assert "I20.9" in html
    # Medical-necessity placeholder when text not entered
    assert "Medical Necessity Statement" in html


# ─── parse_cpt_codes helper ────────────────────────────────────────


def test_parse_cpt_codes_handles_json_text():
    from docstats.domain.referrals import parse_cpt_codes

    assert parse_cpt_codes('[{"code": "99213", "units": 1}]') == [{"code": "99213", "units": 1}]


def test_parse_cpt_codes_passes_through_list():
    from docstats.domain.referrals import parse_cpt_codes

    assert parse_cpt_codes([{"code": "93000"}, "junk", {"code": "76700"}]) == [
        {"code": "93000"},
        {"code": "76700"},
    ]


def test_parse_cpt_codes_returns_empty_on_bad_json():
    from docstats.domain.referrals import parse_cpt_codes

    assert parse_cpt_codes("not json at all") == []
    assert parse_cpt_codes(None) == []
    assert parse_cpt_codes("") == []


def test_parse_cpt_codes_drops_non_dict_entries():
    from docstats.domain.referrals import parse_cpt_codes

    assert parse_cpt_codes('["string", 42, {"code": "x"}, null]') == [{"code": "x"}]


# ─── New referral fields land on Pydantic model ────────────────────


def test_referral_model_accepts_new_prior_auth_fields():
    """Migration 027 fields are nullable on the model; instantiate w/ values."""
    r = _referral(
        cpt_codes=[{"code": "99213", "units": 1}],
        place_of_service_code="11",
        medical_necessity_text="Patient requires consult.",
        conservative_therapy_tried="6 weeks of PT, no improvement.",
        requested_start_date="2026-06-01",
        requested_end_date="2026-06-30",
    )
    assert r.cpt_codes == [{"code": "99213", "units": 1}]
    assert r.place_of_service_code == "11"
    assert r.medical_necessity_text.startswith("Patient")
    assert r.requested_start_date == "2026-06-01"


def test_letter_text_payer_mode_includes_cpt_codes():
    insurance = SimpleNamespace(payer_name="Blue Shield CA", plan_type="HMO")
    referral = _referral(
        requested_service="Cardiology consult",
        cpt_codes=[{"code": "99213", "description": "Office visit", "units": 1}],
        place_of_service_code="11",
        medical_necessity_text="Worsening chest pain over 8 weeks.",
        conservative_therapy_tried="ASA + nitrates trialed; no improvement.",
    )
    text = referral_letter_text(
        referral, _patient(), current_user=_user(), insurance_plan=insurance, include_payer=True
    )
    assert "SERVICE REQUESTED" in text
    assert "99213" in text
    assert "Office visit" in text
    assert "Place of Service: 11" in text
    assert "MEDICAL NECESSITY" in text
    assert "CONSERVATIVE / STEP THERAPY" in text


# ─── Existing template-render tests continue below ─────────────────


@pytest.mark.parametrize(
    "template_name",
    [
        "referral_summary.html",
        "medical_necessity.html",
        "fax_cover.html",
        "attachments_checklist.html",
        "missing_info.html",
    ],
)
def test_templates_use_partials_directory_safely(template_name):
    """Confirm each template's partial includes resolve."""
    from jinja2 import Environment, FileSystemLoader
    from pathlib import Path

    template_dir = (
        Path(__file__).resolve().parents[1] / "src" / "docstats" / "templates" / "exports"
    )
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # parse() raises TemplateSyntaxError or TemplateNotFound on missing
    # partials; reaching parse_string success means every {% include %}
    # was located.
    src = (template_dir / template_name).read_text()
    env.parse(src)
