"""Tests for the patient-to-PCP referral request letter (Flow A — rolodex)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from docstats.formatting import provider_request_letter_text
from docstats.models import Address, NPIResult, Taxonomy


def _result(**overrides) -> NPIResult:
    """Build a minimal NPI-1 NPIResult with one taxonomy + location address."""
    base = dict(
        number="1234567893",
        enumeration_type="NPI-1",
        basic={
            "first_name": "Jane",
            "last_name": "Smith",
            "credential": "MD",
        },
        addresses=[
            Address(
                address_1="123 Main St",
                address_2="Suite 200",
                address_purpose="LOCATION",
                city="Austin",
                state="TX",
                postal_code="787010000",
                telephone_number="5125551212",
                fax_number="5125551313",
            )
        ],
        taxonomies=[
            Taxonomy(code="207R00000X", desc="Internal Medicine", primary=True),
        ],
    )
    base.update(overrides)
    return NPIResult(**base)


def _user(**overrides) -> dict:
    base = {
        "id": 1,
        "first_name": "Sam",
        "last_name": "Patient",
        "email": "sam@example.com",
        "pcp_display_name": "Dr. Alice Cooper",
    }
    base.update(overrides)
    return base


GENERATED_AT = datetime(2026, 5, 5, 14, 30, tzinfo=timezone.utc)


def test_letter_includes_re_line_and_salutation():
    out = provider_request_letter_text(
        _result(),
        current_user=_user(),
        generated_at=GENERATED_AT,
    )
    assert "RE: Referral request: Jane Smith" in out
    assert "Internal Medicine" in out
    assert "Dear Dr. Cooper:" in out


def test_letter_default_salutation_when_no_pcp():
    out = provider_request_letter_text(
        _result(),
        current_user=_user(pcp_display_name=None),
        generated_at=GENERATED_AT,
    )
    assert "Primary Care Team" in out
    assert "Dear Primary Care Team:" in out


def test_letter_no_user_falls_back_gracefully():
    out = provider_request_letter_text(
        _result(),
        current_user=None,
        generated_at=GENERATED_AT,
    )
    # Letterhead falls back, salutation still works.
    assert "Dear Primary Care Team:" in out
    # OLD banner text is gone.
    assert "PROVIDER REFERRAL INFORMATION" not in out
    # New format uses 72-col dividers.
    assert "=" * 72 in out


def test_letter_includes_provider_block():
    out = provider_request_letter_text(
        _result(),
        current_user=_user(),
        generated_at=GENERATED_AT,
    )
    assert "PROVIDER" in out
    assert "NPI:  1234567893" in out
    assert "Type: Individual" in out
    assert "207R00000X" in out


def test_letter_practice_address_when_present():
    out = provider_request_letter_text(
        _result(),
        current_user=_user(),
        generated_at=GENERATED_AT,
    )
    assert "PRACTICE ADDRESS" in out
    assert "123 Main St" in out
    assert "Suite 200" in out
    assert "Austin, TX" in out
    assert "(512) 555-1212" in out


def test_letter_televisit_replaces_appointment_block():
    out = provider_request_letter_text(
        _result(),
        current_user=_user(),
        is_televisit=True,
        appt_address="ignored when televisit",
        generated_at=GENERATED_AT,
    )
    assert "telehealth / virtual visit" in out
    assert "ignored when televisit" not in out


def test_letter_appt_address_renders_when_set():
    out = provider_request_letter_text(
        _result(),
        current_user=_user(),
        appt_address="Memorial Hermann Heights",
        appt_suite="Bldg 4, Floor 7",
        appt_phone="(713) 555-9000",
        generated_at=GENERATED_AT,
    )
    assert "MY APPOINTMENT LOCATION" in out
    assert "Memorial Hermann Heights" in out
    assert "Bldg 4, Floor 7" in out
    assert "(713) 555-9000" in out


def test_letter_mailing_address_only_when_different():
    """Mailing-address section only renders when distinct from location."""
    res_same = _result(
        addresses=[
            Address(
                address_1="123 Main St",
                address_purpose="LOCATION",
                city="Austin",
                state="TX",
                postal_code="78701",
            ),
            Address(
                address_1="123 Main St",
                address_purpose="MAILING",
                city="Austin",
                state="TX",
                postal_code="78701",
            ),
        ]
    )
    out_same = provider_request_letter_text(
        res_same, current_user=_user(), generated_at=GENERATED_AT
    )
    assert "MAILING ADDRESS" not in out_same

    res_diff = _result(
        addresses=[
            Address(
                address_1="123 Main St",
                address_purpose="LOCATION",
                city="Austin",
                state="TX",
                postal_code="78701",
            ),
            Address(
                address_1="PO Box 9000",
                address_purpose="MAILING",
                city="Austin",
                state="TX",
                postal_code="78766",
            ),
        ]
    )
    out_diff = provider_request_letter_text(
        res_diff, current_user=_user(), generated_at=GENERATED_AT
    )
    assert "MAILING ADDRESS" in out_diff
    assert "PO Box 9000" in out_diff


def test_letter_includes_phi_footer():
    out = provider_request_letter_text(_result(), current_user=_user(), generated_at=GENERATED_AT)
    assert "CONFIDENTIAL" in out
    assert "HIPAA" in out


def test_letter_signature_uses_typed_name_with_credentials():
    out = provider_request_letter_text(
        _result(),
        current_user=_user(credentials="MPH"),
        generated_at=GENERATED_AT,
    )
    assert "Sincerely," in out
    assert "Sam Patient, MPH" in out


def test_provider_request_html_renders():
    """Smoke-test the new Jinja template renders without error."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    template_dir = (
        Path(__file__).resolve().parents[1] / "src" / "docstats" / "templates" / "exports"
    )
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("provider_request.html")
    html = template.render(
        result=_result(),
        current_user=_user(),
        appt_address=None,
        appt_suite=None,
        appt_phone=None,
        appt_fax=None,
        is_televisit=False,
        pcp_name="Dr. Alice Cooper",
        signature_image_url=None,
        generated_at=GENERATED_AT,
        organization=None,
    )
    assert 'class="letter-style"' in html
    assert "RE:" in html
    assert "Jane Smith" in html
    assert "Dr. Cooper" in html
    assert "1234567893" in html
    # Partial includes resolved
    assert "CONFIDENTIALITY NOTICE" in html
    assert "Sincerely," in html
