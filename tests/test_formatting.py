"""Tests for referral export formatting."""

from docstats.models import NPIResult, SavedProvider
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


def test_referral_export_with_suite():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    text = referral_export(result, appt_address="1 Shrader St, San Francisco, CA 94117", appt_suite="Suite 6A")
    assert "MY APPOINTMENT LOCATION" in text
    assert "1 Shrader St" in text
    assert "Suite 6A" in text


def test_referral_export_no_suite():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    text = referral_export(result, appt_address="1 Shrader St, San Francisco, CA 94117")
    assert "MY APPOINTMENT LOCATION" in text
    assert "Suite" not in text


def test_export_fields_includes_appt_suite():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    provider = SavedProvider.from_npi_result(result)
    provider.appt_suite = "Suite 6A"
    fields = provider.export_fields()
    assert fields["Appointment Suite"] == "Suite 6A"


def test_export_fields_appt_suite_empty_default():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    provider = SavedProvider.from_npi_result(result)
    fields = provider.export_fields()
    assert fields["Appointment Suite"] == ""


def test_referral_export_with_appt_phone_fax():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    text = referral_export(
        result,
        appt_address="123 Main St",
        appt_phone="(555) 123-4567",
        appt_fax="(555) 987-6543",
    )
    assert "Phone: (555) 123-4567" in text
    assert "Fax: (555) 987-6543" in text


def test_referral_export_televisit():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    text = referral_export(result, is_televisit=True)
    assert "TELEVISIT" in text
    assert "telehealth" in text.lower()
    assert "MY APPOINTMENT LOCATION" not in text


def test_referral_export_televisit_overrides_address():
    """When is_televisit is True, address section should not appear even if address is set."""
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    text = referral_export(result, appt_address="123 Main St", is_televisit=True)
    assert "TELEVISIT" in text
    assert "MY APPOINTMENT LOCATION" not in text


def test_export_fields_includes_new_columns():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    provider = SavedProvider.from_npi_result(result)
    provider.appt_phone = "(555) 123-4567"
    provider.appt_fax = "(555) 987-6543"
    provider.is_televisit = True
    fields = provider.export_fields()
    assert fields["Appointment Phone"] == "(555) 123-4567"
    assert fields["Appointment Fax"] == "(555) 987-6543"
    assert fields["Televisit"] == "Yes"


def test_export_fields_new_columns_defaults():
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    provider = SavedProvider.from_npi_result(result)
    fields = provider.export_fields()
    assert fields["Appointment Phone"] == ""
    assert fields["Appointment Fax"] == ""
    assert fields["Televisit"] == ""
