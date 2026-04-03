"""Tests for referral export formatting."""

from docstats.models import NPIResult
from docstats.formatting import referral_export
from tests.conftest import SAMPLE_NPI1_RESULT


def test_referral_export_no_appt_address():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    text = referral_export(result)
    assert "Smith" in text
    assert "NPI:" in text
    assert "MY APPOINTMENT LOCATION" not in text


def test_referral_export_with_appt_address():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    text = referral_export(result, appt_address="1 Shrader St, San Francisco, CA 94117")
    assert "MY APPOINTMENT LOCATION" in text
    assert "1 Shrader St" in text


def test_referral_export_appt_address_after_npi_address():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    text = referral_export(result, appt_address="1 Shrader St, San Francisco, CA 94117")
    npi_pos = text.index("Practice Address:")
    appt_pos = text.index("MY APPOINTMENT LOCATION")
    assert appt_pos > npi_pos
